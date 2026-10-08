# AutoCST v0.1 验证记录

本文件保留 v0.1 历史验收。当前科研闭环、批次优化与长时验收状态见 [v0.2 验证记录](VERIFICATION_v0.2.md)。

集中元件扩展与后台升级后的状态见 [v0.3 验证记录](VERIFICATION_v0.3.md)。

日期：2026-10-08（Asia/Shanghai）。

本页为本机历史验收摘要；所列原始工程、结果与 `.autocst/` 回执仅保留在本地，不随公开仓库分发。

**MCP → 后台任务 → CST 真实求解 → 结果导出 → 数值核对的完整链路已通过。**

## 实际环境与操作范围

- Windows；CST Studio Suite 2025.2；Python 3.11.9；MCP SDK 1.30.0。
- MATLAB 桥接实测 R2025b。尚未验证 MATLAB 2026。
- 用户手动启动 CST，明确授权连接；使用 PID 36684，仅创建及关闭本次新工程。用户 CST 主程序保留；最终验收还确认先前打开的结果工程未被关闭。
- 本机 CST 兼容设置含 `RUNASADMIN`。默认 `DesignEnvironment.new()` 未能自动启动；已增加明确故障检测。显式 `connect(36684)` 成功，无需变更注册表。

## 最终完整链路

运行 ID：`324b7f62522d4d6181315bcfe02fe424`。

通过真实 MCP stdio 客户端调用 `cst_submit_waveguide(cst_pid=36684)`，随后轮询 `cst_run_status`、读取 `cst_run_results`。后台使用独立 Python 工作进程；状态最终为 `completed`。提交至最终记录约 17 秒。

实际顺序：新建 MWS 工程 → 写入有限模板 History → 保存 → HF Time Domain 求解 → 读取求解器 `SUCCESS` → 保存结果 → 关闭本次工程 → 离线读取 `run_id=0` 的 S11/S21 → CSV 导出 → 独立 TE10 核对。

原始记录：

- `.autocst/verification/mcp-simulation-324b7f62522d4d6181315bcfe02fe424.json`：MCP 验收及源文件哈希。
- `.autocst/runs/324b7f62522d4d6181315bcfe02fe424/status.json`：任务状态与输出哈希。
- 同目录 `waveguide.cst` 及 `waveguide/`：工程与结果目录，应一起保留。
- 同目录 `sparameters.csv`、`numerical_check.json`、`solver_run_info.json`、`cst_messages.json`、`events.jsonl`：结果及原始日志。

## 算例与判据

PEC 壁、真空矩形波导；22.86 × 10.16 × 40 mm，8.2–12.4 GHz，401 频点，端口 1 激励。TE10 截止频率约 6.55714 GHz；分析频带低于下一传播模式的截止频率。

独立核对要求：最大 S11 ≤ −20 dB；S21 在 −0.5 到 +0.15 dB；功率余额误差 ≤ 5%；移除常数模态相位偏置后的 TE10 相位 RMS 残差 ≤ 5°；相位跨度误差 ≤ 10°。

实测：S11 最大约 −106.60 dB；S21 最低约 −0.001713 dB；功率余额最大误差约 0.03943%；相位 RMS 残差约 0.3402°、跨度误差约 1.1091°。五项检查通过。极低 S11 对应理想均匀数值模型，不代表现实器件可达到该指标。

## 软件与连接检查

- 38 项单元测试通过：文档来源与缓存完整性、参数边界、MCP 工具模式、指定实例所有权、状态分类、异常锁释放、单次运行目录及超时监控。
- MCP stdio 与本机 Streamable HTTP 的真实握手、工具枚举和环境查询通过；HTTP 只监听回环地址。
- MATLAB 实际调用 `autocst("doctor")` 并成功取得 CST/Python 环境。
- 原 PDF SHA-256 为 `d60c25f82e2ca03c9940af9ceb97eab6899d702c0a2ad98c7b428732a5f6e14a`，来源文件保持不变。

## 仍有边界

本版本支持这一参数化波导模型，尚未验证任意已有工程、天线、超表面或其他 CST 求解套件。已验证真实软件求解和单网格解析一致性；未完成三维网格收敛研究或硬件实测。端口求解过程自带的网格自适应不等同于整个三维工程的网格收敛研究。

整体超时会终止本工具的 Python 工作进程，保留 `interrupted` 和待核查状态，不能据此声称 CST 已停止。接入配置已写入本项目 `.codex/config.toml`；客户端是否已重新加载该配置，未作为本次测试的已证实项。完整 MCP 服务协议调用与真实求解已独立验证。
