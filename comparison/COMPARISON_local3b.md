# Multi-agent harness vs single-agent baseline

Same model, same tools, same tasks, same code-computed rubric. One run per cell. Cost is shown as LLM calls and tokens: the hosted model runs on a free tier, so dollars are $0.

| Task | Version | Recall /5 | Grounded /5 | Distinct /5 | **Total /15** | Findings | Seconds | Calls | Tokens | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| code | multi-agent | 3.3 | 5.0 | 5.0 | **13.3** | 4 | 229.7 | 14 | 15,114 | complete |
| code | single agent | 0.0 | 5.0 | 5.0 | **10.0** | 2 | 287.1 | 12 | 27,653 | complete |
| triage | multi-agent | 3.3 | 5.0 | 5.0 | **13.3** | 2 | 186.8 | 28 | 23,977 | partial |
| triage | single agent | 0.0 | 5.0 | 5.0 | **10.0** | 1 | 171.5 | 14 | 33,077 | complete |
| research | multi-agent | - | - | - | - | - | - | - | - | not run |
| research | single agent | - | - | - | - | - | - | - | - | not run |

## What each version found

### Code review (samples/buggy_repo, 6 planted issues)
- **multi-agent** found: SQL injection, Hardcoded API key, Division by zero on empty list, Bare except. Missed: Unclosed file handle, is_adult boundary (> 18).
- **single agent** found: nothing. Missed: SQL injection, Hardcoded API key, Division by zero on empty list, Bare except, Unclosed file handle, is_adult boundary (> 18).

### Ticket triage (samples/notes, 3 tickets)
- **multi-agent** found: ticket_01.txt, ticket_02.txt. Missed: ticket_03.txt: not triaged.
- **single agent** found: nothing. Missed: ticket_01.txt: not triaged, ticket_02.txt: not triaged, ticket_03.txt: not triaged.

### Web research brief (SQLite vs PostgreSQL, 5 key facts)

## Reading the numbers

- Quality (sum of totals): multi-agent **26.6** vs single agent **20.0** (out of 30).
- Tokens: multi-agent 39,091 vs single agent 60,730 (0.6x). Time: 416s vs 459s (0.9x).
- **code**: multi-agent clearly better (13.3 vs 10.0).
- **triage**: multi-agent clearly better (13.3 vs 10.0).

## Caveats

- One run per cell: model output varies run to run, so small gaps are noise.
- Recall uses planted ground truth and counts a finding only if its quote really exists in the source.
- Time includes waiting on the free tier's rate limits, which hits the multi-agent run harder because several agents share one token budget.
- The rubric scores findings, not the prose summary, and the research check is keyword-based.
- The single agent gets the same agent-loop rule as the team (it may not answer before reading anything) so the comparison isolates the multi-agent structure.
