"""The mechbench command.

Deliberately bare (task 000307): this module is the front door — the
`mechbench` entry point and its argument surface — and nothing else.
The machinery lives in `mechbench_runner`, which this package also
ships; the cli dispatches into it per command and imports none of it at
startup.

The boundary is drawn here on purpose. If engine releases ever outpace
this front door far enough that a supervisor should survive engine
upgrades untouched on disk, the split into two distributions happens
along exactly this line, with no further renaming.
"""
