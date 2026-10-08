# 线性集中元件

 v0.3 支持通过已有研究接口添加理想、无源、线性的 R/L/C 串并联组合。适用于 `waveguide`、`metasurface`、显式 `history` 和已有工程的副本；新增元件写入本次任务工程，原始来源工程保持不变。

## 输入和调参

在实验的 `spec.model.lumped_elements` 中声明连接与元件类型，在每轮 `job.lumped_elements` 中提供相同定义。使用 [实验示例](../examples/lumped-waveguide-experiment.json) 与 [任务示例](../examples/lumped-waveguide-job.json)，将示例 PID 替换为当次手动启动的 CST 实例号。沿用 `cst_create_experiment` → `cst_prepare_simulation` → `cst_submit_prepared`，无需新增工具。

| 字段 | 含义 |
|---|---|
| `name` | 唯一 ASCII 标识符，最多 64 字符；不会替换已有同名元件 |
| `type` | `rlcserial` 串联或 `rlcparallel` 并联，默认串联 |
| `resistance_ohm` | 电阻，Ω，默认 0 |
| `inductance_nh` | 电感，nH，默认 0；写入 CST 时转换为 H |
| `capacitance_pf` | 电容，pF，默认 0；写入 CST 时转换为 F |
| `point1_mm`、`point2_mm` | 两个全局连接端点，每项三个坐标；工程必须使用 mm |
| `monitor` | 请求记录元件电压、电流，默认 true |

数值字段可填数字或受限算术表达式，例如 `C_load_pf`、`a_mm/2`、`substrate_height_mm+metal_thickness_mm`。只支持参数名、数字、括号与 `+ - * /`，不执行函数、属性访问或脚本。参数必须存在且有限，R/L/C 不能为负，至少一项必须大于零；零值表示不包含对应组件，不开放理想全零 open/short 模型。

每轮准备阶段解析表达式、换算单位并冻结实际数值。修改 `job.parameters.C_load_pf` 会在新运行中重建该电容；不会就地覆盖上一轮，也不声称修改 CST 界面的参数后既有元件值会自动跟随。贝叶斯批次可以优化声明在 `parameter_bounds` 中的元件参数，仍受原实验次数和时间预算约束。

元件拓扑、连接方式和常数值变化需要修订实验；用参数引用声明的量可在已约定范围内调整。模板检查端点仍在原结构范围内，避免改变自动包围盒与端口参考面；周期单元端点不能伸入顶部空气间距。连接是否符合实际研究结构需要依据模型检查。已有工程与自定义 History 使用自己的结果树和判断标准，添加前核对工程几何单位。

## 回执、结果和判据

生成的历史在添加前检查单位和同名冲突，添加后调用官方 `GetProperties`、`GetCoordinates` 核对电路类型、SI 数值和位置。各轮的 `job.json`、冻结历史、`lumped_elements.json`、`model.json` 与参数/结果报告保留定义、解析值、连接点和执行依据。不同元件定义不能混为同一物理模型的网格复验。

电压、电流、阻抗与已有元件耗散功率曲线从实际 CST 结果树发现并导出，`lumped_monitor_exports.json` 保存精确路径、标签、CSV 和哈希。`monitor=true` 请求的电压或电流曲线缺失时明确报告导出失败；不需要监视时可显式设为 false。monitor 设置没有已确认的 getter，以实际导出曲线为依据。监视谱幅度按 CST 参考信号归一化，元件方向会影响电压和电流符号；应结合导出曲线标签解释。耗散功率曲线保留原始标签和归一化，尚未自动用它进行完整能量闭合验收。

带电阻的波导和接地周期单元检查被动性，允许物理耗散。`estimated_absorbed_power` 是所选 S 参数未返回功率的估计，保留轻微负值和误差，不裁剪成理想结果；它不是独立测得的电阻耗散，也不证明完整能量守恒。可声明 `min_estimated_absorbed_power` / `max_estimated_absorbed_power` 约束，原无损 `max_power_balance_error` 不作为耗散模型的能量闭合判据。

加载波导不再适用均匀裸波导的匹配和解析相位验收。无电阻的纯 LC 加载保留功率平衡筛查；内部自适应、独立网格敏感性和实测仍分别判断。任意结构不能仅凭元件成功创建认定科研目标成立。

## 本机复验

手动启动 CST 并确认常驻 Runner 正常后，可运行下面的验证程序，将 `<PID>` 替换为选定实例的进程号：

```powershell
.\.venv\Scripts\python.exe integration\verify_lumped_simulation.py --cst-pid <PID>
```

程序新建实验，依次求解 C=0.2 pF、0.5 pF 的两个并联 RC 波导，自动核对元件回读、监视导出与响应变化。每轮默认超时 300 秒，回执保存在本机 `.autocst/verification/`。这是执行链路验证；[v0.3 验收说明](../VERIFICATION_v0.3.md) 列出了实际结果与科研结论的边界。

## 官方依据与范围

CST 2025 本机 `Online Help/mergedProjects/VBA_3D/special_vbadiscreteelements/special_vbalumpedelement_object.htm` 定义创建及读回接口；`common_vbaunitso/common_vbaunitso_units_object.htm` 定义 `Units.GetUnit("Length")`。用户参考 PDF 的物理页 985、987 说明高频求解器的集中元件支持；1172 的单位图与本机 GUI 帮助一致，R 为 Ω、L 为 H、C 为 F。低频章节的结果路径不能直接推广到高频结果。

本版实现线性 RLC。变容二极管的大信号非线性、SPICE/Touchstone 导入、面分布集中元件、寄生封装和实际器件模型尚未接入；需要按研究目标扩展和独立验收。官方资料与仿真原始记录保留在本地，不随开源仓库分发。
