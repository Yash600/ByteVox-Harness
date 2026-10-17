# Multi-agent harness vs single-agent baseline

Same model, same tools, same tasks, same code-computed rubric. One run per cell. Cost is shown as LLM calls and tokens: the hosted model runs on a free tier, so dollars are $0.

| Task | Version | Recall /5 | Grounded /5 | Distinct /5 | **Total /15** | Findings | Seconds | Calls | Tokens | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| code | multi-agent | 4.2 | 5.0 | 5.0 | **14.2** | 5 | 122.0 | 12 | 13,370 | complete |
| code | single agent | 4.2 | 5.0 | 5.0 | **14.2** | 5 | 1.3 | 2 | 2,145 | complete |
| triage | multi-agent | 5.0 | 5.0 | 5.0 | **15.0** | 3 | 121.8 | 12 | 11,015 | complete |
| triage | single agent | 3.3 | 5.0 | 5.0 | **13.3** | 3 | 61.0 | 6 | 5,930 | complete |
| research | multi-agent | 5.0 | 5.0 | 5.0 | **15.0** | 10 | 607.3 | 22 | 40,523 | partial |
| research | single agent | 5.0 | 5.0 | 5.0 | **15.0** | 8 | 553.8 | 12 | 29,430 | complete |

## What each version found

### Code review (samples/buggy_repo, 6 planted issues)
- **multi-agent** found: SQL injection, Hardcoded API key, Division by zero on empty list, Bare except, Unclosed file handle. Missed: is_adult boundary (> 18).
- **single agent** found: SQL injection, Hardcoded API key, Division by zero on empty list, Bare except, Unclosed file handle. Missed: is_adult boundary (> 18).

### Ticket triage (samples/notes, 3 tickets)
- **multi-agent** found: ticket_01.txt, ticket_02.txt, ticket_03.txt. Missed: nothing.
- **single agent** found: ticket_02.txt, ticket_03.txt. Missed: ticket_01.txt: said medium, truth high.

### Web research brief (SQLite vs PostgreSQL, 5 key facts)
- **multi-agent** found: SQLite is serverless / embedded, PostgreSQL is client-server, SQLite is a single file, Concurrency: writers / locking / MVCC, PostgreSQL has richer features / scale. Missed: nothing.
- **single agent** found: SQLite is serverless / embedded, PostgreSQL is client-server, SQLite is a single file, Concurrency: writers / locking / MVCC, PostgreSQL has richer features / scale. Missed: nothing.

## Reading the numbers

- Quality (sum of totals): multi-agent **44.2** vs single agent **42.5** (out of 45).
- Tokens: multi-agent 64,908 vs single agent 37,505 (1.7x). Time: 851s vs 616s (1.4x).
- **code**: roughly equal quality (14.2 vs 14.2) at 6.2x the tokens.
- **triage**: roughly equal quality (15.0 vs 13.3) at 1.9x the tokens.
- **research**: roughly equal quality (15.0 vs 15.0) at 1.4x the tokens.

## Caveats

- One run per cell: model output varies run to run, so small gaps are noise.
- Recall uses planted ground truth and counts a finding only if its quote really exists in the source.
- Time includes waiting on the free tier's rate limits, which hits the multi-agent run harder because several agents share one token budget.
- The rubric scores findings, not the prose summary, and the research check is keyword-based.
- The single agent gets the same agent-loop rule as the team (it may not answer before reading anything) so the comparison isolates the multi-agent structure.
