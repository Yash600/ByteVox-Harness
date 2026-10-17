"""The orchestrator: plain code (not an LLM) that sequences the agents and owns the policy.

  acquire inputs -> plan -> subtasks in dependency order (each handled by the role that matches
  its kind: analyze / research / edit) -> code check of every quote + LLM checker (bounded
  loops) -> merge duplicates -> assemble the answer.

Nothing here is specific to one task type. Whatever fails is recorded and the run still
finishes, labelled PARTIAL, with the reasons.
"""
import difflib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agents import KIND_ROLE, KINDS, SPECS, Agent, run_agent
from .budget import BudgetExceeded
from .runtime import AgentFailed, Run
from .tools import TOOLS

SEV_RANK = {"high": 0, "medium": 1, "low": 2}
EDIT_WORDS = re.compile(r"(?i)\b(change|edit|update|fix|modify|replace|rename|add|remove|delete|rewrite|"
                        r"set|write|correct|patch|refactor|insert|append|create|apply)\b")
URL_RE = re.compile(r"https?://[^\s)>\]\"'<]+")
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".go", ".rs", ".c", ".h", ".cpp",
            ".cs", ".rb", ".php", ".sh", ".sql", ".json", ".yaml", ".yml", ".toml", ".css", ".html"}
WEB_CUES = re.compile(r"(?i)\b(web|online|internet|search|look ?up|latest|current|currently|today|news|price|"
                      r"official|documentation|docs|compare|comparison|versus|vs|research|find out|sources?|"
                      r"according to|trend|popular|recommend)\b")
_LINENO = re.compile(r"^\s*\d+:\s?")
STOP = {"the", "and", "for", "with", "that", "this", "not", "are", "can", "may", "has", "have", "from",
        "into", "when", "which", "its", "use", "uses", "used", "all", "any", "but", "than", "then"}


_CITE = re.compile(r"\[\s*(?:\d{1,3}|citation needed|note \d+|[a-z])\s*\]", re.I)
_TYPO = str.maketrans({"‘": "'", "’": "'", "“": "'", "”": "'", "–": "-",
                       "—": "-", " ": " ", '"': "'"})


def _norm(s):
    """Form used for matching a quote to its source. Ignores differences that are formatting,
    not content: whitespace, quote and dash styles, spaces before punctuation, and citation
    markers like [12]. The words themselves must still match exactly."""
    t = _CITE.sub("", str(s)).translate(_TYPO)
    t = re.sub(r"\s+([,.;:!?)\]])", r"\1", t)      # "medicines , diets" == "medicines, diets"
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return " ".join(t.split())


def _clean_quote(q):
    # models often copy the "12: " line-number prefix our read tools add
    return "\n".join(_LINENO.sub("", ln) for ln in str(q).splitlines())


def _listing(run):
    return TOOLS["list_dir"].fn(run.sandbox, {"path": ".", "depth": 3})


def _has_material(run):
    return _listing(run).strip() != "(empty directory)"


# ------------------------------------------------------------- evidence (code, no model)

def _evidence_path(run, file):
    """Evidence may cite a sandbox path OR a URL the agent fetched; map both to a saved file."""
    f = str(file).strip().replace("\\", "/")
    if f.startswith(("http://", "https://")):
        src = run.sandbox.sources
        rel = src.get(f) or next((r for u, r in src.items() if u.rstrip("/") == f.rstrip("/")), None)
        if not rel:
            return None
        f = rel
    return run.sandbox.resolve(f)


def _evidence_path_or_none(run, file):
    try:
        p = _evidence_path(run, file)
        return p if p is not None and p.is_file() else None
    except Exception:
        return None


def _locate(run, file, nq):
    """Find the file a quote really lives in. Models often cite a slightly wrong path (for
    example `app.py` instead of `repos/owner__repo/app.py`). If the cited path is wrong, look
    for the quote itself: same-named files first, then any file. Still deterministic and
    still honest, because the quote must genuinely exist in the file that is returned."""
    p = _evidence_path_or_none(run, file)
    if p is not None:
        return p
    from pathlib import Path
    base = Path(str(file).replace("\\", "/")).name.lower()
    files = [f for f in run.sandbox.root.rglob("*") if f.is_file() and f.stat().st_size < 5_000_000][:4000]
    files.sort(key=lambda f: 0 if f.name.lower() == base else 1)
    for f in files:
        try:
            if nq in _norm(f.read_text(encoding="utf-8", errors="replace")):
                return f
        except OSError:
            continue
    return None


