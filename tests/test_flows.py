"""End-to-end flow tests with a scripted model (no network, no API key, runs in seconds).

    python tests/test_flows.py

The scripted model answers according to the agent's role, so each test exercises the real
orchestrator, tool gateway, verifier and budgets. Only the model and the two web tools are fake.
"""
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness import config, llm, tools  # noqa: E402
from harness.config import Limits  # noqa: E402
from harness.llm import LLMResult, Router  # noqa: E402
from harness.logger import read_trace  # noqa: E402
from harness.orchestrator import execute, merge_findings  # noqa: E402
from harness.runtime import Run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_orig_model_for = config.model_for
config.model_for = lambda p, r: "script" if p == "script" else _orig_model_for(p, r)


# ------------------------------------------------------------------ scripted model
class Script:
    """role -> function(messages) -> reply dict. Tests set SCRIPT before each run."""
    SCRIPT = {}

    def chat(self, model, messages, json_mode, timeout):
        sysm = messages[0]["content"]
        role = next((r for r in ("PLANNER", "RESEARCHER", "WORKER", "EDITOR", "CHECKER", "ASSEMBLER")
                     if f"You are a {r}" in sysm or f"You are the {r}" in sysm or f"You are an {r}" in sysm), None)
        fn = Script.SCRIPT.get(role)
        if fn is None:
            raise AssertionError(f"no scripted reply for role {role}")
        out = fn(messages)
        return LLMResult(out if isinstance(out, str) else json.dumps(out), 20, 20, 0.01, "script", "script")


llm.PROVIDERS["script"] = Script


def tool_results(messages):
    return [m["content"] for m in messages if m["content"].startswith("TOOL RESULT")]


def final(**kw):
    return {"action": "final", **kw}


def plan(subs, feasible=True, reason="ok"):
    return final(feasible=feasible, reason=reason, subtasks=subs)


def sub(sid, kind, title="t", instr="do it", after=()):
    return {"id": sid, "title": title, "instructions": instr, "kind": kind, "after": list(after)}


def new_run(goal, folder=None, approve="approve", limits=None):
    return Run(goal, input_dir=(ROOT / folder) if folder else None, runs_root=tempfile.mkdtemp(),
               router=Router(provider="script", fallback=None), approval_mode=approve,
               limits=limits or Limits.from_env())


def kinds(run):
    return [e["kind"] for e in read_trace(run.logger.path)]


# ---------------------------------------------------------------- fake web tools
PAGE = ("Redis is an in-memory data store.\nRedis supports persistence through RDB snapshots and AOF logs.\n"
        "Memcached is a simple in-memory cache without persistence.")


def fake_search(sb, args):
    body = "SEARCH: q (via fake)\n\n1. Redis docs\n   https://redis.io/docs\n   Redis is an in-memory store."
    rel = sb.save_web("search-" + args["query"], body)
    return body + f"\n\nSaved as {rel}."


def fake_fetch(sb, args):
    rel = sb.save_web("redis-io", PAGE, args["url"])
    return f"Saved as {rel} (source: {args['url']}).\n\n{PAGE}"


# ===================================================================== the tests
def test_research_flow_with_url_evidence():
    """planner -> researcher(search, fetch) -> code verifies a quote cited by URL -> assembler."""
    tools.TOOLS["web_search"].fn, tools.TOOLS["fetch_url"].fn = fake_search, fake_fetch

    def researcher(msgs):
        n = len(tool_results(msgs))
        if n == 0:
            return {"action": "tool", "tool": "web_search", "args": {"query": "redis persistence"}}
        if n == 1:
            return {"action": "tool", "tool": "fetch_url", "args": {"url": "https://redis.io/docs"}}
        return final(summary="found", findings=[{
            "title": "Redis can persist data", "severity": "high", "detail": "RDB and AOF.",
            "evidence": [{"file": "https://redis.io/docs",     # cited by URL, not by saved path
                          "quote": "Redis supports persistence through RDB snapshots and AOF logs."}]}])
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "research")]), "RESEARCHER": researcher,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="Redis persists [s1.f1].", priority_order=["s1.f1"])}
    r = new_run("Does Redis persist data? Cite sources.")
    res = execute(r)
    assert res["status"] == "complete", res
    result = json.loads((r.dir / "result.json").read_text(encoding="utf-8"))
    ev = result["findings"][0]["evidence"][0]
    assert ev["verified"] and ev["file"].startswith("_harness_web/") and ev["url"] == "https://redis.io/docs", ev
    assert "redis" in result["summary"].lower()


