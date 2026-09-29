from mechbench_compute.api import In, Op, Output, P, collection, items_of

OP = Op(
    name="geometry/align",
    summary="How far two sets of records share their ids, as one score.",
    description="Reads two record collections and reports the overlap of their ids.",
    params=(P("method", "string", "Shared over the smaller set, or over the union.",
              "overlap", choices=("overlap", "jaccard")),),
    inputs=(In("a", "records/record", "The first set.", many=True),
            In("b", "records/record", "The second set.", many=True)),
    output=Output("geometry/alignment", collection=True, doc="One alignment: the two sizes and their score."),
    example={"method": "overlap"},
    example_inputs={"a": {"$ref": {"bench": "alice/lab/a"}}, "b": {"$ref": {"bench": "alice/lab/b"}}},
)


def run(ctx, inputs, params):
    a = {r["id"] for r in items_of(inputs["a"])}
    b = {r["id"] for r in items_of(inputs["b"])}
    over = len(a | b) if params.get("method") == "jaccard" else min(len(a), len(b))
    score = len(a & b) / over if over else 0.0
    return collection("geometry/alignment", [{"id": "a~b", "a": len(a), "b": len(b), "score": score}],
                      method=params.get("method", "overlap"))
