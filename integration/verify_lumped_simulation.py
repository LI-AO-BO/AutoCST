"""Explicit short RLC acceptance via MCP, persistent runner and completion events.

Creates a new experiment and two real loaded-waveguide runs. Does not start CST,
restart the runner, cancel jobs, acknowledge events or retry a solver.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocst.signals import wait_for_run
from integration.verify_research_simulation import Receipts, call, connection


async def verify(args):
    root = args.root.resolve()
    spec = json.loads((root / "examples/lumped-waveguide-experiment.json").read_text(encoding="utf-8"))
    template = json.loads((root / "examples/lumped-waveguide-job.json").read_text(encoding="utf-8"))
    template["cst_pid"] = args.cst_pid
    bounds = spec["parameter_bounds"]["C_load_pf"]
    if any(not math.isfinite(value) or not bounds[0] <= value <= bounds[1] for value in args.capacitances):
        raise ValueError(f"Acceptance capacitances must lie in the example bounds {bounds}")
    if args.capacitances[0] == args.capacitances[1]:
        raise ValueError("Acceptance needs two distinct capacitances")
    receipts = Receipts(root)
    rows, previous = [], None
    async with connection(root, receipts, "lumped") as session:
        runner = await call(session, "cst_runner_status", {})
        if not runner.get("alive") or not runner.get("responsive"):
            raise RuntimeError("A responsive existing runner is required")
        receipts.save("00-environment.json", await call(session, "cst_environment", {}))
        experiment = await call(session, "cst_create_experiment", {"spec": spec})
        experiment_id = experiment["experiment_id"]
        receipts.save("01-experiment.json", experiment)
        for index, capacitance in enumerate(args.capacitances):
            job = copy.deepcopy(template)
            job["parameters"]["C_load_pf"] = capacitance
            decision = {"reason": "Verify RLC SI readback, exported monitors and capacitance response in separate runs",
                        "parent_run_id": previous, "hypothesis": "Changing the shunt capacitance changes the complex S response",
                        "parameter_change": {"C_load_pf": capacitance}, "evidence": rows[-1:]}
            prepared = await call(session, "cst_prepare_simulation", {
                "experiment_id": experiment_id, "job": job, "decision": decision})
            receipts.save(f"{index}-prepared.json", prepared)
            submitted = await call(session, "cst_submit_prepared", {
                "prepared_id": prepared["prepared_id"], "idempotency_key": f"lumped-cap-{index}"})
            run_id = submitted["run_id"]
            print(json.dumps({"experiment_id": experiment_id, "run_id": run_id,
                              "C_load_pf": capacitance, "waiting": "Windows completion event"}), flush=True)
            receipts.save(f"{index}-submitted.json", submitted)
            state = await asyncio.to_thread(wait_for_run, root, run_id, 420)
            receipts.save(f"{index}-completion.json", state)
            result = await call(session, "cst_research_run_results", {"run_id": run_id})
            receipts.save(f"{index}-result.json", result)
            evidence, analysis = result.get("result") or {}, result.get("analysis") or {}
            if (state["state"] != "completed" or evidence.get("solver_success") is not True or
                    evidence.get("numerical_validity", {}).get("passed") is not True or
                    evidence.get("lumped_monitor_exports", {}).get("voltage_current_exported") is not True or
                    analysis.get("usable_for_optimization") is not True):
                raise RuntimeError(f"RLC acceptance failed for {run_id}; inspect {receipts.directory}")
            loading = evidence["lumped_elements"]["elements"][0]
            if not math.isclose(loading["native_si"]["capacitance_f"], capacitance * 1e-12, rel_tol=1e-12):
                raise RuntimeError("Recorded SI capacitance differs from this run's input")
            rows.append({"run_id": run_id, "C_load_pf": capacitance,
                         "native_si": loading["native_si"], "metrics": analysis["metrics"],
                         "sampling": analysis["interpolation"],
                         "monitor_curves": len(evidence["lumped_monitor_exports"]["curves"])})
            previous = run_id
        context = await call(session, "cst_experiment_context", {"experiment_id": experiment_id})
        receipts.save("02-context.json", context)
    left, right = (complex(row["metrics"]["s11_real"], row["metrics"]["s11_imag"]) for row in rows)
    difference = abs(left - right)
    summary = {"passed": difference > 1e-5, "experiment_id": experiment_id, "runs": rows,
               "complex_s11_difference_at_10ghz": difference, "waiting": "Windows completion events; no CST status polling",
               "scope": "Short RLC creation, SI/coordinate assertions, solver/export and parameter-response acceptance; not mesh convergence or physical validation"}
    receipts.save("03-summary.json", summary)
    print(json.dumps({"receipt_directory": str(receipts.directory), **summary}, ensure_ascii=False), flush=True)
    if not summary["passed"]:
        raise RuntimeError("Two capacitances produced no resolved response change")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--cst-pid", type=int, required=True)
    parser.add_argument("--capacitances", type=float, nargs=2, default=[0.2, 0.5])
    asyncio.run(verify(parser.parse_args()))
