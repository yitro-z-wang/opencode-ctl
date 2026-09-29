> Written for the retired MCP surface (superseded by DESIGN-opencode-ctl.md); the interaction/session/subtree semantics carry over to the octl CLI.

# Model selection

**English** | [中文](sessions.zh-CN.md)

Part of the [opencode-mcp](../README.md) documentation.

- This MCP **never pins models on the caller's behalf**: `create_session` without `model_id` leaves `model=null`, and the run falls back to the **location default model** (`GET /api/model/default`).
- Measured (opencode v2.0.12): the location default does **not** follow agent configuration — the TUI and opencode's internal spawning pin models explicitly at session creation, while bare-API sessions fall back to the location default. Pass `model_id` (`providerID/modelID`) explicitly when you need a specific model.
- **Reading the agent → model mapping**: use `list_agents` (backed by `GET /api/agent`, plugin-resolved). To align a session with an agent's model, read the mapping and pass `model_id` yourself.
