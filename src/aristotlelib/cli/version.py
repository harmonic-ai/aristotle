from importlib.metadata import PackageNotFoundError, version


def get_version():
    """Get the package version from metadata."""
    try:
        return version("aristotlelib")
    except PackageNotFoundError:
        return "unknown"
