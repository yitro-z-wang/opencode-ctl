> 为已退役的 MCP 面而写(已被 DESIGN-opencode-ctl.md 取代);交互/会话/子树语义沿用至 octl CLI。

# 已验证流程

[English](verification.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

以下链路均在**本机 opencode v2.0.12** 上完成过 live 验证:

1. **创建 + 对话**:`create_session` 创建会话,`chat` 发送 prompt,返回 `status: succeeded` 并带 `assistant_text` / `tools_used`。
2. **manual 权限全链路**:`chat(auto_permission="manual")` 返回 `needs_permission` → `permission_reply(decision="once")` → `wait_session` 直至终态。
3. **表单管道**:`chat` / `wait_session` 返回 `needs_form`(含字段详情)→ `form_reply(form_id, answer)` → `wait_session` 直至终态。
4. **默认自动授权**:`chat` 使用默认 `auto_permission="once"`,遇到权限请求自动放行并继续,一次调用即返回 `succeeded`。

5. **连接层**:显式 env 与 MCP 专属拉起两条本地路径;专属 serve 能承载真实对话并随 MCP 退出而退出;失败分类(死端口 = availability、仅返回 HTML 的服务 = compatibility、401 = 凭据问题);重名与未知服务器报错;会话自动路由;断连规则。
6. **真实远端端到端**:`connect_server` 跨网络连接 → 远端 `create_session` → 远端 `chat` → 远端默认手动审批(被阻塞而非自动放行)→ `permission_reply` → `wait_session` → 增量 `get_messages` → `disconnect_server`。
7. **长会话窗口**:在 200+ 条消息的会话上,尾部窗口拉取保证 gate 查找、增量游标与 `last_message_id` 正确。
8. **取消与并发**:`notifications/cancelled` 在 1 秒内停止轮询;`chat` 在途时并发调用 `pending_interactions` 毫秒级返回。
9. **宿主集成**:作为工具提供方挂载进 Hermes agent gateway;由 oh-my-opencode-slim 编排框架托管并驱动嵌套 opencode 会话。
10. **子会话感知等待**:主 agent 后台委派一个卡在 `shell` 审批上的子 agent —— `wait_session`(默认)返回 `needs_permission` 且指向**子会话**的 `session_id`(并带 `root_session_id`),而不是提前的 `succeeded`;`chat`(默认)返回 `succeeded` 但报告 `pending_subagents: 1`。另已验证:三个并行子 agent 各自待审批、子 agent 卡在表单上、`auto_permission="once"` 答复子会话后到达 `succeeded`,以及同样的流程在**远端连接**上不带 `server` 也能正确路由。

## 测试

冒烟测试(仅 MCP 握手 + `tools/list`,不发任何网络请求):

```bash
cd /root/opencode-mcp
python3 -m py_compile server.py test_client.py
python3 test_client.py
```

预期输出 16 个工具名并以退出码 0 结束。

带真实对话测试(需要本机 opencode 正在运行):

```bash
python3 test_client.py --chat "用一句话介绍你自己"
```

场景套件位于 [`tests/`](../tests/README.md):核心交互流程、子会话/子树场景、专属 serve 的生命周期路径。纯 Python 标准库,输出 `PASS` / `FAIL` / `SKIP`;需要额外环境(已有 opencode 服务端、远端实例、具备委派能力的 agent)的场景一律 **SKIP 而非失败**,并通过 `tests/README.md` 中记录的 `OPENCODE_TEST_*` 变量启用。