def verify_findings(run: Run, findings):
    """Check each quoted line really exists in the cited file or saved web page. Hard facts are
    checked by code, not by a model. Tolerates quote-style, whitespace and line-number-prefix
    differences, and near-exact single lines (similarity >= 0.9). Mutates evidence entries
    (verified, line). Returns the findings that have no verified evidence at all."""
    url_of = {r: u for u, r in run.sandbox.sources.items()}
    bad = []
    for f in findings:
        for ev in f.get("evidence", []):
            ev["verified"] = False
            quote = _clean_quote(ev.get("quote", ""))
            nq = _norm(quote)
            if not nq:
                continue
            try:
                path = _locate(run, ev.get("file", ""), nq)
                if path is None:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                rel = run.sandbox.rel(path)
                if _evidence_path_or_none(run, ev.get("file", "")) != path:
                    ev["cited_as"] = str(ev.get("file"))      # the model cited the wrong path
                ev["file"] = rel
                if rel in url_of:
                    ev["url"] = url_of[rel]
            except Exception:
                continue
            lines = text.splitlines()
            if nq in _norm(text):
                ev["verified"] = True
                first = _norm(quote.splitlines()[0])
                for i, line in enumerate(lines, 1):
                    if first and first in _norm(line):
                        ev["line"] = i
                        break
            elif "\n" not in quote.strip():
                best, at = 0.0, None
                for i, line in enumerate(lines, 1):
                    r = difflib.SequenceMatcher(None, nq, _norm(line)).ratio()
                    if r > best:
                        best, at = r, i
                if best >= 0.9:
                    ev["verified"], ev["line"] = True, at
        if not any(e.get("verified") for e in f.get("evidence", [])):
            bad.append(f)
        else:
            _grade_support(f)
    return bad


def _grade_support(f):
    """A quote can exist in a file and still not back the claim ("Small. Fast. Reliable." cited
    for "embedded, file-based architecture"). Cheap check: does the quote share wording with the
    claim? Marks evidence support = ok | weak. Soft signal only: the checker decides."""
    stem = lambda toks: {t[:5] for t in toks}          # so "values" matches "value"
    claim = stem(_tokens(f.get("title", "") + " " + f.get("detail", "")))
    nums = set(re.findall(r"\d[\d,.]*", f.get("title", "") + " " + f.get("detail", "")))
    for e in f.get("evidence", []):
        if not e.get("verified"):
            continue
        if Path(str(e.get("file", ""))).suffix.lower() in CODE_EXT:
            e["support"] = "n/a"      # prose claims rarely share words with code lines; not gradable
            continue
        q = str(e.get("quote", ""))
        shared = claim & stem(_tokens(q))
        shared_nums = nums & set(re.findall(r"\d[\d,.]*", q))
        # only flag a quote that shares NO wording and no number with its claim; a paraphrase
        # of a real line is normal and must not be flagged
        e["support"] = "ok" if (shared or shared_nums) else "weak"


def _weak_findings(findings):
    return [f for f in findings if f.get("evidence") and
            all(e.get("support") == "weak" for e in f["evidence"] if e.get("verified"))
            and any(e.get("verified") for e in f["evidence"])]


def _merge(carried, new):
    """Add new findings to the ones already kept, dropping repeats. A repeat is the same issue worded
    differently ("eval() on user input" / "eval() on user-supplied filter expression", same line), not just
    an identical title. If near-duplicates piled up here the worker could never remove them (kept findings
    are kept), the checker would reject the list for duplicates, and the subtask would never be accepted."""
    kept = list(carried)
    for f in new:
        if not any(_same_issue(f, g) for g in kept):
            kept.append(f)
    return kept


# ------------------------------------------------------------------- hints (code, no model)

def _code_hints(run, sub, limit=12):
    """Rule-scan candidates for an analyze subtask. Cheap, exact lines, no project knowledge
    needed. Only offered when the sandbox actually contains code the scan understands."""
    if not hasattr(run, "_scan"):
        from . import staticscan
        try:
            run._scan = staticscan.scan_tree(run.sandbox.root, run.sandbox.root, run.sandbox.rel)
        except Exception:
            run._scan = []
    if not run._scan:
        return ""
    instr = sub["instructions"].lower()
    mine = [c for c in run._scan if c[0].lower() in instr or c[0].lower().split("/")[-1] in instr]
    picked = (mine or run._scan)[:limit]
    rows = [f"{r}:{ln}: [{sev}] {rule}: {msg} | {code}" for r, ln, sev, rule, msg, code in picked]
    return ("\n\nAUTOMATIC SCAN CANDIDATES (unverified hints from rule checks; confirm each against the "
            "real code before reporting it):\n" + "\n".join(rows))


# ------------------------------------------------------------------ one subtask

