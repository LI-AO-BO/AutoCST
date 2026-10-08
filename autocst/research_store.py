"""Durable research queue and append-only evidence, independent of chat lifetime."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid


STATES = {"queued", "preparing", "prepared", "starting", "solving", "exporting",
          "analyzing", "completed", "failed", "needs_attention", "cancelled"}
ACTIVE_STATES = STATES - {"queued", "completed", "failed", "cancelled"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
DEFAULT_BUDGETS = {"max_runs": 8, "max_total_solver_seconds": 14400,
                   "max_run_solver_seconds": 3600}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _merge(old: dict, new: dict) -> dict:
    result = dict(old)
    for key, value in new.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def normalize_spec(spec: dict) -> dict:
    """Validate a finite, serializable experiment contract; preserve research metadata."""
    if not isinstance(spec, dict) or not spec.get("objective"):
        raise ValueError("spec.objective is required")
    result = json.loads(_json(spec))
    result.setdefault("constraints", {})
    result.setdefault("parameter_bounds", {})
    model = result.get("model", {})
    if not isinstance(model, dict) or not isinstance(model.get("fixed_parameters", {}), dict):
        raise ValueError("model and model.fixed_parameters must be objects")
    for name, value in model.get("fixed_parameters", {}).items():
        if not isinstance(name, str) or not name:
            raise ValueError("Fixed parameter names must be nonempty strings")
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"Fixed parameter {name} must be a finite number")
    if not isinstance(result["parameter_bounds"], dict):
        raise ValueError("parameter_bounds must be an object")
    for name, bound in result["parameter_bounds"].items():
        if not isinstance(name, str) or not name:
            raise ValueError("Parameter names must be nonempty strings")
        limits = (bound.get("min"), bound.get("max")) if isinstance(bound, dict) else bound
        if not isinstance(limits, (list, tuple)) or len(limits) != 2:
            raise ValueError(f"Invalid bounds for {name}")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in limits):
            raise ValueError(f"Non-finite bounds for {name}")
        if limits[0] >= limits[1]:
            raise ValueError(f"Bounds must increase for {name}")
    budgets = dict(DEFAULT_BUDGETS)
    provided = result.get("budgets", {})
    if not isinstance(provided, dict):
        raise ValueError("budgets must be an object")
    budgets.update(provided)
    for key in DEFAULT_BUDGETS:
        value = budgets[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"budgets.{key} must be a positive integer")
    if budgets["max_run_solver_seconds"] > budgets["max_total_solver_seconds"]:
        raise ValueError("Single-run budget exceeds total solver budget")
    result["budgets"] = budgets
    return result


class ResearchStore:
    """SQLite is authoritative; immutable input copies make each run independently inspectable."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir = self.state_dir / "research_runs"
        self.runs_dir.mkdir(exist_ok=True)
        self.database = self.state_dir / "research.sqlite3"
        with self._connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                    state TEXT NOT NULL, created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    experiment_id TEXT NOT NULL, version INTEGER NOT NULL,
                    spec_json TEXT NOT NULL, created_utc TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, version));
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
                    spec_version INTEGER NOT NULL, job_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL, request_sha256 TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,
                    details_json TEXT NOT NULL, run_directory TEXT NOT NULL,
                    created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL,
                    UNIQUE(experiment_id, idempotency_key));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_research_run ON runs((1))
                    WHERE state IN ('preparing','prepared','starting','solving','exporting','analyzing','needs_attention');
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
                    run_id TEXT, event TEXT NOT NULL, data_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL, acknowledged_utc TEXT);
                CREATE TABLE IF NOT EXISTS controls (
                    control_id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
                    action TEXT NOT NULL, created_utc TEXT NOT NULL, acknowledged_utc TEXT);
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, experiment_id: str, event: str, data: dict, run_id=None):
        db.execute("INSERT INTO events(experiment_id,run_id,event,data_json,created_utc) VALUES(?,?,?,?,?)",
                   (experiment_id, run_id, event, _json(data), _now()))

    @staticmethod
    def _experiment(db, experiment_id: str) -> dict:
        row = db.execute("SELECT e.*,v.spec_json FROM experiments e JOIN versions v "
                         "ON e.experiment_id=v.experiment_id AND e.version=v.version WHERE e.experiment_id=?",
                         (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown experiment: {experiment_id}")
        value = dict(row)
        value["spec"] = json.loads(value.pop("spec_json"))
        return value

    @staticmethod
    def _run(db, run_id: str) -> dict:
        row = db.execute("SELECT r.*,v.spec_json FROM runs r JOIN versions v "
                         "ON r.experiment_id=v.experiment_id AND r.spec_version=v.version WHERE r.run_id=?",
                         (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        value = dict(row)
        for name in ("job", "decision", "details", "spec"):
            value[name] = json.loads(value.pop(name + "_json"))
        return value

    def create_experiment(self, spec: dict) -> dict:
        spec = normalize_spec(spec)
        experiment_id, now = uuid.uuid4().hex, _now()
        with self._transaction() as db:
            db.execute("INSERT INTO experiments VALUES(?,?,?,?,?)", (experiment_id, 1, "active", now, now))
            db.execute("INSERT INTO versions VALUES(?,?,?,?)", (experiment_id, 1, _json(spec), now))
            self._event(db, experiment_id, "experiment_created", {"version": 1, "spec": spec})
            return self._experiment(db, experiment_id)

    def revise_experiment(self, experiment_id: str, spec: dict) -> dict:
        spec = normalize_spec(spec)
        with self._transaction() as db:
            prior = self._experiment(db, experiment_id)
            version, now = prior["version"] + 1, _now()
            db.execute("INSERT INTO versions VALUES(?,?,?,?)", (experiment_id, version, _json(spec), now))
            db.execute("UPDATE experiments SET version=?,updated_utc=? WHERE experiment_id=?",
                       (version, now, experiment_id))
            self._event(db, experiment_id, "experiment_revised", {"version": version, "previous_version": prior["version"], "spec": spec})
            return self._experiment(db, experiment_id)

    def experiment(self, experiment_id: str) -> dict:
        with self._connection() as db:
            return self._experiment(db, experiment_id)

    def list_experiments(self) -> list[dict]:
        with self._connection() as db:
            return [self._experiment(db, row[0]) for row in db.execute("SELECT experiment_id FROM experiments ORDER BY created_utc")]

    @staticmethod
    def _budget(db, experiment_id: str, spec: dict) -> dict:
        used, reserved, count = 0.0, 0.0, 0
        for row in db.execute("SELECT state,job_json,details_json FROM runs WHERE experiment_id=?", (experiment_id,)):
            count += 1
            job, details = json.loads(row["job_json"]), json.loads(row["details_json"])
            allocation = job["timeout_seconds"]
            if row["state"] in TERMINAL_STATES:
                elapsed = details.get("solver_elapsed_seconds", allocation)
                if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                    elapsed = allocation
                used += elapsed
            else:
                reserved += allocation
        budgets = spec["budgets"]
        return {"submitted_runs": count, "solver_seconds_used": used, "solver_seconds_reserved": reserved,
                "remaining_runs": max(0, budgets["max_runs"] - count),
                "remaining_solver_seconds": max(0.0, budgets["max_total_solver_seconds"] - used - reserved)}

    def submit(self, experiment_id: str, job: dict, decision: dict, idempotency_key: str,
               *, expected_spec_version: int | None = None) -> dict:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must contain 1 to 256 characters")
        if not isinstance(job, dict) or not isinstance(decision, dict):
            raise ValueError("job and decision must be objects")
        # Hash the caller's request before defaults, so retried requests survive spec revisions.
        request_hash = hashlib.sha256(_json({"job": job, "decision": decision}).encode("utf-8")).hexdigest()
        with self._transaction() as db:
            experiment = self._experiment(db, experiment_id)
            existing = db.execute("SELECT run_id,request_sha256 FROM runs WHERE experiment_id=? AND idempotency_key=?",
                                  (experiment_id, idempotency_key)).fetchone()
            if existing:
                if existing["request_sha256"] != request_hash:
                    raise ValueError("Idempotency key was already used with a different request")
                return self._run(db, existing["run_id"])
            if expected_spec_version is not None:
                if isinstance(expected_spec_version, bool) or not isinstance(expected_spec_version, int) or expected_spec_version < 1:
                    raise ValueError("expected_spec_version must be a positive integer")
                if experiment["version"] != expected_spec_version:
                    raise ValueError("Experiment was revised after preparation; prepare against the new version")
            if experiment["state"] != "active":
                raise ValueError(f"Experiment is {experiment['state']}; resume before submitting")
            spec = experiment["spec"]
            frozen_job = json.loads(_json(job))
            timeout = frozen_job.setdefault("timeout_seconds", spec["budgets"]["max_run_solver_seconds"])
            if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0 or timeout > spec["budgets"]["max_run_solver_seconds"]:
                raise ValueError("Job timeout_seconds exceeds the single-run solver budget")
            parameters = frozen_job.get("parameters", {})
            if not isinstance(parameters, dict):
                raise ValueError("job.parameters must be an object")
            for name, bound in spec["parameter_bounds"].items():
                if name not in parameters:
                    raise ValueError(f"Bounded parameter {name} must be explicit in every run")
                low, high = (bound["min"], bound["max"]) if isinstance(bound, dict) else bound
                value = parameters[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"Parameter {name} is outside the experiment bounds")
            budget = self._budget(db, experiment_id, spec)
            if budget["remaining_runs"] < 1 or budget["remaining_solver_seconds"] < timeout:
                raise ValueError("Experiment run or total solver budget exhausted")
            run_id, now = uuid.uuid4().hex, _now()
            directory = self.runs_dir / run_id
            directory.mkdir()
            manifest = {"run_id": run_id, "experiment_id": experiment_id,
                        "spec_version": experiment["version"], "idempotency_key": idempotency_key,
                        "created_utc": now, "request_sha256": request_hash}
            # Exclusive files can survive a failed DB commit as orphan evidence; they are never reused.
            input_hashes = {}
            for filename, value in (("job.json", frozen_job), ("spec.json", spec), ("decision.json", decision)):
                with (directory / filename).open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
                input_hashes[filename] = hashlib.sha256((directory / filename).read_bytes()).hexdigest()
            manifest["input_sha256"] = input_hashes
            with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (run_id, experiment_id, experiment["version"], _json(frozen_job), _json(decision),
                        request_hash, idempotency_key, "queued", "queued", _json({"input_sha256": input_hashes}), str(directory), now, now))
            self._event(db, experiment_id, "run_submitted", manifest, run_id)
            return self._run(db, run_id)

    def next_run(self) -> dict | None:
        with self._transaction() as db:
            marks = ",".join("?" for _ in ACTIVE_STATES)
            if db.execute(f"SELECT 1 FROM runs WHERE state IN ({marks}) LIMIT 1", tuple(ACTIVE_STATES)).fetchone():
                return None
            row = db.execute("SELECT r.run_id,r.experiment_id FROM runs r JOIN experiments e USING(experiment_id) "
                             "WHERE r.state='queued' AND e.state='active' ORDER BY r.rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE runs SET state='preparing',phase='claimed',updated_utc=? WHERE run_id=?", (_now(), row["run_id"]))
            self._event(db, row["experiment_id"], "run_claimed", {}, row["run_id"])
            return self._run(db, row["run_id"])

    def update_run(self, run_id: str, state: str, phase: str, details: dict | None = None) -> dict:
        if state not in STATES or not isinstance(phase, str) or not phase:
            raise ValueError("Invalid run state or phase")
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise ValueError("details must be an object")
        _json(details)
        if "solver_elapsed_seconds" in details:
            elapsed = details["solver_elapsed_seconds"]
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError("solver_elapsed_seconds must be finite and nonnegative")
        with self._transaction() as db:
            prior = self._run(db, run_id)
            if prior["state"] in TERMINAL_STATES and state != prior["state"]:
                raise ValueError("A final run cannot be restarted; submit a new run ID")
            if state == "queued" and prior["state"] != "queued":
                raise ValueError("An executed run cannot return to the submission queue")
            merged = _merge(prior["details"], details)
            db.execute("UPDATE runs SET state=?,phase=?,details_json=?,updated_utc=? WHERE run_id=?",
                       (state, phase, _json(merged), _now(), run_id))
            event = "run_reconciled" if prior["state"] == "needs_attention" and state != "needs_attention" else "run_updated"
            # Polling updates the durable snapshot without flooding the decision/event inbox.
            if prior["state"] != state or prior["phase"] != phase:
                self._event(db, prior["experiment_id"], event,
                            {"previous_state": prior["state"], "state": state, "phase": phase, "details": details}, run_id)
            return self._run(db, run_id)

    def run(self, run_id: str) -> dict:
        with self._connection() as db:
            return self._run(db, run_id)

    def runs(self, experiment_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            sql = "SELECT run_id FROM runs" + (" WHERE experiment_id=?" if experiment_id else "") + " ORDER BY rowid"
            return [self._run(db, row[0]) for row in db.execute(sql, (experiment_id,) if experiment_id else ())]

    def active_runs(self) -> list[dict]:
        with self._connection() as db:
            # Keep the predicate identical to the partial singleton index so a
            # five-second heartbeat never decodes years of archived run inputs.
            rows = db.execute("SELECT run_id FROM runs WHERE state IN "
                              "('preparing','prepared','starting','solving','exporting','analyzing','needs_attention') "
                              "ORDER BY rowid").fetchall()
            return [self._run(db, row[0]) for row in rows]

    def events(self, experiment_id: str | None = None, after: int = 0) -> list[dict]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative event cursor")
        with self._connection() as db:
            sql = "SELECT * FROM events WHERE event_id>?" + (" AND experiment_id=?" if experiment_id else "") + " ORDER BY event_id"
            result = []
            for row in db.execute(sql, (after, experiment_id) if experiment_id else (after,)):
                item = dict(row)
                item["data"] = json.loads(item.pop("data_json"))
                result.append(item)
            return result

    def ack_event(self, event_id: int) -> dict:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown event: {event_id}")
            db.execute("UPDATE events SET acknowledged_utc=COALESCE(acknowledged_utc,?) WHERE event_id=?", (_now(), event_id))
            value = dict(db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone())
            value["data"] = json.loads(value.pop("data_json"))
            return value

    def control(self, experiment_id: str, action: str) -> dict:
        if action not in {"pause", "resume", "cancel", "stop"}:
            raise ValueError("Control action must be pause, resume, cancel or stop")
        with self._transaction() as db:
            self._experiment(db, experiment_id)
            now = _now()
            state = "active" if action == "resume" else "stopped" if action == "stop" else "paused"
            db.execute("UPDATE experiments SET state=?,updated_utc=? WHERE experiment_id=?", (state, now, experiment_id))
            cursor = db.execute("INSERT INTO controls(experiment_id,action,created_utc) VALUES(?,?,?)", (experiment_id, action, now))
            if action == "resume":
                for row in db.execute("SELECT run_id,details_json FROM runs WHERE experiment_id=? AND state='needs_attention'", (experiment_id,)).fetchall():
                    details = _merge(json.loads(row["details_json"]), {"resume_requested": True, "resume_requested_utc": now})
                    db.execute("UPDATE runs SET details_json=?,updated_utc=? WHERE run_id=?", (_json(details), now, row["run_id"]))
            if action in {"cancel", "stop"}:
                for row in db.execute("SELECT run_id,state,details_json FROM runs WHERE experiment_id=?", (experiment_id,)).fetchall():
                    if row["state"] == "queued":
                        details = _merge(json.loads(row["details_json"]), {"solver_elapsed_seconds": 0})
                        db.execute("UPDATE runs SET state='cancelled',phase='cancelled_before_execution',details_json=?,updated_utc=? WHERE run_id=?",
                                   (_json(details), now, row["run_id"]))
                        self._event(db, experiment_id, "run_cancelled", {"reason": action}, row["run_id"])
                    elif action == "cancel" and row["state"] in ACTIVE_STATES:
                        details = _merge(json.loads(row["details_json"]), {"cancel_requested": True, "cancel_requested_utc": now})
                        db.execute("UPDATE runs SET details_json=?,updated_utc=? WHERE run_id=?", (_json(details), now, row["run_id"]))
            self._event(db, experiment_id, "control_requested", {"action": action, "control_id": cursor.lastrowid})
            return {"control_id": cursor.lastrowid, "action": action, "experiment": self._experiment(db, experiment_id)}

    def controls(self, experiment_id: str | None = None, pending_only: bool = True) -> list[dict]:
        with self._connection() as db:
            where = ["acknowledged_utc IS NULL"] if pending_only else []
            values = []
            if experiment_id:
                where.append("experiment_id=?")
                values.append(experiment_id)
            return [dict(row) for row in db.execute("SELECT * FROM controls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY control_id", values)]

    def ack_control(self, control_id: int) -> None:
        with self._transaction() as db:
            cursor = db.execute("UPDATE controls SET acknowledged_utc=COALESCE(acknowledged_utc,?) WHERE control_id=?", (_now(), control_id))
            if not cursor.rowcount:
                raise KeyError(f"Unknown control: {control_id}")

    def context(self, experiment_id: str) -> dict:
        with self._connection() as db:
            experiment = self._experiment(db, experiment_id)
            runs = [self._run(db, row[0]) for row in db.execute("SELECT run_id FROM runs WHERE experiment_id=? ORDER BY rowid", (experiment_id,))]
            budget = self._budget(db, experiment_id, experiment["spec"])
            cursor = db.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE experiment_id=?", (experiment_id,)).fetchone()[0]
        from .research_analysis import comparison_rows, propose_next
        return {"experiment": experiment, "runs": runs, "budget": budget,
                "comparison": comparison_rows(runs), "next_proposal": propose_next(experiment["spec"], runs),
                "event_cursor": cursor, "pending_controls": self.controls(experiment_id),
                "evidence_boundary": "CST numerical results; solver success does not establish convergence or physical validation."}
