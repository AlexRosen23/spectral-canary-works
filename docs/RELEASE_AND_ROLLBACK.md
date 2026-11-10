# Release and rollback mechanics

The rules are in `RELEASE_POLICY.md`; this document covers the mechanics. Every step below is one HTTP call,
and every call writes an event.

## Happy path (v1 → v2), as `make demo` executes it

```bash
H="Authorization: Bearer $OPERATOR_TOKEN"
curl -X POST :5120/v1/releases -H "$H" -d '{"candidate_version":"v2-hgb"}'   # registered → offline gate → validated
curl -X POST :5120/v1/releases/rel-0001/stage  -H "$H"     # verified load + golden smoke
curl -X POST :5120/v1/releases/rel-0001/shadow -H "$H"     # pointer: mode=shadow (CAS)
#   … traffic: the champion answers; the candidate is scored after and recorded
curl -X POST :5120/v1/releases/rel-0001/canary -H "$H"     # shadow gate, then pointer: mode=canary, fraction=0.10
#   … traffic: 10% of request keys served by v2; every 1,500 routed requests → guardrail.evaluated
curl -X POST :5120/v1/releases/rel-0001/approve -H "Authorization: Bearer $RELEASE_MANAGER_TOKEN"
#   → re-verify the artifact hash → pointer: champion=v2, last_known_good=v1, mode=none → 50 verification predictions
```

Responses carry `X-Model-Version`, `X-Release-Arm` (`champion|candidate`), `X-Deployment-Generation` and
`X-Trace-Id`, so you can trace which version served any request.

## Failure path (v2 → v3) and automatic rollback

1. v3 passes the offline gate (inside every margin; see `INCIDENT_POSTMORTEM.md` for the recorded numbers),
   shadow and two healthy baseline windows.
2. A SIMULATED distribution shift starts. In the next windows, paired labeled evidence shows v3 missing
   frauds that v2 catches (McNemar p < 0.01).
3. `evaluate()` records `guardrail.evaluated` (breach), then `rollback()`:
   - a CAS swap of the pointer: `candidate=None, mode=none, fraction=0`, plus the release state `rolled_back`,
     plus the `rollback.executed` event, **all in one SQLite transaction**;
   - the in-memory snapshot is refreshed (in-flight requests finish on the snapshot they started with);
   - verification: 50 real predictions through `InferenceService.predict` must all be served by the champion,
     and readiness must pass → `rollback.verified`;
   - the postmortem is generated from the event log → `postmortem.written`.
4. 200 more shifted requests after the rollback are all served by v2 (asserted in `release_story`).

## Failure modes and how each is handled

| failure | handling | proven by |
|---|---|---|
| racing approve vs rollback | CAS on generation; the loser re-reads and becomes a no-op or a conflict | chaos `promotion_race`, `test_concurrent_swaps_exactly_one_wins` |
| repeated rollback requests | idempotent; `rollback.duplicate_ignored` is audited; generation unchanged | chaos `repeat_rollback` |
| process crash mid-canary | state is persisted; a restart within 30 min resumes the window counters; later, `release.stuck_reverted` sends traffic back to the champion | chaos `restart_recovery` (fake clock) |
| stale evidence | promotion refused when there's been no routed traffic for 15 min | `Controller.approve` |
| unhealthy candidate node (load fails) | snapshot marks the candidate unavailable; canary requests fall back to the champion; the error budget trips the guardrail | `Snapshot.candidate_error`, chaos `corrupted_artifact` |
| artifact changed after validation (race/tamper) | hash re-verified at approval; on mismatch, automatic rollback + refusal | `Controller.approve` |
| candidate timeout | the 500 ms timeout abandons the call; the champion answers (HTTP 200, `X-Fallback`) | chaos `candidate_timeout` |
| champion failure | fail closed: HTTP 503 + `Retry-After`; never a fabricated score | chaos `partial_outage` |
| pointer says canary but release isn't active | `recover()` reconciles the pointer to `mode=none` | `recovery.pointer_reconciled` |
| champion artifact corrupted | `recover()` reverts to `last_known_good` if that verifies | `recovery.champion_reverted` |
| gateway saturated during canary | champion p95 over `saturation_champion_p95_ms` and only timeouts over the error budget → `inconclusive_saturated`, release **paused** (candidate out of traffic, no rollback, no postmortem); real errors and quality breaches still roll back | chaos `gateway_saturation`; `LOAD_TEST_RESULTS.md` finding 5 |
| clustered traffic (repeat cards) | statistics use one observation per card | `test_guardrail_counts_cards_not_requests` |
| guardrail on small samples | `insufficient_evidence`; no claim, no promotion | `test_guardrail_ignores_small_samples` |