def test_invented_web_quote_is_rejected():
    tools.TOOLS["web_search"].fn, tools.TOOLS["fetch_url"].fn = fake_search, fake_fetch
    def researcher(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "fetch_url", "args": {"url": "https://redis.io/docs"}}
        return final(summary="x", findings=[{"title": "Redis is written in Java", "evidence": [
            {"file": "https://redis.io/docs", "quote": "Redis is written entirely in Java."}]}])
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "research")]), "RESEARCHER": researcher,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Is Redis written in Java?")
    res = execute(r)
    assert res["status"] == "partial", res                      # never accepted
    assert "evidence_check" in kinds(r)
    assert json.loads((r.dir / "result.json").read_text(encoding="utf-8"))["findings"] == []


def test_dependency_order_research_then_edit_with_context():
    tools.TOOLS["web_search"].fn, tools.TOOLS["fetch_url"].fn = fake_search, fake_fetch
    seen = {}

    def researcher(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "fetch_url", "args": {"url": "https://redis.io/docs"}}
        return final(summary="Redis in-memory", findings=[{"title": "Redis is in-memory", "detail": "in-memory store",
            "evidence": [{"file": "web/01_x.txt", "quote": "Redis is an in-memory data store."}]}])

    def editor(msgs):
        seen["task"] = msgs[1]["content"]
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_file", "args": {"path": "utils.py"}}
        if len(tool_results(msgs)) == 1:
            return {"action": "tool", "tool": "edit_file",
                    "args": {"path": "utils.py", "old": "return age > 18", "new": "return age >= 18"}}
        return final(summary="edited")
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "research"), sub("s2", "edit", after=["s1"])]),
                     "RESEARCHER": researcher, "EDITOR": editor,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="done", priority_order=[])}
    r = new_run("Look up what Redis is, then update utils.py so is_adult returns age >= 18.", "samples/buggy_repo")
    res = execute(r)
    assert res["status"] == "complete", res
    assert "RESULTS FROM EARLIER SUBTASKS" in seen["task"] and "Redis is in-memory" in seen["task"]
    assert "return age >= 18" in (r.workspace / "utils.py").read_text(encoding="utf-8")
    assert "return age > 18" in (ROOT / "samples/buggy_repo/utils.py").read_text(encoding="utf-8")  # original untouched


def test_edit_is_skipped_when_its_dependency_failed():
    def researcher(msgs):
        raise AssertionError("not reached")
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "research"), sub("s2", "edit", after=["s1"])]),
                     "RESEARCHER": lambda m: "this is not json at all",
                     "EDITOR": lambda m: (_ for _ in ()).throw(AssertionError("editor must not run")),
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Look something up, then change utils.py.", "samples/buggy_repo")
    res = execute(r)
    st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))["subtasks"]
    assert st["s1"]["status"] == "failed" and st["s2"]["status"] == "skipped", st
    assert res["status"] == "partial"