def _dependency_context(run, sub):
    if not sub.get("after"):
        return ""
    snap = run.state.snapshot()["subtasks"]
    rows = []
    for d in sub["after"]:
        st = snap.get(d, {})
        facts = "; ".join(f"{f.get('title')}: {str(f.get('detail', ''))[:140]}"
                          for f in st.get("findings", [])[:6])
        rows.append(f"- {d} \"{st.get('title', '')}\" [{st.get('status', 'unknown')}]: "
                    f"{str(st.get('summary', ''))[:200]} {('FINDINGS: ' + facts) if facts else ''}")
    return "\n\nRESULTS FROM EARLIER SUBTASKS YOU DEPEND ON:\n" + "\n".join(rows)


FINDING_CAP = 5     # a worker's reply is short (output-token limits), so it reports at most this many


def _budget_left(run):
    """Fraction (0-1) of the tightest run-level budget still unspent."""
    b = run.budget
    return min(1 - b.calls / b.max_calls, 1 - b.tokens / b.max_tokens, 1 - b.elapsed() / b.max_seconds)


def _task_text(run, sub, feedback, more_than=None):
    t = (f"SUBTASK {sub['id']}: {sub['title']}\nOVERALL GOAL: {run.goal}\n"
         f"INSTRUCTIONS: {sub['instructions']}")
    if sub["kind"] == "analyze":
        t += f"\nFILES IN THE SANDBOX:\n{_listing(run)}" + _code_hints(run, sub)
    elif sub["kind"] == "research":
        t += "\nWeb pages saved by earlier steps live under _harness_web/ in the sandbox."
    t += _dependency_context(run, sub)
    if more_than:
        t += ("\n\nYOU HAVE ALREADY REPORTED THESE (do not repeat them): "
              + "; ".join(f"'{x}'" for x in more_than)
              + f".\nYou hit the limit of {FINDING_CAP} findings, so there may be more. Report up to "
                f"{FINDING_CAP} NEW, different problems in the same material. If there are none, "
                "return an empty findings list.")
    if feedback:
        t += (f"\n\nYour previous attempt was REJECTED by the checker. Fix this: {feedback}"
              "\nFindings whose evidence was verified are already kept; do not drop them. "
              "Fix or replace the others, and add anything you missed.")
    return t


