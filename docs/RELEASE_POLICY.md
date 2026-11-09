# Release policy

**How do you decide whether a model is safe to deploy?** With four gates in sequence. Each gate is code
plus thresholds from `configs/release_policy.toml`, and each one runs against the real artifact and the real
request path. Every decision is written to the hash-chained event log together with its evidence and the
policy's SHA-256.

```
registered ──offline gate──▶ validated ──load+smoke──▶ staging ──▶ shadow ──shadow gate──▶ canary 10% ──guardrails──▶ promoted
     │                           │                        │            │                       │  (human approval)
     └──── blocked ◀─────────────┘                        └── rolled_back ◀────────────────────┴── paused (policy option)
```

Transitions not shown in `controller.TRANSITIONS` are refused (`illegal transition`). Only one release can
be active at a time.

## 1. Offline gate (`gates.py`). Is this artifact allowed to see any traffic?

| check | rule | why |
|---|---|---|
| artifact_integrity | SHA-256 of the bytes equals the registered hash; every serialized type is on the skops allowlist; the artifact really loads | a tampered or unsafe artifact must never load, let alone serve |
| schema_compatible | the manifest's input-schema hash equals the gateway's | a schema break has to ship as a migration, not reach a model by accident |
| predict_shape_and_range | finite scores in [0,1], shape (n,) on the frozen test split | correctness smoke |
| golden_ordering | an obviously risky hand-written case scores above an obviously routine one | catches inverted labels and broken feature order |
| reproducible_predictions | re-scoring the frozen test split reproduces the training-time prediction hash exactly | detects environment drift and swapped artifacts |
| latency_p95_ms | single-row p95 measured now ≤ 50 ms | the latency budget for one request's model call |
| test_support | ≥ 100 test positives | below this, the quality comparison is noise |
| calibration_ece | ECE ≤ 0.05 | consumers read the score as a probability |
| pr_auc / recall / f1 vs champion | candidate ≥ champion − margin (0.03 / 0.05 / 0.04) | **non-inferiority**, not "better on one number" |
| slice_recall | no slice with ≥ 25 positives on both sides drops more than 0.12 recall | catches defects concentrated in one segment; slices with too little support are skipped and named, not judged |

**Metric choice.** Fraud is about 6% of rows, so accuracy and ROC-AUC flatter every model. PR-AUC is the
ranking metric. Precision, recall and F1 are reported at the threshold that maximized F1 on the **validation**
split; the test split is never used to choose anything. Brier and ECE cover calibration. Every reported
proportion carries a 95% interval (bootstrap for PR-AUC, Wilson for precision/recall).

## 2. Shadow gate (`monitor.shadow_summary`)

The champion answers; the candidate scores the identical request afterwards, and its result is only
recorded. Its exceptions and timeouts are swallowed into telemetry (`test_shadow_never_changes_responses`
proves that responses are identical with and without a failing shadow). Canary is refused unless the shadow
window has ≥ 400 requests, a candidate error rate ≤ 1%, a score Jensen–Shannon divergence ≤ 0.20 and a
decision disagreement ≤ 15%.

## 3. Canary guardrails (`monitor.evaluate_canary_window`)

- **Routing:** `sha256("scw-canary-v1:" + request_key) mod 10000 < fraction × 10000`, so the same card always
  lands in the same arm (sticky), the arms are a randomized split of the same traffic, and a ramp is monotone.
  10% is the default.
- **Window:** every 1,500 routed requests (a logical clock, so replays are deterministic). A window needs at
  least 120 candidate requests before any judgement.
- **Breaches**, each with an explicit statistical guard:
  - *error budget:* candidate model failures (error, timeout, unavailable) above 2% of candidate-arm requests.
    The user still got a champion answer through the fallback.
  - *latency:* candidate **model-call** p95 above 50 ms **and** more than 6× the champion's model-call p95 in
    the same window. A whole-machine slowdown hits both arms and does not count.
  - *flag rate (label-free, early):* the candidate flags less than 65% of the champion arm's rate **and** the
    95% Newcombe CI of the difference excludes 0. Counted **one observation per card** (routing key):
    requests from one card are correlated, while cards are independent because the routing hash randomizes
    them. A request-level CI caused a false rollback in the load test (see `LOAD_TEST_RESULTS.md`).
  - *recall (labeled, delayed):* the champion is also scored on every candidate-served request (a paired
    design). On candidate-served requests whose label has arrived and is positive, breach when the **exact
    one-sided McNemar test** gives p < 0.01 and candidate recall < champion recall (again one labeled fraud
    per card). This needs ≥ 20 labeled
    positives per arm; below that the window says `insufficient` and **no accuracy claim is made**.
- **On breach:** `on_breach = "rollback"` (default) or `"pause"`. Both are tested
  (`test_breach_policy_pause_vs_rollback`).
- **Promotion:** needs ≥ 2 consecutive healthy windows, sufficient labeled evidence, fresh evidence (no
  traffic gap > 15 min), a re-verified artifact hash, and an approval from a `release_manager` who is **not**
  the release creator (separation of duties). The approval is recorded in the event.

## 4. Roles

`viewer` < `operator` (propose, stage, shadow, canary, pause, rollback, chaos) < `release_manager` (approve).
Bearer tokens are random per installation (`var/secrets/operators.local.json`, mode 0600, gitignored); the
server keeps only SHA-256 hashes. This is a demonstrable local control, **not** enterprise IAM.

## LLM lane

Prompt bundles (prompt + model + params) follow the same four gates with LLM evidence: a code-graded regression suite
with pass^3 and per-segment escalation recall offline, shadow, a 10% canary with online grading plus p95 latency and
token-cost SLOs, and an input-drift trigger. Thresholds live in `configs/llm/release_policy.toml`; details in
[`LLM_RELEASE_LANE.md`](LLM_RELEASE_LANE.md).

## Changing the policy

Edit `configs/release_policy.toml`, run `make test`, and commit. The new SHA-256 appears on every subsequent
release event. `tests/test_release_controller.py::test_gate_margins_are_policy_driven` shows a stricter
margin blocking v3.
