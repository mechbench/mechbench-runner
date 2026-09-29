from mechbench_compute.api import Extension, Package

MANIFEST = Extension(
    name="alice/interp-extras",
    extension="interp-extras",
    version=2,
    module=__name__,
    package=Package("mechbench-ext-interp-extras"),
    min_compute="0.165.0",
    links={"repo": "https://github.com/alice/interp-extras"},
    provenance={"source": "github.com/alice/interp-extras@a1b2c3d"},
)
