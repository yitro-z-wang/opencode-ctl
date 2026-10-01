---
name: octl
description: 从命令行驱动操作者部署的 opencode 服务器——创建会话、发送提示、等待结果，并处理权限与表单交互。当需要通过 octl CLI 运行或控制 opencode agent 会话时使用。
---

# octl —— 从命令行驱动 opencode 会话

`octl` 是面向操作者部署的 `opencode serve` 的一次性 CLI 客户端。每次调用即一个进程：
JSON 走 **stdout**，人读日志走 **stderr**，**退出码**即结果类别。只解析 stdout。

## 循环

**a. 会话首步——门禁。** 运行 `octl doctor`，先修好它报告的所有问题再继续。`doctor` 是硬
门禁：可达性、`/api/info` 形状（仅 v2 API 放行）、认证、版本基准。它不做端点发现。

**b. 开始一轮。**
```
octl create                      # 从项目目录运行；或：octl create --directory /绝对路径
octl create --trust "/tmp/opencode/*"   # 仅为本会话预信任 scratch 路径
# -> JSON，包含 ses_... 会话 id
octl chat -s ses_... --text "你的指令"   # 异步：入队即返回，不阻塞
octl wait -s ses_... --timeout 1800 > /tmp/opencode/wait-ses_....json   # 经你的 shell 工具后台运行，绝不前台（见"等待"一节）
```
`create` 总是显式发送 location（缺省为当前目录，可用 `--directory` 覆盖）；此后所有命令只按
`ses_` id 寻址会话。提示很长或较复杂时，把正文以 **stdin 上的 JSON** 传入，而非放进 argv
（防引号注入）。

**c. 退出码 7 = needs_permission。** 查看返回的请求详情（request id、工具、参数）。对
**安全、常规**的操作回应 `always`，以减少后续交互轮次；`once` 仅本次放行；`reject` 拒绝。
```
octl permission-reply -s ses_... --request-id per_... --decision always
octl wait -s ses_...
```

**d. 退出码 8 = needs_form。** 按返回的 `needs_form` 载荷填写，用 `octl form-reply -s ses_... --form-id FID`
提交（结构化字段答案以 stdin 上的 JSON 传入），然后再 `wait`。

**e. 退出码 0 = 成功。** 增量读取新输出：
```
octl messages -s ses_... --after <last_message_id>
```
把 wait/messages JSON 里的 `last_message_id` 作为下一次的游标；不要重读整段会话。

**f. 没有后台 shell？轮询。** `octl wait -s ses_... --once` 返回一次快照＋游标后立即退出
（忽略 `--timeout`）。带短睡眠自行循环，直到 `status` 离开 `running`——退出码 0 同样涵盖
`running`，必须按 JSON 的 `status` 字段分支。后台 wait 输出缺失时，它也是廉价的存活探测。

## 等待——永远在后台

`chat` 只入队；一轮由 `wait` 收口。前台 shell 调用会被你的运行时按命令超时掐断（通常只有
几分钟），短于中大型轮次——被杀后没有 JSON、没有退出码，轮次仍然打开。因此：

**每一次 `wait`——首次与全部续挂——都必须在后台运行**：走你 shell 工具的后台模式
（run-in-background bash、后台终端或等价功能），stdout 重定向到文件。`compact` 同为阻塞
调用，同样处理。

```
octl wait -s ses_... --timeout 1800 > /tmp/opencode/wait-ses_....json   # 后台
```

- `--timeout` 是检查点，不是死线：按轮次规模设置（大型轮次 1800–3600s）；退出码 6 表示
  "仍在生成"，并附 `partial_text` 诊断。
- 后台调用结束后读文件，按 JSON 的 `status` 字段（始终存在）分发。
- `succeeded` 隐含整个会话子树（含子 agent）已静默——一个 wait 跟踪全部委派工作。

| 结果 | 动作 |
|---|---|
| `succeeded`（0） | 轮次完成——增量读输出：`messages --after <last_message_id>` |
| `timeout`（6） | 仍在生成——续挂后台 `wait` |
| `needs_permission`（7） | `permission-reply` 后续挂 |
| `needs_form`（8） | `form-reply` 后续挂 |
| `failed` / `interrupted`（5） | 读部分输出，再决策 |
| 无 JSON / 进程消失 | 按仍在运行处理——`wait --once` 探测后续挂 |

