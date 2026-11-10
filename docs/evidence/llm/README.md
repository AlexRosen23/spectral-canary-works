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
- [`promptfoo/`](promptfoo/): `make llm-gate` output for the champion prompt and for the regressed prompt. `triage-p2.github-cpu.*`: the same
  champion on a GitHub CPU runner (84.7%). `relative.github-runs.json`: the relative CI gate
  (`make llm-gate-relative`) on three GitHub runners (two PASS with base = head, one FAIL for the regressed prompt);
  `relative.github-cpu.*`: the first run's promptfoo outputs and verdict.

**Local paths (public-repo hygiene, 2026-11-10).** promptfoo writes absolute `basePath` and prompt `file://` paths
into its results. In `promptfoo/triage-p2.results.json.gz` and `triage-p3.results.json.gz` these were rewritten to
repo-relative paths (`evals/triage/...`); no score, grade or case changed (`tests/test_llm_gate_compare.py` reads
both files). The two `events.jsonl.gz` logs still contain one absolute path each, in the `postmortem.written`
payload. They are hash-chained, so editing a payload would break `verify_event_chain` on the evidence. They are
left as recorded. Since 2026-11-10 the controller records that path relative to the run directory.
