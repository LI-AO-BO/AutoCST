# AutoCST

用 ChatGPT/Codex 控制本机 CST Studio Suite 2025 开展仿真。Python 执行本地操作，MATLAB 调用同一命令入口，MCP 提供结构化工具。

v0.2 增加科研实验、可审查的预备输入、持久队列、独立 Runner、事件、恢复上下文和有明确范围的贝叶斯优化批次。原 v0.1 波导接口继续保留；真实 CST 求解证据与各版本验收范围见 [VERIFICATION.md](VERIFICATION.md)。新增模板或接口通过软件测试，并不等于已经完成 CST 求解、网格收敛或物理验证。

v0.3 增加结构化线性 lumped element：R/L/C 串并联、明确单位与连接点、元件值参数调节、属性/坐标读回及修改日志。通过已有研究 MCP/CLI/MATLAB 接口使用，详见 [集中元件说明](docs/LUMPED_ELEMENTS.md) 与 [实机验收](VERIFICATION_v0.3.md)。

## 执行与科研闭环

```text
ChatGPT/Codex ── MCP ──┐
MATLAB ── Python CLI ──┼─ 实验目标/边界 → 准备输入 → 审查 → 幂等提交
命令行 ───────────────┘                              ↓
                                            独立 Runner → 指定 CST 实例
                                                   ↓
                            原始结果、指标、事件、实验上下文
                                                   ↓
                         Codex 解读证据 → 边界内选择下一组参数
```

Runner 执行已提交的建模、求解、导出与分析；MCP 连接负责短请求，长时运行不占住一个 MCP 调用。正常求解由独立工作进程调用官方 `Model3D.run_solver(timeout=None)`，等待求解及后处理结束；Runner 等待该进程的 Windows 原生退出信号，核对成功收据后导出、分析并发出运行专属的命名完成事件。正常路径没有固定间隔的 CST 状态查询。接口与真实探针依据见[官方证据](docs/official-evidence.md)。

后台每 10 秒更新健康记录，并检查持久数据库中漏发通知的待办与完成记录，不查询 CST。同步工作进程丢失时，异常恢复可以由工程目录变化触发状态核查；恢复过程不再次启动同一次求解。

Codex 收到完成或失败信号后读取上下文、解释证据，再决定继续、调整参数或停止。`integration/verify_research_simulation.py --wait` 使用 Windows 完成事件；观察超时只结束等待，不取消或重试求解。应用不在线时，Runner 可以继续已提交的执行；已明确授权的优化批次也可按冻结算法继续有限调参，结果与决策记录留在本地。重新打开对话后读取上下文恢复 Codex 分析。旧的 15 分钟 Codex 定期唤醒已暂停，当前没有经验证的外部接口可即时唤醒已结束回合的本机聊天，详见[完成信号与聊天唤醒边界](docs/CODEX_EVENT_WAKEUP.md)。

实验记录目标、参数范围与运行预算。已授权范围内的调参使用新的预备任务；改变目标、拓扑或预算需用户授权后修订实验。旧运行保留原来的实验版本、输入和决策依据。

## 模型范围

- `waveguide`：均匀真空矩形波导，PEC 壁、两波导端口和 TE10 激励；默认 WR90 尺寸 22.86 × 10.16 × 40 mm、8.2–12.4 GHz。模板限制在 TE10 单模频段。
- `metasurface`：理想方形贴片、无损介质基板和 PEC 接地板的周期单元，法向入射 Floquet 设置。适用范围以模板、输出指标及验收记录为准。
- `history`：冻结明确提供的 CST 建模历史，审查后提交。准备结果包含历史文件与哈希。
- `existing_project`：以明确的 `.cst` 来源工程创建本次运行副本；准备阶段记录来源哈希，提交时检查变化。

通用历史和已有工程需要明确的参数、结果树路径及判断标准。求解器成功退出不等于研究目标达成。

## 安装与环境

需要 Windows、可正常启动的 CST Studio Suite 2025 和相应许可证。本机开发使用 Python 3.11.9；项目虚拟环境加载 CST 安装目录中的官方 Python 库。

先克隆仓库并进入目录，再运行安装脚本。CST 安装库和官方 PDF 不随仓库分发。手册检索需要用户将自己合法取得的 PDF 放在 `sources/CSTStudioSuite_All_In_One.pdf`；未提供 PDF 不影响建模、求解与结果分析。