def process_subtask(run: Run, sub):
    sid, kind = sub["id"], sub["kind"]
    st = run.state.update_subtask
    st(sid, status="running", title=sub["title"], kind=kind, attempts=0, after=sub.get("after", []))
    # an edit that depends on work which did not complete must not run blind
    snap = run.state.snapshot()["subtasks"]
    bad_deps = [d for d in sub.get("after", []) if snap.get(d, {}).get("status") != "accepted"]
    if kind == "edit" and bad_deps:
        st(sid, status="skipped", reason=f"depends on {', '.join(bad_deps)}, which did not complete", findings=[])
        run.logger.log("orchestrator", "subtask_skipped", subtask=sid, reason=f"dependency {bad_deps} not accepted")
        return
    if kind == "edit":
        return process_edit(run, sub)
    role = KIND_ROLE[kind]
    feedback, findings, summary = None, [], ""
    carried = []   # evidence-verified findings kept from earlier attempts (an agent cannot
                   # "escape" a rejection by deleting its findings)
    # attempts = first try + checker rejections allowed. A follow-up pass for MORE findings shares this
    # same budget, so it can never increase the number of model calls beyond the original limits.
    max_attempts = 1 + run.limits.max_check_loops
    continuing, approved = False, []     # continuing = asking for MORE after a full (capped) answer
    for attempt in range(1, max_attempts + 1):
        st(sid, attempts=attempt, status="working")
        try:
            out = run_agent(run, role, f"{role}-{sid}-a{attempt}",
                            _task_text(run, sub, feedback, [f["title"] for f in carried] if continuing else None))
        except BudgetExceeded as e:
            return _stop(run, sid, e, carried)
        except AgentFailed as e:
            run.logger.log(f"{role}-{sid}", "subtask_failed", reason=str(e))
            if continuing and approved:      # the extra pass failed, but the approved work stands
                st(sid, status="accepted", findings=approved, summary=summary,
                   reason="a follow-up pass for more findings failed; the approved findings are kept")
            else:
                st(sid, status="failed", reason=str(e), findings=carried)
            return
        raw = out.get("findings", [])
        summary = (summary or out.get("summary", "")) if continuing else out.get("summary", "")
        before = len(carried)
        bad = verify_findings(run, raw)
        carried = _merge(carried, [f for f in raw if f not in bad])
        findings = carried
        run.logger.log(f"checker-{sid}", "evidence_check", attempt=attempt,
                       findings=len(findings), unsupported=[b.get("title") for b in bad])
        if continuing:
            bad = []     # unsupported extras were dropped by code (logged above); never re-litigate them
            if len(carried) == before:
                run.logger.log(f"checker-{sid}", "no_more_findings", attempt=attempt, total=len(carried))
                st(sid, status="accepted", findings=carried, summary=summary, feedback="")
                return
        if bad:
            verdict, feedback = "reject", (
                "These findings quote text that does NOT exist in the cited sources: "
                + "; ".join(f"'{b.get('title')}'" for b in bad)
                + ". Re-read the source and copy a real line exactly, or drop the finding.")
        else:
            to_check = carried[before:] if continuing else carried    # earlier findings were already approved
            weak = _weak_findings(to_check)
            if weak:
                run.logger.log(f"checker-{sid}", "weak_evidence", attempt=attempt,
                               findings=[w.get("title") for w in weak])
            try:
                chk = run_agent(run, "checker", f"checker-{sid}-a{attempt}",
                                f"SUBTASK: {sub['title']}\nINSTRUCTIONS: {sub['instructions']}\n"
                                + ("NOTE: these are ADDITIONAL findings from a follow-up pass; the earlier ones were "
                                   "already approved.\n" if continuing else "")
                                + "RESULT:\n" + json.dumps({"summary": summary, "findings": to_check},
                                                           ensure_ascii=False) + "\n"
                                "EVIDENCE CHECK: every quote was found in its source."
                                + (("\nWEAK EVIDENCE MATCH: the quoted lines for these findings share almost no "
                                    "wording with the claim they are meant to support: "
                                    + "; ".join(f"'{w.get('title')}'" for w in weak)
                                    + ". Reject them unless the quote clearly supports the claim.") if weak else ""))
                verdict, feedback = chk["verdict"], chk.get("feedback", "")
            except BudgetExceeded as e:
                return _stop(run, sid, e, findings, "unchecked")
            except AgentFailed as e:
                run.logger.log(f"checker-{sid}", "checker_failed", reason=str(e))
                st(sid, status="unverified", reason=f"checker failed: {e}", findings=findings,
                   summary=summary)
                return
        run.logger.log(f"checker-{sid}", "verdict", attempt=attempt, verdict=verdict,
                       feedback=feedback)
        if verdict == "accept":
            approved = list(carried)
            if len(raw) >= FINDING_CAP and attempt < max_attempts and _budget_left(run) > 0.3:
                # the worker filled its quota, so it probably stopped early: ask for what is left
                continuing, feedback = True, None
                run.logger.log(f"checker-{sid}", "continuing_after_cap", attempt=attempt, have=len(carried))
                st(sid, findings=carried, summary=summary)
                continue
            st(sid, status="accepted", findings=findings, summary=summary, feedback=feedback)
            return
        if attempt == max_attempts:
            run.logger.log(f"checker-{sid}", "loop_limit", attempts=attempt)
            if continuing and approved:
                st(sid, status="accepted", findings=approved, summary=summary,
                   reason="extra findings from a follow-up pass were not approved and were dropped")
            else:
                st(sid, status="unverified", findings=carried, summary=summary,
                   reason=f"checker still rejected after {attempt} attempts: {feedback}")
            return


def _stop(run, sid, e, findings=None, status="skipped"):
    run.logger.log("orchestrator", "budget_exceeded", scope=e.scope, reason=e.reason, subtask=sid)
    run.state.update_subtask(sid, status=status if findings else "skipped",
                             reason=f"stopped: {e.reason}", findings=findings or [])


def process_edit(run: Run, sub):
    sid = sub["id"]
    try:
        out = run_agent(run, "editor", f"editor-{sid}", _task_text(run, sub, None))
    except BudgetExceeded as e:
        return _stop(run, sid, e)
    except AgentFailed as e:
        return run.state.update_subtask(sid, status="failed", reason=str(e), findings=[])
    applied = [ev for ev in _read_trace_edits(run, f"editor-{sid}") if ev["ok"]]
    status = "accepted" if applied else "failed"
    run.state.update_subtask(sid, status=status, findings=[], summary=out.get("summary", ""),
                             reason="" if applied else "no edit was applied (denied or failed)",
                             edits=[e["args"] for e in applied])


def _read_trace_edits(run, agent_id):
    from .logger import read_trace
    return [e for e in read_trace(run.logger.path)
            if e["agent"] == agent_id and e["kind"] == "tool_call" and e.get("tool") == "edit_file"]


# ------------------------------------------------------------------- acquiring inputs

_GH_URL = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
                     r"(?:/tree/([A-Za-z0-9_./-]+))?", re.I)
MAX_REPOS = 2


