# 官方资料依据与版本边界

本页是开发时的资料核对和本机验收摘要。官方 PDF、安装库以及 `.autocst/` 原始探针和仿真结果不随公开仓库分发；以下本地路径仅供开发现场追溯，其他用户需要用自己的正版安装和运行记录复核。

来源：`sources/CSTStudioSuite_All_In_One.pdf`，69,847,493 bytes，1660 个 PDF 物理页。

SHA-256：`d60c25f82e2ca03c9940af9ceb97eab6899d702c0a2ad98c7b428732a5f6e14a`。

以下页码为从 1 开始的 PDF 物理页，不是各分册页脚。关键签名已查看原页图像，并与本机 CST 2025.2 HTML 帮助交叉核对。

| 物理页 | 依据及实现意义 |
|---|---|
| 1 | 主体封面 Version 2025.0。 |
| 992 | Python、VBA、OLE 自动化与 MATLAB 集成的概述，不能据此推定每个具体 COM 调用均可用。 |
| 1558–1559 | Python 库支持 64-bit 3.9–3.12；3.8 deprecated。实际开发使用 3.11.9；本机 link wheel 的 metadata 声明 `<3.12`，故不依赖该 wheel 推断 3.12 可用。 |
| 1561 | `DesignEnvironment.new()` 创建新实例；`connect_to_any()` 在多个实例存在时可能随机连接。v0.2 采用用户明确选择的现有实例 PID，并核对进程创建时间；只在该实例创建或复制独立任务工程，不关闭用户主应用。 |
| 1563 | `new_mws()`、`open_project(path)`；quiet mode 不能屏蔽所有要求用户输入的对话框。 |
| 1565 | `Project.save(path='',include_results=True,allow_overwrite=False)`、`add_to_history(header,vba_code,/,timeout=None)`、`abort_solver()`、`get_solver_run_info()`。History 会执行代码；v0.2 在执行前冻结模板参数或经审阅的 History、源文件和代码哈希，再按准备记录提交。 |
| 1566 | `run_solver()` 等待求解和后处理结束，错误抛出 RuntimeError。仅凭 `is_solver_running()==False` 不能证明成功。 |
| 1571–1572 | 离线结果接口 `cst.results.ProjectFile`、`get_3d()`、`get_tree_items()`、`get_result_item(treepath,run_id=0)`。 |
| 1574 | 不支持读取正在仿真、受保护或归档打包的工程。实现采用求解完成→保存→关闭→离线读取。 |
| 1648–1660 | 旧版应用笔记，Version 1.0 / 2020-04-27，正文为 CST 2020 + Python 3.6；不能作为 2025 接口实测。 |

本机官方帮助目录：`C:\Program Files (x86)\CST Studio Suite 2025\Online Help\Python\source\`，其中 `cst.interface.html`、`cst.results.html` 是准确接口签名来源。VBA 对象帮助亦在同一安装的 `Online Help` 中。

原 PDF、安装库及官方文档版权归原权利人；本项目不重新分发安装库。检索缓存只为本地使用，保留源哈希与页码。

## v0.2 同步完成信号：文档语义与本机证据

本机 `Online Help/Python/source/cst.interface.html#cst.interface.Model3D.run_solver` 明确区分两个调用：`run_solver(timeout=None)` 等待当前求解器及后处理完成，出错抛异常；`start_solver(timeout=None)` 异步返回，调用者另行检查运行状态。当前后端的 `solve_wait()` 使用前者，在独立工作进程内等待原生返回，再导出和分析；Runner 等待工作进程退出并发布每个运行专属的 Windows 完成事件。没有以固定间隔重复询问 CST 是否结束。

同步实机探针位于 `.autocst/backend_probe/metasurface_sync_20261008T142207`：`start_returned.json` 记录 `completed_native_wait=true`；`.autocst/backend_probe/metasurface_sync_20261008T142207/solver_evidence.json` 记录求解成功、仅 `Zmax` mode 1、实际 6 次自适应、最终 17,623 个网格单元、最后 ΔS=0.00519394，内部自适应判据满足。`.autocst/backend_probe/metasurface_sync_20261008T142207/numerical_check.json` 记录功率检查通过，同时明确 `mesh_convergence=false`、`modal_convergence=false`。同步返回和内部判据通过不能替代独立网格/模态收敛或实测。

本机内核事件跨进程验证记录：`.autocst/verification/native-event-20261008T063635338355Z.json`。它只验证两个测试 Python 进程之间的事件通知，不冒充 CST 求解证据。原聊天的外部即时唤醒边界见 [CODEX_EVENT_WAKEUP.md](CODEX_EVENT_WAKEUP.md)。

