# LLM release lane: fraud-alert triage

The same release machinery as the ML lane (immutable registry, offline gate, shadow, 10% canary, automatic
rollback, hash-chained audit log, generated postmortem), applied to an LLM component. Nothing about *how* a release
moves or rolls back is reimplemented: `llm/controller.py` subclasses `Controller` and only supplies LLM evidence
through its hooks (`_offline_gate`, `_staging_smoke`, `_shadow_summary`, `_evaluate_window`,
`_verification_payloads`, `_write_postmortem`).

> **Honesty labels.** Alerts are synthetic (seeded generator, below). Labels are produced by a fixed decision table in
> code, not by people and not by a model. Alert traffic is a seeded **simulation**. Every model call is a real call
> to a local Ollama server (llama3.2:3b, and qwen2.5-coder:3b for the model swap) on a shared laptop; grades, routing,
> SLO checks and rollbacks are real executions. No hosted API, no keys, nothing deployed.

## The component

Input: a short English summary of a fraud alert. Output: one JSON object

```json
{"reason_code": "ACCOUNT_TAKEOVER", "escalate": true, "rationale": "Password reset 3 h before on a 40-minute-old device."}
```

`reason_code` is one of `CARD_TESTING`, `ACCOUNT_TAKEOVER`, `STOLEN_CARD_CNP`, `FRIENDLY_FRAUD`, `BENIGN_ANOMALY`.
Ollama's structured outputs (`format` = the JSON schema) constrain the shape; the code graders still check it.

## Data: seeded alerts with rule-derived labels (`llm/alerts.py`)

- `sample_alert` draws fields that lean toward one of five archetypes (with ~15% of amounts near the $1,000 line and
  device/reset ages straddling 24 h), and `render_summary` writes them as English with seeded phrasing and order.
- `label_alert` is the decision table (first matching reason code; escalate if *any* of three rules fires). The rule
  names are the evaluation **segments**: `high_value_cnp` (online and >= $1,000), `account_takeover`,
  `card_testing_burst`.
- Gold set `evals/triage/triage_gold_v1.jsonl`: 60 dev cases (prompt development only) + 150 test cases (the release
  gate), fixed per-archetype counts so every segment has support (34 takeover cases in test, 25 of them under the
  dollar line). `scw llm-gold` / `make llm-gold` regenerates it and fails if the committed file differs.
- A 12-case **smoke subset** is deliberately *textbook*: its takeover examples are all high-value online purchases,
  which the dollar rule escalates anyway. It is reported (never gating) to show what a quick review would have seen.
- Simulated traffic (`iter_traffic`) uses the same sampler with a baseline mix, sticky customer ids for routing, and
  delayed analyst labels (10% never arrive). `ato_campaign` is a SIMULATED account-takeover surge (50% takeovers).

Scope this buys: the gold set measures whether a prompt + model **follows a written policy**. It does not measure
fraud judgement, and there are no human labels anywhere in this lane.

## The releasable unit: a prompt bundle (`llm/bundles.py`)

`configs/llm/bundles/<version>.toml` -> one canonical JSON (`prompt_bundle_v1`) with the full prompt text, user
template, model tag, **Ollama weights digest** (read from `/api/tags` at registration), sampling params, the output
schema in generation order, owner and change summary. It is written read-only, hashed (SHA-256) and HMAC-signed, and
inserted through the same immutable `model_versions` table as an ML artifact. A changed prompt character, param or
weights digest is a different hash; the gate and staging refuse a bundle whose weights behind the tag changed.

| bundle | what changed | role in the story |
|---|---|---|
| `triage-p1` | the written policy, temperature 0.2 | champion at bootstrap |
| `triage-p2` | same policy, rationale capped at 25 words, temperature 0 (greedy) | better-or-equal candidate |
| `triage-p3` | p2 minus one escalation rule ("reason_code is ACCOUNT_TAKEOVER") | deliberately regressed |
| `triage-p2-qwen` | p2 prompt and params on `qwen2.5-coder:3b` | model swap (cost/latency vs quality) |
| `triage-p2-ctx4k` | p2 with `num_ctx` 4096 instead of 2048 (serving config only) | canary rollback |

## Gates (`configs/llm/release_policy.toml`, SHA-256 on every event)

