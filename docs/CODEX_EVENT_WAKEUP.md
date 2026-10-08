# 完成信号与 Codex 聊天唤醒

核对日期：2026-10-08。当前环境为 Windows、本机 Codex 聊天和独立 AutoCST Runner。

## 当前实现的边界

Runner 在完成导出和分析、失败、取消或需要人工处理时，发出该运行专属的 Windows 完成事件；SQLite 保存可恢复的状态。`integration/verify_research_simulation.py --wait` 注册事件并等待，收到信号后读取结果与实验上下文。等待没有固定间隔的状态查询，超时仅结束观察，不取消或重试求解。

这支持**当前仍在执行的 Codex 工具调用**收到结果后继续分析。独立 Runner 完成任务与重新唤醒一个已经结束的聊天回合是两件事。应用关闭时，已提交的 Runner 工作和证据持久化可以继续；本项目没有另启模型进程，也没有实现经验证的“完成事件自动启动原聊天新回合”接口。下次打开应用后可读取上下文恢复工作。

Windows 事件是即时通知，持久化状态才是判断依据。等待前先注册事件、再检查是否已经完成，避免运行恰好在连接重建期间完成而漏掉信号。不同运行使用不同事件名。

## 已核对的公开接口

| 接口 | 核对结果 | 本项目是否使用 |
|---|---|---|
| 本机 `codex queue --thread ID --message TEXT` | 已安装 CLI `0.162.0-alpha.2` 的公开帮助列出此命令；支持现有 session UUID 或精确名称。帮助没有承诺连接当前桌面客户端、应用关闭时只存队列而不执行模型，或重新打开时自动投递。 | 未执行、未接入；不能声称已验证即时唤醒。 |
| `codex exec resume ID` | 官方将其定义为继续非交互任务；它会发送后续提示并执行模型。文档未给出与正在运行的桌面聊天共用执行状态的保证。 | 不使用，符合用户“关闭应用后模型等重开”的选择。 |
| Codex app-server | 官方提供线程恢复与开始回合的协议，用于构建客户端；文档标注 app-server/WebSocket 为实验性，非生产支持接口。另启一个服务不等于连接正在运行的桌面服务。 | 不使用私有 IPC，也不另启服务绕过桌面生命周期。 |
| MCP Events | 官方提供订阅与 webhook：事件到达订阅聊天后按用户指令响应。目前适用 Work web、桌面 Work 的 Cloud 模式和 dots，并要求 MCP 2.0；不适用于此本机 Codex 聊天。 | 不迁移聊天、不增加云/API工作流。 |

参考：[CLI 开发命令](https://learn.chatgpt.com/docs/developer-commands)、[App Server](https://learn.chatgpt.com/docs/app-server)、[MCP Events](https://developers.openai.com/plugins/build/mcp-events)。官方[消息排队说明](https://learn.chatgpt.com/docs/prompting)解释了桌面中排队与插入当前回合的区别，没有定义外部程序唤醒本机聊天的生命周期保证。

## 本机只读证据

- 应用工具返回测试聊天为 `kind=codex`、`hostId=local`，工作目录是本项目镜像。公开记录省略聊天 ID 和个人路径。
- CLI 为桌面应用随附的 `codex.exe`，桌面包版本为 `26.1002.7124.0`。这些是核对日期的本机观察，其他版本需重新核对。
- 在桌面正在运行时，只读 `codex app-server daemon version` 仍无法连接默认 `app-server-control.sock`，返回 Windows `os error 10050`。因此未证明 CLI 的共享服务入口就是当前桌面聊天服务。
- 当前桌面 app-server 进程参数没有 `daemon` 或 `--listen`；结合官方默认传输说明，可推断它采用默认标准输入输出连接，不能据此宣称存在供外部 CLI 接入的公开监听端点。
- 只读检查安装包可见客户端消息排队的就绪与所有权条件；未调用内部 RPC、读取认证文件、修改全局配置、发送队列消息、开启新聊天或启动模型任务。

结论范围：**已实现完成信号驱动的本机执行与观察；尚未验证桌面原聊天在回合结束后的即时外部唤醒。** 固定周期扫描不能冒充即时完成通知。
