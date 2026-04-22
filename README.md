# mechbench-agent

The agent-facing surface for [mechbench](https://github.com/mechbench/mechbench). Exposes `mechbench-core` primitives as agent-callable tools (MCP) plus a structured-summary / JSON-query interface for agents that don't speak MCP.

**Status:** scoped but not yet populated. See the meta repo for the family overview, philosophy, and current roadmap. Task backlog for this repo lives at [`mechbench/tasks/mechbench-agent/`](https://github.com/mechbench/mechbench/tree/main/tasks/mechbench-agent) once tasks are filed.

## Planned scope

- MCP server wrapping `mechbench-core` primitives.
- REST endpoints (`GET /summary`, `POST /query`) for non-MCP clients.
- Structured emission of dense numerical results via `mechbench-schema` types.

## Out of scope

- The human-facing visualization layer (→ `mechbench-ui`).
- The compute engine itself (→ `mechbench-core`).
- Remote-compute orchestration (→ `mechbench-remote`).

## License

MIT.