**轮次门控信任：** `chat` 会记录轮次门，因此 `wait` 返回的 `succeeded` 一定属于你刚开启的这一轮；
退出码 6 携带 `diagnostics.round_gate`。该门**仅限本机**——若 `wait` 运行的机器上没有 `chat` 记录的门
（未记录），则 `chat` 后异常迅速的 `succeeded` 可能是上一轮的陈旧结果：行动前先用
`messages --after <last_message_id>` 交叉核对。

**绝不带着打开的轮次结束回合：**要么已握有经验证的结果，要么后台 wait 存活且收尾消息
写明（会话 id＋"仍在运行"）。若你的运行环境没有后台模式——或后台完成不通知——改用 §f
的 `--once` 轮询循环，在回合内一直循环到 `status` 离开 `running`。

## 退出码

| 码 | status | 含义 |
|---|---|---|
| 0 | success | 命令完成；阻塞式 `wait` 到达终态（`chat` 0 仅表示已入队；`--once` 0 可能仍是 `running`） |
| 2 | usage | 调用方式或参数错误 |
| 3 | `[availability]` | 端点不可达 / 不可用 |
| 4 | `[compatibility]` | API 或版本不兼容（需要 v2 API） |
| 5 | `[other]` | 未分类错误 |
| 6 | timeout | wait/chat 超时；会话可能仍在生成 |
| 7 | needs_permission | 阻塞，等待权限审批 |
| 8 | needs_form | 阻塞，等待表单输入 |

`status` 字段也始终同时出现在 JSON 里。

## 动词

| 动词 | 用途 |
|---|---|
| `doctor` | 会话首步硬门禁：可达性、`/api/info` v2 形状、认证、版本基准。 |
| `endpoints` | 枚举端点别名＋URL＋版本基准状态（不探活）；`--check` 追加逐端点探活、认证校验与基准。 |
| `agents` | 列出 agent 及其解析后的默认模型（只读）。 |
| `create` | 创建会话；以 cwd 作为 location，`--directory` 可覆盖。`--trust PATTERN`（可重复）为本会话预授权路径。`--model PROVIDER/ID[#variant]` 为本会话固定模型。返回 `ses_` id。 |
| `chat` | 异步入队一条提示并返回（不阻塞）。 |
| `wait` | 等待终态或需交互状态；`--timeout` 缺省 300s；`--once` = 单次快照＋游标。务必后台运行——见"等待"一节。 |
| `messages` | 拉取消息；`--after <id>` 只返回新输出及下一个游标。 |
| `permission-reply` | 回应权限请求：`--request-id` 加 `--decision once\|always\|reject`。 |
| `form-reply` | 提交表单答案：`--form-id` 加 stdin 上的 JSON 答案。 |
| `pending` | 列出会话子树的待处理权限/表单（非阻塞）。 |
| `interrupt` | 打断当前进行中的生成。 |
| `compact` | 压缩会话上下文并等待完成。 |
| `context` | 查看上下文用量（tokens/cost）与会话元数据。 |
| `delete` | 删除会话；不可逆，级联到子会话。 |

## 说明

- **JSON 在外，日志在旁：** 机器可读结果在 stdout，只解析 stdout。
- **会话寻址：** 传 `-s ses_...`；显式指定端点为 `--endpoint <别名>`。`create` 会记录路由，
  后续命令按 `ses_` id 自动解析端点；未命中时显式传 `--endpoint`。
- **pending 是子树作用域：** 子 agent 的权限/表单会上报给父会话控制者，其载荷携带请求的真实
  属主 `sessionID`。回应时用该 sessionID，而不一定是父会话的。
- **预信任 scratch 目录：** `create --trust PATTERN`（可重复）仅为本会话预授权资源——caller 声明、
  会话作用域、随会话消亡——例如 `/tmp/opencode/*`，以避免权限轮次。
- **为会话固定模型：** `create --model PROVIDER/ID[#variant]` 仅为本会话覆盖服务器默认模型——
  例如对简单任务使用便宜的 flash 模型。
- **端点为操作者配置：** 只用别名——绝不用 URL 或密码。
- **interrupt** 取消运行中的任务（例如超时后）；**compact** 在上下文接近上限时裁剪；
  **context** 在压缩前查看 tokens/cost；**delete** 删除会话及其全部子会话。

为 octl v1（CLI 契约 v1）编写。
