"""CLI entry point.  Example:
    python run.py "Review this repo for bugs and security issues" --input samples/buggy_repo
"""
import argparse
import sys

from harness import config
from harness.config import HARD_TIME_LIMIT, Limits
from harness.llm import Router
from harness.orchestrator import execute
from harness.runtime import Run

QUIET = {"llm_call", "agent_start"}


def live(ev):
    k, a = ev["kind"], ev["agent"]
    if k == "llm_call":
        line = f"llm {ev['model']} {ev['input_tokens']}+{ev['output_tokens']} tok {ev['seconds']}s"
    elif k == "tool_call":
        line = f"tool {ev['tool']} {ev['args']} ok={ev['ok']}"
    elif k in ("llm_error", "retry_wait", "tool_denied", "injection_flagged", "budget_exceeded",
               "verdict", "evidence_check", "invalid_output", "fallback", "approval_result",
               "subtask_failed", "loop_limit", "refused", "run_end", "plan"):
        line = f"{k.upper()} " + ", ".join(f"{x}={str(v)[:90]}" for x, v in ev.items()
                                          if x not in ("seq", "t", "ts", "run_id", "agent", "kind"))
    else:
        return
    print(f"[{ev['t']:7.1f}s] {a:<16} {line}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("goal")
    ap.add_argument("--input", help="folder the agents may read/edit (copied into the run)")
    ap.add_argument("--provider", choices=["ollama", "groq"])
    ap.add_argument("--fallback", choices=["ollama", "groq"])
    ap.add_argument("--approve", choices=["ask", "approve", "deny"], default="ask")
    ap.add_argument("--chaos", type=float, default=0.0, help="fraction of LLM calls to fail on purpose")
    ap.add_argument("--max-llm-calls", type=int)
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--run-id")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    limits = Limits.from_env()
    for arg, field in [(a.max_llm_calls, "max_llm_calls"), (a.max_seconds, "max_seconds"),
                       (a.max_tokens, "max_tokens"), (a.concurrency, "concurrency")]:
        if arg is not None:
            setattr(limits, field, arg)
    if limits.max_seconds > HARD_TIME_LIMIT:
        print(f"note: the time limit is capped at {HARD_TIME_LIMIT:.0f} s (15 minutes)")
    limits.max_seconds = min(limits.max_seconds, HARD_TIME_LIMIT)
    limits.agent_max_seconds = min(limits.agent_max_seconds, limits.max_seconds)

    router = Router(provider=a.provider, fallback=a.fallback, chaos_rate=a.chaos, seed=7)
    run = Run(a.goal, input_dir=a.input, runs_root="runs", run_id=a.run_id, limits=limits,
              router=router, approval_mode=a.approve)
    run.logger.listeners.append(live)
    print(f"run {run.run_id}  provider={router.primary}  dir={run.dir}\n")
    res = execute(run)
    print("\n" + "=" * 70 + f"\nSTATUS: {res['status'].upper()}")
    for r in res["reasons"]:
        print("  -", r)
    print(f"report: {run.dir / 'report.md'}\ntrace:  {run.logger.path}")


if __name__ == "__main__":
    main()
