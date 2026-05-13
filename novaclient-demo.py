from pathlib import Path

from managers import packaging

project_file = Path("/tmp/o7k-python-novaclient-gazpacho-projects.yaml")
project_file.write_text(
    """projects:
  - package: python-novaclient
    openstack_series: gazpacho
    ubuntu_series: resolute
"""
)

packaging.PROJECTS_PATH = project_file
packaging.run("python-novaclient")