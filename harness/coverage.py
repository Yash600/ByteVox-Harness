"""Coverage guarantee for code review. Plain code, not a model.

Why this exists: the planner (a model) splits the files differently on every run, and nothing used to check that the
split covered everything. Two runs of the same project found 17 and 12 of 20 planted issues because in one of them a
file got one worker's full attention and in the other it got almost none. The checker could not notice: it judges
whether quotes are real, not whether the files were reviewed.

So, after the planned subtasks finish, code does three things whatever the plan looked like:

  1. SECOND LOOK  Every source file gets a second, independent pass by a worker that is told what was already reported
                  for that file and asked only for NEW problems (security, logic, error handling, resources, tests).
  2. SCAN FLOOR   Every rule-scan hit (an exact line, found without any model) that still has no finding near it is
                  added as a finding, labelled as unconfirmed by an agent. The pattern bugs therefore cannot go missing.
  3. A RECORD     Which files were reviewed, with how many findings each, is logged, so "clean" is visible, not silence.

It runs inside the same budgets (it stops when the call/token/time budget is mostly spent) and never raises.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agents import run_agent
from .budget import BudgetExceeded
from .runtime import AgentFailed

SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rs", ".c", ".h", ".cpp",
              ".cs", ".rb", ".php", ".sh", ".sql"}
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", "_harness_web", "repos_cache"}
MAX_FILE_BYTES = 120_000
GROUP_FILES, GROUP_BYTES, MAX_GROUPS = 4, 14_000, 3
MIN_BUDGET_LEFT = 0.3          # do not start the second look when less than this share of any budget remains
NEAR = 2                       # a finding within this many lines of a scan hit covers it

CHECKLIST = ("Look for every kind of problem, not only the obvious ones: (1) security: hardcoded secrets, injection, "
             "unsafe eval/exec/shell, disabled TLS, weak hashing, unsafe comparisons, exposed bind addresses; "
             "(2) logic: wrong operator or sign, off-by-one, division by zero, wrong default, undefined names, "
             "unchecked indexes or inputs; (3) error handling: bare or empty except/catch that hides failures; "
             "(4) resources: files, connections or handles never closed; mutable default arguments; "
             "(5) tests that do not really test the behaviour they claim to.")


def source_files(run):
    """[(rel path, size)] of reviewable source files in the sandbox, in a stable order."""
    out = []
    root = run.sandbox.root
    for p in sorted(root.rglob("*")):
        try:
            if not p.is_file() or p.suffix.lower() not in SOURCE_EXT:
                continue
            if SKIP_PARTS & set(p.relative_to(root).parts):
                continue
            size = p.stat().st_size
            if 0 < size <= MAX_FILE_BYTES:
                out.append((run.sandbox.rel(p), size))
        except OSError:
            continue
    return out


def _evidence(snap):
    """{file: [(line, finding title)]} for every verified quote in every finding."""
    seen = {}
    for st in snap["subtasks"].values():
        for f in st.get("findings", []):
            for e in f.get("evidence", []):
                if e.get("verified"):
                    seen.setdefault(e.get("file"), []).append((e.get("line"), str(f.get("title", ""))))
    return seen


def _groups(files):
    """Pack files into at most MAX_GROUPS groups of bounded size; the biggest groups are filled first so that no
    file is dropped. Files that do not fit are spread over the groups rather than skipped."""
    groups = []
    for rel, size in files:
        for g in groups:
            if len(g["files"]) < GROUP_FILES and g["bytes"] + size <= GROUP_BYTES:
                g["files"].append(rel)
                g["bytes"] += size
                break
        else:
            groups.append({"files": [rel], "bytes": size})
    while len(groups) > MAX_GROUPS:                     # too many: merge the two smallest, never drop a file
        groups.sort(key=lambda g: g["bytes"])
        a = groups.pop(0)
        groups[0]["files"] += a["files"]
        groups[0]["bytes"] += a["bytes"]
    return groups


def _task(group, evidence, goal):
    lines = []
    for rel in group["files"]:
        titles = sorted({t for _, t in evidence.get(rel, [])})
        lines.append(f"- {rel}: already reported: " + ("; ".join(f"'{t}'" for t in titles) if titles else "NOTHING YET"))
    return ("SUBTASK: Second look at every file (a coverage pass)\n"
            f"OVERALL GOAL: {goal}\n"
            "Read EVERY file listed below completely (read_files reads several at once). Other workers already "
            "reviewed the project once; this is an independent second look. Report only problems that are NOT "
            "already reported; a file with 'NOTHING YET' needs special care. If after reading a file you find nothing "
            "new, report nothing for it. At most 5 findings per reply.\n"
            + CHECKLIST + "\nFILES (path: what is already reported):\n" + "\n".join(lines))


def _run_group(run, idx, group, evidence):
    from .orchestrator import _merge, verify_findings
    sid = f"c{idx}"
    st = run.state.update_subtask
    st(sid, status="working", title=f"Second look: {', '.join(Path(f).name for f in group['files'])}",
       kind="analyze", attempts=1, after=[], reason="coverage pass: evidence verified by code, no checker agent")
    try:
        out = run_agent(run, "worker", f"worker-{sid}", _task(group, evidence, run.goal))
    except BudgetExceeded as e:
        st(sid, status="skipped", reason=f"stopped: {e.reason}", findings=[])
        return
    except AgentFailed as e:
        run.logger.log(f"worker-{sid}", "coverage_failed", reason=str(e))
        st(sid, status="skipped", reason=f"second look failed: {str(e)[:120]}", findings=[])
        return
    raw = out.get("findings", [])
    bad = verify_findings(run, raw)
    good = [f for f in raw if f not in bad]
    known = [f for s in run.state.snapshot()["subtasks"].values() for f in s.get("findings", [])]
    fresh = _merge([], [f for f in good if not any(_same_issue(f, k) for k in known)])
    run.logger.log(f"checker-{sid}", "evidence_check", attempt=1, findings=len(fresh),
                   unsupported=[b.get("title") for b in bad], dropped_as_known=len(good) - len(fresh))
    st(sid, status="accepted", findings=fresh, summary=out.get("summary", ""))


def _same_issue(f, g):
    from .orchestrator import _same_issue as same
    return same(f, g)


def _scan_floor(run):
    """Rule-scan hits no finding sits near become findings of their own, honestly labelled."""
    from . import staticscan
    from .orchestrator import SEV_RANK  # noqa: F401  (severity vocabulary is shared)
    if not hasattr(run, "_scan"):
        try:
            run._scan = staticscan.scan_tree(run.sandbox.root, run.sandbox.root, run.sandbox.rel)
        except Exception:
            run._scan = []
    ev = _evidence(run.state.snapshot())
    added, taken = [], set()
    for rel, line, sev, rule, msg, code in run._scan:       # sorted most severe first
        if Path(rel).suffix.lower() not in SOURCE_EXT or sev not in ("high", "medium"):
            continue                                          # low-severity hints (TODO markers...) stay hints
        if (rel, line) in taken:                              # one lead per source line
            continue
        taken.add((rel, line))
        if any(ln is not None and abs(int(ln) - line) <= NEAR for ln, _ in ev.get(rel, [])):
            continue
        added.append({"title": f"{msg}", "severity": sev, "category": f"rule-scan: {rule}",
                      "detail": "Found by the rule scan at this exact line; no agent confirmed it. Treat as a lead.",
                      "origin": "rule-scan", "unconfirmed": True,
                      "evidence": [{"file": rel, "line": line, "quote": code, "verified": True,
                                    "support": "n/a"}]})
    return added


def ensure_coverage(run):
    """Run the coverage stage. Never raises; returns a short dict for the log."""
    log = run.logger.log
    try:
        files = source_files(run)
        if not files:
            return {"files": 0}
        snap = run.state.snapshot()
        have = {sid: s for sid, s in snap["subtasks"].items()}
        evidence = _evidence(snap)
        from .orchestrator import TERMINAL, _budget_left
        todo = [(i, g) for i, g in enumerate(_groups(files), 1)
                if have.get(f"c{i}", {}).get("status") not in TERMINAL]
        if todo and _budget_left(run) > MIN_BUDGET_LEFT:
            log("orchestrator", "coverage_start", files=len(files), groups=len(todo),
                silent=[r for r, _ in files if r not in evidence])
            with ThreadPoolExecutor(max_workers=run.limits.concurrency) as pool:
                for fut in [pool.submit(_run_group, run, i, g, evidence) for i, g in todo]:
                    fut.result()
        elif todo:
            log("orchestrator", "coverage_skipped", reason="budget mostly spent; rule-scan floor only")
        added = [] if run.state.snapshot()["subtasks"].get("c0", {}).get("status") in TERMINAL else _scan_floor(run)
        if added:
            run.state.update_subtask("c0", status="accepted", title="Rule-scan hits nobody reported", kind="analyze",
                                     attempts=0, after=[], findings=added,
                                     reason="added by code from the rule scan; not confirmed by an agent")
        after = _evidence(run.state.snapshot())
        record = {r: len({t for _, t in after.get(r, [])}) for r, _ in files}
        log("orchestrator", "coverage_done", per_file_findings=record, scan_floor_added=len(added),
            files_with_nothing=[r for r, n in record.items() if n == 0])
        return {"files": len(files), "added": len(added)}
    except Exception as e:  # noqa: BLE001 - coverage must never take the run down
        log("orchestrator", "coverage_error", error=f"{type(e).__name__}: {e}")
        return {"error": str(e)}
