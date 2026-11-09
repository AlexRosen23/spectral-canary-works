# 90-second recruiter demo

**Setup (once, ~3 min):** `make setup && make train-fixtures && make demo`, then `make serve` and `make web`
in two terminals, and open http://127.0.0.1:5121.

Everything on screen comes from the run `make demo` just recorded. The yellow **SIMULATED TRAFFIC** badge is
accurate: the traffic is a seeded replay, while the predictions, routing, statistics and rollback are real
executions.

| t | show | say |
|---|---|---|
| 0–10 s | header + pointer strip | "This is a release control room for ML models. Right now `v2-hgb` serves, `v1-logreg` is the last known good, and the audit chain of every decision verifies." |
| 10–25 s | tab **rel-0002 v4 · blocked**, gate table | "A candidate trained with a bad data filter never saw traffic. The offline gate blocked it with actionable reasons: PR-AUC 0.127 below the champion, and recall on electronics 0.336 below." |
| 25–45 s | tab **rel-0003 v3 · rolled back**, release track + charts | "This one *passed* offline: it was within every margin. Shadow and two canary windows on normal traffic were healthy. Then a simulated surge of high-value online purchases started (the shaded band)." |
| 45–70 s | **Incident replay**: press ▶, stop on window 4 | "Feature drift went 'major', but that's only an alert because both arms see it. The decision came from paired evidence: on 124 labeled frauds the candidate served, it caught 72; the champion, scored on the same requests, would have caught 91. Exact McNemar p = 0.0013, so the controller rolled back automatically." |
| 70–85 s | `rollback.executed` → `rollback.verified`, postmortem panel | "Rollback is one compare-and-swap of the deployment pointer, written in the same transaction as this audit event. Then 50 real predictions confirmed v2 serves, and the postmortem was generated from the log, with no hand-written numbers." |
| 85–90 s | **Model registry** → "card & repro" drawer | "Every version is immutable, hash-verified on load, and traceable to its config, seed, dataset hash and commit." |

## If they ask "is any of this real?"

- **Real:** four trained models (held-out metrics in `make eval`), every prediction, the routing headers
  (`X-Model-Version`), the guardrail statistics, the rollback, the chaos outcomes, and load-test measurements on
  this laptop.
- **Simulated:** the traffic and the label delivery (seeded, labeled everywhere), and the injected faults
  (labeled test doubles).
- **Not done:** any cloud deployment. The AWS path is implemented and validated but not provisioned.

## Backup commands (if the UI is not available)

```bash
make demo           # prints expected vs actual and the postmortem path
make chaos-replay   # every failure scenario with PASS/FAIL
cat var/runs/latest/reports/postmortem-rel-0003.md
```
