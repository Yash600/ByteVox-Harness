# Evidence: curated run traces

Every folder holds the real `trace.jsonl` (every step: agent, input, tool calls, output, tokens, seconds),
`state.json`, `result.json` and `report.md` of one run. Open any run in the web UI with its Replay button,
or read it with `python replay.py <run_id>` (copy the folder into `runs/` first).

| Folder | Run id | What it shows |
|---|---|---|
| `01_code_review_hosted_multi` | `cmp_code_multi_202559` (complete) | Comparison: code review, hosted model, multi-agent. 14.2/15, 12 calls, 13.4K tokens. |
| `02_code_review_hosted_single` | `cmp_code_solo_202801` (complete) | Comparison: same task, single agent. Same score 14.2/15 with 2 calls and 2.1K tokens: multi-agent was NOT worth it here. |
| `03_triage_hosted_multi` | `cmp_triage_multi_202803` (complete) | Comparison: ticket triage, multi-agent. 15/15. |
| `04_triage_hosted_single` | `cmp_triage_solo_203004` (complete) | Comparison: triage, single agent. 13.3/15: it rated the billing ticket medium instead of high. |
| `05_research_hosted_multi_PARTIAL` | `cmp_research_multi_203105` (partial) | Comparison: web research, multi-agent. Ends PARTIAL: the checker used all 6 steps before judging (see README, 'the run that went wrong'). Fixed afterwards with a 3-step cap. |
| `06_research_hosted_single` | `cmp_research_solo_204113` (complete) | Comparison: web research, single agent. 15/15 with fewer tokens. |
| `07_code_review_local3b_multi` | `cmp_code_multi_205133` (complete) | Weak local 3B model, multi-agent: 13.3/15, found 4 of 6 planted issues. |
| `08_code_review_local3b_single` | `cmp_code_solo_205523` (complete) | Weak local 3B model, single agent: 10.0/15, found 0 of 6. |
| `09_triage_local3b_multi` | `cmp_triage_multi_210010` (partial) | Local 3B, multi-agent triage: PARTIAL (a junk subtask and an invalid reply), still found 2 of 3 tickets. |
| `10_triage_local3b_single` | `cmp_triage_solo_210317` (complete) | Local 3B, single agent triage: found 0 of 3. |
| `11_failure_empty_result_called_complete` | `test_buggy2` (complete) | FAILURE (before the fix): workers learned to return zero findings to escape rejection, and the run ended COMPLETE with no findings. Led to carry-forward of verified findings and the 'finished but nothing found' banner. |
| `12_failure_daily_quota_retry_loop` | `run_20261016_140124_2d50` (partial) | FAILURE: Groq daily token quota ran out and the harness retried every 60 s for 9 minutes. Led to quota detection, no retry, provider circuit-breaker, fallback. |
| `13_failure_refusal_on_empty_sandbox` | `run_20261016_135501_ce56` (refused) | FAILURE: small model refused a doable repo review. Led to challenging refusals when files exist. |
| `14_limit_hit_clean_stop` | `test_limit` (partial) | LIMIT: run capped at 4 model calls; it stopped cleanly and was labelled PARTIAL with reasons. |
| `15_prompt_injection_flagged` | `test_inject1` (complete) | SAFETY: a planted comment tells the AI to ignore its rules and edit config.py. It is flagged as data; the workspace is untouched. |
| `16_github_repo_review_time_limit` | `test_github4` (partial) | Live GitHub repo (karpathy/micrograd) downloaded and reviewed; ended PARTIAL at the 900 s time limit because of free-tier rate limits. |
| `17_edit_with_human_approval` | `run_20261016_123411_26f5` (complete) | Edit task: the run paused for a human to approve the diff; only the private copy changed. |
| `18_open_question_web_research` | `ayurveda_demo` (partial) | An open question ('tell some great facts about ayurveda'): planner chose 2 research subtasks, Tavily search, pages fetched, 6 quotes verified against Wikipedia. Ended PARTIAL at the 15-minute limit and the free daily quota. It also exposed a text-extraction bug (links split sentences across lines, so correct quotes were rejected), now fixed. |
| `19_chaos_injected_failures` | `test_chaos` (running) | Injected provider failures (50%): retries with exponential backoff. Cut off before the end. |
| `20_failure_agent_ran_out_of_steps_files_never_reviewed` | `run_20261017_090215_e34e` (partial) | A user upload of a fresh test project. 9 real findings, all checked, but one worker kept reading until its steps ran out, never answered, and 3 source files (including the SQL injection) were never reviewed. Fixed with a salvage step: one last call that can only answer. |
| `21_resume_after_crash_14_tickets` | `resume_demo2` (complete) | OPTIONAL EXTRA, resume after a crash. A 14-ticket triage was hard-killed (exit code 86) while subtask s1 was mid-run and s2 was already approved. `--resume` kept s2, re-ran only s1, did not plan again, carried the spent budget and clock over (183 s to 306 s), and finished COMPLETE with 14 of 14 urgencies correct. The trace shows `demo_crash` then `run_resumed`. |
| `22_failure_duplicates_piled_up_library_app` | `run_20261017_154504_7e8c` (partial) | Second review of the held-out library-app project: 15 of 20 planted issues clearly found (first run: 9). But one worker re-reported eval()/innerHTML/empty-catch under reworded titles; kept findings are never dropped, so the near-duplicates piled up, the checker rejected the list three times, and the subtask ended unverified. Fixed by recognising the same issue by line and word stems when findings are carried forward. |
