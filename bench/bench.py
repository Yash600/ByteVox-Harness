"""Quick Ollama benchmark: speed, JSON reliability, and a planted-bug sanity check.

Usage: python bench.py qwen2.5:3b llama3.2:3b
"""
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:11434/api/chat"

PLAN_PROMPT = (
    "You are a planner. Goal: 'Review the Python repo in ./app for issues.' "
    "Split it into 3 independent subtasks. Reply ONLY with JSON of the form "
    '{"subtasks":[{"id":"s1","title":"...","focus":"..."}]}'
)

BUGGY = '''def get_user(db, name):
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return db.execute(query)

def avg(xs):
    return sum(xs) / len(xs)
'''

REVIEW_PROMPT = (
    "Review this code. Reply ONLY with JSON "
    '{"findings":[{"line":0,"issue":"...","severity":"high|medium|low","evidence":"..."}]}\n\n'
    "```python\n" + BUGGY + "```"
)


def chat(model, prompt, timeout=300):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d, time.time() - t0


def bench(model, runs=10):
    print(f"\n=== {model} ===")
    chat(model, "hi")  # warm-up (loads model)

    ok, speeds, secs = 0, [], []
    for i in range(runs):
        d, dt = chat(model, PLAN_PROMPT)
        try:
            j = json.loads(d["message"]["content"])
            good = isinstance(j.get("subtasks"), list) and len(j["subtasks"]) >= 2 and all(
                "title" in s for s in j["subtasks"])
        except Exception:
            good = False
        ok += good
        tps = d.get("eval_count", 0) / max(d.get("eval_duration", 1) / 1e9, 1e-9)
        speeds.append(tps)
        secs.append(dt)
        print(f"  plan run {i+1}: valid={good} {tps:.1f} tok/s {dt:.1f}s")

    d, dt = chat(model, REVIEW_PROMPT)
    txt = d["message"]["content"]
    found_sql = "sql" in txt.lower() or "inject" in txt.lower()
    found_div = "zero" in txt.lower() or "empty" in txt.lower() or "len" in txt.lower()
    print(f"  review ({dt:.1f}s): SQL injection found={found_sql}, div-by-zero found={found_div}")
    print(f"  review output: {txt[:300]}")
    print(f"  SUMMARY {model}: JSON valid {ok}/{runs}, avg {sum(speeds)/len(speeds):.1f} tok/s, "
          f"avg plan call {sum(secs)/len(secs):.1f}s")


if __name__ == "__main__":
    for m in sys.argv[1:]:
        bench(m)