def ingest_github(run: Run):
    """If the goal names public GitHub repos, download them into the sandbox before planning.
    Deterministic on purpose: small models are unreliable at remembering to fetch first.
    Returns an error string, or None."""
    seen = []
    for m in _GH_URL.finditer(run.goal):
        owner, repo, ref = m.group(1), m.group(2), m.group(3)
        repo = re.sub(r"\.git$", "", repo).rstrip(".")
        if (owner, repo) not in [(o, r) for o, r, _ in seen] and repo:
            seen.append((owner, repo, ref))
    agent = Agent(SPECS["ingest"], "ingest", None)
    for owner, repo, ref in seen[:MAX_REPOS]:
        args = {"owner": owner, "repo": repo}
        if ref:
            args["ref"] = ref
        out = run.call_tool(agent, "github_fetch", args)
        if out.startswith("ERROR"):
            return f"Could not download github.com/{owner}/{repo}: {out[7:].strip()}"
    if len(seen) > MAX_REPOS:
        run.state.warn(f"only the first {MAX_REPOS} GitHub repositories were downloaded")
    return None


def _goal_urls(run):
    """Non-GitHub URLs named in the goal: given to the planner as pointers, not fetched blindly."""
    return [u.rstrip(".,;") for u in URL_RE.findall(run.goal) if "github.com" not in u.lower()][:5]


# ------------------------------------------------------------------- planning

def _plan_task(run, listing, extra=""):
    urls = _goal_urls(run)
    return (f"GOAL: {run.goal}\n"
            f"SANDBOX FILES (root is '.'):\n{listing}\n"
            + (f"URLS NAMED IN THE GOAL: {', '.join(urls)}\n" if urls else "")
            + "Plan the work." + extra)


def fallback_plan(run, reason):
    """If the planner cannot produce a usable plan, do the goal as one subtask instead of failing."""
    kind = "analyze" if _has_material(run) else "research"
    run.logger.log("orchestrator", "fallback_plan", reason=reason, subtask_kind=kind)
    run.state.warn(f"The planner could not produce a plan ({reason}); the goal was run as a single subtask instead.")
    return {"feasible": True, "reason": "fallback plan", "subtasks": [{
        "id": "s1", "title": "Complete the goal", "kind": kind,
        "instructions": f"{run.goal}\nBe specific and back every point with a quoted source.", "after": []}]}


def normalize_plan(run, plan):
    """Turn whatever the planner returned into a safe, runnable list of subtasks."""
    has_files = _has_material(run)
    subs, notes = [], []
    for i, s in enumerate(plan["subtasks"][: run.limits.max_subtasks], 1):
        kind = str(s.get("kind", "")).lower().strip()
        if kind not in KINDS:
            kind = "analyze" if has_files else "research"
            notes.append(f"s{i}: unknown kind, using {kind}")
        if kind == "edit" and not EDIT_WORDS.search(run.goal):
            kind = "analyze" if has_files else "research"
            notes.append(f"s{i}: goal does not ask for changes, so edit was downgraded to {kind}")
        if kind == "analyze" and not has_files:
            kind = "research"
            notes.append(f"s{i}: nothing in the sandbox to analyse, so it became a research subtask")
        if kind == "research" and has_files and not WEB_CUES.search(run.goal) and not _goal_urls(run):
            kind = "analyze"       # small models invent web research for plain file jobs
            notes.append(f"s{i}: the goal shows no web intent and files are present, so research became analyze")
        if kind == "edit" and not has_files and not any(
                str(a) in ("s%d" % j for j in range(1, i)) for a in (s.get("after") or [])):
            notes.append(f"s{i}: edit with no files; it will likely fail safely")
        subs.append({"id": f"s{i}", "title": str(s["title"])[:120], "instructions": str(s["instructions"]),
                     "kind": kind, "after": []})
        after = s.get("after") or []
        if isinstance(after, list):   # only earlier subtasks: this also rules out cycles
            subs[-1]["after"] = [a for a in (str(x) for x in after)
                                 if re.fullmatch(r"s\d+", a) and int(a[1:]) < i]
    if notes:
        run.logger.log("orchestrator", "plan_adjusted", notes=notes)
    return subs


def _run_subtasks(run, subs, done=None):
    """Run subtasks in dependency order; independent ones run in parallel. `done` = ids already
    finished (a resumed run): they are not run again, and anything that depends on them is unblocked."""
    done = set(done or [])
    pending = {s["id"]: s for s in subs if s["id"] not in done}
    while pending:
        ready = [s for s in pending.values() if all(d in done for d in s["after"])]
        if not ready:                       # cannot happen after normalize_plan; never deadlock
            ready = list(pending.values())
        with ThreadPoolExecutor(max_workers=run.limits.concurrency) as pool:
            for f in [pool.submit(_safe_subtask, run, s) for s in ready]:
                f.result()
        for s in ready:
            pending.pop(s["id"], None)
            done.add(s["id"])


