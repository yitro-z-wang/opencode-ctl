# DESIGN — opencode-ctl (`octl`)

状态:**定案**(2026-09-30)。本文取代 `DESIGN-remote-connections.md` 的地位(后者降级为历史记录)。重构讨论的完整决策过程见仓库会话记录;本文只记录结论与依据。

---

## 0. 一句话定位

`octl` 是一个面向 agent 的 opencode 命令行客户端:agent 按 skills 的指引调用 CLI,CLI 驱动操作者预先部署的 `opencode serve`。**没有 MCP 服务器,没有自建 daemon**——daemon 就是 `opencode serve` 本身。

## 1. 架构

```
agent ──读──> skills (SKILL.md)          教"怎么用"
      ──调──> octl (CLI, 每次调用一进程)   会话面: 14 个动词
                │  core lib (stdlib-only)  领域逻辑: 连接/分类/子树/等待
                └──HTTP──> opencode serve (操作者部署, 多客户端, 多目录)
                          ~/.config/octl/endpoints.toml (操作者私有)
```

三层职责:

- **core lib**:从现有 `server.py` 提取的领域逻辑(纯 stdlib,无进程状态假设);
- **CLI(`octl`)**:每命令一进程,JSON→stdout、日志→stderr、退出码=结果类别;
- **skills**:标准 SKILL.md,教交互循环,不含凭据不含 URL。

不建的东西(YAGNI,含触发器):

- 事件扇出 / 自定义 daemon —— 触发条件:单机多 agent 并发且互不信任,或团队共享主机;
- 每会话 ACL / 多租户 —— 同上;
- 项目级配置层叠 —— 无需求(v1 仅全局配置 + env);
- 任何形式的自动权限代答 —— 见 §5;
- MCP 面 —— 现有 `server.py` 在 CLI 达到功能对等后整块删除;
- `octl` spawn `opencode serve` —— serve 由操作者部署,`_spawn_local_serve` 整块消亡。

依据(外部证据,2026-09 调研):MCP 工具 schema 的 token 开销为等价 CLI 的 4–32 倍;Skills 已成开放标准(agentskills.io,约 40 家兼容);opencode v2 原生支持多客户端共享 serve(TUI 即客户端之一)。MCP 保留面(OAuth/审计/结构化 IO)对本项目场景无需求。

## 2. 威胁模型与安全边界

**范围内**(本项目负责):

- CLI 动词面不暴露凭据:无任何凭据参数、无端点写入动词、无凭据回显(含错误与调试输出);
- agent 不可枚举会话:无 list 动词;pending 类查询按会话子树精确过滤,`verified=false` 时返回空集(失败关闭,宁可漏报不可越界);
- agent 不可指定 URL:只有别名,别名→端点映射只存在于操作者配置;
- HTTP 传输卫生:响应体限额(见 §7),重定向跟随但跨源剥离 `Authorization`。

**范围外**(明确不防,写入文档以免未来"修复"):

- **同用户恶意 agent**:拥有裸 shell 的同用户 agent 可直接读配置文件、读 opencode 状态目录。这是宿主权限门控的职责,CLI 层无法兜底;
- **路径入侵**(http 明文链路窃听、TLS 拦截代理):与"端点被入侵"同级别,不单独防御;
- **操作者配置了恶意端点**:端点收到的是它自己的凭据,无增量泄露面。

