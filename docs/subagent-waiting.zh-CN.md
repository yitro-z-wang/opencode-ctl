> 为已退役的 MCP 面而写(已被 DESIGN-opencode-ctl.md 取代);交互/会话/子树语义沿用至 octl CLI。

# 子会话感知等待(多 agent 会话)

[English](subagent-waiting.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

主 agent 委派子 agent 之后,**自己的这一轮会先结束,而子 agent 还在跑**。opencode 的 `outcome` 是**按轮**的 —— 它的含义是"这个会话此刻没有生成在跑",而不是"任务完成" —— 所以朴素地等待会提前宣布成功,甚至让子 agent 卡在一个永远无人看到的审批上。

因此本 server 会依据权威的 `parentID` 关系解析出会话的**子 agent 子树**(`GET /api/session?parentID=…`,深度 ≤ 3、节点 ≤ 64,完全不解析对话内容),并且在子树里还有任何活着的节点时拒绝报告 `succeeded`:

- `wait_session` 的 `wait_for_subagents` 默认 `true` —— 只有整棵子树静止(无节点在生成、且子树内无待处理权限/表单)才返回 `succeeded`。
- `chat` 的 `wait_for_subagents` 默认 `false` —— 本轮停止流式输出即返回,但 payload 始终带 `subagents` / `pending_subagents` / `subtree_truncated` / `subtree_verified`,调用方据此可看到仍有工作在跑,再用 `wait_session` 继续跟进。
- **子 agent 的阻塞交互同样会被上报**:子会话卡在审批时返回 `needs_permission`,其 `session_id` 是**真正持有该请求的子会话**(并附 `root_session_id`),答复会自动路由到持有该会话的那台服务器。`auto_permission` 取 `once` / `always` / `reject` 时,答复作用于整棵子树;表单永不自动答复。
- **fail-closed**:若活动表或待处理交互状态无法校验,绝不返回 `succeeded` —— 等待会持续到超时,并明确告知哪些无法校验(`subtree_verified: false`)。缺失这些端点的老服务端按连接探测一次后回退到旧行为,并如实标注,绝不谎称已验证。
