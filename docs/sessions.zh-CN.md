> 为已退役的 MCP 面而写(已被 DESIGN-opencode-ctl.md 取代);交互/会话/子树语义沿用至 octl CLI。

# 模型选择说明

[English](sessions.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

- 本 MCP **不替调用方钉扎模型**:`create_session` 不传 `model_id` 时,会话 `model=null`,运行时会回落到 **位置默认模型**(可通过 `GET /api/model/default` 查询)。
- 实测(opencode v2.0.12):位置默认模型**不会**跟随 agent 配置——TUI 和 opencode 内部 spawn 在创建会话时会显式钉模型,裸 API 建的会话则回落位置默认。需要指定模型时显式传 `model_id`(格式 `providerID/modelID`)。
- **读取 agent→模型映射**:用 `list_agents` 工具(数据来自 `GET /api/agent`,已包含插件解析结果)。想与会话与某 agent 的行为对齐,由调用方读取映射后显式传 `model_id`。