**1. Offline regression suite (`llm/evals.py`, `llm/gates.py`).** Every test case x k=3 tries, each try with its own
deterministic seed (`sha256(case_id:try)`), sequential calls. Code graders only (`llm/graders.py`): output contract
(parses, exact keys, enum, boolean, rationale <= 300 chars), reason code, escalate. Reported per try (pass@1) and
**pass^3** = share of cases fully correct on all three tries (the consistency metric from Anthropic's
"Demystifying evals for AI agents", 2026-01-09). Blocking checks: bundle integrity, pinned weights, schema validity
>= 98%; vs the champion (same cases, same k, same seeds): escalation recall on all must-escalate cases (-0.05),
escalation recall per segment with >= 15 cases (-0.10), false-escalation rate (+0.10), reason accuracy (-0.05),
pass^3 (-0.05). Cost and latency ratios vs the champion are reported as **advisory** (`cost_latency_blocking = false`):
offline latency is a sequential measurement on a shared laptop, and the binding check is the canary SLO measured
on live paired traffic. Raw per-try results are saved as JSONL next to the run.

**2. Shadow.** The champion answers; the candidate runs afterwards on the same alert and is only recorded
(`test_shadow_never_changes_the_answer`). Canary needs >= 60 shadow requests, candidate failures (errors, timeouts,
invalid outputs) <= 2%, and, with >= 10 labeled escalations, candidate escalation recall within 0.10 of the champion.

**3. Canary 10%** (same sticky hash as the ML lane, on customer id), evaluated every 200 routed requests:

- *online code grading, label-free:* candidate failures incl. invalid outputs <= 2% (the caller got the champion);
- *latency SLO:* candidate model-call p95 > 2,000 ms **and** > 1.5x the champion arm's p95 in the same window;
- *token-cost SLO:* candidate mean notional cost per call > 1.25x the champion arm's (USD 0.10 / 0.40 per million
  input / output tokens, an assumption: local inference has no per-token bill);
- *online code grading, labeled and paired:* on candidate-served alerts whose label arrived, the champion was also
  run; breach when the candidate escalates significantly fewer must-escalate alerts (exact one-sided McNemar
  p < 0.05, needs >= 10 such alerts);
- *input drift (extra rollback trigger):* PSI of alert fields vs a fixed reference sample of the baseline mix. With
  `drift_action = "rollback"`, a major shift (max PSI > 0.25 on >= 150 rows) is a breach: the offline evidence that
  justified the canary no longer describes the traffic. This differs from the ML lane on purpose, where drift only
  alerts. With no release active there is nothing to roll back; drift is then visible on `/v1/llm/drift`.

On breach: `Controller.rollback` (one CAS swap + audit event in one transaction), 20 real verification calls that must
be answered by the champion, and a postmortem generated from the event log (`llm/postmortem.py`). Promotion needs two
healthy windows, >= 10 paired labeled escalations, fresh evidence, an unchanged bundle hash, and a `release_manager`
who is not the release creator, exactly as in the ML lane.

## Observability: OpenTelemetry GenAI metrics (`llm/otel.py`)

Each model call records `gen_ai.client.token.usage` (`{token}`, `gen_ai.token.type` input/output) and
`gen_ai.client.operation.duration` (`s`) with `gen_ai.operation.name=chat`, `gen_ai.provider.name=ollama`,
`gen_ai.request.model`, `gen_ai.response.model`, `server.address`/`server.port`, `error.type` on failures, the
spec's explicit bucket boundaries, and two bounded custom attributes (`scw.release.arm`, `scw.bundle.version`). The
OTel SDK's Prometheus reader registers into the gateway's own registry, so `/metrics`, the Prometheus scrape config
and the Grafana dashboard (two new panels) pick them up unchanged.

**Status: Development.** These names follow semantic-conventions v1.38.0. On the newer
`open-telemetry/semantic-conventions-genai` main branch, inference is moving to `gen_ai.client.inference.duration` and
`gen_ai.client.inference.operation.{input,output}_tokens` (plus usage counters), and `gen_ai.client.operation.duration`
is no longer meant for inference. The rename is a two-line change in `otel.py`; it was not made because the released
convention is what current SDK instrumentations emit. (Checked 2026-11-09 against the spec repository.)

Verification: `make up` was **not** run, because port 5120 (and the stack's 5120-5123) belongs to the owner's running
app. Instead `tests/test_llm_lane.py::test_llm_api_and_genai_metrics` asserts the series on `/metrics` and that every
`gen_ai_*` series the Grafana panels query is exported; the recorded run saved its `/metrics` output
(`docs/evidence/llm/<run>/metrics-genai.prom`).

## promptfoo regression gate (`evals/triage/`, `make llm-gate`)

`promptfoo@0.125.1` (exact pin, npm dev dependency, Node >= 22.22) runs the 150 test cases against the local
Ollama endpoint with the `triage-p2` bundle's exact model, params and output schema (a pytest keeps the YAML and
the bundle in sync; `SCW_TRIAGE_PROMPT=triage-p3` swaps only the prompt file). JavaScript code graders mirror the
Python ones. **A case fails when the reply breaks the output contract or when a must-escalate alert is not
escalated**; false escalations and reason accuracy are reported as named metrics and never fail a case. The pass
rate is therefore 1 - (invalid + missed escalations) / 150, an escalation-recall gate in pass-rate form.
`PROMPTFOO_PASS_RATE_THRESHOLD` = 90 (derived in the Makefile from the recorded champion's recall minus the gate's
0.05 margin) makes `promptfoo eval` exit 100 below it.