def test_plan_guards():
    """unknown kind is fixed, edit is refused when the goal asks for no change,
    analyze with nothing to analyse becomes research, bad `after` references are dropped."""
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "banana"), sub("s2", "edit", after=["s9", "s1"]),
                                                sub("s3", "analyze")]),
                     "WORKER": lambda m: final(summary="x", findings=[]),
                     "RESEARCHER": lambda m: final(summary="x", findings=[]),
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    tools.TOOLS["web_search"].fn, tools.TOOLS["fetch_url"].fn = fake_search, fake_fetch
    # researchers need a tool call first (grounding), so give them one
    Script.SCRIPT["RESEARCHER"] = lambda m: ({"action": "tool", "tool": "web_search", "args": {"query": "q"}}
                                             if not tool_results(m) else final(summary="x", findings=[]))
    r = new_run("Tell me about Mars.")       # no files, no change requested
    execute(r)
    st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))["subtasks"]
    assert st["s1"]["kind"] == "research", st["s1"]          # unknown kind, empty sandbox -> research
    assert st["s2"]["kind"] != "edit", st["s2"]               # goal asks for no change
    assert st["s3"]["kind"] == "research", st["s3"]           # nothing to analyse
    assert st["s2"]["after"] == ["s1"], st["s2"]              # s9 does not exist, dropped
    assert "plan_adjusted" in kinds(r)


def test_fallback_plan_when_planner_is_useless():
    def worker(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_file", "args": {"path": "utils.py"}}
        return final(summary="ok", findings=[])
    Script.SCRIPT = {"PLANNER": lambda m: "garbage, not json", "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    assert "fallback_plan" in kinds(r)
    assert res["status"] in ("complete", "partial") and res["status"] != "failed"
    st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))
    assert list(st["subtasks"]) == ["s1"] and st["subtasks"]["s1"]["kind"] == "analyze"
    assert any("single subtask" in w for w in st["warnings"])


def test_code_review_gets_scan_hints_and_merges_duplicates():
    seen = {}

    def worker(msgs):
        seen.setdefault("task", msgs[1]["content"])
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_files", "args": {"paths": ["app.py", "utils.py"]}}
        sid = "A" if "SUBTASK s1" in msgs[1]["content"] else "B"
        title = "SQL injection in get_user" if sid == "A" else "SQL injection via string concatenation in get_user"
        return final(summary="x", findings=[{"title": title, "severity": "high" if sid == "A" else "medium", "detail": "d",
            "evidence": [{"file": "app.py", "quote": "query = \"SELECT * FROM users WHERE name = '\" + name + \"'\""}]}])
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze", instr="Read app.py"),
                                                sub("s2", "analyze", instr="Read app.py and utils.py")]),
                     "WORKER": worker, "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="One SQL injection [s1.f1].", priority_order=["s1.f1"])}
    r = new_run("Review this repo for bugs.", "samples/buggy_repo")
    res = execute(r)
    assert "AUTOMATIC SCAN CANDIDATES" in seen["task"] and "py-sql-injection" in seen["task"]
    result = json.loads((r.dir / "result.json").read_text(encoding="utf-8"))
    assert len(result["findings"]) == 1, result["findings"]          # two workers, one finding
    assert result["findings"][0]["seen_by"] == 2 and result["findings"][0]["severity"] == "high"
    assert "findings_merged" in kinds(r)


def test_merge_keeps_distinct_issues_on_the_same_line():
    f = lambda i, t, line: {"id": i, "title": t, "severity": "high", "checked": True,
                            "evidence": [{"file": "app.py", "line": line, "verified": True, "quote": "q"}]}
    out = merge_findings([f("a", "Hardcoded API key in source", 5), f("b", "Unfinished work marker TODO", 5),
                          f("c", "Hardcoded secret key present", 5)])
    assert sorted(x["id"] for x in out) == ["a", "b"], out        # key+secret merge, TODO stays separate


def test_refusal_challenge_and_impossible_goal():
    def planner(msgs):
        last = msgs[1]["content"]
        return plan([sub("s1", "analyze")]) if "RECHECK" in last else plan([], feasible=False, reason="needs code execution")
    def worker(msgs):
        return ({"action": "tool", "tool": "list_dir", "args": {}} if not tool_results(msgs)
                else final(summary="x", findings=[]))
    Script.SCRIPT = {"PLANNER": planner, "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this.", "samples/buggy_repo")
    execute(r)
    assert "refusal_overturned" in kinds(r)
    r2 = new_run("Book me a flight")                         # empty sandbox: refusal stands
    res2 = execute(r2)
    assert res2["status"] == "refused" and "refusal_challenged" not in kinds(r2)


