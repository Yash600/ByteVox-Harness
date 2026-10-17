"""Append-only JSONL trace. One line per step; listeners get events live (CLI, UI)."""
import json
import threading
import time
from pathlib import Path

MAX_FIELD = 20_000


def _clip(v):
    if isinstance(v, str) and len(v) > MAX_FIELD:
        return v[:MAX_FIELD] + f"...[clipped {len(v) - MAX_FIELD} chars]"
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clip(x) for x in v]
    return v


class RunLogger:
    def __init__(self, run_dir, run_id):
        self.run_id = run_id
        self.path = Path(run_dir) / "trace.jsonl"
        self.t0 = time.time()
        self._seq = 0
        self._lock = threading.Lock()
        self.listeners = []

    def log(self, agent, kind, **fields):
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "t": round(time.time() - self.t0, 2), "ts": time.time(),
                  "run_id": self.run_id, "agent": agent, "kind": kind, **_clip(fields)}
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:
                pass  # a broken listener must never break the run
        return ev


def read_trace(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