```powershell
git clone https://github.com/LI-AO-BO/AutoCST.git
cd AutoCST
.\setup.ps1 -Python 'C:\path\to\python.exe' -InstallRunner
.\.venv\Scripts\python.exe -m autocst doctor
.\.venv\Scripts\python.exe -m autocst research environment
.\.venv\Scripts\python.exe -m autocst research runner-status
.\.venv\Scripts\python.exe -m autocst research start-runner
```

`-InstallRunner` 安装当前用户的执行任务，不启动 CST。`start-runner` 启动已安装的任务；状态查询同时检查进程身份、`alive`、`responsive` 与心跳，不能仅凭旧 PID 判断存活。

本机 CST 设置了 Windows `RUNASADMIN` 兼容选项，普通 Python 自动新开 CST 会受启动权限影响。已验证的连接方式是用户手动打开 CST，再提供当次明确的 `cst_pid`。自动发现列表可能为空，显式进程号仍可连接。不要沿用上次启动的 PID。

v0.2 准备任务要求选择已运行的 CST 实例，并记录 PID 与进程创建时间。任务在指定实例中使用独立工程，保留主程序及已有工程；不会随机连接任意实例。

## 开始一个研究实验

将目标和预算保存为 `experiment.json`，例如只研究波导长度对传播相位的影响：

```json
{
  "objective": "核对 WR90 波导传播相位随长度的变化",
  "model": {"kind": "waveguide", "fixed_parameters": {"a_mm": 22.86, "b_mm": 10.16}},
  "parameter_bounds": {"length_mm": [30, 60]},
  "budgets": {
    "max_runs": 3,
    "max_total_solver_seconds": 1800,
    "max_run_solver_seconds": 600
  }
}
```

`job.json` 中的 PID 必须替换为当次 CST 进程号：

```json
{
  "kind": "waveguide",
  "cst_pid": 12345,
  "parameters": {"length_mm": 40},
  "timeout_seconds": 300,
  "solve": true
}
```

可在 `decision.json` 记录本步理由和依据，例如 `{"reason":"先建立40 mm基线，再比较长度引起的相位变化","evidence":[]}`。

```powershell
.\.venv\Scripts\python.exe -m autocst research create-experiment experiment.json
.\.venv\Scripts\python.exe -m autocst research prepare <experiment_id> job.json --decision decision.json
```

准备阶段校验并冻结输入，不执行 CST。检查返回的 `review`、历史文件、参数、实例身份和时间预算后提交：

```powershell
.\.venv\Scripts\python.exe -m autocst research submit-prepared <prepared_id> --idempotency-key baseline-40mm
.\.venv\Scripts\python.exe -m autocst research status <run_id>
.\.venv\Scripts\python.exe -m autocst research results <run_id>
.\.venv\Scripts\python.exe -m autocst research context <experiment_id>
.\.venv\Scripts\python.exe -m autocst research events --experiment-id <experiment_id> --after 0
```

同一实验、相同提交内容使用同一个幂等键重试，返回同一个运行；不同内容不能复用该键。准备后若实现代码、建模历史或来源工程改变，需要重新准备。`solve: false` 仅建模，不把已有结果当作本次求解。

事件的 `event_id` 是整数。解读结果并决定下一步后，使用 `research ack <event_id>` 确认已处理；查询事件不会隐式确认。保留事件游标，下次用 `--after <event_id>` 获取后续事件。

```powershell
.\.venv\Scripts\python.exe -m autocst research control <experiment_id> pause
.\.venv\Scripts\python.exe -m autocst research control <experiment_id> resume
.\.venv\Scripts\python.exe -m autocst research control <experiment_id> cancel
```

`pause` 阻止后续任务启动，让当前任务完成；`cancel` 是单独的取消请求，需查询状态确认求解是否停止。`needs_attention` 表示结果或执行状态仍有不确定性，保留任务的执行占用；恢复时核查已有状态，不重复启动同一次求解。

获用户授权改变目标、拓扑或预算后，使用 `research revise-experiment <experiment_id> updated-spec.json` 创建新版本。原运行记录不随之改写。

## 有范围的贝叶斯优化批次