def test_weak_evidence_is_flagged_to_the_checker():
    """A quote that exists but says nothing about the claim must be flagged for the checker."""
    seen = {}

    def worker(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_file", "args": {"path": "ticket_01.txt"}}
        return final(summary="x", findings=[
            {"title": "Database corruption detected", "detail": "the storage layer lost rows",
             "evidence": [{"file": "ticket_01.txt", "quote": "Subject: Charged twice for March invoice"}]},  # real, irrelevant
            {"title": "Customer was charged twice", "detail": "duplicate charge on the March invoice",
             "evidence": [{"file": "ticket_01.txt", "quote": "Subject: Charged twice for March invoice"}]}])  # real, relevant

    def checker(msgs):
        seen["task"] = msgs[1]["content"]
        return final(verdict="accept", feedback="ok")
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker, "CHECKER": checker,
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Triage these tickets.", "samples/notes")
    execute(r)
    assert "WEAK EVIDENCE MATCH" in seen["task"]
    assert "Database corruption detected" in seen["task"].split("WEAK EVIDENCE MATCH")[1]
    assert "Customer was charged twice" not in seen["task"].split("WEAK EVIDENCE MATCH")[1]
    assert "weak_evidence" in kinds(r)


def test_code_evidence_is_never_graded_as_weak():
    """prose claims and code lines share no words ("Hardcoded Secret" vs API_KEY = ...), so code is exempt"""
    seen = {}

    def worker(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_file", "args": {"path": "app.py"}}
        return final(summary="x", findings=[{"title": "Hardcoded Secret", "detail": "credential in source",
            "evidence": [{"file": "app.py", "quote": 'API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c"  # TODO: move to env'}]}])

    def checker(msgs):
        seen["task"] = msgs[1]["content"]
        return final(verdict="accept", feedback="ok")
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker, "CHECKER": checker,
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    execute(r)
    assert "WEAK EVIDENCE MATCH" not in seen["task"]


def test_invented_research_is_downgraded_for_plain_file_jobs():
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze"), sub("s2", "research", title="Research security issues")]),
                     "WORKER": lambda m: ({"action": "tool", "tool": "read_file", "args": {"path": "app.py"}}
                                          if not tool_results(m) else final(summary="x", findings=[])),
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this repo for bugs and security issues.", "samples/buggy_repo")
    execute(r)
    st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))["subtasks"]
    assert st["s2"]["kind"] == "analyze", st["s2"]
    # but a goal that really wants the web keeps its research subtask
    Script.SCRIPT["RESEARCHER"] = lambda m: final(summary="x", findings=[])
    Script.SCRIPT["PLANNER"] = lambda m: plan([sub("s1", "research")])
    r2 = new_run("Compare SQLite and PostgreSQL with sources.", "samples/buggy_repo")
    execute(r2)
    assert json.loads((r2.dir / "state.json").read_text(encoding="utf-8"))["subtasks"]["s1"]["kind"] == "research"


def test_pacing_never_waits_past_the_run_deadline():
    import time
    p = llm.TokenPacer(100)
    p.acquire(90)
    t = time.time()
    entry, waited = p.acquire(90, None, max_wait=1.0)      # window is full; would wait ~60 s
    assert entry is None and time.time() - t < 8, (entry, time.time() - t)


def test_a_checker_that_wanders_cannot_stall_the_run():
    """Found in the real comparison: a checker spent all 6 steps reading pages and never judged.
    It now has a 3-step cap and is forced to give a verdict on its last step."""
    calls = {"n": 0}

    def checker(msgs):
        calls["n"] += 1
        if "LAST step" in msgs[-1]["content"]:
            return final(verdict="accept", feedback="decided at the last step")
        return {"action": "tool", "tool": "read_file", "args": {"path": "app.py"}}   # keeps wandering
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]),
                     "WORKER": lambda m: (final(summary="x", findings=[{"title": "Hardcoded secret key", "detail": "api key",
                         "evidence": [{"file": "app.py", "quote": 'API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c"  # TODO: move to env'}]}])
                         if tool_results(m) else {"action": "tool", "tool": "read_file", "args": {"path": "app.py"}}),
                     "CHECKER": checker, "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    assert calls["n"] <= 3, calls
    assert res["status"] == "complete", res


