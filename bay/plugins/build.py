import attr
import click
import datetime
import sys
from docker.errors import NotFound

from .base import BasePlugin
from ..cli.colors import CYAN, GREEN, RED, remove_ansi
from ..cli.argument_types import ContainerType, HostType
from ..cli.tasks import Task
from ..constants import PluginHook
from ..docker.build import Builder
from ..docker.introspect import FormationIntrospector
from ..docker.runner import FormationRunner
from ..exceptions import BuildFailureError, ImagePullFailure
from .gc import GarbageCollector
from ..utils.sorting import dependency_sort


def _get_providers(app):
    providers = {}
    for container in app.containers:
        provides_volume = container.extra_data.get("provides-volume", None)
        if provides_volume:
            providers[provides_volume] = container
    return providers


@attr.s
class BuildPlugin(BasePlugin):
    """
    Plugin for building containers.
    """

    provides = ["build"]

    def load(self):
        self.add_command(build)
        self.add_catalog_type("registry")
        self.add_hook(PluginHook.PRE_START, self.pre_start)
        self.add_hook(PluginHook.POST_BUILD, self.post_build)

    def pre_start(self, host, instance, task):
        """
        Safety net to stop you booting volume-providing containers normally,
        and to catch and build volume containers if they're needed
        """
        # Safety net
        if instance.container.extra_data.get("provides-volume", None):
            raise ValueError("You cannot run a volume-providing container {}".format(instance.container.name))
        # If the container has named volumes, see if they're provided by anything else
        # and if so, if they're built.
        # First, collect what volumes are provided by what containers
        providers = _get_providers(self.app)
        # Now see if any of the volumes we're trying to add need it
        for _, name in instance.container.named_volumes.items():
            if name in providers:
                # Alright, this is one that could be provided. Does it already exist?
                try:
                    host.client.inspect_volume(name)
                except NotFound:
                    # Aha! Build it!
                    Builder(
                        host,
                        providers[name],
                        self.app,
                        parent_task=task,
                        logfile_name=self.app.config.get_path(
                            'bay',
                            'build_log_path',
                            self.app,
                        ),
                        verbose=True,
                    ).build()

    def post_build(self, host, container, task):
        """
        Intercepts builds of volume-providing containers and unpacks them.

        Volumes are stored with the ID of the corresponding volume-providing image. This will only run the container
        to recreate the volume if the image"s ID (hash) has changed.
        """
        image_details = host.client.inspect_image(container.image_name)
        provides_volume = container.extra_data.get("provides-volume", None)

        def should_extract_volume():
            if not provides_volume:
                return False
            try:
                volume_details = host.client.inspect_volume(provides_volume)
            except NotFound:
                return True
            labels = volume_details.get("Labels") or {}
            return labels.get("build_id") != image_details["Id"]

        if should_extract_volume():
            # Stop all containers that have the volume mounted
            formation = FormationIntrospector(host, self.app.containers).introspect()
            # Keep track of instances to remove after they are stopped
            instances_to_remove = formation.get_instances_using_volume(provides_volume)
            if instances_to_remove:
                formation.remove_instances(instances_to_remove)
                stop_task = Task("Stopping containers", parent=task)
                FormationRunner(self.app, host, formation, stop_task).run()
                stop_task.finish(status="Done", status_flavor=Task.FLAVOR_GOOD)
                remove_task = Task("Removing containers", parent=task)
                for instance in instances_to_remove:
                    host.client.remove_container(instance.name)
                    remove_task.update(status="Removed {}".format(instance.name))
                remove_task.finish(status="Done", status_flavor=Task.FLAVOR_GOOD)

            # Prune any orphan stopped containers, so we don't get conflict errors
            GarbageCollector(host).gc_containers(task)

            volume_task = Task("(Re)creating volume {}".format(provides_volume), parent=task)
            # Recreate the volume with the new image ID
            try:
                host.client.remove_volume(provides_volume)
                volume_task.update(status="Removed {}. Recreating".format(provides_volume))
            except NotFound:
                volume_task.update(status="Volume {} not found. Creating")
            host.client.create_volume(provides_volume, labels={"build_id": image_details["Id"]})

            # Configure the container
            volume_mountpoints = ["/volume/"]
            volume_binds = {provides_volume: {"bind": "/volume/", "mode": "rw"}}
            container_pointer = host.client.create_container(
                container.image_name,
                detach=False,
                volumes=volume_mountpoints,
                host_config=host.client.create_host_config(
                    binds=volume_binds,
                ),
            )
            # Start it in the foreground so we wait till it exits (detach=False above)
            volume_task.update(status="Extracting")
            host.client.start(container_pointer)
            host.client.wait(container_pointer["Id"])
            host.client.remove_container(container_pointer["Id"])
            volume_task.update(status="Done", status_flavor=Task.FLAVOR_GOOD)