用户明确授权批次后，Runner 可在原实验的参数范围、次数和时间预算内自动完成多轮“结果分析 → 选择参数 → 准备 → 提交”。当前评分面向结构化相位目标，使用高斯过程与期望改进选择候选，约束不满足的有效结果按记录的惩罚评分处理。每轮保留训练依据、预测、参数变化和实际反馈；算法建议不代表全局最优。

以下命令用于已有相位目标实验；`job-template.json` 应匹配其模型和固定参数，并指定当次 CST 实例：

```powershell
.\.venv\Scripts\python.exe -m autocst research start-optimization <experiment_id> job-template.json --max-new-runs 4 --seed 7 --idempotency-key bayesian-batch-1
.\.venv\Scripts\python.exe -m autocst research optimization-status <batch_id>
.\.venv\Scripts\python.exe -m autocst research control-optimization <batch_id> pause
.\.venv\Scripts\python.exe -m autocst research control-optimization <batch_id> resume
.\.venv\Scripts\python.exe -m autocst research control-optimization <batch_id> stop
```

启动返回 `batch_id`，不等待求解结束；同内容、同幂等键重试返回同批次。同一实验只能有一个未结束批次。模板、实验版本、实现代码和 CST 实例身份冻结，变化或执行不明确时停止自动推进，进入 `needs_attention`。恢复提交复用已保存的预备任务及同一运行键，避免断线造成重复求解。

`max_new_runs` 限制本批次新增运行，候选网格复验也计入；原实验预算仍有约束。相位目标命中后，若超表面实验已声明独立初始网格与变化容差，批次保持候选的物理参数，补齐缺少的网格运行，再分别保存批次 `validation.json` 与实验复验汇总。有限网格敏感性通过不代表模态收敛或硬件实测。未声明复验时，命中以候选状态结束。

批次 `pause`、`stop` 阻止未来提出新运行，已经提交的任务继续，即使它仍在队列中。要阻止已排队任务开始，使用实验 `pause`；要停止当前求解，使用明确的实验 `cancel` 并核对停止证据。已结束或停止的批次不能重新启动，继续研究应使用新批次键。

## ChatGPT/Codex 接入

本机优先使用 stdio MCP：

```powershell
.\.venv\Scripts\python.exe -m autocst.mcp_server --root '<本项目绝对路径>'
```

当前提供 21 个工具：

|用途|工具|
|---|---|
|环境与官方手册|`cst_environment`、`cst_search_manual`、`cst_read_manual_pages`|
|实验目标与上下文|`cst_create_experiment`、`cst_revise_experiment`、`cst_experiment_context`|
|准备与提交|`cst_prepare_simulation`、`cst_submit_prepared`|
|研究运行与事件|`cst_research_run_status`、`cst_research_run_results`、`cst_completion_events`、`cst_acknowledge_event`|
|执行控制|`cst_control_experiment`、`cst_runner_status`、`cst_start_runner`|
|自动优化批次|`cst_start_optimization`、`cst_optimization_status`、`cst_control_optimization`|
|v0.1 兼容|`cst_submit_waveguide`、`cst_run_status`、`cst_run_results`|

科研任务使用实验与准备/提交接口。提示词示例：

> 在已授权的波导长度范围和运行预算内研究传播相位。先读取环境与官方手册，明确目标、指标和停止条件；每步准备可审查输入后提交。完成后消费事件、核对原始结果并解释证据，再决定下一组参数。目标、拓扑或预算变化先取得我的授权。

根据 [配置示例](integration/codex-config.example.toml) 替换本机绝对路径，再在可信项目的 `.codex/config.toml` 或个人 MCP 配置中登记。个人配置不随仓库分发；已打开的聊天可能需要重新加载连接。

