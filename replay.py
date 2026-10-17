"""Read a past run's trace.   python replay.py <run_id> [--full]"""
import sys
from pathlib import Path

from harness.logger import read_trace


def main():
    if len(sys.argv) < 2:
        runs = sorted(p.name for p in Path("runs").iterdir() if p.is_dir())
        print("usage: python replay.py <run_id> [--full]\nruns:", *runs, sep="\n  ")
        return
    full = "--full" in sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for ev in read_trace(Path("runs") / sys.argv[1] / "trace.jsonl"):
        k = ev["kind"]
        head = f"{ev['seq']:>3} [{ev['t']:7.1f}s] {ev['agent']:<16} {k:<18}"
        if k == "llm_call":
            body = f"{ev['model']} in={ev['input_tokens']} out={ev['output_tokens']} {ev['seconds']}s"
            if full:
                body += "\n      PROMPT(last): " + ev["messages"][-1]["content"][:600] + \
                        "\n      OUTPUT: " + ev["output"][:600]
        elif k == "tool_call":
            body = f"{ev['tool']} {ev['args']} ok={ev['ok']} -> {ev['result'][:100]!r}"
        else:
            body = ", ".join(f"{x}={str(v)[:100]}" for x, v in ev.items()
                             if x not in ("seq", "t", "ts", "run_id", "agent", "kind", "messages",
                                          "limits"))
        print(head, body)


if __name__ == "__main__":
    main()
