"""Refresh a human-readable research report without changing any run evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import re
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autocst.research_store import ResearchStore
from autocst.lumped import lumped_metadata


def _number(value, digits=5) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    if isinstance(value, (int, float)) and math.isfinite(value):
        return f"{value:.{digits}g}"
    return str(value)


def _cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _analysis(run: dict) -> dict:
    return run.get("details", {}).get("analysis", run.get("analysis", {}))


def _initial_cells(run: dict):
    log = Path(run["run_directory"]) / "solver.log"
    if log.is_file():
        cells = re.findall(r"Number of mesh cells\s*:\s*(\d+)", log.read_text(encoding="utf-8", errors="replace"))
        return int(cells[0]) if cells else None
    return None


def _link(label: str, path: Path) -> str:
    return f"[{label}](<{path.resolve().as_posix()}>)"


def _phase_delta(before, after) -> str:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return _number((after - before + 180) % 360 - 180)
    return "—"


def _mesh_group(samples: list[dict]) -> list[dict]:
    """Only compare independent initial grids at identical physical inputs and spec."""
    groups = {}
    for sample in samples:
        if not isinstance(sample["mesh"], (int, float)) or isinstance(sample["mesh"], bool):
            continue
        key = (sample["version"], json.dumps(sample["physical_parameters"], sort_keys=True),
               json.dumps(sample.get("lumped_elements", []), sort_keys=True))
        groups.setdefault(key, []).append(sample)
    eligible = [group for group in groups.values() if len({item["mesh"] for item in group}) >= 2]
    return max(eligible, key=lambda group: max(item["number"] for item in group), default=[])


def _plot(context: dict, output: Path) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available = {item.name for item in font_manager.fontManager.ttflist}
    font = next((name for name in ("Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "Noto Sans CJK SC") if name in available), None)
    plt.rcParams.update({"font.family": font or "DejaVu Sans", "axes.unicode_minus": False,
                         "font.size": 10, "savefig.dpi": 180, "axes.spines.top": False,
                         "axes.spines.right": False})
    spec = context["experiment"]["spec"]
    bounds = spec.get("parameter_bounds", {})
    parameter = next(iter(bounds)) if len(bounds) == 1 else None
    runs = context["runs"]
    samples = []
    for index, run in enumerate(runs, 1):
        analysis = _analysis(run)
        metrics = analysis.get("metrics", {})
        x = run["job"].get("parameters", {}).get(parameter) if parameter else index
        if isinstance(x, (int, float)) and not isinstance(x, bool) and metrics.get("phase_deg") is not None:
            params = run["job"].get("parameters", {})
            samples.append({"x": x, "phase": metrics["phase_deg"],
                            "magnitude": metrics.get("reflection_magnitude"),
                            "power": metrics.get("total_reflected_power"),
                            "version": run["spec_version"], "number": index,
                            "mesh": _initial_cells(run),
                            "lumped_elements": run["job"].get("lumped_elements", []),
                            "physical_parameters": {key: value for key, value in params.items()
                                                    if key not in {"mesh_steps_per_wavelength", "mesh_cells_per_box"}},
                            "usable": analysis.get("usable_for_optimization", False)})
    mesh_group = _mesh_group(samples)
    fig, axes = plt.subplots(3 if mesh_group else 2, 1,
                             figsize=(10, 10.2 if mesh_group else 7.2), layout="constrained")
    axes[1].sharex(axes[0])
    objective = spec.get("objective", {})
    frequency = objective.get("frequency_ghz") if isinstance(objective, dict) else None
    title = ("科研参数与独立初始网格对比" if font else "Research parameters and independent initial grids")
    fig.suptitle(title + (f"  |  f = {frequency:g} GHz" if isinstance(frequency, (int, float)) else ""), fontsize=14)
    colors = plt.get_cmap("tab10")
    versions = sorted({(sample["version"], sample["mesh"]) for sample in samples}, key=str)
    for color_index, (version, mesh) in enumerate(versions):
        selected = [sample for sample in samples if sample["version"] == version and sample["mesh"] == mesh]
        color = colors(color_index % 10)
        for usable in (True, False):
            subset = [sample for sample in selected if sample["usable"] == usable]
            if not subset:
                continue
            label = f"v{version}, initial cells={mesh}" + (" / invalid" if not usable else "")
            marker = "o" if usable else "x"
            axes[0].scatter([sample["x"] for sample in subset], [sample["phase"] for sample in subset],
                            s=48, marker=marker, color=color, label=label, zorder=3)
            amplitudes = [sample for sample in subset if sample["magnitude"] is not None]
            if amplitudes:
                axes[1].scatter([sample["x"] for sample in amplitudes], [sample["magnitude"] for sample in amplitudes],
                                s=48, marker=marker, color=color, label=f"|S11| {label}", zorder=3)
        powers = [sample for sample in selected if sample["power"] is not None]
        if powers:
            axes[1].scatter([sample["x"] for sample in powers], [sample["power"] for sample in powers],
                            s=42, marker="s", facecolors="none", edgecolors=color, label=f"Total power, v{version}, cells={mesh}")
    if len(samples) <= 24:
        label_counts = {}
        for sample in samples:
            offset_index = label_counts.get(sample["x"], 0)
            label_counts[sample["x"]] = offset_index + 1
            axes[0].annotate(str(sample["number"]), (sample["x"], sample["phase"]),
                             xytext=(5, 6 + 11 * offset_index), textcoords="offset points", fontsize=8)
    if isinstance(objective, dict) and isinstance(objective.get("target_phase_deg"), (int, float)):
        target = (objective["target_phase_deg"] + 180) % 360 - 180
        axes[0].axhline(target, color="#b5403b", linestyle="--", linewidth=1.2,
                        label=f"Current target: {target:g} deg")
        tolerance = objective.get("tolerance_deg")
        if isinstance(tolerance, (int, float)) and 0 < tolerance < 180:
            # Split a target interval crossing the +/-180 degree branch cut.
            for shift in (-360, 0, 360):
                lo, hi = max(-180, target - tolerance + shift), min(180, target + tolerance + shift)
                if lo < hi:
                    axes[0].axhspan(lo, hi, color="#b5403b", alpha=0.10)
    constraints = spec.get("constraints", {})
    threshold = constraints.get("min_reflection_magnitude") if isinstance(constraints, dict) else None
    if threshold is None and isinstance(constraints, dict) and isinstance(constraints.get("min_s11_db"), (int, float)):
        threshold = 10 ** (constraints["min_s11_db"] / 20)
    if isinstance(threshold, (int, float)):
        axes[1].axhline(threshold, color="#b5403b", linestyle="--", linewidth=1.2,
                        label=f"Current |S11| minimum: {threshold:g}")
    axes[0].set_ylim(-190, 190)
    axes[0].set_yticks([-180, -90, 0, 90, 180])
    if samples:
        displayed = [sample["phase"] for sample in samples]
        if isinstance(objective, dict) and isinstance(objective.get("target_phase_deg"), (int, float)):
            displayed.append((objective["target_phase_deg"] + 180) % 360 - 180)
        if max(displayed) - min(displayed) < 180:
            margin = max(5, 0.15 * (max(displayed) - min(displayed)))
            axes[0].set_ylim(max(-190, min(displayed) - margin), min(190, max(displayed) + margin))
            from matplotlib.ticker import MaxNLocator
            axes[0].yaxis.set_major_locator(MaxNLocator(6))
    axes[0].set_ylabel("Phase (deg, wrapped)")
    axes[1].set_ylabel("Magnitude / normalized power")
    axes[1].set_xlabel(parameter if parameter else "Run number (multiple or no tunable parameters)")
    axes[1].set_ylim(bottom=0)
    if mesh_group:
        selected = sorted(mesh_group, key=lambda sample: sample["mesh"])
        axes[2].scatter([sample["mesh"] for sample in selected], [sample["phase"] for sample in selected],
                        s=48, color="#227c9d", zorder=3)
        annotations = {}
        for sample in selected:
            annotations.setdefault((sample["mesh"], round(sample["phase"], 6)), []).append(str(sample["number"]))
        for (mesh, phase), numbers in annotations.items():
            axes[2].annotate(",".join(numbers), (mesh, phase),
                             xytext=(5, 6), textcoords="offset points", fontsize=8)
        if isinstance(objective, dict) and isinstance(objective.get("target_phase_deg"), (int, float)):
            axes[2].axhline(target, color="#b5403b", linestyle="--", linewidth=1.2)
        axes[2].set_xlabel("Actual initial tetrahedral mesh cells (each run uses internal adaptation)")
        axes[2].set_ylabel("Phase (deg, wrapped)")
        axes[2].set_title("Same physical model: " + ", ".join(f"{key}={value:g}" for key, value in
                         selected[0]["physical_parameters"].items() if key == parameter), fontsize=10)
        axes[2].set_xticks(sorted({sample["mesh"] for sample in selected}))
    if not samples:
        axes[0].text(0.5, 0.5, "暂无可绘制的相位结果" if font else "No phase measurements available yet",
                     ha="center", va="center", transform=axes[0].transAxes, color="#666666")
    for axis in axes:
        axis.grid(alpha=0.2)
        if axis.get_legend_handles_labels()[0]:
            axis.legend(loc="best", fontsize=8)
    fig.savefig(output)
    plt.close(fig)
    return {"plotted_samples": len(samples), "parameter": parameter, "font": font or "DejaVu Sans",
            "independent_grid_samples": len(mesh_group)}


def render_report(context: dict, output_dir: Path) -> dict:
    """Write only replaceable report views; never repair, rewrite or delete run inputs."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    image = output_dir / "report.png"
    chart = _plot(context, image)
    experiment = context["experiment"]
    spec, runs, budget = experiment["spec"], context["runs"], context["budget"]
    objective = spec.get("objective", {})
    updated = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    lines = ["# AutoCST 科研实验对比", "",
             f"实验：`{experiment['experiment_id']}` · 当前版本：v{experiment['version']} · 状态：`{experiment['state']}`", "",
             f"报告更新：{updated}（Asia/Shanghai）。这是当前数据的可更新视图；各运行目录中的原始证据保持不变。", "",
             "## 参数与结果修改日志", "",
             "每次调参创建新的运行与输入，结果随新运行追加；不会用新结果替换旧结果。下表优先比较决策中指定的父运行，首轮记录基线。", "",
             "| 轮次 | 调整依据 | 参数变化 | 相位变化 / ° | 目标误差 / ° | 判定 |",
             "| --- | --- | --- | ---: | ---: | --- |"]
    by_id = {run["run_id"]: run for run in runs}
    for index, run in enumerate(runs, 1):
        decision, analysis = run.get("decision", {}), _analysis(run)
        parent = by_id.get(decision.get("parent_run_id"))
        params = run["job"].get("parameters", {})
        old_params = parent["job"].get("parameters", {}) if parent else {}
        changes = "; ".join(f"{key}: {_number(old_params.get(key))} → {_number(value)}"
                            for key, value in params.items() if key not in old_params or old_params[key] != value)
        if parent and parent["job"].get("lumped_elements", []) != run["job"].get("lumped_elements", []):
            changes += ("; " if changes else "") + "集中元件定义变化（详见连接记录）"
        if not parent:
            changes = "基线：" + "; ".join(f"{key}={_number(params.get(key))}" for key in
                                          list(spec.get("parameter_bounds", {})) + ["mesh_steps_per_wavelength"]
                                          if key in params)
        old_phase = _analysis(parent).get("metrics", {}).get("phase_deg") if parent else None
        phase = analysis.get("metrics", {}).get("phase_deg")
        phase_change = (f"{_number(old_phase)} → {_number(phase)} (Δ {_phase_delta(old_phase, phase)})"
                        if parent and phase is not None else _number(phase))
        reason = decision.get("hypothesis", decision.get("reason", decision.get("stage", "未记录")))
        row = [str(index), reason, changes or "参数不变的复验", phase_change,
               _number(analysis.get("metrics", {}).get("phase_error_deg")),
               analysis.get("target_status", run["state"])]
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    if not runs:
        lines.append("| — | 尚未提交运行 | — | — | — | 等待实验 |")
    loaded = [run for run in runs if run["job"].get("lumped_elements")]
    if loaded:
        lines += ["", "## 集中元件与连接记录", "",
                  "各轮解析元件参数后重建，单位明确；吸收数值是 S 参数未返回功率的估计，不是独立耗散功率验收。", "",
                  "| 运行 | 元件 | 类型 | R / Ω | L / nH | C / pF | 起点 / mm | 终点 / mm | 估计未返回功率 |",
                  "| --- | --- | --- | ---: | ---: | ---: | --- | --- | ---: |"]
        for run in loaded:
            loading = lumped_metadata(run["job"]["lumped_elements"], run["job"]["parameters"])
            for element in loading["elements"]:
                row = [run["run_id"][:12], element["name"], element["type"],
                       _number(element["resistance_ohm"]), _number(element["inductance_nh"]),
                       _number(element["capacitance_pf"]), str(element["point1_mm"]), str(element["point2_mm"]),
                       _number(_analysis(run).get("metrics", {}).get("estimated_absorbed_power"))]
                lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines += ["",
             "## 目标与实验合同", "",
             "```json", json.dumps({"objective": objective, "constraints": spec.get("constraints", {}),
                                     "parameter_bounds": spec.get("parameter_bounds", {}), "model": spec.get("model", {})},
                                    ensure_ascii=False, indent=2), "```", "",
             "所有相位、误差与约束判定都属于该次运行冻结的实验版本。目标或模型更改后，不把旧目标下的命中结论当作新目标已达到。", "",
             "## 参数与响应", "", f"![相位与反射幅度对比](<{image.as_posix()}>)", "",
             "图中编号对应下表；只画实际已导出的数据点，不插入或连线推断未测参数。空心方块为总反射功率，圆点为同极化反射幅度，叉号为不可用于优化的数据。", "",
             "| 序号 | 冻结输入 | 版本 | 状态 | 调整参数 | 网格请求 / 实际初始单元 | 相位 / ° | 相位误差 / ° | 反射幅度 | 总反射功率 | 数值状态 |",
             "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    if not runs:
        lines.append("| — | 尚未提交运行 | — | 等待实验 | — | — | — | — | — | — | 未评估 |")
    for index, run in enumerate(runs, 1):
        analysis = _analysis(run)
        metrics = analysis.get("metrics", {})
        params = run["job"].get("parameters", {})
        bounds = run.get("spec", {}).get("parameter_bounds", spec.get("parameter_bounds", {}))
        display_parameters = {name: params.get(name) for name in bounds} if bounds else params
        parameter_text = "; ".join(f"{name}={_number(value)}" for name, value in display_parameters.items()) or "—"
        evidence = Path(run["run_directory"])
        run_link = _link(run['run_id'][:12], evidence / "job.json")
        row = [str(index), run_link, f"v{run['spec_version']}", run["state"], parameter_text,
               f"box={_number(params.get('mesh_cells_per_box'))}; legacy wave={_number(params.get('mesh_steps_per_wavelength'))}; cells={_initial_cells(run)}",
               _number(metrics.get("phase_deg")), _number(metrics.get("phase_error_deg")),
               _number(metrics.get("reflection_magnitude")), _number(metrics.get("total_reflected_power")),
               analysis.get("scientific_status", {}).get("numerical_validity", "未评估")]
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines += ["", "## 独立初始网格复验", "",
              "初始网格步数改变后，每次均重新建模和求解，再运行内部自适应。只有物理参数与冻结实验版本一致的运行才可用于这项比较；初始网格步数不等于最终网格单元数。", ""]
    validation_file = output_dir / "validation.json"
    if validation_file.is_file():
        try:
            validation = json.loads(validation_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            lines += [f"复验汇总无法读取：`{type(exc).__name__}`。不据此确认网格结论。", ""]
        else:
            lines += [_link("独立复验汇总原始记录", validation_file), "", "```json",
                      json.dumps(validation, ensure_ascii=False, indent=2), "```", ""]
    else:
        lines += ["尚无独立网格复验汇总结论。图与表展示已完成的实际运行，目标命中保持候选状态。", ""]
    lines += ["## 每轮依据与判定", ""]
    if not runs:
        lines.append("尚无运行。")
    for index, run in enumerate(runs, 1):
        analysis = _analysis(run)
        decision = {key: value for key, value in run.get("decision", {}).items()
                    if key not in {"implementation_sha256", "prepared_id"}}
        directory = Path(run["run_directory"])
        evidence_links = [_link(label, directory / filename) for label, filename in
                          (("参数", "job.json"), ("实验版本", "spec.json"), ("决策", "decision.json"),
                           ("原始结果", "reflection.csv"), ("分析", "analysis.json"),
                           ("求解器证据", "solver_evidence.json"), ("CST 日志", "solver.log"),
                           ("建模历史", "history.vba"), ("工程", "project.cst"),
                           ("完成信号", "completion_signal.json"), ("输入哈希", "manifest.json"))
                          if (directory / filename).is_file()]
        lines += [f"### {index}. `{run['run_id']}`", "",
                  f"冻结版本 v{run['spec_version']}；阶段 `{run['phase']}`；求解用时 {_number(run.get('details', {}).get('solver_elapsed_seconds'))} 秒。", "",
                  "证据：" + " · ".join(evidence_links), "",
                  "决策记录：", "", "```json", json.dumps(decision, ensure_ascii=False, indent=2), "```", "",
                  "判定记录：", "", "```json",
                  json.dumps({"objective": run.get("spec", {}).get("objective"),
                              "target_status": analysis.get("target_status", "not_evaluated"),
                              "constraints": analysis.get("constraints", {}),
                              "physical_checks": analysis.get("physical_checks", {}),
                              "scientific_status": analysis.get("scientific_status", {}),
                              "error": run.get("details", {}).get("error")}, ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 预算与下一步", "",
              f"已提交 {budget['submitted_runs']} / {spec['budgets']['max_runs']} 次；剩余可提交 {budget['remaining_runs']} 次。", "",
              f"求解已计 {_number(budget['solver_seconds_used'])} 秒；在途预留 {_number(budget['solver_seconds_reserved'])} 秒；"
              f"剩余可预留 {_number(budget['remaining_solver_seconds'])} 秒；单次上限 {spec['budgets']['max_run_solver_seconds']} 秒。", "",
              "```json", json.dumps({key: value for key, value in context.get("next_proposal", {}).items() if key != "comparison"},
                                      ensure_ascii=False, indent=2), "```", "",
              "## 证据边界", "",
              "- CST 求解成功、数据完整性、约束命中、独立数值核对、网格收敛与物理验证分别记录；任何一个通过都不替代其他层级。",
              "- 单轮分析中的 `pending` 表示该轮本身没有证明独立网格一致性；跨运行复验另外保存，不改写单轮分析。有限的初始网格对比即使通过，也只支持记录条件下的数值稳定性。",
              "- 总反射功率仅在输出包含对应传播模态总功率时展示；缺失项显示空值，不按同极化幅度自行冒充总功率。",
              "- 相位采用该模型的端口、极化与参考面定义；不同参考面或激励定义不能直接合并比较。",
              "- 仿真结果不代表硬件实测。后台存活、应用关闭与十几小时真实 CST 求解也须各自有独立验收记录。", ""]
    report = output_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return {"experiment_id": experiment["experiment_id"], "report": str(report), "figure": str(image),
            "run_count": len(runs), **chart}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    store = ResearchStore(root / ".autocst")
    context = store.context(args.experiment_id)
    # Use the database's ID, not arbitrary command-line path material.
    directory = root / ".autocst" / "experiments" / context["experiment"]["experiment_id"]
    print(json.dumps(render_report(context, directory), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
