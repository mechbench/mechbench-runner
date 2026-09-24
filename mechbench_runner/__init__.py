from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mechbench")
except PackageNotFoundError:
    __version__ = "0+unknown"