## Floquet 端口大小写：官方帮助与本版本实机冲突

本机官方 `Online Help/mergedProjects/VBA_3D/special_vbasolver/special_vbasolver_fdsolver_object.htm` 的 `FDSolver.AddToExcitationList(port, mode)` 把 Floquet 端口列为小写 `zmin`/`zmax`。同安装目录 `special_vbaports/floquetport_object.htm` 的 `Port` 签名也列小写，但紧接的说明、默认值和示例使用 `Zmin`/`Zmax`。这是官方资料内部存在的大小写不一致，不能仅凭枚举表断言两者可互换。

本机早期小写 `zmax` 激励被 CST 移除，因而未建立“仅请求的 TE(0,0) 被激励”的证据。即使功率近似守恒，也不能据此认可该激励。当前模板显式使用 `FloquetPort.Port "Zmax"`、`FDSolver.AddToExcitationList "Zmax", "TE(0,0)"`，并核对模态编号及实际 solver log。后期探针的 `solver_evidence.json` 记录 `only_zmax_mode_1=true`；没有声称存在未发现的激励列表 getter（记录字段为 `excitation_list_item_getter_available=false`）。本机可执行写法和有效性判断以这些实测及日志为准，保留文档冲突。

## 自适应次数和参考面：最大允许 8 次，实际完成 7 次

官方 `FloquetPort.SetDistanceToReferencePlane(value)` 定义相位去嵌距离；新参考面位于结构内部时用负值。当前模板设置 `-air_height_mm`，将参考面固定在贴片上表面 z=1.635 mm。两个空气高度 10 mm、12 mm 的后期对照记录如下：

| 探针 | 设置及实际结果 | 证据用途 |
|---|---|---|
| `metasurface_canonical_20261008T141214` | 10 mm 空气层；实际 6 次自适应，ΔS=0.00519394，17,623 单元，内部判据满足。 | 大写端口与参考面对照基准。 |
| `metasurface_reference_20261008T141434` | 12 mm 空气层；最多 6 次，实际 6 次，ΔS=0.0468863，内部判据未满足。 | 保留诊断记录；不得用求解 SUCCESS 或功率通过覆盖未收敛结论。 |
| `metasurface_reference8_20261008T141646` | 12 mm 空气层；`history.vba` 允许最多 8 次，日志实际 7 次，ΔS=0.01695，18,351 单元，内部判据满足。 | 与 canonical 比较去嵌参考面的敏感性；不能称为“实际 8 次”。 |

后一个对照的 `.autocst/backend_probe/metasurface_reference8_20261008T141646/reference_plane_comparison.json` 给出：9.5–10.5 GHz 全带最大相位差 0.0205518°，10 GHz 相位差绝对值 0.00822835°，全带最大幅度差 2.12789×10⁻⁷。两次独立网格存在离散误差；该检查支持本例参考面处理一致，不是完整网格、模态、材料或物理验证。对应 `.autocst/backend_probe/metasurface_reference8_20261008T141646/solver_evidence.json` 和 `.autocst/backend_probe/metasurface_reference8_20261008T141646/numerical_check.json` 保留原始判据。

## 无效或不充分探针索引

以下目录保留诊断价值，但不进入有效目标比较：

| 目录（均在 `.autocst/backend_probe/`） | 排除依据 |
|---|---|
| `metasurface_v02_20261008T135923` | `excitation_invalid.json`：小写端口激励被移除，`comparison_eligible=false`；数值检查明确标记 `scientific_invalid_excitation`，即使功率检查通过。 |
| `metasurface_adaptive_20261008T140838` | 同一大小写激励问题，`excitation_invalid.json` 明确排除。 |
| `metasurface_reference_20261008T141434` | `numerical_check.json`：`adaptation_not_converged`；`solver_evidence.json` 的自适应判据为 false。 |

正式研究实验与这些探针分开存储。三次正式运行的只读 MCP 恢复记录为 `.autocst/verification/research-bridge-20261008T064151872656Z.json`：18 项工具配置恢复，3 个既有 run ID 的冻结目标、输入和决策哈希保持一致，断开期间同一 Runner 身份的心跳继续推进。运行状态和预算属于可正常变化的状态，不要求整个上下文逐字相同。此前 `.autocst/verification/research-bridge-20261008T064012608394Z.json` 保留 Windows 心跳文件短暂占用引起的失败，未将其改写为成功。