Why not "escalate decision correct" as the pass criterion (the first version of this suite)? In the recorded run,
overall escalate accuracy was 62.0% for the champion p2, 61.3% for the regressed p3 and 69.3% for the qwen swap:
a model that under-escalates gains on the 84 should-not-escalate cases what it loses on the 66 must-escalate ones.
A symmetric pass rate would have passed both bad candidates. (Numbers recomputed from the saved per-try files.)

Recorded (`docs/evidence/llm/promptfoo/`, commit 3b5f0dc, Ollama 0.40.13): the champion prompt passes 139 of 150
cases (92.7%, exit 0; its 11 failures are exactly its 11 missed escalations, matching the Python gate's 83.3% recall
on 66 must-escalate cases); the regressed p3 prompt passes 111 (74.0%) and `promptfoo eval` exits 100.

**On GitHub (2026-11-09, after Actions was enabled).** The CI job (`llm-gate`, Ollama 0.40.13 as a service
container pinned by digest) ran once on a pull request, on a GitHub-hosted runner. Same model manifest and layer
digests, same Ollama version, prompt, params and seed, but Ollama's CPU backend (AMD EPYC, AVX-512) instead of
Metal: the champion prompt scored **127/150 (84.7%)**, and all 23 failures were missed escalations (recall 65.2% vs
83.3%). Greedy decoding is not bit-identical across compute backends, so the 90% threshold, calibrated on Metal,
fails the accepted champion on CPU. Evidence: `docs/evidence/llm/promptfoo/triage-p2.github-cpu.*` (run
42692611563). So CI does not use an absolute threshold; it uses the relative gate below.

### Relative gate on pull requests (`make llm-gate-relative`, CI job `llm-gate`)

Greedy decoding differs between compute backends, but two runs on one backend are comparable. The CI job checks
out the PR base (`github.base_ref`) next to the PR head, pulls `llama3.2:3b` once into the Ollama service container,
and runs the same promptfoo suite twice, back to back on one runner: the base's `evals/triage/` (with the base's
prompts and bundle config), then the head's. `scripts/llm_gate_compare.py` then fails the job when

    head pass rate < base pass rate - margin
    margin = 100 * max_escalation_recall_drop * must_escalate_cases / cases = 100 * 0.05 * 66/150 = 2.2 points

This is the same derivation as the Makefile's absolute threshold: a case fails on an invalid reply or a missed
must-escalate alert, so the policy's 0.05 escalation-recall non-inferiority margin, over 66 must-escalate cases out
of 150, is 3.3 cases or 2.2 pass-rate points. In practice the head may lose at most 3 cases relative to the base, and
a 4th fails the gate. The margin is read from the **base's** `configs/llm/release_policy.toml`, and the counts from the
base run, so a PR can't loosen its own gate by editing the policy. The gate also fails closed: on any provider error
in either run, on an empty run, or when base and head cover a different number of cases (for example a changed gold
split). On a PR that changes the gold set, the comparison is between two different test sets; review that diff on its
own.

Each run, the job log and the step summary show both pass rates, the margin, the difference and the failed cases by
metric; the `promptfoo-results` artifact keeps `relative/base.json`, `relative/head.json` and `relative/verdict.json`.
It runs on pull requests that touch `configs/llm/`, `evals/triage/`, `src/spectral_canary/llm/`,
`scripts/llm_gate_*` or its own workflow, `.github/workflows/llm-gate.yml` (a native `pull_request.paths` filter, so
the Ollama container starts only then), and on manual dispatch (`gh workflow run llm-gate --ref <branch> -f
llm_gate_base_ref=main`; add `-f llm_gate_head_prompt=triage-p3` to score the regressed prompt as the head and watch
the gate fail). Two suite runs take 26–41 minutes on a CPU runner, depending on which CPU it gets.

