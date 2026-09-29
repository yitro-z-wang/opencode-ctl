# opencode-ctl (`octl`)

[English](README.md) | **中文**

`octl` 是一个面向 agent 的命令行客户端与配套 skill,通过 v2 HTTP API 驱动**由操作者部署**的
`opencode serve`。每次调用即一个进程:JSON 走 **stdout**,人读日志走 **stderr**,**退出码**即
结果类别。

本仓库由 `opencode-mcp` 改名而来。CLI 达到功能对等后,原 MCP server 已整块退役 —— 决策依据见
[DESIGN-remote-connections.md](DESIGN-remote-connections.zh-CN.md)。

## 要求

- **Python ≥ 3.11**(纯标准库,无任何第三方包)。
- 一个**由操作者运行的 `opencode serve`**。`octl` 绝不自行拉起服务,只连接一个已就绪的实例。
- 可选:若通过 `password_command` 配置凭据,需要 PATH 上有 `pass` 一类的辅助工具。

## 安装

```sh
git clone https://github.com/yitro-z-wang/opencode-ctl ~/opencode-ctl
ln -s ~/opencode-ctl/octl ~/.local/bin/octl     # 确认 ~/.local/bin 在 PATH 上
```

把 `skills/octl/` 复制(或符号链接)到宿主 agent 的 skill 目录即可安装 skill,例如:

- `~/.opencode/skills/octl/`(项目本地为 `.opencode/skills/`)
- `~/.claude/skills/octl/`
- 其他任何兼容 SKILL.md 的宿主的等价 skill 目录

## 配置

`octl` 只读取一个操作者私有的文件 `~/.config/octl/endpoints.toml`(权限 **0600**)。没有任何
CLI 动词会写它 —— 手动编辑。

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

解析顺序:显式 `--endpoint <别名>` → 配置中的 `default` → `OPENCODE_URL` / `OPENCODE_PASSWORD`
环境变量。端点只以**别名**寻址;没有服务发现。

## 快速开始

```sh
octl doctor                                    # 硬门禁:可达性、v2 形状、认证、基准
octl create                                    # -> JSON,包含 ses_... 会话 id
octl chat -s ses_... --text "你的指令"           # 异步:入队即返回,不阻塞
octl wait -s ses_...                           # 阻塞,最长到 --timeout(缺省 300s)
octl messages -s ses_... --after <游标>         # 从 last_message_id 游标增量读取
```

`create` 总是显式发送 location(缺省为当前目录,可用 `--directory` 覆盖);此后所有命令只按
`ses_` id 寻址会话。提示很长或较复杂时,把正文以 stdin 上的 JSON(`{"text": ...}`)传入,而非
放进 argv(防引号注入)。

**退出码**

| 码 | status | 含义 |
| --- | --- | --- |
| 0 | success | 命令完成;对 `wait`/`chat` 表示到达终态 |
| 2 | usage | 调用方式或参数错误 |
| 3 | `[availability]` | 端点不可达 / 不可用 |
| 4 | `[compatibility]` | API 或版本不兼容(需要 v2 API) |
| 5 | `[other]` | 未分类错误 |
| 6 | timeout | `wait`/`chat` 超时;会话可能仍在生成 |
| 7 | needs_permission | 阻塞,等待权限审批 |
| 8 | needs_form | 阻塞,等待表单输入 |

`status` 字段也始终同时出现在 JSON 里。

## 安全模型

- **凭据只从操作者通道进入**(配置文件 / env / `password_command`)。agent 面没有任何凭据参数,
  也没有写端点的动词。
- **agent 面没有 URL** —— 只有别名;别名 → 端点的映射只存在于操作者配置里。
- **不允许枚举会话** —— 没有 `list` 动词。pending 类查询按会话子树作用域过滤,无法验证时空集
  失败关闭。
- **操作者预信任:** 操作者还可通过 opencode 自身的权限配置(`opencode.json` 中的 `permissions`
  规则)全局预信任路径/命令——见 <https://opencode.ai/v2/docs/permissions>。
- **范围外:** 同用户的恶意 agent 可以直接读取配置文件与 opencode 状态目录。这是宿主权限门控的
  职责,CLI 层无法兜底。

## 文档

- [设计:opencode-ctl(`octl`)](DESIGN-opencode-ctl.zh-CN.md) · [English](DESIGN-opencode-ctl.md)
- [Agent skill](skills/octl/SKILL.zh-CN.md) · [English](skills/octl/SKILL.md)
- [测试](tests/README.zh-CN.md) · [English](tests/README.md)
- [历史:远端连接设计记录(已退役的 MCP)](DESIGN-remote-connections.zh-CN.md) · [English](DESIGN-remote-connections.md)
