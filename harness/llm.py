"""Model adapters. The rest of the harness only sees Router.chat(); swapping providers is a
one-env-var change (LLM_PROVIDER=ollama|groq). Stdlib only."""
import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config


class LLMError(Exception):
    def __init__(self, msg, retryable=False, retry_after=None, rate_limited=False, quota=False):
        super().__init__(msg)
        self.quota = quota
        self.retryable = retryable
        self.retry_after = retry_after
        self.rate_limited = rate_limited


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    seconds: float
    model: str
    provider: str


GEN_FAIL_CODES = ("output_parse_failed", "tool_use_failed", "json_validate_failed")


def _post(url, payload, headers, timeout):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                             "User-Agent": "bytevox-harness/0.1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1200]
        ra = e.headers.get("Retry-After")
        try:
            ra = float(ra) if ra else None
        except ValueError:
            ra = None
        if ra is None:  # Groq also puts the wait in the message: "try again in 9.87s"
            m = re.search(r"try again in ([\d.]+)\s*(ms|s)", detail)
            if m:
                ra = float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)
        retryable = e.code in (408, 409, 425, 429) or e.code >= 500
        quota = e.code == 429 and bool(re.search(r"per day|\(TPD\)|\(RPD\)", detail))
        if quota:   # a daily cap resets in minutes-to-hours: retrying every minute is pointless
            retryable = False
        # model-side generation failures (bad JSON / stray native tool call): a retry can succeed
        if e.code == 400 and any(c in detail for c in GEN_FAIL_CODES):
            retryable = True
        raise LLMError(("DAILY QUOTA USED UP. " if quota else "") + f"HTTP {e.code}: {detail}", retryable, ra,
                       rate_limited=(e.code == 429 and not quota), quota=quota)
    except (socket.timeout, TimeoutError):
        raise LLMError(f"timeout after {timeout}s", retryable=True)
    except urllib.error.URLError as e:
        raise LLMError(f"connection error: {e.reason}", retryable=True)


class OllamaProvider:
    name = "ollama"

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host

    def chat(self, model, messages, json_mode, timeout):
        t0 = time.time()
        payload = {"model": model, "messages": messages, "stream": False,
                   "options": {"temperature": 0, "num_ctx": 16384}}
        if json_mode:
            payload["format"] = "json"
        d = _post(self.host + "/api/chat", payload, {}, timeout)
        return LLMResult(d["message"]["content"], d.get("prompt_eval_count", 0),
                         d.get("eval_count", 0), time.time() - t0, model, self.name)


class GroqProvider:
    name = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def chat(self, model, messages, json_mode, timeout):
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not set", retryable=False)
        t0 = time.time()
        # Groq's strict json_object mode breaks reasoning models (gpt-oss), so it is off by
        # default; the harness validates and repairs JSON itself. GROQ_JSON_MODE=1 re-enables.
        payload = {"model": model, "messages": messages,
                   "temperature": float(os.environ.get("GROQ_TEMPERATURE", "0.2"))}
        if json_mode and os.environ.get("GROQ_JSON_MODE") == "1":
            payload["response_format"] = {"type": "json_object"}
        # Free tier caps OUTPUT tokens/min (1000 on qwen3.8-27b) and rejects any request whose
        # expected output exceeds it, so always set an explicit, smaller max_tokens.
        payload["max_tokens"] = int(os.environ.get("GROQ_MAX_TOKENS", "900"))
        effort = os.environ.get("GROQ_REASONING_EFFORT") or ("none" if model.startswith("qwen/") else None)
        if effort:
            payload["reasoning_effort"] = effort
        headers = {"Authorization": f"Bearer {key}"}
        try:
            d = _post(self.URL, payload, headers, timeout)
        except LLMError as e:
            # some models reject json mode; retry once without it (prompt still demands JSON)
            if json_mode and "response_format" in str(e):
                payload.pop("response_format")
                d = _post(self.URL, payload, headers, timeout)
            else:
                raise
        u = d.get("usage", {})
        text = d["choices"][0]["message"].get("content") or ""
        return LLMResult(text, u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                         time.time() - t0, model, self.name)


