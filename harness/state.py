"""Shared run state: one dict, one lock, persisted to state.json after every change."""
import json
import os
import threading
from pathlib import Path


class RunState:
    def __init__(self, run_dir, goal):
        self.path = Path(run_dir) / "state.json"
        self._lock = threading.Lock()
        self.data = {"goal": goal, "status": "running", "plan": None, "subtasks": {},
                     "warnings": [], "report_path": None}
        self._save()

    def _save(self):
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def set(self, key, value):
        with self._lock:
            self.data[key] = value
            self._save()

    def update_subtask(self, sid, **kw):
        with self._lock:
            st = self.data["subtasks"].setdefault(sid, {})
            st.update(kw)
            self._save()

    def warn(self, msg):
        with self._lock:
            self.data["warnings"].append(msg)
            self._save()

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self.data))
