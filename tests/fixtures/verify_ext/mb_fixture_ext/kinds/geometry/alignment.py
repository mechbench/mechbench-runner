from mechbench_compute.api import F, Kind

KIND = Kind(
    "geometry/alignment",
    "One overlap score between two sets of records.",
    extends="records/record",
    fields={"a": F("integer", "Records in the first set."),
            "b": F("integer", "Records in the second set."),
            "score": F("number", "Shared ids over the smaller set, or over the union.")},
    required=("id", "score"),
    key=("id",),
    header={"method": "overlap or jaccard"},
    speak="{header.method} of {a} and {b} records: {score}",
)
