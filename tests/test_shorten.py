from __future__ import annotations

from mechbench_runner.verbs.shorten import shorten_numbers, summarize_push


def test_a_long_list_of_numbers_keeps_its_first_8_and_its_count():
    direction = [i / 1000 for i in range(1536)]
    out = shorten_numbers({"id": "d", "vector": direction, "layers": [4, 9, 14], "norm": 2.5})
    assert out["vector"] == {"first": direction[:8], "count": 1536,
                             "shortened": "pass full: true for every value"}
    assert out["layers"] == [4, 9, 14]
    assert out["norm"] == 2.5
    assert shorten_numbers([{"v": direction}])[0]["v"]["count"] == 1536


def test_a_list_that_is_not_all_numbers_is_left_alone():
    mixed = list(range(40)) + ["end"]
    assert shorten_numbers(mixed) == mixed
    flags = [True] * 40
    assert shorten_numbers(flags) == flags


def test_a_push_answers_the_protocol_without_its_graph():
    answer = {"action": "created", "protocol": {
        "id": "prt_1", "name": "emotions", "version": 1, "description": "d",
        "graph": {"dataflow": 2, "nodes": [{}, {}, {}], "edges": [{}, {}]},
        "signature": {"params": [{"name": "model"}], "inputs": [{"name": "prompts"}],
                      "outputs": [{"name": "v"}]}}}
    assert summarize_push(answer) == {
        "action": "created",
        "protocol": {"id": "prt_1", "name": "emotions", "version": 1, "description": "d",
                     "graph": {"nodes": 3, "edges": 2},
                     "signature": {"params": ["model"], "inputs": ["prompts"], "outputs": ["v"]}},
        "shortened": "pass full: true for the whole protocol",
    }
    assert summarize_push({"action": "refused", "findings": []}) == {"action": "refused", "findings": []}