TERMINAL = ("accepted", "unverified", "unchecked", "failed", "skipped")


def _safe_subtask(run, sub):
    """Last line of defence: nothing a subtask does may take the whole run down."""
    try:
        process_subtask(run, sub)
    except Exception as e:  # noqa: BLE001
        run.logger.log(f"{KIND_ROLE.get(sub['kind'], 'worker')}-{sub['id']}", "subtask_crashed",
                       error=f"{type(e).__name__}: {e}")
        run.state.update_subtask(sub["id"], status="failed", reason=f"crashed: {e}", findings=[])
    _demo_crash(run)


def _demo_crash(run):
    """DEMO ONLY: HARNESS_DEMO_CRASH_AFTER=N hard-kills the whole process once N subtasks have finished,
    so crash recovery can be shown on demand. It does nothing unless that variable is set, and never on a resume."""
    n = os.environ.get("HARNESS_DEMO_CRASH_AFTER")
    if not n or run.resumed:
        return
    finished = sum(1 for s in run.state.snapshot()["subtasks"].values() if s.get("status") in TERMINAL)
    if finished >= int(n):
        run.logger.log("orchestrator", "demo_crash", finished=finished,
                       note="deliberate hard kill to demonstrate resume; nothing else is saved after this line")
        os._exit(86)


# ------------------------------------------------------------------- whole run

def _resume(run: Run):
    """Continue an interrupted run: keep every finished subtask, re-run only the unfinished ones.
    The plan is reused (no second planner call) and the spent budget carries over."""
    log = run.logger.log
    snap = run.state.snapshot()
    subs = snap["plan"]["subtasks"]
    done = {sid for sid, st in snap["subtasks"].items() if st.get("status") in TERMINAL}
    redo = [s["id"] for s in subs if s["id"] not in done]
    log("orchestrator", "run_resumed", kept=sorted(done), redo=redo,
        seconds_already_used=round(run.budget.elapsed(), 1), calls_already_used=run.budget.calls)
    run.state.warn("This run was interrupted and resumed. Finished subtasks were kept"
                   f" ({', '.join(sorted(done)) or 'none'}); {len(redo)} unfinished subtask(s) were re-run.")
    for sid in redo:
        run.state.update_subtask(sid, status="queued")
    _run_subtasks(run, subs, done=done)
    snap = run.state.snapshot()
    reasons = [f"{sid} ({st.get('title')}): {st['status']} - {st.get('reason', '')}"
               for sid, st in snap["subtasks"].items() if st["status"] != "accepted"]
    return _finish(run, "complete" if not reasons else "partial", reasons, subs)


def execute(run: Run):
    log = run.logger.log
    if run.resumed:
        if (run.state.snapshot().get("plan") or {}).get("subtasks"):
            return _resume(run)
        log("orchestrator", "run_resumed", kept=[], redo=["(planning)"],
            note="the run stopped before it had a plan, so it starts again from planning")
    log("orchestrator", "run_start", goal=run.goal, limits=vars(run.limits),
        provider=run.router.primary, fallback=run.router.fallback)
    reasons = []
    err = ingest_github(run)
    if err:
        log("orchestrator", "run_end", status="failed", reason=err)
        return _finish(run, "failed", [err], [])
    listing = _listing(run)
    try:
        plan = run_agent(run, "planner", "planner", _plan_task(run, listing))
    except BudgetExceeded as e:
        log("orchestrator", "run_end", status="failed", reason=str(e))
        run.state.warn(f"planning stopped: {e}")
        return _finish(run, "failed", [f"planning stopped: {e}"], [])
    except AgentFailed as e:
        plan = fallback_plan(run, str(e)[:120])

    if not plan["feasible"] and listing.strip() != "(empty directory)":
        # Small models over-refuse. Material is present, so ask once more before accepting "no".
        log("orchestrator", "refusal_challenged", reason=plan.get("reason"))
        try:
            again = run_agent(run, "planner", "planner", _plan_task(run, listing,
                              f"\n\nYou previously answered feasible=false because: {plan.get('reason')}\n"
                              "RECHECK: the team CAN read and search every file listed above, which is all that "
                              "reviewing, summarising, analysing or classifying needs. Nothing has to be executed and "
                              "no private data is required. Answer feasible=true and give the subtasks, unless the goal "
                              "truly needs something else (running code, email, a database, controlling apps)."))
            if again["feasible"]:
                plan = again
                log("orchestrator", "refusal_overturned")
        except (BudgetExceeded, AgentFailed) as e:
            log("orchestrator", "refusal_recheck_failed", reason=str(e))
    if not plan["feasible"]:
        log("orchestrator", "refused", reason=plan.get("reason"))
        run.state.set("plan", plan)
        return _finish(run, "refused", [plan.get("reason", "goal not feasible with available tools")], [])

    subs = normalize_plan(run, plan)
    run.state.set("plan", {"reason": plan.get("reason"), "subtasks": subs})
    for s in subs:
        run.state.update_subtask(s["id"], status="queued", title=s["title"], kind=s["kind"], after=s["after"])
    log("orchestrator", "plan", subtasks=subs)

    _run_subtasks(run, subs)

    snap = run.state.snapshot()
    for sid, st in snap["subtasks"].items():
        if st["status"] != "accepted":
            reasons.append(f"{sid} ({st.get('title')}): {st['status']} - {st.get('reason', '')}")
    status = "complete" if not reasons else "partial"
    return _finish(run, status, reasons, subs)


