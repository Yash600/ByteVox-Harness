"""Single-agent baseline: ONE agent, ONE prompt, the SAME tools and the same model.

What it deliberately does NOT have: a planner, parallel workers, a checker, a rejection loop,
duplicate merging, or an assembler. Everything else (budgets, retries, rate-limit pacing, the
tool sandbox, the log) is shared so the comparison isolates the multi-agent structure.

Its quotes are verified AFTER the fact, only so the rubric can measure how many are real.
Nothing is filtered or labelled: the baseline presents whatever the model said.
"""
import json

from .agents import run_agent
from .budget import BudgetExceeded
from .orchestrator import _listing, ingest_github, render_report, verify_findings, workspace_diff
from .runtime import AgentFailed, Run

BASELINE_STEPS = 14     # one agent must do the work of the whole team, so it gets more steps


def run_baseline(run: Run):
    log = run.logger.log
    run.limits.agent_max_steps = max(run.limits.agent_max_steps, BASELINE_STEPS)
    run.limits.agent_max_tokens = max(run.limits.agent_max_tokens, run.limits.max_tokens)
    log("orchestrator", "run_start", goal=run.goal, mode="single-agent baseline",
        provider=run.router.primary, limits=vars(run.limits))
    reasons, findings, summary, status = [], [], "", "complete"
    err = ingest_github(run)
    if err:
        reasons, status = [err], "failed"
    else:
        task = f"GOAL: {run.goal}\nFILES IN THE SANDBOX (root is '.'):\n{_listing(run)}"
        try:
            out = run_agent(run, "solo", "solo", task)
            findings, summary = out.get("findings", []), out.get("summary", "")
        except (BudgetExceeded, AgentFailed) as e:
            reasons, status = [f"the single agent failed: {e}"], "failed"
    verify_findings(run, findings)            # measurement only; nothing is removed or flagged
    for i, f in enumerate(findings, 1):
        f["id"] = f"solo.f{i}"
        f["subtask"] = "solo"
        f["checked"] = False
    run.state.update_subtask("solo", status="accepted" if status == "complete" else "failed",
                             title="Single agent does everything", kind="solo", findings=findings,
                             summary=summary, attempts=1)
    snap = run.state.snapshot()
    report = render_report(run, status, reasons, summary, findings, snap)
    (run.dir / "report.md").write_text(report, encoding="utf-8")
    (run.dir / "result.json").write_text(json.dumps(
        {"status": status, "reasons": reasons, "summary": summary, "findings": findings, "mode": "baseline"},
        indent=2, ensure_ascii=False), encoding="utf-8")
    (run.dir / "diff.patch").write_text(workspace_diff(run), encoding="utf-8")
    run.state.set("status", status)
    run.state.set("budget", run.budget.snapshot())
    run.state.set("stats", run.stats)
    log("orchestrator", "run_end", status=status, reasons=reasons, budget=run.budget.snapshot(),
        stats=run.stats)
    return {"status": status, "reasons": reasons, "findings": findings, "summary": summary,
            "dir": str(run.dir)}