def test_an_agent_that_runs_out_of_steps_is_asked_for_its_answer_not_discarded():
    """Found in a real run: a worker read files until its steps ran out, never answered, and the whole
    subtask (three source files) was lost. It now gets one last call that can only answer."""
    def worker(msgs):
        if "OUT of steps" in msgs[-1]["content"]:                       # the salvage call
            return final(summary="from what I read", findings=[{"title": "Division by length", "detail": "average divides by len(values)",
                "evidence": [{"file": "utils.py", "quote": "return sum(values) / len(values)"}]}])
        return {"action": "tool", "tool": "read_file", "args": {"path": "utils.py"}}      # never answers by itself
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    assert "salvage_answer" in kinds(r)
    assert res["status"] == "complete", res
    assert len(json.loads((r.dir / "result.json").read_text(encoding="utf-8"))["findings"]) == 1
    # and an agent that ignores even the salvage request still fails cleanly instead of looping
    Script.SCRIPT["WORKER"] = lambda m: {"action": "tool", "tool": "read_file", "args": {"path": "utils.py"}}
    r2 = new_run("Review this code.", "samples/buggy_repo")
    assert execute(r2)["status"] == "partial"


FIVE = [("Hardcoded API key", "app.py", 'API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c"  # TODO: move to env'),
        ("SQL injection in get_user", "app.py", "query = \"SELECT * FROM users WHERE name = '\" + name + \"'\""),
        ("Division by zero in average", "utils.py", "return sum(values) / len(values)"),
        ("Bare except hides errors", "utils.py", "except:"),
        ("File handle never closed", "utils.py", "f = open(path)")]


def _finding(title, file, quote):
    return {"title": title, "severity": "high", "detail": title, "evidence": [{"file": file, "quote": quote}]}


def test_a_worker_that_fills_its_quota_is_asked_for_more():
    """Found in a real run: one worker returned exactly the cap of 5 findings, so the rest were lost.
    A full answer now triggers one follow-up pass for NEW findings."""
    seen = {"follow_up_task": None, "calls": 0}

    def worker(msgs):
        task = msgs[1]["content"]
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_files", "args": {"paths": ["app.py", "utils.py"]}}
        if "ALREADY REPORTED" in task:
            seen["follow_up_task"] = task
            seen["calls"] += 1
            return final(summary="more", findings=[_finding("Boundary check in is_adult", "utils.py", "return age > 18")])
        return final(summary="first pass", findings=[_finding(*f) for f in FIVE])     # exactly the cap
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    result = json.loads((r.dir / "result.json").read_text(encoding="utf-8"))
    assert len(result["findings"]) == 6, [f["title"] for f in result["findings"]]
    assert "Hardcoded API key" in seen["follow_up_task"]            # told what is already reported
    assert "continuing_after_cap" in kinds(r) and res["status"] == "complete"


