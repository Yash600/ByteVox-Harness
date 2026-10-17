"""The comparison plumbing, tested with a scripted model so it costs zero tokens."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_flows as T  # noqa: E402  (installs the scripted provider)
import compare  # noqa: E402
from harness import rubric  # noqa: E402
from harness.config import Limits  # noqa: E402
from harness.llm import Router  # noqa: E402


def script_for_triage():
    def worker(msgs):
        if not T.tool_results(msgs):
            return {"action": "tool", "tool": "read_files", "args": {"paths": ["ticket_01.txt", "ticket_02.txt", "ticket_03.txt"]}}
        q = {"ticket_01.txt": ("Billing: charged twice", "Please refund the duplicate charge."),
             "ticket_02.txt": ("Feature request: CSV export", "Not urgent, just a nice to have."),
             "ticket_03.txt": ("Login outage", "Our whole team is blocked.")}
        sev = {"ticket_01.txt": "high", "ticket_02.txt": "low", "ticket_03.txt": "high"}
        return T.final(summary="triaged", findings=[{"title": t, "severity": sev[f], "detail": t, "category": "x",
                                                      "evidence": [{"file": f, "quote": quote}]} for f, (t, quote) in q.items()])
    T.Script.SCRIPT = {
        "PLANNER": lambda m: T.plan([T.sub("s1", "analyze", instr="Read ticket_01.txt ticket_02.txt ticket_03.txt")]),
        "WORKER": worker, "SOLO": None,
        "CHECKER": lambda m: T.final(verdict="accept", feedback="ok"),
        "ASSEMBLER": lambda m: T.final(summary="done", priority_order=[])}
    # the single agent uses the same worker behaviour
    T.Script.SCRIPT["SOLO"] = worker


def test_both_versions_run_score_and_render():
    script_for_triage()
    # the scripted model recognises roles by their prompt; teach it the solo prompt too
    orig = T.Script.chat

    def chat(self, model, messages, json_mode, timeout):
        if "You are a single AI agent" in messages[0]["content"]:
            out = T.Script.SCRIPT["SOLO"](messages)
            return T.LLMResult(json.dumps(out), 20, 20, 0.01, "script", "script")
        return orig(self, model, messages, json_mode, timeout)
    T.Script.chat = chat
    try:
        root = tempfile.mkdtemp()
        results = {}
        for v in ("multi", "solo"):
            r = compare.run_one("triage", v, Router(provider="script", fallback=None), Limits.from_env(), runs_root=root)
            results[f"triage/{v}"] = r
            assert r["score"]["recall"] == 1.0 and r["score"]["grounded"] == 1.0, (v, r["score"])
            assert r["llm_calls"] > 0 and r["tokens"] > 0
        md = compare.render(results)
        assert "| triage | multi-agent |" in md and "| triage | single agent |" in md and "Caveats" in md
        assert "| code | multi-agent | - |" in md      # tasks that were not run say so, they are not invented
    finally:
        T.Script.chat = orig


if __name__ == "__main__":
    try:
        test_both_versions_run_score_and_render()
        print("PASS  test_both_versions_run_score_and_render")
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
