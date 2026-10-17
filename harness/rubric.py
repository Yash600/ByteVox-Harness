"""Scoring rubric, applied IDENTICALLY to the multi-agent run and the single-agent baseline.

Three dimensions, each scored 0-5 by code (no model, no human judgement):

  Recall        how much of the task's known ground truth was found WITH real evidence
  Groundedness  share of presented findings whose quoted evidence really exists in its source
  Distinctness  share of presented findings that are not duplicates of another finding

The ground truth is planted in the sample inputs, so the right answers are known in advance.
Limits, stated up front: it scores the findings list, not the prose summary; the research
check is keyword-based; one run per task is a small sample.
"""
import json
import os
import re

# ------------------------------------------------------------------ the tasks
TASKS = {
    "code": {
        "title": "Code review (samples/buggy_repo, 6 planted issues)",
        "goal": "Review this repo for bugs and security issues. Produce a prioritized list with evidence.",
        "input": "samples/buggy_repo",
    },
    "library": {
        "title": "Code review, held-out project (library-app, 20 planted issues)",
        "goal": "Review this repo for bugs and security issues. Produce a prioritized list with evidence.",
        # the project lives outside this repo on purpose (it is the held-out test); point this at your copy
        "input": os.environ.get("HARNESS_LIBRARY_APP", "samples/library_app"),
    },
    "triage": {
        "title": "Ticket triage (samples/notes, 3 tickets)",
        "goal": "Triage these support tickets by urgency (high, medium or low) and category. "
                "Set each finding's severity to the ticket's urgency and quote the ticket.",
        "input": "samples/notes",
    },
    "research": {
        "title": "Web research brief (SQLite vs PostgreSQL, 5 key facts)",
        "goal": "What are the main differences between SQLite and PostgreSQL for a small web app? "
                "Give sources.",
        "input": None,
    },
}

# (name, file, truth lines, keyword regex the finding text must match)
CODE_TRUTH = [
    ("SQL injection", "app.py", {9}, r"sql|inject|query"),
    ("Hardcoded API key", "app.py", {5}, r"secret|api[_ -]?key|hardcod|credential|password|token"),
    ("Division by zero on empty list", "utils.py", {5}, r"zero|division|divid|empty|average|len"),
    ("Bare except", "utils.py", {12}, r"except|bare|swallow"),
    ("Unclosed file handle", "utils.py", {9}, r"clos|handle|leak|with|resource|open"),
    ("is_adult boundary (> 18)", "utils.py", {17}, r"adult|boundary|18|>=|off-by-one|inclusive"),
]

# Held-out code review: the 20 planted issues of the separate `library-app` project, written AFTER the harness was
# tuned. (name, files that may be cited, truth lines, keyword regex the finding text must match). Line numbers are
# those of the project as given; a finding must cite a real quoted line within 2 lines of one of them.
LIBRARY_TRUTH = [
    ("Hardcoded admin password", ("auth.py",), {5}, r"password|credential|hardcod"),
    ("Hardcoded signing secret", ("auth.py",), {6}, r"secret|signing|key|hardcod|forge"),
    ("MD5 used for hashing", ("auth.py",), {10}, r"md5|hash"),
    ("Timing-unsafe token compare", ("auth.py",), {25}, r"timing|constant|compare|=="),
    ("SQL injection in search", ("search.py",), {13}, r"sql|inject"),
    ("Command injection (shell=True)", ("server.py",), {15}, r"command|shell|inject"),
    ("TLS off / SSRF / no timeout", ("server.py",), {19}, r"tls|ssl|verify|ssrf|timeout|certificate"),
    ("Server binds 0.0.0.0", ("server.py",), {31}, r"0\.0\.0\.0|bind|expos|all interfaces|listen"),
    ("XSS via innerHTML", ("app.js",), {14}, r"xss|innerhtml|escap|sanitiz"),
    ("eval() on user input", ("app.js",), {20}, r"eval"),
    ("Division by zero (no ratings)", ("utils.py",), {10}, r"zero|division|divid|empty|average"),
    ("Pagination off by one", ("utils.py", "test_utils.py"), {15, 14}, r"pagina|off-by-one|first page|skips|page 0|from 1"),
    ("Late fee has the wrong sign", ("utils.py",), {21}, r"late|fee|sign|negative|revers|wrong order|order"),
    ("Mutable default argument", ("storage.py",), {22, 25}, r"mutable|default|tags"),
    ("`books` undefined in applyFilter", ("app.js",), {20}, r"undefined|not defined|referenceerror|scope|books"),
    ("Bare except / unclosed file / data loss", ("storage.py",), {9, 12}, r"except|swallow|bare|data loss|overwrit|close|handle|leak"),
    ("DB connection never closed", ("search.py",), {6, 10}, r"close|connection|leak|resource"),
    ("Unchecked list index in app.py", ("app.py",), {30}, r"index|bound|valid|crash|negative|range"),
    ("Empty catch hides errors", ("app.js",), {7}, r"catch|swallow|silent|error"),
    ("Weak tests hide the pagination bug", ("test_utils.py",), {15, 10, 14}, r"test|coverage|cover"),
]