def test_follow_up_that_finds_nothing_new_stops_without_another_check():
    def worker(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_files", "args": {"paths": ["app.py", "utils.py"]}}
        if "ALREADY REPORTED" in msgs[1]["content"]:
            return final(summary="nothing more", findings=[])
        return final(summary="first", findings=[_finding(*f) for f in FIVE])
    checks = {"n": 0}

    def checker(msgs):
        checks["n"] += 1
        return final(verdict="accept", feedback="ok")
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker, "CHECKER": checker,
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    assert len(json.loads((r.dir / "result.json").read_text(encoding="utf-8"))["findings"]) == 5
    assert "no_more_findings" in kinds(r) and checks["n"] == 1, checks      # the checker ran once, for the first pass
    assert res["status"] == "complete"


def test_tool_arguments_are_accepted_beside_the_tool_name_too():
    """Found in a real run: {"tool":"read_files","paths":[...]} (no "args") was rejected twice, wasting steps."""
    got = {}

    def worker(msgs):
        res = tool_results(msgs)
        if not res:
            return {"action": "tool", "tool": "read_files", "paths": ["utils.py"]}        # no "args" wrapper
        got["first"] = res[0]
        return final(summary="x", findings=[])
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    execute(new_run("Review this code.", "samples/buggy_repo"))
    assert "=== utils.py" in got["first"] and "ERROR" not in got["first"], got["first"][:200]
    sb = tools.Sandbox(ROOT / "samples/buggy_repo")
    assert "=== utils.py" in tools.read_files(sb, {"files": ["utils.py"]})              # alias spellings work
    assert "=== utils.py" in tools.read_files(sb, {"path": "utils.py"})


def test_limits_that_spend_model_calls_are_the_original_conservative_ones():
    """Only non-LLM caps (downloads, uploads, file sizes, search, network timeouts) and the time ceiling may be
    generous. Anything that spends model calls or tokens stays under the provider-friendly originals."""
    d = Limits()
    assert (d.max_llm_calls, d.max_tokens, d.agent_max_steps, d.agent_max_tokens) == (40, 150_000, 6, 40_000)
    assert (d.max_subtasks, d.max_check_loops, d.concurrency) == (3, 2, 2)
    assert not hasattr(d, "max_followups")        # follow-ups share the attempt budget, they add no calls


def test_the_15_minute_ceiling_holds_whatever_is_asked_for(monkeypatch=None):
    import os
    from harness.config import HARD_TIME_LIMIT
    assert HARD_TIME_LIMIT == 900.0
    L = Limits.from_env()
    assert L.max_seconds <= 900 and L.agent_max_seconds <= L.max_seconds
    os.environ["HARNESS_MAX_SECONDS"] = "7200"
    os.environ["HARNESS_AGENT_MAX_SECONDS"] = "7200"
    try:
        L2 = Limits.from_env()
        assert L2.max_seconds == 900 and L2.agent_max_seconds == 900, (L2.max_seconds, L2.agent_max_seconds)
    finally:
        del os.environ["HARNESS_MAX_SECONDS"], os.environ["HARNESS_AGENT_MAX_SECONDS"]
    import server
    assert server.clamp(5000, 10.0, HARD_TIME_LIMIT, 900.0) == 900.0       # the web form cannot exceed it either


def test_follow_up_passes_never_exceed_the_original_attempt_budget():
    """Asking for more findings shares the same attempts as checker rejections, so it can never add
    model calls beyond the limits that existed before: a worker that keeps filling its quota stops at 3 attempts."""
    state = {"n": 0}

    def worker(msgs):
        if not tool_results(msgs):
            return {"action": "tool", "tool": "read_files", "args": {"paths": ["app.py", "utils.py"]}}
        if "ALREADY REPORTED" in msgs[1]["content"]:
            state["n"] += 1
            titles = [f"Extra issue {state['n']}-{i}" for i in range(5)]       # always a full batch: it would never stop
            lines = ["import json", "def average(values):", "return sum(values) / len(values)", "def load_config(path):", "f = open(path)"]
            return final(summary="more", findings=[_finding(t, "utils.py", q) for t, q in zip(titles, lines)])
        return final(summary="first", findings=[_finding(*f) for f in FIVE])
    Script.SCRIPT = {"PLANNER": lambda m: plan([sub("s1", "analyze")]), "WORKER": worker,
                     "CHECKER": lambda m: final(verdict="accept", feedback="ok"),
                     "ASSEMBLER": lambda m: final(summary="s", priority_order=[])}
    r = new_run("Review this code.", "samples/buggy_repo")
    res = execute(r)
    k = kinds(r)
    st = json.loads((r.dir / "state.json").read_text(encoding="utf-8"))["subtasks"]["s1"]
    assert st["attempts"] == 3, st["attempts"]                       # 1 + max_check_loops(2): the same as before
    assert k.count("continuing_after_cap") == 2, k.count("continuing_after_cap")
    assert res["status"] == "complete" and st["status"] == "accepted" and len(st["findings"]) == 15, len(st["findings"])


def test_all_roles_have_least_privilege():
    from harness.agents import SPECS_TOOLS as T
    assert "edit_file" not in T["worker"] and "edit_file" not in T["researcher"] and "edit_file" not in T["checker"]
    assert "fetch_url" not in T["worker"] and "web_search" not in T["worker"]
    assert "web_search" in T["researcher"] and "github_fetch" not in T["researcher"]
    assert T["assembler"] == [] and T["planner"] == ["list_dir"]


def test_web_text_keeps_sentences_whole_and_natural_quotes_verify():
    """Found in a real run: every link was put on its own line, so a correctly copied sentence
    failed the exact-match check and good findings were rejected."""
    from harness.orchestrator import _norm
    html = ("<html><body><h1>Ayurveda</h1><p>Therapies include <a href='x'>herbal medicines</a>, "
            "<a href='y'>special diets</a>, <b>meditation</b>,<sup>[12]</sup> and massage.</p>"
            "<script>var x = 1;</script><ul><li>First item</li><li>Second item</li></ul></body></html>")
    p = tools._Text()
    p.feed(html)
    lines = p.parts
    assert lines[1] == "Therapies include herbal medicines, special diets, meditation,[12] and massage.", lines
    assert "var x" not in " ".join(lines) and lines[-2:] == ["First item", "Second item"], lines
    page = "\n".join(lines)
    for natural in ("Therapies include herbal medicines, special diets, meditation, and massage.",   # no [12]
                    "Therapies include herbal medicines , special diets",                         # odd spacing
                    "“First item”".strip("“”")):
        assert _norm(natural) in _norm(page), natural
    assert _norm("Therapies include herbal medicines and surgery") not in _norm(page)      # words must still match


def test_rule_scan_finds_secrets_whatever_the_variable_is_called():
    """Found while building a fresh test project: ADMIN_PASSWORD and SECRET_KEY were missed because the
    pattern wanted the name to be exactly 'password' or 'api_key'."""
    from harness import staticscan
    d = Path(tempfile.mkdtemp())
    (d / "cfg.py").write_text(
        'ADMIN_PASSWORD = "Library#2024!"\nSECRET_KEY = "7d9f1c0e5b3a48f2a6c1e9d4b8f03a52"\n'
        'db_passwd = "hunter2hunter2"\nAWS_SECRET_ACCESS_KEY = "abcd1234abcd1234abcd"\n'
        'token = os.environ["TOKEN"]\nPASSWORD_HINT = "your_password_here"\nimport hashlib\nh = hashlib.md5(b"x")\n',
        encoding="utf-8")
    found = staticscan.scan_tree(d, d, lambda f: f.name)
    secret_lines = sorted(ln for _, ln, _, rule, _, _ in found if rule == "hardcoded-secret")
    assert secret_lines == [1, 2, 3, 4], found           # real secrets caught; env lookup and placeholder are not
    assert any(rule == "py-weak-hash" for _, _, _, rule, _, _ in found)


def test_tools_are_safe():
    sb = tools.Sandbox(tempfile.mkdtemp())
    for bad in ("__import__('os').system('x')", "open('/etc/passwd')", "2**99999", "().__class__"):
        try:
            tools.calculate(sb, {"expression": bad})
            raise AssertionError("accepted " + bad)
        except tools.ToolError:
            pass
    assert "= 7.0" in tools.calculate(sb, {"expression": "sqrt(49)"})
    (sb.root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    assert "=== a.txt (2 lines) ===" in tools.read_files(sb, {"paths": ["a.txt", "nope.txt"]})
    try:
        tools.read_files(sb, {"paths": ["../../etc/passwd"]})
    except tools.ToolError:
        raise AssertionError("read_files should report per-file errors, not raise")
    assert "escapes the sandbox" in tools.read_files(sb, {"paths": ["../../etc/passwd"]})


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    saved = {k: v.fn for k, v in tools.TOOLS.items()}
    for name, fn in tests:
        for k, v in saved.items():
            tools.TOOLS[k].fn = v          # undo fake web tools between tests
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
