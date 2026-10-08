"""JSON CLI shared by the local assistant and MATLAB."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .service import Service


def _research_service(root: Path):
    from .research_service import ResearchService

    return ResearchService(root)


def _json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _add_research_commands(commands) -> None:
    research = commands.add_parser("research", help="Manage v0.2 research experiments and runner events")
    actions = research.add_subparsers(dest="research_command", required=True)
    actions.add_parser("environment")
    create = actions.add_parser("create-experiment", help="Record an experiment specification without running CST")
    create.add_argument("spec", type=Path)
    revise = actions.add_parser("revise-experiment", help="Record an authorized new specification; preserve prior run versions")
    revise.add_argument("experiment_id")
    revise.add_argument("spec", type=Path)
    prepare = actions.add_parser("prepare", help="Validate and freeze a reviewable job without running it")
    prepare.add_argument("experiment_id")
    prepare.add_argument("job", type=Path)
    prepare.add_argument("--decision", type=Path, help="JSON evidence and reasoning for this step")
    submit = actions.add_parser("submit-prepared", help="Queue frozen inputs with a retry-safe submission key")
    submit.add_argument("prepared_id")
    submit.add_argument("--idempotency-key", required=True)
    context = actions.add_parser("context")
    context.add_argument("experiment_id")
    for name in ("status", "results"):
        sub = actions.add_parser(name)
        sub.add_argument("run_id")
    events = actions.add_parser("events", help="Read events without acknowledging them")
    events.add_argument("--experiment-id")
    events.add_argument("--after", type=int, default=0)
    ack = actions.add_parser("ack", help="Acknowledge an event after reviewing its evidence and next action")
    ack.add_argument("event_id", type=int)
    control = actions.add_parser("control", help="Pause after current work, resume, or explicitly cancel")
    control.add_argument("experiment_id")
    control.add_argument("action", choices=("pause", "resume", "cancel"))
    optimize = actions.add_parser("start-optimization", help="Start a bounded optimization batch using the current experiment limits")
    optimize.add_argument("experiment_id")
    optimize.add_argument("job", type=Path)
    optimize.add_argument("--max-new-runs", type=int, default=4)
    optimize.add_argument("--seed", type=int, default=0)
    optimize.add_argument("--idempotency-key", required=True)
    optimization_status = actions.add_parser("optimization-status", help="Read a batch without advancing it")
    optimization_status.add_argument("batch_id")
    optimization_control = actions.add_parser("control-optimization", help="Pause, resume or stop subsequent optimization rounds")
    optimization_control.add_argument("batch_id")
    optimization_control.add_argument("action", choices=("pause", "resume", "stop"))
    actions.add_parser("runner-status")
    actions.add_parser("start-runner", help="Start an already installed local runner task")


def _run_research(args) -> dict:
    api = _research_service(args.root)
    command = args.research_command
    if command == "environment":
        return api.environment()
    if command == "create-experiment":
        return api.create_experiment(_json_object(args.spec))
    if command == "revise-experiment":
        return api.revise_experiment(args.experiment_id, _json_object(args.spec))
    if command == "prepare":
        decision = _json_object(args.decision) if args.decision is not None else {}
        return api.prepare_job(args.experiment_id, _json_object(args.job), decision)
    if command == "submit-prepared":
        if not args.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        return api.submit_job(args.prepared_id, args.idempotency_key)
    if command == "context":
        return api.experiment_context(args.experiment_id)
    if command == "status":
        return api.run_status(args.run_id)
    if command == "results":
        return api.run_results(args.run_id)
    if command == "events":
        if args.after < 0:
            raise ValueError("after must be a non-negative event cursor")
        return api.events(args.experiment_id, args.after)
    if command == "ack":
        if args.event_id < 1:
            raise ValueError("event_id must be a positive integer")
        return api.acknowledge(args.event_id)
    if command == "control":
        return api.control(args.experiment_id, args.action)
    if command == "start-optimization":
        if not args.idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        if args.max_new_runs < 1:
            raise ValueError("max_new_runs must be positive")
        return api.start_optimization(args.experiment_id, _json_object(args.job),
                                      max_new_runs=args.max_new_runs, config={"seed": args.seed},
                                      idempotency_key=args.idempotency_key)
    if command == "optimization-status":
        return api.optimization_status(args.batch_id)
    if command == "control-optimization":
        return api.control_optimization(args.batch_id, args.action)
    if command == "runner-status":
        return api.runner_status()
    if command == "start-runner":
        return api.start_runner()
    raise ValueError(f"Unsupported research command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AutoCST 2025 local automation")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    pages = commands.add_parser("pages")
    pages.add_argument("start", type=int)
    pages.add_argument("end", type=int)
    submit = commands.add_parser("submit")
    submit.add_argument("job", type=Path)
    for command in ("status", "results"):
        sub = commands.add_parser(command)
        sub.add_argument("run_id")
    _add_research_commands(commands)
    args = parser.parse_args(argv)
    try:
        if args.command == "research":
            response = _run_research(args)
        else:
            response = _run_legacy(args)
        print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1


def _run_legacy(args) -> dict:
    api = Service(args.root)
    if args.command == "doctor":
        return api.doctor()
    if args.command == "search":
        return api.search(args.query, args.limit)
    if args.command == "pages":
        return api.pages(args.start, args.end)
    if args.command == "submit":
        return api.submit(_json_object(args.job))
    if args.command == "status":
        return api.status(args.run_id)
    return api.results(args.run_id)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