class TokenPacer:
    """Client-side limiter for a provider's input-tokens-per-minute cap. Waiting politely
    before a call is cheaper than being rejected with 429 and retrying."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._log = []          # [time, tokens] entries from the last 60 s

    def acquire(self, est, cancel=None, max_wait=None):
        waited = 0.0
        while True:
            if max_wait is not None and waited >= max_wait:
                return None, waited      # out of time: stop waiting, the caller's budget check ends the run
            with self._lock:
                now = time.time()
                self._log = [e for e in self._log if now - e[0] < 60]
                used = sum(e[1] for e in self._log)
                if not self._log or used + est <= self.per_minute:
                    entry = [now, est]
                    self._log.append(entry)
                    return entry, waited
                delay = max(0.5, 60 - (now - self._log[0][0]) + 0.1)
            if cancel is not None and cancel.wait(min(delay, 5)):
                return None, waited
            elif cancel is None:
                time.sleep(min(delay, 5))
            waited += min(delay, 5)

    def settle(self, entry, actual):
        if entry is not None and actual:
            with self._lock:
                entry[1] = actual


def estimate_tokens(messages):
    return int(sum(len(m["content"]) for m in messages) / 2.3) + 50   # measured 1.8-3.9 chars/token on code+JSON


PROVIDERS = {"ollama": OllamaProvider, "groq": GroqProvider}


class Router:
    """Picks provider+model per role, and can inject failures to test the harness."""

    def __init__(self, provider=None, fallback=None, chaos_rate=0.0, seed=None):
        self.primary = provider or config.provider_name()
        self.fallback = fallback if fallback is not None else config.fallback_provider()
        if self.fallback == self.primary:
            self.fallback = None
        self.chaos_rate = chaos_rate
        self._rng = random.Random(seed)
        self._instances = {}
        self._down = {}
        # Groq free tier: ~7000 input tokens/min per model; stay a little under it.
        itpm = int(os.environ.get("GROQ_ITPM", "6000"))
        self.pacers = {"groq": TokenPacer(itpm)} if itpm > 0 else {}
        otpm = int(os.environ.get("GROQ_OTPM", "850"))        # output tokens/min (cap is 1000)
        self.out_pacers = {"groq": TokenPacer(otpm)} if otpm > 0 else {}

    def pace(self, provider, messages, cancel=None, max_wait=None):
        """Wait until both the input and output tokens-per-minute windows have room (but never
        longer than max_wait seconds: the run's own time limit comes first)."""
        ip, op = self.pacers.get(provider), self.out_pacers.get(provider)
        if not ip and not op:
            return None, 0.0
        waited, ie, oe = 0.0, None, None
        if ip:
            ie, w = ip.acquire(estimate_tokens(messages), cancel, max_wait)
            waited += w
        if op:
            oe, w = op.acquire(300, cancel, None if max_wait is None else max(0.0, max_wait - waited))
            waited += w
        return (ip, ie, op, oe), waited

    def settle(self, token, in_tokens, out_tokens=0):
        if token:
            ip, ie, op, oe = token
            if ip:
                ip.settle(ie, in_tokens)
            if op:
                op.settle(oe, out_tokens)

    def context_cap(self, provider):
        """Providers without a token-rate cap can still be too slow on long prompts (a local
        CPU model reads only a few hundred tokens per second), so cap the context for them."""
        if provider == "ollama":
            return int(os.environ.get("OLLAMA_CONTEXT_CAP", "5000"))
        return None

    def mark_down(self, provider, seconds):
        """Circuit breaker: stop trying a provider that has said it is out of quota."""
        self._down[provider] = time.time() + seconds

    def is_down(self, provider):
        return self._down.get(provider, 0) > time.time()

    def candidates(self, role):
        names = [self.primary] + ([self.fallback] if self.fallback else [])
        live = [n for n in names if not self.is_down(n)] or names   # never end up with nothing
        return [(n, config.model_for(n, role)) for n in live]

    def chat(self, provider, model, messages, json_mode, timeout):
        if self.chaos_rate and self._rng.random() < self.chaos_rate:
            raise LLMError("chaos: injected provider failure (HTTP 503)", retryable=True)
        if provider not in self._instances:
            self._instances[provider] = PROVIDERS[provider]()
        return self._instances[provider].chat(model, messages, json_mode, timeout)