凭据通道原则(承自 PR #1 的教训并强化):**秘密只从操作者通道进入(配置文件 / env / password_command),agent 只持有非秘密引用(别名、ses_)。**

## 3. 端点与凭据

配置文件 `~/.config/octl/endpoints.toml`(0600,操作者手编,无 CLI 写入动词):

```toml
default = "main"

[endpoints.main]
url = "http://127.0.0.1:4096"
username = "opencode"                      # 可省,v2 basic auth 缺省用户
password = "..."                           # 二选一

[endpoints.lab]
url = "https://build-box.example.com:4096"
password_command = ["pass", "show", "opencode/lab"]   # 秘密不落盘的替代通道
```

解析顺序:显式 `--endpoint <alias>` → 配置 `default` → `OPENCODE_URL`/`OPENCODE_PASSWORD` env。**无服务发现**(用户自行搞清有哪些实例,`doctor` 也不做发现)。

CLI 面仅有:`endpoints`(枚举别名+URL+版本基准状态,不探活)、`endpoints --check`(逐端点探活+认证校验+版本基准)。增删改 = 手编文件。

## 4. 会话 location 语义

依据(源码 + 官方文档核实,2026-09-30):

- `POST /api/session` 不带 `location` 时,目录 = **serve 进程的 cwd**(非客户端 cwd),共享部署下不可控;
- 单个 serve 原生承载多目录会话,config/skills/AGENTS.md 按会话目录 per-location 加载(服务图 60min 空闲 TTL);
- 官方客户端(desktop/TUI/SDK)全部显式传递客户端自己的目录。

规则:

- `octl create` **无条件显式发送** `location: {directory: <绝对路径>}`;缺省 = 调用时 cwd,`--directory` 覆盖;
- 后续命令按 `ses_` 寻址,serve 从会话行自解析目录,CLI 不再传 location;
- 多仓库工作不需要多端点:一个共享 serve + 每次创建带目录;
- core 统一解包运行时路由的 `{location, data}` 响应包裹(现有 server.py 已按实测形状处理,随提取带走)。

## 5. CLI 契约

### 动词表(相对现有 16 个 MCP 工具)

| 删除 | 原因 |
|---|---|
| `connect_server` / `disconnect_server` | 动态注册终结,端点唯一来源是配置文件 |
| `list_servers` | → `endpoints` |
| `list_sessions` | 不允许枚举会话(多 agent 靠 ses_ 不可预测隔离) |

保留映射:`doctor` `endpoints` `agents` `create` `chat` `wait` `messages` `permission-reply` `form-reply` `pending` `interrupt` `compact` `context` `delete`。

### 参数命名约定(阶段 3 实施中固化的契约细化)

- 会话寻址旗标:`-s SES` / `--session SES`;
- `permission-reply -s SES --request-id RID --decision once|always|reject [--message M]`;
- `form-reply -s SES --form-id FID`,字段答案以 JSON 对象从 stdin 传入(`{"fieldKey": value}`);
- 游标字段名:`last_message_id`(与现有 MCP `get_messages`/`wait` 输出一致);
- 等待旗标:`--timeout`(秒,缺省 300),不用 `timeout_secs`;
- `create` 返回 JSON 顶层键含 `session_id`;
- `chat` v1 参数面:`--text`(或 stdin JSON 传 `{"text": ...}`);v2 的 `files`/`delivery` 不进 v1;
- 未列出旗标的动词(`pending`/`interrupt`/`compact`/`context`/`delete`)只用 `-s SES`(+全局 `--endpoint`)。

### 关键语义

- **输出**:JSON→stdout,人读日志→stderr;退出码:0 成功 / 2 用法 / 3 `[availability]` / 4 `[compatibility]` / 5 `[other]` / 6 超时 / 7 needs_permission / 8 needs_form。`status` 字段始终同时在 JSON 里;
- **`chat`**:异步入队即返回(v2 prompt 语义),不阻塞;复杂/长文本参数走 stdin JSON(防引号注入);
- **`wait`**:`--timeout` 缺省 **300s**(阻塞长轮询,单进程内循环);`--once` = 单次快照+游标(agent 自管循环模式);**只有 manual 权限模式,不存在任何自动代答**——权限必须由 agent 审批。skills 明确指引:对安全请求回应 `always` 以减少轮次;终态判定为**轮次门控**(见下),绝不裸读 `outcome`;
- **`pending` / `pending` 类查询**:子树作用域(`parentID` 树 idset 精确成员过滤,子 agent 请求上报给父会话控制者,payload 带真实属主 `sessionID`);全局端点不可用回退逐会话端点;再不行 `verified=false` 空集失败关闭;
- **`permission-reply` / `form-reply`**:会话内寻址(`/api/session/{sid}/permission/{rid}/reply` 路径本身绑定会话);
- **预信任路径**:已解决(2026-09-30):`octl create --trust` 经 POST /api/session 的 `permissions: Permission.Ruleset` 实现——caller 声明、会话作用域、随会话消亡,比 always 沉淀的持久项目规则更窄且在 transcript 可审计。

### 轮次门控(stale-outcome 防御)

v2 的 `Session.outcome` 文档定义为"最近一次已完成执行的 Outcome":它**没有 running 值**,新提示入队或新一代开始时**不重置**,只在终态跃迁时改写,同时单调递增 `time.idle`(`time_idle = max(now, old+1)`);每个完成的轮次还会追加一条携带 outcome 的 `type:"idle"` 消息。因此任何已完成轮次之后,裸读 `outcome == "succeeded"` 在**下一整轮**期间都是陈旧的——第二次及以后的 chat→wait 轮次会返回上一轮的结果。

- `chat` 在状态库中记录每会话的**轮次门(round gate)**(见"路由缓存"):提交前的 `session.time.idle` 水位 + 入队提示的消息 id(prompt 响应的 `id`),尽力而为且**仅限本机**;后一次 `chat` 覆盖之。
- `wait` / `wait --once` 自动载入该门。`outcome` 属于 {`succeeded`、`failed`、`interrupted`} 时,**只有**满足以下条件才算终态:(a) `session.time.idle` 严格越过水位,**且**(b) 已知门消息 id 时,消息列表中存在位于其后的 `type:"idle"` 轮末消息。两者都是**服务端权威的持久标记,而非消息形状启发式**(设计史已移除形状启发式;这里门控的是轮次边界而非消息形状)。门通过之前,wait 持续轮询(`running`,最终退出码 6 超时)。
- 退出码 6 的超时 JSON 追加 `diagnostics.round_gate = {watermark, message_id, time_idle, watermark_passed, idle_message_seen}`;`--once` 的 running 载荷追加 `round_open: true` 与同形状的 `round_gate`。
- 轮次行在终态 outcome(`succeeded`/`failed`/`interrupted`)或会话删除时关闭。
- **过渡期回退**:chat 与 wait 分处不同机器、或未记录门时,wait 保持旧语义——直接信任原始 `outcome`,它可能是上一轮的陈旧值。

### 路由缓存

`~/.local/state/octl/routes.db`(**sqlite**)。两张表:`routes(ses_ TEXT PRIMARY KEY, endpoint TEXT, created_at)` 与 `rounds`(每会话轮次门:`time.idle` 水位 + 入队提示消息 id)。路由行在 `create` 时写入;后续命令按 ses_ 自动解析端点;miss 则报错要求显式 `--endpoint`。轮次行在 `chat` 时写入(尽力而为),由 `wait` 读取/关闭。选 sqlite 而非 JSON+原子写:事务与损坏检测内建,损坏概率与重建逻辑最小化。

### 版本策略

- `octl doctor`:硬门禁——可达性、`/api/info` 形状(v2 API 才放行)、认证、版本基准报告;不做发现;
- 常规命令:信任配置,零探测开销;失败时按 `[availability]/[compatibility]/[other]` 分类给退出码(失败后重查版本再分类,承现有机制);
- 版本与基准漂移:告警不阻断(opencode 迭代快,硬阻断会频繁变砖)。

## 6. skills

- 仓库 `skills/octl/SKILL.md` 单文件(标准 SKILL.md 格式,agentskills.io 兼容宿主通用);
- 教的是循环:`doctor`(会话首步)→ `create` → `chat` → `wait` → 分支[needs_permission → 审批,安全请求答 `always` → `permission-reply` → `wait`]→ 终态 → `messages --after <cursor>`;
- 高级内容(表单/子 agent/compact)为同级章节,不拆文件;
- 锁 CLI 大版本;双语(en / zh-CN)。

## 7. core 提取与 PR #1 的处置

从 `server.py` 提取:连接对象、`http_request`、探测/版本门禁、失败分类、子树解析、`poll_once` 拆分(轮询体=状态快照+游标;循环壳留在表层)、数据整形。

自 PR #1 **只摘两样**(响应体上限按实测重估:限额作用于**原始报文**,消息端点内嵌完整工具 I/O,1 MiB 会误伤工具 I/O 重的会话):

1. **响应体限额,两级**:默认端点(info/session/permission/form,正常 KB 级)** 4 MiB**;消息拉取(messages / wait 的 assistant_text 路径)** 64 MiB**;HTTP 错误体 64 KiB 读上限。声明 Content-Length 按同级预检(读前拒绝),分块传输受实读上限约束——与 URL 来源无关的通用客户端卫生;
2. **重定向策略**:跟随重定向,但 **Location 跨源时剥离 `Authorization`**(curl `--location` / 浏览器 fetch / Go `net/http` 的通行姿态;urllib 与 requests 默认原样重放是已知 footgun)。

不移植:SSRF URL 门禁(agent 不再提交 URL,整体移植会误伤合法私网端点)、凭据通道改动(被架构本身取代:agent 面根本没有凭据参数)、MCP stdio 加固(stdin 行上限/workers/params 容错,随 server 消亡)。

PR #1 关闭,标注 superseded by opencode-ctl refactor;其评审发现的 F1(`::1` 解包错序)/F2(探测路径重验缺口)/F3(stdin 假上限)随门禁消亡,无需修复。

## 8. 迁移计划(每步全绿)

| 阶段 | 内容 |
|---|---|
| 0 | GitHub 改名 `opencode-ctl`(旧 URL 自动重定向);确定 Python ≥3.11 / TOML |
| 1 | core 提取;`server.py` 改为引用 core,行为不变;live 测试(MCP driver)全绿;并入 §7 两项硬化 |
| 2 | CLI(`octl`)实现 §5 全部契约;测试套件参数化 driver(MCP/CLI 双跑) |
| 3 | skills(`skills/octl/SKILL.md` 双语)+ 安装矩阵(各宿主 skill 目录) |
| 4 | 删除 `server.py`、MCP 测试与 `mcp_client.py` harness;close PR #1;README 双语重写;`DESIGN-remote-connections.md` 标注为历史记录 |

## 9. 语言与文档约定

- 代码/CLI 机器输出:英文;文档:en + zh-CN 双语(项目既有约定);
- 本文档英文版在中文稿审定后跟进;README 重写在阶段 4。