# ------------------------------------------------------------------- merge + assembly

def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9_]+", str(text).lower()) if len(w) >= 4 and w not in STOP}


def _locs(f):
    return {(e.get("file"), e.get("line")) for e in f.get("evidence", []) if e.get("verified") and e.get("line")}


def _files(f):
    return {e.get("file") for e in f.get("evidence", []) if e.get("verified")}


def _same_issue(f, g):
    """True if two findings describe the same problem: identical title, or the same source line plus a shared
    key word, or near-identical titles on the same source."""
    if str(f.get("title", "")).strip().lower() == str(g.get("title", "")).strip().lower():
        return True
    # compare word STEMS so "average" matches "averaging" and "secret" matches "secrets"
    sf = {t[:5] for t in _tokens(f.get("title"))}
    sg = {t[:5] for t in _tokens(g.get("title"))}
    jac = len(sf & sg) / max(1, len(sf | sg))
    same_loc = bool(_locs(f) & _locs(g)) and bool(sf & sg)
    same_src = bool(_files(f) & _files(g)) and jac >= 0.7
    return same_loc or same_src


def merge_findings(findings):
    """Merge duplicates reported by different workers. Same source location plus a shared key
    word, or near-identical titles on the same source, count as one finding. A finding seen by
    several independent workers is corroborated, which is useful signal in itself."""
    order = sorted(findings, key=lambda f: SEV_RANK.get(str(f.get("severity")).lower(), 3))
    out = []
    for f in order:
        twin = next((g for g in out if _same_issue(f, g)), None)
        if twin is None:
            f = dict(f)
            f["seen_by"] = 1
            out.append(f)
            continue
        twin["seen_by"] += 1
        twin["checked"] = twin.get("checked") or f.get("checked")
        have = {(e.get("file"), str(e.get("quote"))) for e in twin["evidence"]}
        twin["evidence"] += [e for e in f.get("evidence", []) if (e.get("file"), str(e.get("quote"))) not in have]
        twin.setdefault("merged_ids", []).append(f["id"])
    return out


def _plain_summary(goal, findings):
    if not findings:
        return ""
    top = "; ".join(f"{f['title']} [{f['id']}]" for f in findings[:4])
    return f"{len(findings)} finding(s) were verified. The most important: {top}."


def _finish(run: Run, status, reasons, subs):
    snap = run.state.snapshot()
    all_findings = []
    for sid, st in snap["subtasks"].items():
        for i, f in enumerate(st.get("findings", []), 1):
            f = dict(f)
            f["id"] = f"{sid}.f{i}"
            f["subtask"] = sid
            f["checked"] = st["status"] == "accepted"
            all_findings.append(f)
    n_raw = len(all_findings)
    all_findings = merge_findings(all_findings)
    if len(all_findings) < n_raw:
        run.logger.log("orchestrator", "findings_merged", before=n_raw, after=len(all_findings))

    summary, order = "", []
    if all_findings:
        try:
            brief = [{"id": f["id"], "title": f["title"], "severity": f.get("severity"),
                      "category": f.get("category"), "detail": str(f.get("detail", ""))[:200],
                      "seen_by": f.get("seen_by", 1)} for f in all_findings]
            out = run_agent(run, "assembler", "assembler",
                            f"GOAL: {run.goal}\nFINDINGS:\n{json.dumps(brief, ensure_ascii=False)}")
            summary, order = out["summary"], [x for x in out.get("priority_order", [])
                                              if isinstance(x, str)]
        except (BudgetExceeded, AgentFailed) as e:
            run.logger.log("assembler", "assembler_failed", reason=str(e))
            reasons.append(f"assembler unavailable ({e}); findings sorted by severity instead")
            summary = _plain_summary(run.goal, all_findings)
            if status == "complete":
                status = "partial"
    rank = {fid: i for i, fid in enumerate(order)}
    all_findings.sort(key=lambda f: (rank.get(f["id"], 999),
                                     SEV_RANK.get(str(f.get("severity")).lower(), 3)))

    if run.stats.get("fallbacks"):
        run.state.warn("The primary model became unavailable (see the Safety log), so some steps "
                       "ran on the fallback model. Results are still evidence-checked, but quality may be lower.")
        snap = run.state.snapshot()
    run.budget.freeze()          # the run is over: stop the clock so the saved and displayed time stay true
    report = render_report(run, status, reasons, summary, all_findings, snap)
    (run.dir / "report.md").write_text(report, encoding="utf-8")
    (run.dir / "result.json").write_text(json.dumps(
        {"status": status, "reasons": reasons, "summary": summary, "findings": all_findings},
        indent=2, ensure_ascii=False), encoding="utf-8")
    (run.dir / "diff.patch").write_text(workspace_diff(run), encoding="utf-8")
    run.state.set("status", status)
    run.state.set("report_path", str(run.dir / "report.md"))
    run.state.set("budget", run.budget.snapshot())
    run.state.set("stats", run.stats)
    run.logger.log("orchestrator", "run_end", status=status, reasons=reasons,
                   budget=run.budget.snapshot(), stats=run.stats)
    return {"status": status, "reasons": reasons, "report": report, "dir": str(run.dir)}


