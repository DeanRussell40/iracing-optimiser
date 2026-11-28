"""iRacing Optimiser: measures iRacing on this PC and tunes it from the measurements.

The version lives here only. The build adds iroptimizer/_build.py (build number, commit, date); a release tag
vX.Y.Z must match __version__ or the build fails.
"""
__version__ = "0.27.0"

try:  # written by the GitHub Actions build, absent when running from source
    from ._build import BUILD  # type: ignore
except ImportError:
    BUILD = {}


def version_label() -> str:
    """e.g. '0.13.0 · build 52 (3de78cc), 26 Nov 2026'. Source runs say 'from source'."""
    if not BUILD:
        return f"{__version__} (from source)"
    return f"{__version__} · build {BUILD.get('number')} ({BUILD.get('commit', '')[:7]}), {BUILD.get('date', '')}"