@click.command()
@click.argument('containers', type=ContainerType(profile=True), nargs=-1)
@click.option('--host', '-h', type=HostType(), default='default')
@click.option('--cache/--no-cache', default=True)
@click.option('--recursive/--one', '-r/-1', default=True)
@click.option('--verbose/--quiet', '-v/-q', default=True)
# TODO: Add a proper requires_docker check
@click.pass_obj
def build(app, containers, host, cache, recursive, verbose):
    """
    Build container images, along with its build dependencies.
    """
    logfile_name = app.config.get_path('bay', 'build_log_path', app)
    containers_to_pull = []
    containers_to_build = []
    pulled_containers = set()
    failed_pulls = set()

    task = Task("Building", parent=app.root_task)
    start_time = datetime.datetime.now().replace(microsecond=0)

    providers = _get_providers(app)

    # Go through the containers, expanding "ContainerType.Profile" into a list
    # of default boot containers in the profile.
    for container in containers:
        if container is ContainerType.Profile:
            for con in app.containers:
                # When building the profile, rebuild system containers too
                if app.containers.options(con).get('in_profile') or con.system:
                    containers_to_pull.append(con)
                    # Build any volumes that are required by the containers
                    for volume in con.named_volumes.values():
                        if volume in providers:
                            containers_to_pull.append(providers[volume])
        else:
            containers_to_build.append(container)
            for volume in container.named_volumes.values():
                if volume in providers:
                    containers_to_build.append(providers[volume])

    # Expand containers_to_pull (At this point just the default boot containers
    # from profile) to include runtime dependencies.
    containers_to_pull = dependency_sort(containers_to_pull, app.containers.dependencies)

    # Try pulling each container to pull, and add it to containers_to_build if
    # it fails. If it works, remember we pulled it, so we don't have to pull it
    # again later.
    for container in containers_to_pull:
        try:
            host.images.pull_image_version(
                app,
                container.image_name,
                container.image_tag,
                parent_task=task,
                fail_silently=False,
            )
        except ImagePullFailure:
            failed_pulls.add(container)
            containers_to_build.append(container)
        else:
            pulled_containers.add(container)

    ancestors_to_build = []
    # For each container to build, find its ancestry, trying to pull each
    # ancestor and stopping short if it works.
    for container in containers_to_build:
        # Always add `container` to final build list, even if recursive is
        # False.
        ancestors_to_build.append(container)
        if recursive:
            # We need to look at the ancestry starting from the oldest, up to
            # and not including the `container`
            ancestry = dependency_sort(containers_to_build,
                                       lambda x: [app.containers.build_parent(x)])[:-1]
            for ancestor in reversed(ancestry):
                try:
                    # If we've already attempted to pull it and failed, short
                    # circuit to failure block.
                    if ancestor in failed_pulls:
                        raise ImagePullFailure("We've already attempted to pull this image, and failed.")
                    # Check if we've pulled it already
                    if ancestor not in pulled_containers:
                        host.images.pull_image_version(
                            app,
                            ancestor.image_name,
                            ancestor.image_tag,
                            parent_task=task,
                            fail_silently=False,
                        )
                except ImagePullFailure:
                    failed_pulls.add(ancestor)
                    ancestors_to_build.insert(0, ancestor)
                else:
                    # We've pulled the current ancestor successfully, so skip
                    # all the older ancestors.
                    pulled_containers.add(ancestor)
                    break

    # Sort ancestors so we build the most depended on first.
    sorted_ancestors_to_build = dependency_sort(ancestors_to_build, lambda x: [app.containers.build_parent(x)])

    # dependency_sort would insert back the pulled containers into the ancestry
    # chain, so we only include ones that were in the list before
    ancestors_to_build = [
        container
        for container in sorted_ancestors_to_build
        if container in ancestors_to_build
    ]

    task.add_extra_info(
        "Order: {order}".format(
            order=CYAN(", ".join([container.name for container in ancestors_to_build])),
        ),
    )

    app.run_hooks(PluginHook.PRE_GROUP_BUILD, host=host, containers=ancestors_to_build, task=task)

    for container in ancestors_to_build:
        image_builder = Builder(
            host,
            container,
            app,
            parent_task=task,
            logfile_name=logfile_name,
            docker_cache=cache,
            verbose=verbose,
        )
        try:
            image_builder.build()
        except BuildFailureError:
            click.echo(RED("Build failed! Last 15 lines of log:"))
            # TODO: More efficient tailing
            lines = []
            with open(logfile_name, "r") as fh:
                for line in fh:
                    lines = lines[-14:] + [line]
            for line in lines:
                click.echo("  " + remove_ansi(line).rstrip())
            click.echo("See full build log at {log}".format(log=click.format_filename(logfile_name)), err=True)
            sys.exit(1)

    app.run_hooks(PluginHook.POST_GROUP_BUILD, host=host, containers=ancestors_to_build, task=task)

    task.finish(status="Done", status_flavor=Task.FLAVOR_GOOD)

    # Show total build time metric after everything is complete
    end_time = datetime.datetime.now().replace(microsecond=0)
    time_delta_str = str(end_time - start_time)
    if time_delta_str.startswith('0:'):
        # no point in showing hours, unless it runs for more than one hour
        time_delta_str = time_delta_str[2:]
    click.echo("Total build time [{}]".format(GREEN(time_delta_str)))
