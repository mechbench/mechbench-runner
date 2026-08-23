"""mechbench — MCP server + job-runner for the mechbench platform."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution rather than repeating it here:
    # a hand-maintained copy drifts, and this one had — it still said
    # 0.1.0 at 0.1.3, which is the number reported to the API and shown
    # by `status`.
    __version__ = version("mechbench")
except PackageNotFoundError:  # running from a source tree, uninstalled
    __version__ = "0+unknown"
