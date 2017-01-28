from setuptools import setup, find_packages
from bay import __version__

setup(
    name='bay',
    version=__version__,
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'attrs',
        'Click>=6.6',
        'PyYAML',
        'docker-py>=1.6',
        'dockerpty==0.4.1',
        'scandir',
        'requests<2.11.0',
    ],
    extras_require={
        'spell': ['pylev'],
    },
    test_suite="tests",
    entry_points='''
        [console_scripts]
        bay = bay.cli:cli
        tug = bay.cli:cli

        [bay.plugins]
        attach = bay.plugins.attach:AttachPlugin
        boot = bay.plugins.boot:BootPlugin
        build = bay.plugins.build:BuildPlugin
        build_scripts = bay.plugins.build_scripts:BuildScriptsPlugin
        build_volumes = bay.plugins.build_volumes:BuildVolumesPlugin
        container = bay.plugins.container:ContainerPlugin
        gc = bay.plugins.gc:GcPlugin
        hosts = bay.plugins.hosts:HostsPlugin
        legacy_env = bay.plugins.legacy_env:LegacyEnvPlugin
        mounts = bay.plugins.mounts:DevModesPlugin
        profile = bay.plugins.profile:ProfilesPlugin
        ps = bay.plugins.ps:PsPlugin
        run = bay.plugins.run:RunPlugin
        ssh_agent = bay.plugins.ssh_agent:SSHAgentPlugin
        tail = bay.plugins.tail:TailPlugin
        volume = bay.plugins.volume:VolumePlugin
        waits = bay.plugins.waits:WaitsPlugin
    ''',
)
