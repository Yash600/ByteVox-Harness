"""Run context: the single choke point every LLM call and tool call goes through.

Agents never touch a model or a tool directly. That is what makes the limits, retries,
permissions, approvals and logs unavoidable rather than optional.
"""
import json
import random
import shutil
import threading
import time
from pathlib import Path

from .budget import Budget, BudgetExceeded
from .config import Limits
from .llm import LLMError, Router, estimate_tokens
from .logger import RunLogger
from .safety import ApprovalGate, scan_injection, wrap_untrusted
from .state import RunState
from .tools import TOOLS, Sandbox, ToolError

TOOL_RESULT_CAP = 8000


class AgentFailed(Exception):
    pass


class Run:
    def __init__(self, goal, input_dir=None, runs_root="runs", run_id=None, limits=None,
                 router=None, approval_mode="ask"):
        self.goal = goal
        self.run_id = run_id or time.strftime("run_%Y%m%d_%H%M%S")
        self.dir = Path(runs_root).resolve() / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        # Agents work on a COPY of the input, so even approved edits never touch the original.
        self.workspace = self.dir / "workspace"
        if input_dir:
            shutil.copytree(input_dir, self.workspace, dirs_exist_ok=True)
            self.original = Path(input_dir).resolve()
        else:
            self.workspace.mkdir(exist_ok=True)
            self.original = None
        self.sandbox = Sandbox(self.workspace)
        self.limits = limits or Limits.from_env()
        L = self.limits
        self.budget = Budget("run", L.max_llm_calls, L.max_seconds, L.max_tokens)
        self.logger = RunLogger(self.dir, self.run_id)
        self.state = RunState(self.dir, goal)
        self.router = router or Router()
        self.approver = ApprovalGate(approval_mode, logger=self.logger)
        self.cancel = threading.Event()
        self.stats = {"retries": 0, "llm_errors": 0, "injections_flagged": 0, "tool_denied": 0,
                      "fallbacks": 0}
        self._stats_lock = threading.Lock()

    def bump(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    # ------------------------------------------------------------------ LLM

    def check(self, agent):
        if self.cancel.is_set():
            raise BudgetExceeded("run", "cancelled by user")
        agent.budget.check()

    def _fit(self, agent, provider, messages):
        """Shrink the context so one request can fit the provider's tokens-per-minute cap.
        A request bigger than the cap can never succeed, so retrying it is pointless. Oldest
        tool outputs go first; the system prompt, task and latest message are kept."""
        pacer = self.router.pacers.get(provider)
        limit = int(pacer.per_minute * 0.8) if pacer else self.router.context_cap(provider)
        if not limit:
            return messages
        before = estimate_tokens(messages)
        if before <= limit:
            return messages
        msgs = [dict(m) for m in messages]
        stub = "TOOL RESULT: [older output omitted to fit the model's token limit; call the tool again if you still need it]"
        for i in range(len(msgs) - 1):
            if estimate_tokens(msgs) <= limit:
                break
            if msgs[i]["role"] == "user" and msgs[i]["content"].startswith("TOOL RESULT:"):
                msgs[i]["content"] = stub
        if estimate_tokens(msgs) > limit:     # still too big: clip the newest message
            over = int((estimate_tokens(msgs) - limit) * 3.2) + 200
            last = msgs[-1]["content"]
            msgs[-1]["content"] = last[:max(500, len(last) - over)] + "\n...(clipped to fit the token limit)"
        self.logger.log(agent.agent_id, "context_trimmed", provider=provider,
                        tokens_before=before, tokens_after=estimate_tokens(msgs), limit=limit)
        return msgs

    def llm_call(self, agent, messages, json_mode=True):
        """One guarded model call: budget check, pacing, timeout, retries with backoff,
        fallback to another provider, and a full log line."""
        L = self.limits
        last = None
        cands = self.router.candidates(agent.role)
        for pi, (provider, model) in enumerate(cands):
            attempt, max_attempts = 0, L.llm_retries
            while attempt < max_attempts:
                attempt += 1
                self.check(agent)
                sent = self._fit(agent, provider, messages)
                left = max(0.0, L.max_seconds - self.budget.elapsed())   # run time still available
                token, waited = self.router.pace(provider, sent, self.cancel, max_wait=left)
                self.check(agent)   # a cancel (or spent budget) during pacing aborts the call
                agent.budget.credit(waited)
                if waited >= 1:
                    self.logger.log(agent.agent_id, "throttle", provider=provider,
                                    seconds=round(waited, 1),
                                    reason="staying under the provider's tokens-per-minute limit")
                try:
                    # never let one call outlive the run: cap its timeout by the time that is left
                    cap = L.llm_timeout_local if provider == "ollama" else L.llm_timeout
                    left = max(0.0, L.max_seconds - self.budget.elapsed())
                    res = self.router.chat(provider, model, sent, json_mode, max(5.0, min(cap, left + 5.0)))
                except LLMError as e:
                    last = e
                    self.bump("llm_errors")
                    self.logger.log(agent.agent_id, "llm_error", provider=provider, model=model,
                                    attempt=attempt, retryable=e.retryable, error=str(e))
                    if e.quota and len(cands) > 1:
                        # daily cap: skip this provider for 10 minutes instead of failing on every call
                        self.router.mark_down(provider, 600)
                        self.logger.log(agent.agent_id, "provider_down", provider=provider,
                                        seconds=600, reason="daily quota used up")
                    if e.rate_limited:   # expected on free tiers: be more patient
                        max_attempts = L.llm_retries + 4
                    if not e.retryable or attempt >= max_attempts:
                        break
                    self.bump("retries")
                    wait = (e.retry_after + 0.5) if e.retry_after else (
                        L.backoff_base ** attempt + random.random())
                    wait = min(wait, 60.0)
                    self.logger.log(agent.agent_id, "retry_wait", seconds=round(wait, 1))
                    agent.budget.credit(wait)
                    if self.cancel.wait(wait):
                        raise BudgetExceeded("run", "cancelled by user")
                    continue
                self.router.settle(token, res.input_tokens, res.output_tokens)
                agent.budget.charge(res.input_tokens + res.output_tokens)
                self.logger.log(agent.agent_id, "llm_call", role=agent.role, provider=provider,
                                model=res.model, attempt=attempt, messages=sent,
                                output=res.text, input_tokens=res.input_tokens,
                                output_tokens=res.output_tokens, seconds=round(res.seconds, 2))
                return res
            if pi + 1 < len(cands):
                self.bump("fallbacks")
                self.logger.log(agent.agent_id, "fallback", from_provider=provider,
                                to_provider=cands[pi + 1][0], quota=bool(getattr(last, "quota", False)),
                                reason=str(last))
        raise AgentFailed("LLM provider daily quota used up: " + str(last) if getattr(last, "quota", False)
                          else f"LLM unavailable after retries: {last}")

    @staticmethod
    def _parse_json(text):
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            a, b = text.find("{"), text.rfind("}")
            if a != -1 and b > a:
                return json.loads(text[a:b + 1])
            raise

    def llm_json(self, agent, messages, validate_final):
        """Call the model and demand a valid structured message; one repair attempt."""
        for attempt in (1, 2):
            res = self.llm_call(agent, messages)
            err = None
            obj = None
            try:
                obj = self._parse_json(res.text)
                err = self._validate(agent, obj, validate_final)
            except json.JSONDecodeError as e:
                err = f"not valid JSON ({e.msg})"
            if err is None:
                messages.append({"role": "assistant", "content": res.text})
                return obj
            self.logger.log(agent.agent_id, "invalid_output", attempt=attempt, error=err)
            messages.append({"role": "assistant", "content": res.text})
            messages.append({"role": "user", "content":
                             f"Your reply was rejected: {err}. Reply again with ONE valid JSON "
                             "object in the required format and nothing else."})
        raise AgentFailed(f"model kept returning invalid output: {err}")

    @staticmethod
    def _validate(agent, obj, validate_final):
        if not isinstance(obj, dict):
            return "reply must be a JSON object"
        act = obj.get("action")
        if act == "tool":
            if not agent.spec.allowed_tools:
                return "you have no tools; reply with action=final"
            if not isinstance(obj.get("tool"), str):
                return "tool call needs a string `tool`"
            return None
        if act == "final":
            return validate_final(obj) if validate_final else None
        return 'field `action` must be "tool" or "final"'

    # ---------------------------------------------------------------- tools

    def call_tool(self, agent, name, args):
        """The tool gateway: allow-list -> approval -> sandboxed run -> scan -> wrap."""
        aid = agent.agent_id
        if not isinstance(args, dict):
            args = {}
        if name not in agent.spec.allowed_tools:
            self.bump("tool_denied")
            self.logger.log(aid, "tool_denied", tool=name, args=args,
                            reason=f"role '{agent.role}' may not use this tool")
            return f"ERROR: tool '{name}' is not permitted for your role. Allowed: " \
                   f"{', '.join(agent.spec.allowed_tools) or 'none'}"
        tool = TOOLS.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        if tool.risky:
            preview = tool.preview(self.sandbox, args) if tool.preview else json.dumps(args)
            if not self.approver.request(aid, name, args, preview):
                self.bump("tool_denied")
                self.logger.log(aid, "tool_denied", tool=name, args=args, reason="human denied")
                return "ERROR: a human reviewer denied this action. Do not retry it."
        t0 = time.time()
        try:
            out = tool.fn(self.sandbox, args)
            ok = True
        except ToolError as e:
            out, ok = f"ERROR: {e}", False
        except Exception as e:  # a buggy tool must not crash the run
            out, ok = f"ERROR: tool crashed: {type(e).__name__}: {e}", False
        secs = round(time.time() - t0, 3)
        flagged = scan_injection(out) if ok else []
        if flagged:
            self.bump("injections_flagged")
            self.logger.log(aid, "injection_flagged", tool=name, args=args, matches=flagged)
        clipped = out if len(out) <= TOOL_RESULT_CAP else out[:TOOL_RESULT_CAP] + "\n...(clipped)"
        self.logger.log(aid, "tool_call", tool=name, args=args, ok=ok, seconds=secs,
                        result_chars=len(out), result=clipped[:1500])
        if not ok or name == "edit_file":
            return clipped
        src = f"{name}:{args.get('path') or args.get('url') or args.get('query') or args.get('pattern', '')}"
        return wrap_untrusted(src, clipped, flagged)
