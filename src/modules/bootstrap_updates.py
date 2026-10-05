"""Read update versions from the same interpreter that receives pip installs."""
from importlib import metadata
from pathlib import Path
import re


def site_packages(python):
    """Resolve the supplied private/venv interpreter's own metadata directory."""
    python = Path(python).absolute()
    if python.parent.name.lower() in ("scripts", "bin"):
        root = python.parent.parent
    else:
        root = python.parent
    windows = root / "Lib" / "site-packages"
    if windows.is_dir():
        return [str(windows)]
    return [str(path) for path in sorted((root / "lib").glob("python*/site-packages"))
            if path.is_dir()]


def installed_version(python, package):
    """Read fresh distribution metadata without importing an already-loaded module.

    Explicit paths exclude the bootstrap host, unrelated global packages and
    stale sys.path entries. Conflicting metadata requires repair, not a guess
    that the highest recorded version is installed.
    """
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    wanted = normalize(package)
    versions = {distribution.version for distribution in
                metadata.distributions(path=site_packages(python))
                if normalize(distribution.metadata.get("Name", "")) == wanted}
    if len(versions) > 1:
        raise RuntimeError(f"Conflicting {package} version metadata in {python}: {', '.join(sorted(versions))}")
    return next(iter(versions), None)
