"""Append-only audit trail: one JSON line per agent step, tool call, guard decision, and run summary."""

import json
import threading
from datetime import datetime, timezone

from backend.config import OUTPUT_DIR

AUDIT_FILE = OUTPUT_DIR / "audit_trail.jsonl"
_lock = threading.Lock()


def log(run_id: str, kind: str, **fields) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    record = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id, "kind": kind, **fields}
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _lock, AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def recent(limit: int = 200, run_id: str | None = None) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    rows = []
    for line in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if run_id is None or rec.get("run_id") == run_id:
            rows.append(rec)
    return rows[-limit:]
