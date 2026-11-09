# Operations runbook

**How do you recover?** You put traffic back on the last known good model with a single compare-and-swap,
prove the recovery with real predictions, and write the postmortem from the audit log. The sections below
cover how to do that and how to confirm it worked.

Base URL `http://127.0.0.1:5120`. `TOKEN` is the operator token from `var/secrets/operators.local.json`
(`RM_TOKEN` is the release manager's).

## First look (any alert)

```bash
curl -s localhost:5120/readyz | jq            # champion loaded + hash verified?
curl -s localhost:5120/v1/overview | jq       # pointer, active release, audit-chain status
curl -s localhost:5120/v1/events?limit=50 | jq '.[] | [.seq,.kind,.actor] | @tsv' -r | tail -20
curl -s localhost:5120/metrics | grep -E 'scw_(requests_total|rollbacks|candidate_fallbacks|schema_errors)'
```

## Playbooks

| symptom | likely cause | action |
|---|---|---|
| `guardrail.evaluated` verdict `breach`, then `rollback.executed` | the controller acted (policy `on_breach=rollback`) | confirm `rollback.verified`, then read `var/runs/<run>/reports/postmortem-<rel>.md`. Do not re-propose the same version. Fix and register a **new** version |
| verdict `breach` + `release.paused` | policy `on_breach=pause` | inspect the evidence, then `POST /v1/releases/<id>/rollback` (or fix and resume through a new release) |
| `scw_candidate_fallbacks_total{reason="candidate_timeout"}` rising | slow or hung candidate | the champion is answering; the error budget will trip the guardrail; roll back manually to speed it up |
| `scw_schema_errors_total` rising | a client sends an old or new schema | 422s with `problems` tell the client what is wrong; ship a schema migration, never a lenient model |
| `/readyz` 503 "champion artifact" | champion bytes changed on disk | restart the gateway: `recover()` reverts to `last_known_good` if that verifies (`recovery.champion_reverted`); otherwise `recovery.no_safe_champion` and an operator must re-register |
| a release stuck in shadow/canary after a crash | the controller died | on restart, a release with no heartbeat for 30 min is reverted to the champion (`release.stuck_reverted`); a quick restart resumes the canary counters |
| `/v1/registry/verify` shows `ok: false` | tampering or a missing artifact | treat as a security incident; do not promote; compare hashes against the committed `manifests/` |
| `scw_telemetry_dropped_total` > 0 | writer queue full (overload) | predictions still served; guardrail evidence is incomplete, so do not promote during the overload |

## Manual rollback (idempotent)

```bash
curl -s -X POST localhost:5120/v1/releases/rel-0003/rollback \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"reason":"operator: candidate misbehaving"}' | jq '{state, rollback_status, verification}'
```

The response includes `verification`: 50 real predictions through the gateway, all expected to come from the
champion, plus readiness. Repeating the call returns `already_rolled_back` and changes nothing.

## Rolling back a promoted champion

The same endpoint on a `promoted` release restores `champion_version` from that release. The target's
artifact hash is re-verified first, so a rollback never lands on tampered bytes.

## Verifying recovery (checklist)

1. `rollback.executed` is followed by `rollback.verified` (`served_by_expected == requests`).
2. `curl -sI -X POST .../v1/predict ...` shows `x-model-version: <champion>`.
3. `/v1/overview` → `deployment.mode == "none"` and `event_chain.ok == true`.
4. The postmortem is written: `postmortem.written` event → `var/runs/<run>/reports/`.

## Chaos drills

`make chaos-replay` runs every failure scenario from a fresh fixture state and exits non-zero if any expected
outcome does not happen. The report is `var/runs/chaos-*/report.json`.