可选回环 HTTP：`python -m autocst.mcp_server --transport streamable-http`，地址 `http://127.0.0.1:8765/mcp`。它仅供本机连接；ChatGPT 网页需要另行配置可达的 MCP 连接与认证。本项目没有发布公网服务。接入依据 [官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 和 [ChatGPT 自定义 MCP 说明](https://developers.openai.com/api/docs/guides/custom-mcp-server)。

## MATLAB

```matlab
addpath(fullfile(projectRoot, 'matlab'))
env = autocst("doctor");
runner = autocst("research", "runner-status");
experiment = autocst("research", ["create-experiment", fullfile(projectRoot, "experiment.json")]);
prepared = autocst("research", ["prepare", experiment.experiment_id, fullfile(projectRoot, "job.json")]);
run = autocst("research", ["submit-prepared", prepared.prepared_id, "--idempotency-key", "baseline-40mm"]);
context = autocst("research", ["context", experiment.experiment_id]);
```

第二个参数用字符串数组传递 `research` 子命令及参数，每项作为独立进程参数传入，路径不会拼接成 shell 命令。原 `autocst("submit", jobFile)`、`autocst("status", runId)` 和 `autocst("results", runId)` 保留。

MATLAB 通过 Python 子进程调用，无需在 MATLAB 进程中加载 CST Python 库。本机 R2025b 已实测新增 `research runner-status` 只读调用；这不等于已验证 MATLAB 驱动的 v0.2 求解。MATLAB 2026 的定位和验收情况以验证记录为准。

## 记录与验证

- `sources/CSTStudioSuite_All_In_One.pdf`：开发时使用的本地参考资料为 1660 个物理页，原文只读保留；用户自行提供，仓库不分发。
- [官方依据](docs/official-evidence.md)：关键接口及物理页码；OCR 未命中不能证明 API 不存在，需核对原页及本机同版本帮助。
- `.autocst/manual/`：按 PDF 哈希隔离的本地检索缓存，可用 `autocst search` 和 `autocst pages` 查询。
- `.autocst/research.sqlite3` 及其旁的 `experiments/`、`prepared/`、`research_runs/`：实验、冻结输入、事件、工作记录与结果；v0.1 运行保留在 `.autocst/runs/`。

每次调参创建新的 `run_id`，旧参数和旧结果保留。每轮目录保存冻结的 `job.json`、`spec.json`、`decision.json` 与输入哈希，另保存实际建模脚本、CST 工程、求解日志、原始 CSV 和 `analysis.json`。实验修订创建新版本；原运行保留提交时的目标与判据。SQLite 另记录阶段变化、完成、故障和执行控制。

自动批次另保存在 `.autocst/optimization_batches/<batch_id>/`，包含冻结模板与策略、批次状态、每轮提议和预备提交检查点、控制记录及复验汇总。批次目录关联具体 `run_id`，不替代各轮原始证据。

查看参数与结果修改日志：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-report.txt
.\.venv\Scripts\python.exe integration\render_research_report.py --root . --experiment-id <experiment_id>
```

生成 `.autocst/experiments/<experiment_id>/report.md` 与 `report.png`，展示父运行、调整依据、参数差异、相位变化、目标误差和原始证据链接。报告是可更新的汇总视图，各轮原始记录保留；网格复验结论另外保存。完整对话工作流见[科研工作流](docs/RESEARCH_WORKFLOW.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Runner 已安装运行后，对已有实验进行只读重连检查：

```powershell
.\.venv\Scripts\python.exe integration\verify_research_bridge.py --experiment-id <experiment_id> --require-runner
```

脚本关闭自己的 MCP 连接，检查原 Runner 心跳继续推进，再建立新连接核对工具协议、事件和实验输入恢复。它不提交、取消或确认任务，也不安装或启动 Runner。报告保存在 `.autocst/verification/`；未指定实验则不检验该实验恢复，没有运行中的 Runner 时不声称独立执行已验证。

v0.1 的真实短波导链路保留为历史验收；其状态轮询方式与 v0.2 科研 Runner 的事件等待分别记录。一次理想波导结果的数值核对不代表网格收敛、硬件实测或其他模型正确性。求解成功、数据完整、数值有效、研究目标达成需要分别判断。

当前 16 小时后台存活与真实超过 12 小时的 CST 求解尚未完成验收；先前存活检查因 v0.3 升级中断，原证据保留。后台存活、Codex 应用关闭后恢复、锁屏以及真实长时求解需要各自留证，不能以短算例或健康记录替代。

## 开源范围

原创代码和项目说明采用 [MIT 许可证](LICENSE)。仓库发布源代码、测试、配置示例与本机验证摘要；原始仿真结果、CST 工程、运行数据库、私人配置、官方 PDF 和安装库仅保留在本地。验证摘要中的本地证据路径是索引，不表示原始文件可从 GitHub 获取。CST 与第三方组件的权利见 [第三方说明](THIRD_PARTY_NOTICES.md)。
