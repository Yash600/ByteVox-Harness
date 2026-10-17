"""All tunables in one place. Environment variables override the defaults."""
import os
from dataclasses import dataclass, fields


def _env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return type(default)(v) if not isinstance(default, str) else v


# The one hard time ceiling: no run, agent or override may exceed 15 minutes.
HARD_TIME_LIMIT = 900.0


@dataclass
class Limits:
    # whole-run budgets
    # Everything that spends MODEL calls or tokens stays at its original, conservative value: those
    # are bounded by the provider's limits, so only non-LLM caps (downloads, uploads, file sizes, search,
    # network timeouts) and the time ceiling were raised.
    max_llm_calls: int = 40
    max_seconds: float = HARD_TIME_LIMIT
    max_tokens: int = 150_000
    # per-agent budgets (one agent instance = one subtask attempt)
    agent_max_steps: int = 6
    agent_max_seconds: float = HARD_TIME_LIMIT   # time, not model calls: never cut off before the run itself
    agent_max_tokens: int = 40_000
    # verification / structure
    max_check_loops: int = 2       # checker may send work back this many times
    max_subtasks: int = 3
    concurrency: int = 2
    # LLM call robustness
    llm_timeout: float = 120.0
    llm_timeout_local: float = 400.0    # CPU-only local models are slow on long prompts
    llm_retries: int = 3
    backoff_base: float = 1.6

    @classmethod
    def from_env(cls):
        d = cls()
        for f in fields(cls):
            setattr(d, f.name, _env("HARNESS_" + f.name.upper(), getattr(d, f.name)))
        d.max_seconds = min(float(d.max_seconds), HARD_TIME_LIMIT)          # 15-minute ceiling, always
        d.agent_max_seconds = min(float(d.agent_max_seconds), d.max_seconds)
        return d


# Which model tier each role uses. "strong" = judgement roles, "fast" = many cheap calls.
ROLE_TIER = {"planner": "strong", "checker": "strong", "assembler": "strong",
             "worker": "fast", "researcher": "fast", "editor": "fast", "solo": "fast"}


def provider_name():
    return os.environ.get("LLM_PROVIDER", "ollama").lower()


def fallback_provider():
    v = os.environ.get("LLM_FALLBACK", "").lower()
    return v or None


def model_for(provider, role):
    tier = ROLE_TIER.get(role, "fast")
    if provider == "ollama":
        return os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    if provider == "groq":
        # Probed on Groq (bench/groq_probe.py): qwen3.8-27b follows our JSON tool protocol 6/6;
        # openai/gpt-oss-* insist on Groq's native function-call format and fail it. Override
        # per tier with GROQ_MODEL_STRONG / GROQ_MODEL_FAST.
        default = "qwen/qwen3.8-27b"
        return os.environ.get("GROQ_MODEL_STRONG" if tier == "strong" else "GROQ_MODEL_FAST", default)
    raise ValueError(f"unknown provider {provider!r}")
