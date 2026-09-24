# Comments

The code carries no comments and no docstrings, with three exceptions.
`tests/test_comments.py` enforces this.

1. **Directives**, which are instructions to a tool rather than prose:
   `noqa`, `type: ignore`, pragmas, lint switches, the shebang.
2. **Text the program reads at run time**, which is data: argparse help,
   the verb registry's descriptions (`mechbench_runner/verbs/`), MCP tool
   descriptions, error messages, and a docstring something reads (an MCP
   tool registered from a function sends its docstring as the tool's
   description). A kept docstring is listed in `READ_AT_RUN_TIME` in the
   gate with what reads it.
3. **A fact about the outside world** (a provider, launchd, the model
   hub, the API's limits) that the code cannot show. Hold it with a test
   that fails when the behaviour it explains is removed, and name the test
   after the fact; `tests/test_external_facts.py` collects the ones with
   no better home. Only a fact no test can reach stays as a comment, one
   line: `# external: <the outside thing> — <the fact>`.

Everything else is deleted: what the code does, why it does it when the
code shows that, history, dates, versions, task numbers, roadmaps,
section banners, commented-out code. The reader is expected to read the
code.

No text in the repository names a task in a private tracker by number;
`CHANGELOG.md` is the one exception.
