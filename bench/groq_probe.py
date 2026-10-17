"""Probe which Groq model/setting reliably returns our JSON protocol on a real worker prompt."""
import json, os, sys, time, urllib.request, urllib.error
sys.path.insert(0, ".")
from harness.agents import worker_prompt

class R: limits = type("L", (), {"max_subtasks": 3})()
SYSTEM = worker_prompt(R)
TASK = ("SUBTASK s1: Analyze app.py\nOVERALL GOAL: Review for bugs\nINSTRUCTIONS: Read app.py and report bugs.\n"
        "FILES IN THE SANDBOX:\napp.py  (479 bytes)\nutils.py  (300 bytes)")

def call(model, extra, sys_prompt=SYSTEM):
    p = {"model": model, "temperature": 0.2, "messages": [
        {"role": "system", "content": sys_prompt}, {"role": "user", "content": TASK}], **extra}
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", json.dumps(p).encode(),
        {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["GROQ_API_KEY"], "User-Agent": "bytevox-harness/0.1"})
    t = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=60))
        txt = d["choices"][0]["message"].get("content") or ""
        try: j = json.loads(txt[txt.find("{"):txt.rfind("}")+1]); ok = j.get("action") in ("tool", "final")
        except Exception: ok = False
        return ("OK " if ok else "BADJSON ") + txt[:90].replace("\n", " "), time.time()-t
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        code = json.loads(b)["error"].get("code") if b.startswith("{") else e.code
        return f"HTTP{e.code} {code}", time.time()-t

variants = {
  "gpt-oss-120b default": ("openai/gpt-oss-120b", {}),
  "gpt-oss-120b effort=low": ("openai/gpt-oss-120b", {"reasoning_effort": "low"}),
  "gpt-oss-120b effort=low+hidden": ("openai/gpt-oss-120b", {"reasoning_effort": "low", "include_reasoning": False}),
  "gpt-oss-20b effort=low": ("openai/gpt-oss-20b", {"reasoning_effort": "low"}),
  "qwen3.8-27b": ("qwen/qwen3.8-27b", {}),
  "qwen3.8-27b no-think": ("qwen/qwen3.8-27b", {"reasoning_effort": "none"}),
}
for name, (m, extra) in variants.items():
    res = [call(m, extra) for _ in range(3)]
    print(f"{name:34} " + " | ".join(f"{r[0][:70]} ({r[1]:.1f}s)" for r in res), flush=True)
