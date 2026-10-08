"""Start a labelled resident-runner endurance check; does not run a simulation."""
import argparse
from pathlib import Path
import math
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocst.service import utc_now, write_json

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
parser.add_argument("--hours", type=float, default=16)
args = parser.parse_args()
if not math.isfinite(args.hours) or args.hours < 12:
    parser.error("Long endurance acceptance needs at least 12 hours")
path = args.root / ".autocst" / "background_soak.json"
if path.exists():
    parser.error("Existing endurance evidence must be preserved; use a new dedicated test workspace")
write_json(path, {"state": "running", "started_utc": utc_now(), "required_seconds": args.hours * 3600,
                  "samples": 0, "restart_count": 0, "max_sample_gap_seconds": 0,
                  "passed": None, "scope": "Runner survival, not a long CST simulation",
                  "app_close_test": "not_performed", "screen_lock_test": "not_performed"})
print(path)
