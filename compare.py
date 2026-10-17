"""Multi-agent harness vs single-agent baseline: same tasks, same model, same tools, same rubric.

    python compare.py                          # all 3 tasks, both versions, hosted model
    python compare.py --tasks triage           # one task
    python compare.py --versions multi         # only one version
    python compare.py --force                  # rerun even if a result is already saved

Each (task, version) is run ONCE and saved to comparison/results.json, so re-running the script
never spends tokens on a run that already has a result. It stops at the first sign that the
provider's quota is gone instead of burning calls on failures.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from harness import rubric
from harness.baseline import run_baseline
from harness.config import HARD_TIME_LIMIT, Limits
from harness.llm import Router
from harness.orchestrator import execute
from harness.runtime import Run

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "comparison"


def run_one(task, version, router, limits, runs_root="runs"):
    """Run one task with one version; return a result dict with metrics and rubric score."""
    spec = rubric.TASKS[task]
    run_id = f"cmp_{task}_{version}_{time.strftime('%H%M%S')}"
    run = Run(spec["goal"], input_dir=(ROOT / spec["input"]) if spec["input"] else None,
              runs_root=runs_root, run_id=run_id, limits=limits, router=router, approval_mode="deny")
    t0 = time.time()
    res = execute(run) if version == "multi" else run_baseline(run)
    wall = time.time() - t0
    result = json.loads((run.dir / "result.json").read_text(encoding="utf-8"))
    findings = result.get("findings", [])
    b = run.budget.snapshot()
    return {
        "task": task, "version": version, "run_id": run_id, "status": res["status"],
        "seconds": round(wall, 1), "llm_calls": b["calls"], "tokens": b["tokens"],
        "retries": run.stats["retries"], "llm_errors": run.stats["llm_errors"],
        "fallbacks": run.stats.get("fallbacks", 0),
        "reasons": res.get("reasons", [])[:3],
        "score": rubric.score(task, findings),
        "quota_down": router.is_down(router.primary),
    }


def render(results):
    L = ["# Multi-agent harness vs single-agent baseline\n",
         "Same model, same tools, same tasks, same code-computed rubric. One run per cell. "
         "Cost is shown as LLM calls and tokens: the hosted model runs on a free tier, so dollars are $0.\n",
         "| Task | Version | Recall /5 | Grounded /5 | Distinct /5 | **Total /15** | Findings | Seconds | Calls | Tokens | Status |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for task in rubric.TASKS:
        for v, label in (("multi", "multi-agent"), ("solo", "single agent")):
            r = results.get(f"{task}/{v}")
            if not r:
                L.append(f"| {task} | {label} | - | - | - | - | - | - | - | - | not run |")
                continue
            p = r["score"]["points"]
            L.append(f"| {task} | {label} | {p['recall']} | {p['grounded']} | {p['distinct']} | "
                     f"**{r['score']['total']}** | {r['score']['n_findings']} | {r['seconds']} | "
                     f"{r['llm_calls']} | {r['tokens']:,} | {r['status']} |")
    L.append("\n## What each version found\n")
    for task, spec in rubric.TASKS.items():
        L.append(f"### {spec['title']}")
        for v, label in (("multi", "multi-agent"), ("solo", "single agent")):
            r = results.get(f"{task}/{v}")
            if r:
                d = r["score"]["detail"]
                missed = d["missed"] if isinstance(d["missed"], list) else [f"{k}: {x}" for k, x in d["missed"].items()]
                L.append(f"- **{label}** found: {', '.join(d['found']) or 'nothing'}. "
                         f"Missed: {', '.join(missed) or 'nothing'}.")
        L.append("")
    both = [(t, results.get(f"{t}/multi"), results.get(f"{t}/solo")) for t in rubric.TASKS]
    both = [(t, m, s) for t, m, s in both if m and s]
    if both:
        L.append("## Reading the numbers\n")
        mt = sum(m["tokens"] for _, m, _ in both)
        st = sum(s["tokens"] for _, _, s in both)
        ms = sum(m["seconds"] for _, m, _ in both)
        ss = sum(s["seconds"] for _, _, s in both)
        mq = sum(m["score"]["total"] for _, m, _ in both)
        sq = sum(s["score"]["total"] for _, _, s in both)
        L.append(f"- Quality (sum of totals): multi-agent **{mq:.1f}** vs single agent **{sq:.1f}** "
                 f"(out of {15 * len(both)}).")
        L.append(f"- Tokens: multi-agent {mt:,} vs single agent {st:,} "
                 f"({mt / max(st, 1):.1f}x). Time: {ms:.0f}s vs {ss:.0f}s ({ms / max(ss, 1):.1f}x).")
        for t, m, s in both:
            dq = m["score"]["total"] - s["score"]["total"]
            tok = m["tokens"] / max(s["tokens"], 1)
            verdict = ("multi-agent clearly better" if dq >= 2 else
                       "roughly equal quality" if abs(dq) < 2 else "single agent better")
            note = f" at {tok:.1f}x the tokens" if tok > 1.2 else ""
            L.append(f"- **{t}**: {verdict} ({m['score']['total']} vs {s['score']['total']}){note}.")
    L.append("\n## Caveats\n")
    L += ["- One run per cell: model output varies run to run, so small gaps are noise.",
          "- Recall uses planted ground truth and counts a finding only if its quote really exists in the source.",
          "- Time includes waiting on the free tier's rate limits, which hits the multi-agent run harder "
          "because several agents share one token budget.",
          "- The rubric scores findings, not the prose summary, and the research check is keyword-based.",
          "- The single agent gets the same agent-loop rule as the team (it may not answer before reading anything) "
          "so the comparison isolates the multi-agent structure."]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="code,triage,research")
    ap.add_argument("--versions", default="multi,solo")
    ap.add_argument("--provider", default="groq", choices=["groq", "ollama"])
    ap.add_argument("--max-seconds", type=float, default=900)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--tag", default="", help="saves to results_<tag>.json / COMPARISON_<tag>.md")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    OUT.mkdir(exist_ok=True)
    sfx = f"_{a.tag}" if a.tag else ""
    path = OUT / f"results{sfx}.json"
    md_path = OUT / f"COMPARISON{sfx}.md"
    results = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for task in a.tasks.split(","):
        for version in a.versions.split(","):
            key = f"{task}/{version}"
            if key in results and not a.force:
                print(f"skip {key}: already have a result (use --force to rerun)")
                continue
            limits = Limits.from_env()
            limits.max_seconds, limits.max_llm_calls, limits.max_tokens = min(a.max_seconds, HARD_TIME_LIMIT), 60, 120_000
            limits.agent_max_seconds = min(limits.agent_max_seconds, limits.max_seconds)
            router = Router(provider=a.provider, fallback=None)      # no fallback: one model for both
            print(f"\n=== {key} ===", flush=True)
            r = run_one(task, version, router, limits)
            results[key] = r
            path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            md_path.write_text(render(results), encoding="utf-8")
            s = r["score"]
            print(f"{r['status']}: score {s['total']}/15 (recall {s['points']['recall']}, grounded "
                  f"{s['points']['grounded']}, distinct {s['points']['distinct']}), {r['llm_calls']} calls, "
                  f"{r['tokens']:,} tokens, {r['seconds']}s", flush=True)
            if r["quota_down"]:
                print("The provider reported its quota is used up. Stopping so no more calls are wasted.")
                return
    md_path.write_text(render(results), encoding="utf-8")
    print(f"\nwrote {md_path}")


if __name__ == "__main__":
    main()
