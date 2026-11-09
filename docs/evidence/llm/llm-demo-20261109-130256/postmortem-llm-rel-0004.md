# Postmortem: llm-rel-0004 (triage-p2-ctx4k vs champion triage-p2)

_LLM triage lane. Generated from the hash-chained event log by `spectral_canary.llm.postmortem`. Blameless._

> Alert traffic in this release was 100% **simulated** (seeded synthetic alerts with rule-derived labels). Every model call, grade, routing decision, SLO check and the rollback are real executions against a local Ollama server.

## Summary

- **Outcome:** rolled_back
- **Trigger:** guardrail_breach
- **Reason recorded:** latency SLO: candidate model p95 2830 ms > budget 2000 ms and 2.11x the champion's 1342 ms in the same window (limit 1.5x)
- **Recovery verification:** 20/20 real triage calls answered by `triage-p2`; readiness=True; passed=True

## Impact (from request telemetry)

- Requests in release: 270
- Answered by the candidate: 20 (plus 0 candidate failures answered by the champion)
- Tokens spent on candidate answers: 9096
- Labeled must-escalate alerts answered by the candidate: 6; escalations it missed that the champion made: 0

## Detection evidence

Window 1 (200 routed requests):

| signal | candidate | champion arm |
|---|---|---|
| model calls | 20 | 180 |
| failures (error/timeout/invalid) | 0 | 0 |
| model-call p95 ms | 2829.506 | 1342.31 |
| mean input / output tokens | 407.0 / 47.8 | 407.1 / 47.5 |
| notional cost per 1k calls (USD) | 0.05982 | 0.0597 |
| escalation rate | 0.5 | 0.5667 |

Paired online grading so far: 16 labeled candidate answers; on 6 must-escalate alerts the candidate escalated 5, the champion 5 (McNemar p = 1.0).

Breaches: latency SLO: candidate model p95 2830 ms > budget 2000 ms and 2.11x the champion's 1342 ms in the same window (limit 1.5x)


## Timeline

| seq | time | event | actor | simulated | summary |
|---|---|---|---|---|---|
| 21 | 2026-11-09 19:15:07 UTC | `release.created` | oncall-operator | no | {"candidate": "triage-p2-ctx4k", "champion": "triage-p2", "state": "registered"} |
| 22 | 2026-11-09 19:15:07 UTC | `release.gate_evaluated` | oncall-operator | no | offline gate PASSED |
| 23 | 2026-11-09 19:15:10 UTC | `release.staged` | oncall-operator | no | {"loaded": "triage-p2-ctx4k", "model_digest": "a80c4f17acd55265", "smoke_latency_ms": 2845.3, "smoke_reply_valid": true} |
| 24 | 2026-11-09 19:15:10 UTC | `release.shadow_started` | oncall-operator | no | {"candidate": "triage-p2-ctx4k", "champion": "triage-p2", "generation": 5} |
| 25 | 2026-11-09 19:20:39 UTC | `release.shadow_evaluated` | oncall-operator | no | shadow: 70 requests, candidate failure rate 0.0, escalate agreement 1.0, passed=True |
| 26 | 2026-11-09 19:20:39 UTC | `release.canary_started` | oncall-operator | no | canary 10% to triage-p2-ctx4k |
| 27 | 2026-11-09 19:25:32 UTC | `guardrail.evaluated` | controller | yes | window 1: breach — latency SLO: candidate model p95 2830 ms > budget 2000 ms and 2.11x the champion's 1342 ms in the same window (limit 1.5x) |
| 28 | 2026-11-09 19:25:32 UTC | `rollback.executed` | controller | no | rollback (guardrail_breach): traffic → triage-p2; latency SLO: candidate model p95 2830 ms > budget 2000 ms and 2.11x the champion's 1342 ms in the same window (limit 1.5x) |
| 29 | 2026-11-09 19:25:52 UTC | `rollback.verified` | controller | no | 20/20 verification predictions served by triage-p2 |

## What went well / what to change

- The SLOs compared both arms in the same window on the same machine, so a busy laptop alone could not
  trigger the rollback.
- Gap: the offline gate report already showed the cost/latency ratio, but the policy treats it as
  advisory (`cost_latency_blocking = false`). Teams that never accept a costlier model should make it
  blocking.
- Rollback was one compare-and-swap of the lane's deployment pointer; verification used real model calls.

Event chain: first `90e59fa74d0cfb07…` last `c993090a731b9b26…`