TRIAGE_TRUTH = {"ticket_01.txt": "high", "ticket_02.txt": "low", "ticket_03.txt": "high"}

RESEARCH_TRUTH = [
    ("SQLite is serverless / embedded", r"serverless|embedded|no separate server|in-process|self-contained|without a server"),
    ("PostgreSQL is client-server", r"client[- /]?server|separate server|server process|runs as a server|daemon"),
    ("SQLite is a single file", r"single (disk )?file|one file|file-based|database file|single-file"),
    ("Concurrency: writers / locking / MVCC", r"one writer|single writer|multiple writers|concurren|mvcc|write lock|locking"),
    ("PostgreSQL has richer features / scale", r"extension|data types|json|scal|full[- ]text|replication|advanced|stored procedure|roles|permissions"),
]


# ------------------------------------------------------------------------ helpers
def _text(f):
    return " ".join(str(f.get(k, "")) for k in ("title", "detail", "category")).lower()


def _good_ev(f):
    return [e for e in f.get("evidence", []) if e.get("verified")]


def _near(line, truth, slack=2):
    return line is not None and any(abs(int(line) - t) <= slack for t in truth)


def _urgency(sev):
    s = str(sev).strip().lower()
    if s in ("high", "critical", "urgent", "p1", "severe"):
        return "high"
    if s in ("medium", "normal", "moderate", "p2"):
        return "medium"
    if s in ("low", "minor", "trivial", "p3"):
        return "low"
    return None


# ----------------------------------------------------------------- dimension scorers
def groundedness(findings):
    if not findings:
        return 0.0
    return sum(1 for f in findings if _good_ev(f)) / len(findings)


def distinctness(findings):
    if not findings:
        return 0.0
    from .orchestrator import merge_findings
    copy = json.loads(json.dumps(findings))            # merge_findings edits in place
    for i, f in enumerate(copy):
        f.setdefault("id", f"f{i}")
    return len(merge_findings(copy)) / len(findings)


def recall_library(findings):
    """Recall on the 20-issue held-out project. A finding earns an issue only if it quotes a real line in one of the
    right files, near the right line, and its text mentions the issue."""
    hits = []
    for name, files, lines, kw in LIBRARY_TRUTH:
        for f in findings:
            ev = [e for e in _good_ev(f) if any(str(e.get("file", "")).lower().endswith(x) for x in files)]
            if not ev or not re.search(kw, _text(f)):
                continue
            has_line = [e.get("line") for e in ev if e.get("line")]
            if not has_line or any(_near(x, lines) for x in has_line):
                hits.append(name)
                break
    return len(hits) / len(LIBRARY_TRUTH), {"found": hits, "missed": [n for n, *_ in LIBRARY_TRUTH if n not in hits]}


def recall_code(findings):
    hits = []
    for name, file, lines, kw in CODE_TRUTH:
        for f in findings:
            ev = [e for e in _good_ev(f) if str(e.get("file", "")).lower().endswith(file)]
            if not ev or not re.search(kw, _text(f)):
                continue
            has_line = [e.get("line") for e in ev if e.get("line")]
            if not has_line or any(_near(x, lines) for x in has_line):
                hits.append(name)
                break
    missed = [n for n, *_ in CODE_TRUTH if n not in hits]
    return len(hits) / len(CODE_TRUTH), {"found": hits, "missed": missed}


def recall_triage(findings):
    got, wrong = {}, {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for ticket, truth in TRIAGE_TRUTH.items():
        levels = [_urgency(f.get("severity")) for f in findings
                  if any(str(e.get("file", "")).lower().endswith(ticket) for e in _good_ev(f))]
        levels = [lv for lv in levels if lv]
        if not levels:
            wrong[ticket] = "not triaged"
            continue
        top = max(levels, key=lambda lv: rank[lv])
        if top == truth:
            got[ticket] = top
        else:
            wrong[ticket] = f"said {top}, truth {truth}"
    return len(got) / len(TRIAGE_TRUTH), {"found": sorted(got), "missed": wrong}


def recall_research(findings):
    blob = " ".join(_text(f) + " " + " ".join(str(e.get("quote", "")) for e in _good_ev(f)).lower()
                    for f in findings if _good_ev(f))
    hits = [n for n, rx in RESEARCH_TRUTH if re.search(rx, blob)]
    sources = {e.get("url") or e.get("file") for f in findings for e in _good_ev(f)}
    return len(hits) / len(RESEARCH_TRUTH), {
        "found": hits, "missed": [n for n, _ in RESEARCH_TRUTH if n not in hits], "distinct_sources": len(sources)}


RECALL = {"code": recall_code, "library": recall_library, "triage": recall_triage, "research": recall_research}


def score(task, findings):
    """Return {recall, grounded, distinct (0-1 each), points (0-5 each), total (0-15), detail}."""
    r, detail = RECALL[task](findings)
    g, d = groundedness(findings), distinctness(findings)
    pts = {"recall": round(5 * r, 1), "grounded": round(5 * g, 1), "distinct": round(5 * d, 1)}
    return {"recall": r, "grounded": g, "distinct": d, "points": pts,
            "total": round(sum(pts.values()), 1), "n_findings": len(findings), "detail": detail}