def workspace_diff(run: Run):
    if not run.original:
        return ""
    out = []
    for p in sorted(run.workspace.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(run.workspace)
        if rel.parts[0] in ("_harness_web", "repos"):   # fetched material, not edits
            continue
        orig = run.original / rel
        try:
            new = p.read_text(encoding="utf-8").splitlines(keepends=True)
            old = orig.read_text(encoding="utf-8").splitlines(keepends=True) if orig.exists() else []
        except (UnicodeDecodeError, OSError):
            continue
        if new != old:
            out += difflib.unified_diff(old, new, f"a/{rel.as_posix()}", f"b/{rel.as_posix()}")
    return "".join(out)


def render_report(run, status, reasons, summary, findings, snap):
    b = run.budget.snapshot()
    L = []
    banner = {"complete": "COMPLETE", "partial": "PARTIAL RESULT", "refused": "REFUSED",
              "failed": "FAILED"}[status]
    L.append(f"# Result: {banner}\n")
    L.append(f"**Goal:** {run.goal}\n")
    if status == "complete" and not findings:
        L.append("> **Note:** the run finished cleanly but produced no findings. Verify by hand "
                 "before trusting an empty result.\n")
    if status != "complete":
        L.append("> **This result is " + ("incomplete." if status == "partial" else status + ".")
                 + "** Reasons:")
        L += [f"> - {r}" for r in reasons]
        L.append("")
    for w in snap.get("warnings", []):
        L.append(f"> Note: {w}\n")
    if summary:
        L.append(f"## Summary\n{summary}\n")
    L.append("## Findings (most important first)\n")
    if not findings:
        L.append("_No findings._\n")
    for n, f in enumerate(findings, 1):
        tag = "" if f["checked"] else " **[UNCHECKED - checker did not approve]**"
        cat = f"  [{f['category']}]" if f.get("category") else ""
        seen = f"  (found independently {f['seen_by']}x)" if f.get("seen_by", 1) > 1 else ""
        L.append(f"### {n}. {f['title']}  `{f.get('severity', 'n/a')}`{cat}  ({f['id']}){seen}{tag}")
        L.append(f"{f.get('detail', '')}\n")
        for e in f.get("evidence", []):
            ok = "verified" if e.get("verified") else "NOT VERIFIED"
            loc = f"{e.get('file')}" + (f":{e['line']}" if e.get("line") else "")
            src = f" <{e['url']}>" if e.get("url") else ""
            L.append(f"- Evidence ({ok}) `{loc}`{src}: `{str(e.get('quote'))[:200]}`")
        L.append("")
    L.append("## Subtasks\n\n| id | kind | title | status | attempts | note |\n|---|---|---|---|---|---|")
    for sid, st in snap["subtasks"].items():
        L.append(f"| {sid} | {st.get('kind', '')} | {st.get('title', '')} | {st['status']} | {st.get('attempts', '')} "
                 f"| {str(st.get('reason', '') or st.get('summary', ''))[:120]} |")
    L.append(f"\n## Run stats\n\n- LLM calls: {b['calls']}/{b['max_calls']}  "
             f"tokens: {b['tokens']}/{b['max_tokens']}  seconds: {b['seconds']}/{b['max_seconds']:.0f}")
    L.append(f"- Retries: {run.stats['retries']}  LLM errors: {run.stats['llm_errors']}  "
             f"injections flagged: {run.stats['injections_flagged']}  "
             f"tool calls denied: {run.stats['tool_denied']}")
    return "\n".join(L) + "\n"
