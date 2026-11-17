"""Where this process runs: container, Kubernetes, OS. Mirrors
botasaurus.env (IS_DOCKER, IS_IN_KUBERNETES, get_os) so patchright_driver.py
and config.py no longer import botasaurus: the browser worker on the boxes
ships without it (requirements-worker.txt). Same semantics, same values."""
import os
import sys

IS_IN_KUBERNETES = "KUBERNETES_SERVICE_HOST" in os.environ


def _is_docker():
    if os.name == "nt" or sys.platform == "darwin":
        return False
    try:
        return (os.path.exists("/.dockerenv")
                or (os.path.isfile("/proc/self/cgroup") and "docker" in open("/proc/self/cgroup").read())
                or IS_IN_KUBERNETES)
    except OSError:
        return IS_IN_KUBERNETES


IS_DOCKER = _is_docker()
IS_VM = os.environ.get("VM") == "true"
IS_VM_OR_DOCKER = IS_DOCKER or IS_VM
IS_PRODUCTION = os.environ.get("ENV") == "production"

_os = None


def get_os():
    """'windows' | 'mac' | 'linux'."""
    global _os
    if not _os:
        _os = "windows" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "linux")
    return _os