**Recorded on GitHub (2026-11-09, PR #6).** Base `docs/readme-writeup` (9bda89b, champion `triage-p2`) in every run:

| run | runner CPU (Ollama kernel) | base | head | change | verdict |
|---|---|---|---|---|---|
| [42701455881](https://github.com/AlexRosen23/spectral-canary-works/actions/runs/42701455881) (PR) | AMD EPYC 9V74 (zen4, AVX-512) | 127/150 | 127/150 (PR merge) | +0.00 | **PASS** |
| [42706176139](https://github.com/AlexRosen23/spectral-canary-works/actions/runs/42706176139) (PR) | AMD EPYC 7763 (haswell, AVX2) | 136/150 | 136/150 (PR merge) | +0.00 | **PASS** |
| [42706182502](https://github.com/AlexRosen23/spectral-canary-works/actions/runs/42706182502) (dispatch) | Intel Xeon 8370C (icelake, AVX-512) | 127/150 | 104/150 (`triage-p3`) | −15.33 | **FAIL** (exit 2) |

The same champion scored 127 on two runners and **136 on the third**. GitHub assigns different CPUs, and Ollama
picks a different kernel for each, so an absolute threshold doesn't hold even from one GitHub runner to the next. Base
and head on one runner matched exactly in both PASS runs, and the regressed prompt fell far outside the margin. In
the first run the failed case ids were also identical to the earlier CPU run 42692611563. The raw reply text matched
for 150/150 cases between base and that run, but for 149/150 between base and head (one reply worded differently,
same grade). So even one runner isn't perfectly bit-stable, which is one reason the gate keeps a margin rather than
requiring equality. Evidence: `docs/evidence/llm/promptfoo/relative.github-runs.json` (all three runs) and
`relative.github-cpu.*` (the first run's outputs and verdict). On the build machine (Metal),
`make llm-gate-relative` scored 139/150 for both runs (PASS), and with `LLM_GATE_HEAD_PROMPT=triage-p3` scored the
head 111/150, −18.67 points, **FAIL** (`make` exits 2).

Locally, `make llm-gate-relative` does the same against a temporary git worktree of `LLM_GATE_BASE` (default
`origin/main`); `LLM_GATE_BASE_PROMPT` / `LLM_GATE_HEAD_PROMPT` override the prompt of either run.
`make llm-gate` with the absolute 90% threshold still works on the Metal machine it was calibrated on.
`tests/test_llm_gate_compare.py` checks the comparator on the recorded outputs: champion vs champion passes, p2 vs
p3 fails (-18.7 points), Metal vs CPU for the same champion fails (-8.0, the reason the runs must share a runner),
3 lost cases pass and 4 fail, and errors or a case-count mismatch fail.

## Recorded run

`make llm-demo` (= `scw llm-demo --port 5141`): fresh registries, p1 bootstrapped through the offline gate
in-process, then the gateway starts and the whole story runs over real HTTP. Results, generated from the run files:
[`docs/evidence/llm/`](evidence/llm/). The summary is in [AI_ENGINEER_READINESS.md](AI_ENGINEER_READINESS.md).

| | first run `llm-demo-20261109-111237` | primary run `llm-demo-20261109-130256` |
|---|---|---|
| offline suites | all live (5 x 450 calls) | reused from the first run (`--reuse-evals`, same bundle hashes) |
| p2 | promoted after 2 healthy windows | promoted after 2 healthy windows |
| p3 / qwen swap | blocked / blocked | blocked / blocked |
| ctx4k canary window 1 | candidate p95 5,105 ms vs 2,319 ms (2.20x) -> rolled back | 2,830 ms vs 1,342 ms (2.11x) -> rolled back |
| duration, result | 6,585 s, FAILED (smoke hypothesis was an expectation) | 1,408 s, PASSED |

Model-call latency was about twice as high during the first run (the laptop was busier); the ratio against the
same-window champion arm, which the SLO uses, was nearly the same.

## Limitations

- A 3B model follows this five-code policy poorly: the promoted champion gets 56% of reason codes and 37% of whole
  replies right, and escalates 55% of alerts that should not be escalated. The lane is about release engineering;
  the absolute quality is a property of the model, and it is reported, not hidden.
- Prompt variants tried on the dev split during development (few-shot examples, rationale-first key order, a
  key-facts block, a longer analyst rationale) all did worse than p1 for this model; they were pilots, not recorded
  runs, so no numbers are claimed for them.
- 150 test cases x 3 tries cannot resolve differences of a few points; the non-inferiority margins are wide for that
  reason, and a candidate within noise of the champion passes.
- All graders are code. There is no LLM-as-judge and no human calibration; rationales are only checked for
  presence and length (and one-sentence form, reported).
- Canary windows on a laptop are small (about 20 candidate calls per 200 routed requests), so latency p95 per window
  is noisy; the ratio rule against the same-window champion arm absorbs whole-machine slowdowns, not variance.
- Labels are delivered by the simulation; in production they would come from analyst dispositions days later.
