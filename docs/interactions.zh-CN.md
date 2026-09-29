> 为已退役的 MCP 面而写(已被 DESIGN-opencode-ctl.md 取代);交互/会话/子树语义沿用至 octl CLI。

# 权限 / 表单交互流程

[English](interactions.md) | **中文**

opencode-mcp [文档](../README.zh-CN.md)的一部分。

### 自动模式(默认)

`chat` 的 `auto_permission` 默认为 `once`,遇到权限请求会自动答复并继续等待,通常一次调用即可拿到 `succeeded`。

### 手动模式

当希望人工决定是否授权时,使用 `auto_permission: "manual"`:

1. 调用 `chat(..., auto_permission="manual")`,若遇到权限请求,返回:

   ```json
   {
     "status": "needs_permission",
     "requests": [
       {"id": "per_1", "sessionID": "ses_abc", "action": "bash", "resources": ["rm -rf ..."]}
     ]
   }
   ```

2. 根据 `requests` 内容决定策略,调用 `permission_reply(session_id, request_id, decision, message?)`。
3. 调用 `wait_session(session_id)` 继续等待。若又出现新的权限请求,会返回 `needs_permission`,重复步骤 2–3;终态返回 `succeeded` / `failed` / `interrupted`,再用 `get_messages(after_message_id=...)` 拉取增量回复。

### 表单流程

1. 调用 `chat` 或 `wait_session` 时若遇到表单,返回:

   ```json
   {
     "status": "needs_form",
     "forms": [
       {
         "id": "frm_1",
         "title": "请选择部署环境",
         "fields": [
           {"key": "env", "title": "环境", "type": "string", "required": true,
            "options": [{"value": "dev", "label": "开发"}, {"value": "prod", "label": "生产"}]}
         ]
       }
     ]
   }
   ```

   `fields` 里的 `type` 可能为 `string` / `number` / `integer` / `boolean` / `multiselect` / `external`;`multiselect` 的答案用字符串数组。

2. 调用 `form_reply(session_id, form_id, answer)`,例如 `answer = {"env": "prod"}`。
3. 调用 `wait_session(session_id)` 继续等待生成完成。

> 提示:若某个权限请求已经被其他途径处理,`permission_reply` 可能返回错误;此时直接调用 `wait_session` 或 `pending_interactions` 重新确认状态即可。

## 权限规则与 action 命名

权限请求 `Permission.Request` 的关键字段:

- `action`:要执行的动作,**命名与工具名一致**。实测出现过的 action 包括:
  `shell`、`bash`、`edit`、`write`、`read`、`glob`、`grep`、`webfetch`、`external_directory` 等。
- `resources`:动作作用的对象列表。对命令类工具(`shell`/`bash`)通常是**命令文本**,对文件类工具(`edit`/`write`/`read`/`glob`/`grep`)通常是**路径模式**(如 `*`、`/root/project/**`),`webfetch` 为 URL,`external_directory` 为目录路径。
- `save?`:可被 `always` 记住的资源列表。
- `message?`:可选的说明文本。

因此 `permission_reply` 的 `decision` 语义是:`once` 仅本次放行;`always` 放行并把该 `action` + `resource` 规则保存;`reject` 拒绝。

### 在会话上强制把某类动作设为 ask

opencode 的会话支持在 `session.create` 时通过 `permissions` 传入规则(`effect` 可为 `allow` / `deny` / `ask`),用来覆盖默认行为。例如强制所有 shell 命令都必须询问:

```bash
curl -u opencode:$OPENCODE_PASSWORD \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:49374/api/session \
  -d '{
    "title": "权限演示",
    "permissions": [
      {"action": "shell", "resource": "*", "effect": "ask"},
      {"action": "bash",  "resource": "*", "effect": "ask"}
    ]
  }'
```

等价的 JSON body:

```json
{
  "title": "权限演示",
  "permissions": [
    {"action": "shell", "resource": "*", "effect": "ask"},
    {"action": "bash",  "resource": "*", "effect": "ask"}
  ]
}
```

配置后调用 `chat(..., auto_permission="manual")`,即可稳定复现 `needs_permission` 流程;用 `permission_reply` 逐条答复后再 `wait_session` 继续。

> 说明:`create_session` 工具当前只透传 `title` / `agent` / `model_id`;若需要自定义 `permissions` 规则,可按上面的例子直接调用 opencode HTTP API 创建会话,拿到 `ses_...` 后继续用本 server 的其他工具。
