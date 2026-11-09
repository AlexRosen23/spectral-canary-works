# LLM lane evidence

Copied from local run directories (`var/runs/`, gitignored) by `scripts/build_llm_evidence.py`. Nothing here is
hand-edited. Each run folder holds:

| file | what it is |
|---|---|
| `RESULTS.md` | generated tables: expected vs actual, online evidence, offline suite, every gate check |
| `report.json` | the run report written by `scw llm-demo` (machine, commits, story, registry/chain verification) |
| `gate_reports.json` | full offline gate reports, including both suite summaries per release |
| `evals/*.jsonl.gz` | raw per-try results (case, try, seed-derived reply, grade, tokens, latency) behind every offline number |
| `events.jsonl.gz` | the LLM lane's hash-chained event log (kind, actor, payload, hash, prev_hash) |
| `postmortem-llm-rel-0004.{md,json}` | the generated postmortem of the automatic rollback |
| `metrics-genai.prom` | the `gen_ai_*` series from the gateway's `/metrics` at the end of the run |

Runs:

- [`llm-demo-20261109-130256`](llm-demo-20261109-130256/RESULTS.md): primary run at the branch's code; offline
  suite results reused from the first run (same immutable bundles, gold set and k; marked in each gate report),
  shadow and canary re-run live.
- [`llm-demo-20261109-111237`](llm-demo-20261109-111237/RESULTS.md): first run, everything live (6,585 s). Recorded
  as FAILED only because it treated "the smoke subset would have shipped p3" as a pass/fail expectation, which did
  not hold for this model (see `docs/AI_ENGINEER_READINESS.md`). Every release decision matched the expectation.
- [`promptfoo/`](promptfoo/): `make llm-gate` output for the champion prompt and for the regressed prompt.
