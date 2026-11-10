<!-- Regenerated on 2026-11-09 from the recorded run's own event log and request telemetry (var/runs/demo-20261109-001407/registry.sqlite; run demo-20261109-001407, driver: real HTTP, gateway commit f01561016adf, models trained at dc464b9) by spectral_canary.postmortem after the impact section was fixed to count labels known at the rollback, per card as the guardrail does, in both directions. The first version (copied verbatim when the run was recorded) reported 124 labeled fraud requests and 28 missed frauds: request-level and one direction only, which read as if it contradicted the 65 vs 83 of 112 detection evidence. Do not hand-edit numbers; regenerate with `make demo`. -->

# Postmortem: rel-0003 (v3-hgb-fresh vs champion v2-hgb)

_Generated from the hash-chained event log by `spectral_canary.postmortem`. Blameless format._

> Traffic in this release was 100% **simulated** (seeded replay). Predictions, routing, guardrail math and the rollback are real executions of this codebase.

## Summary

- **Outcome:** rolled_back
- **Trigger:** guardrail_breach
- **Reason recorded:** on 112 labeled frauds served by the candidate, it caught 65 vs 83 for the champion (recall 0.58 vs 0.74, McNemar p=0.0015)
- **Recovery verification:** 50/50 real predictions served by `v2-hgb`; readiness=True; passed=True

## Impact (measured from request telemetry)

_Labels as known at the rollback (2026-11-09 06:16:03 UTC); later-arriving labels are not counted._

- Requests in release: 6600
- Served by the candidate: 634
- Labeled fraud requests served by the candidate (labels delayed; some never arrive): 124, from 112 distinct cards
- Per card (the unit the guardrail tests): the candidate missed 26 frauds the champion would have caught, and caught 8 the champion would have missed; net 18 fewer frauds caught
- Per request (repeat cards counted each time): 28 missed vs 9 extra caught

## Detection evidence

Window 4 (1500 requests):

| signal | candidate arm | champion arm |
|---|---|---|
| requests | 173 | 1327 |
| flag rate | 0.3815 | 0.4431 |
| model error rate | 0.0 | 0.0 |
| handler p95 latency ms | 15.124 | 7.955 |

Paired labeled evidence on candidate-served frauds: candidate caught 65/112, champion would have caught 83/112 (exact one-sided McNemar p = 0.001468).

Breaches: on 112 labeled frauds served by the candidate, it caught 65 vs 83 for the champion (recall 0.58 vs 0.74, McNemar p=0.0015)

Feature drift in that window: major (max PSI 2.2982). Drift is an alert, not the rollback trigger: both arms saw the same traffic.

## Timeline

| seq | time | event | actor | simulated | summary |
|---|---|---|---|---|---|
| 20 | 2026-11-09 06:14:53 UTC | `release.created` | oncall-operator | no | {"candidate": "v3-hgb-fresh", "champion": "v2-hgb", "state": "registered"} |
| 21 | 2026-11-09 06:14:56 UTC | `release.gate_evaluated` | oncall-operator | no | offline gate PASSED |
| 22 | 2026-11-09 06:14:56 UTC | `release.staged` | oncall-operator | no | {"loaded": "v3-hgb-fresh", "smoke_score": 0.233433} |
| 23 | 2026-11-09 06:14:56 UTC | `release.shadow_started` | oncall-operator | no | {"candidate": "v3-hgb-fresh", "champion": "v2-hgb", "generation": 5} |
| 24 | 2026-11-09 06:15:04 UTC | `release.shadow_evaluated` | oncall-operator | no | shadow: 600 requests, JS 0.0109, disagreement 0.005, passed=True |
| 25 | 2026-11-09 06:15:04 UTC | `release.canary_started` | oncall-operator | no | canary 10% to v3-hgb-fresh |
| 26 | 2026-11-09 06:15:19 UTC | `guardrail.evaluated` | controller | yes | window 1: healthy |
| 27 | 2026-11-09 06:15:32 UTC | `guardrail.evaluated` | controller | yes | window 2: healthy |
| 28 | 2026-11-09 06:15:46 UTC | `guardrail.evaluated` | controller | yes | window 3: healthy (feature drift major, max PSI 2.1998) |
| 29 | 2026-11-09 06:15:46 UTC | `drift.alert` | controller | yes | {"max_psi": 2.1998, "note": "affects both arms; not a rollback trigger", "window_index": 3} |
| 30 | 2026-11-09 06:16:03 UTC | `guardrail.evaluated` | controller | yes | window 4: breach — on 112 labeled frauds served by the candidate, it caught 65 vs 83 for the champion (recall 0.58 vs 0.74, McNemar p=0.0015) (feature drift major, max PSI 2.2982) |
| 31 | 2026-11-09 06:16:03 UTC | `drift.alert` | controller | yes | {"max_psi": 2.2982, "note": "affects both arms; not a rollback trigger", "window_index": 4} |
| 32 | 2026-11-09 06:16:03 UTC | `rollback.executed` | controller | no | rollback (guardrail_breach): traffic → v2-hgb; on 112 labeled frauds served by the candidate, it caught 65 vs 83 for the champion (recall 0.58 vs 0.74, McNemar p=0.0015) |
| 33 | 2026-11-09 06:16:03 UTC | `rollback.verified` | controller | no | 50/50 verification predictions served by v2-hgb |

## What went well / what to change

- The guardrail used paired labeled evidence and a significance test, so it did not roll back on noise.
- Rollback was a single compare-and-swap of the deployment pointer; verification used real predictions.
- Gap: the offline gate cannot see defects confined to a segment that is rare in the frozen test split.
  Follow-up: add a label-maturity check to training data validation (see docs/DRIFT_AND_LIMITATIONS.md).

Event chain: first `204edfdbe5485dcd…` last `90ce9d0f27cba85d…`
