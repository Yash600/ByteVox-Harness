"""Web UI server (stdlib only).   python server.py  ->  http://127.0.0.1:8765

Starts runs in background threads, serves live state/trace by polling, and lets a human
approve or reject risky actions. Bound to localhost only.
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from harness.config import HARD_TIME_LIMIT, Limits
from harness.llm import Router
from harness.logger import read_trace
from harness.orchestrator import execute
from harness.runtime import Run

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
SAMPLES = ROOT / "samples"
ACTIVE = {}          # run_id -> Run (kept in memory after finishing so approvals/state stay live)
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


UPLOADS = ROOT / "uploads"
UP_MAX_FILE = 5 * 1024 * 1024        # per file
UP_MAX_TOTAL = 60 * 1024 * 1024      # per upload
UP_MAX_FILES = 2500
UP_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
UP_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".exe", ".dll", ".so", ".bin", ".woff",
               ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".pyc", ".class", ".jar", ".whl"}


def _safe_rel(name):
    """Turn a client-supplied name into a safe relative path, or None."""
    parts = [p for p in str(name).replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts) or len(parts) > 12:
        return None
    if set(parts[:-1]) & UP_SKIP_DIRS or Path(parts[-1]).suffix.lower() in UP_SKIP_EXT:
        return None
    return "/".join(parts)


def save_upload(files):
    """files: [{name, b64}]. Plain files are saved as given; .zip files are unpacked.
    Returns (upload_id, count, skipped). Raises ValueError on anything unsafe or too big."""
    import base64
    import io
    import zipfile
    if not isinstance(files, list) or not files:
        raise ValueError("no files received")
    uid = "up_" + uuid.uuid4().hex[:10]
    root = UPLOADS / uid
    root.mkdir(parents=True)
    count = total = skipped = 0

    def put(rel, data):
        nonlocal count, total, skipped
        rel = _safe_rel(rel)
        if rel is None or len(data) > UP_MAX_FILE or b"\0" in data[:2048]:
            skipped += 1
            return
        total += len(data)
        count += 1
        if count > UP_MAX_FILES or total > UP_MAX_TOTAL:
            raise ValueError(f"upload is too large (limit {UP_MAX_FILES} files / {UP_MAX_TOTAL // 1048576} MB)")
        dest = (root / rel).resolve()
        if not dest.is_relative_to(root.resolve()):
            raise ValueError("unsafe path in upload")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    try:
        for f in files:
            data = base64.b64decode(f.get("b64", ""), validate=False)
            name = str(f.get("name", ""))
            if name.lower().endswith(".zip"):
                z = zipfile.ZipFile(io.BytesIO(data))
                infos = [i for i in z.infolist() if not i.is_dir()]
                if sum(i.file_size for i in infos) > 4 * UP_MAX_TOTAL:
                    raise ValueError("zip expands to too much data")
                for i in infos:
                    if (i.external_attr >> 16) & 0o170000 == 0o120000:
                        continue                                    # symlink entry
                    put(i.filename, z.read(i))
            else:
                put(name, data)
        if count == 0:
            raise ValueError("no readable text files in the upload (binary files are skipped)")
    except Exception:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        raise
    return uid, count, skipped


def samples():
    return sorted(p.name for p in SAMPLES.iterdir() if p.is_dir()) if SAMPLES.exists() else []


def clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, type(default)(v)))
    except (TypeError, ValueError):
        return default


def start_run(body):
    goal = str(body.get("goal", "")).strip()
    if not goal or len(goal) > 2000:
        raise ValueError("goal must be 1-2000 characters")
    inp = body.get("input") or ""
    input_dir = None
    if inp.startswith("upload:"):
        uid = inp[7:]
        if not ID_RE.match(uid) or not (UPLOADS / uid).is_dir():
            raise ValueError("unknown upload; please upload the files again")
        input_dir = UPLOADS / uid
    elif inp:
        if inp not in samples():
            raise ValueError(f"unknown input folder: {inp}")
        input_dir = SAMPLES / inp
    provider = body.get("provider") or "ollama"
    if provider not in ("ollama", "groq"):
        raise ValueError("provider must be ollama or groq")
    if provider == "groq" and not os.environ.get("GROQ_API_KEY"):
        raise ValueError("GROQ_API_KEY is not set in the environment of the server process")
    fallback = body.get("fallback") or None
    if fallback not in (None, "ollama", "groq"):
        raise ValueError("bad fallback")
    L = Limits.from_env()
    L.max_llm_calls = clamp(body.get("max_llm_calls"), 1, 500, L.max_llm_calls)
    L.max_seconds = clamp(body.get("max_seconds"), 10.0, HARD_TIME_LIMIT, L.max_seconds)   # 15-minute ceiling
    L.agent_max_seconds = min(L.agent_max_seconds, L.max_seconds)
    L.max_tokens = clamp(body.get("max_tokens"), 500, 2_000_000, L.max_tokens)
    L.concurrency = clamp(body.get("concurrency"), 1, 6, L.concurrency)
    chaos = clamp(body.get("chaos"), 0.0, 0.9, 0.0)
    run_id = time.strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
    router = Router(provider=provider, fallback=fallback, chaos_rate=chaos, seed=7)
    run = Run(goal, input_dir=input_dir, runs_root=str(RUNS),
              run_id=run_id, limits=L, router=router, approval_mode="external")
    _launch(run)
    return run_id


def _launch(run):
    """Run a (new or resumed) run in a background thread and track it."""
    run.done = False
    ACTIVE[run.run_id] = run

    def work():
        try:
            execute(run)
        except Exception as e:  # noqa: BLE001
            run.logger.log("orchestrator", "run_crashed", error=f"{type(e).__name__}: {e}")
            run.state.set("status", "failed")
        finally:
            run.done = True

    threading.Thread(target=work, daemon=True, name=run.run_id).start()


def is_resumable(run_id):
    """An interrupted run (marked 'running' on disk, but nothing is running it) that saved its settings."""
    d = RUNS / run_id
    live = ACTIVE.get(run_id)
    try:
        status = json.loads((d / "state.json").read_text(encoding="utf-8")).get("status")
    except (OSError, json.JSONDecodeError):
        return False
    return status == "running" and not (live and not live.done) and (d / "run.json").exists()


def resume_run(run_id):
    if not is_resumable(run_id):
        raise ValueError("this run cannot be resumed: it is not interrupted, or it was saved before resume existed")
    meta = json.loads((RUNS / run_id / "run.json").read_text(encoding="utf-8"))
    if meta.get("provider") == "groq" and not os.environ.get("GROQ_API_KEY"):
        raise ValueError("GROQ_API_KEY is not set in the environment of the server process")
    router = Router(provider=meta.get("provider"), fallback=meta.get("fallback"))
    _launch(Run.resume_from(RUNS / run_id, router=router, approval_mode="external"))
    return run_id


def slim(ev):
    """Trace event without the bulky prompt; full event is fetched on demand."""
    if ev["kind"] == "llm_call":
        ev = dict(ev)
        msgs = ev.pop("messages", [])
        ev["n_messages"] = len(msgs)
        ev["prompt_tail"] = msgs[-1]["content"][:2500] if msgs else ""
    return ev


def run_view(run_id, after):
    d = RUNS / run_id
    if not (d / "state.json").exists():
        return None
    live = ACTIVE.get(run_id)
    try:
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = {}
    events = [slim(e) for e in read_trace(d / "trace.jsonl") if e["seq"] > after] \
        if (d / "trace.jsonl").exists() else []
    active = bool(live and not live.done)
    status = state.get("status")
    if status == "running" and not active:
        status = "interrupted"
    out = {"run_id": run_id, "state": state, "status": status, "active": active,
           "resumable": is_resumable(run_id),
           "events": events, "pending": live.approver.list_pending() if live else []}
    if live and active:
        out["budget"] = live.budget.snapshot()
        out["stats"] = live.stats
        out["limits"] = vars(live.limits)
    else:
        # a finished run reports the figures saved when it ended, never a clock that keeps running
        out["budget"] = state.get("budget")
        out["stats"] = state.get("stats")
        if live:
            out["limits"] = vars(live.limits)
    return out


def list_runs():
    """All runs, most recently active first. (Sorting by folder NAME buried new runs under old
    test_* folders and a fixed cap then hid them; time is the only order that makes sense.)"""
    rows = []
    if RUNS.exists():
        for d in RUNS.iterdir():
            sp = d / "state.json"
            if not sp.exists():
                continue
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
                updated = sp.stat().st_mtime
            except (json.JSONDecodeError, OSError):
                continue
            live = ACTIVE.get(d.name)
            st = s.get("status")
            if st == "running" and not (live and not live.done):
                st = "interrupted"
            rows.append({"id": d.name, "goal": s.get("goal", ""), "status": st, "updated": updated,
                         "subtasks": len(s.get("subtasks", {})), "budget": s.get("budget")})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows[:300]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            body if isinstance(body, str) else json.dumps(body, default=str)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit=100_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        try:
            if not parts:
                return self._send(200, (ROOT / "ui" / "index.html").read_bytes(), "text/html")
            if parts == ["api", "config"]:
                L = Limits.from_env()
                return self._send(200, {"samples": samples(), "limits": vars(L),
                                        "groq_ready": bool(os.environ.get("GROQ_API_KEY")),
                                        "default_provider": os.environ.get("LLM_PROVIDER", "ollama")})
            if parts == ["api", "runs"]:
                return self._send(200, list_runs())
            if len(parts) >= 3 and parts[:2] == ["api", "runs"] and ID_RE.match(parts[2]):
                rid = parts[2]
                if len(parts) == 3:
                    v = run_view(rid, int(q.get("after", ["0"])[0]))
                    return self._send(200 if v else 404, v or {"error": "no such run"})
                if parts[3] == "report":
                    f = RUNS / rid / "report.md"
                    return self._send(200, f.read_text(encoding="utf-8") if f.exists() else "",
                                      "text/plain")
                if parts[3] == "result":
                    f = RUNS / rid / "result.json"
                    return self._send(200, f.read_text(encoding="utf-8") if f.exists() else "{}")
                if parts[3] == "diff":
                    f = RUNS / rid / "diff.patch"
                    return self._send(200, f.read_text(encoding="utf-8") if f.exists() else "",
                                      "text/plain")
                if parts[3] == "event" and len(parts) == 5:
                    seq = int(parts[4])
                    for e in read_trace(RUNS / rid / "trace.jsonl"):
                        if e["seq"] == seq:
                            return self._send(200, e)
            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if parts == ["api", "upload"]:
                body = self._body(limit=90 * 1024 * 1024)          # base64 of up to ~60 MB of files
                uid, n, skipped = save_upload(body.get("files"))
                return self._send(200, {"upload_id": uid, "files": n, "skipped": skipped})
            body = self._body()
            if parts == ["api", "runs"]:
                return self._send(200, {"run_id": start_run(body)})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and ID_RE.match(parts[2]) and parts[3] == "resume":
                return self._send(200, {"run_id": resume_run(parts[2])})
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and ID_RE.match(parts[2]):
                run = ACTIVE.get(parts[2])
                if not run:
                    return self._send(404, {"error": "run is not active"})
                if parts[3] == "approve":
                    ok = run.approver.resolve(str(body.get("id")), bool(body.get("approved")))
                    return self._send(200, {"ok": ok})
                if parts[3] == "cancel":
                    run.cancel.set()
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    port = int(os.environ.get("PORT", "8765"))
    RUNS.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ByteVox harness UI on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
