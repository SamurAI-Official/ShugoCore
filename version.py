"""ShugoCore version (SemVer: frozen for the 1.x series)."""

__version__ = "1.3.0"
VERSION = __version__

# Breaking-change policy for the 1.x series:
#   * The public surface in README "Frozen API surface" is guaranteed stable
#     across all 1.x patches and minors.
#   * Deprecated functionality may be removed only after being deprecated for
#     at least one minor release.