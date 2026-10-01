# octl CLI 测试

[English](README.md) | **中文**

面向 `octl` CLI(`octl_core.py` + `octl`)的离线与实机测试套件。离线套件在无外部网络下
校验 CLI 契约;实机套件对真实的 `opencode serve` 驱动常规 agent 循环。

## 前置条件

- Python 3.11 或更高版本。套件只使用标准库:不依赖 pytest,也不依赖任何第三方包。
- 实机套件需要 `opencode` 位于 `PATH`。它自行派生一个 `serve`,二进制缺失时报告 SKIP。
  离线套件不需要任何服务。

## 运行方式

每个套件都可独立运行,输出每个场景一行,外加一行汇总:

```sh
python3 tests/cli_offline_test.py
python3 tests/live_cli_test.py
```

仅当至少一个场景 **失败** 时进程退出码才为 `1`。SKIP 不会改变退出码,因此环境无法支持某个
场景时绝不会变成一次失败的构建。

## 环境

套件不需要任何 `OPENCODE_TEST_*` 变量。实机测试的 harness(而非 `octl`)在空闲端口上派生
`opencode serve`,以随机 `OPENCODE_SERVER_PASSWORD` 启动,并把一次性的 `endpoints.toml` 写入
临时 `XDG_CONFIG_HOME`,因此完全自包含。离线套件同样把 `XDG_CONFIG_HOME` / `XDG_STATE_HOME`
重定向到自己的临时目录。

## 覆盖范围

### `cli_offline_test.py`

`octl` CLI 的离线套件,无外部网络:覆盖配置解析(`endpoints.toml` 的 `password` 与
`password_command` 两种通道,以及宽松权限警告)、路由数据库(写入 / 查找 / 未命中)、
URL 作为别名被拒、`OpenCodeError` 类别 → 退出码映射(monkeypatch)、`chat` / `form-reply`
的 stdin JSON 参数处理,以及凭据不泄露不变量(错误密码与失败的 `password_command`);
以上均针对一个绑定在 `127.0.0.1` 的一次性 HTTP 服务。

该一次性服务精确复刻 opencode v2 的 `Session.outcome` 语义:没有 running 值,新提示入队时
不重置,仅在终态转换时与单调递增的 `time.idle` 一起被重写。五个 round-gate(陈旧结果防御)
场景通过 `POST /__test/complete` 控制端点驱动这一模型:

- 陈旧 succeeded:某轮完成后,新的 `chat` + `wait --timeout` 必须超时(退出 6,
  `diagnostics.round_gate.watermark_passed == false`),而不是返回上一轮;待该轮真正完成后
  再成功;
- 陈旧 failed:上一轮的 `failed` 结果绝不会返回给新一轮;
- legacy 回退:没有已记录的 `rounds` 行时,`wait` 立即信任原始结果(有文档记载的过渡行为);
- idle 消息闸门:水印前进但 gate 消息之后没有 `type:"idle"` 消息时仍会超时
  (`idle_message_seen == false`);
- `--once`:轮次开放时报告 `status:"running"` + `round_open` + `round_gate`,终态后报告
  `succeeded` 并关闭该行,使之后的 wait 走 legacy。

### `live_cli_test.py`

harness(而非 `octl`)在空闲端口上以随机 `OPENCODE_SERVER_PASSWORD` 派生 `opencode serve`,
等待就绪后把一次性的 `endpoints.toml` 写入临时 `XDG_CONFIG_HOME`,随后驱动常规 agent 循环:
`doctor` → `create` → `chat`(异步)→ `wait` → `messages --after` → `delete`。之后运行一次
两轮冒烟:以唯一标记短语发起第二次 `chat`,可选一次 `--once`,再做阻塞 `wait`,断言返回的是
第二轮的文本(绝非陈旧的第一轮结果)。每一步都断言退出码与 JSON 键,并断言服务密码绝不泄露到
stdout+stderr;`api_version_warning` 存在时被容忍。当 `opencode` 不在 `PATH` 上时报告 SKIP。

## 跳过行为

- `opencode` 不在 `PATH` → 实机套件报告 SKIP(退出码 0)。
- 离线套件没有外部依赖,从不跳过。
