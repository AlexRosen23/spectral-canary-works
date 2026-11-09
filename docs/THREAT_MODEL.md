# Threat model

**Scope:** a local, single-tenant release lab. Assets: the model artifacts, the registry and deployment
pointer, the audit log, operator tokens, the manifest signing key, and the integrity of served predictions.
Non-goals: multi-tenant isolation, internet exposure, enterprise IAM.

| # | Threat | Where | Control in this repo | Test / evidence | Residual risk |
|---|---|---|---|---|---|
| T1 | Malicious artifact runs code on load (pickle RCE) | registration, serving | skops format; any type outside `artifacts.TRUSTED_TYPES` is refused before load | `test_tampered_artifact_cannot_load`; manifests record `untrusted_types_outside_allowlist: []` | an allowlisted sklearn type with an exploitable `__setstate__` |
| T2 | Artifact swapped or tampered after validation | disk | SHA-256 recorded in an immutable registry row, re-checked on every load, before promotion and before rollback onto a target | chaos `corrupted_artifact` (tampered → blocked at gate; truncated → refused at registration) | attacker with write access to both the DB and the artifact |
| T3 | Forged manifest (metrics edited to pass gates) | registry | HMAC-SHA256 signature over the canonical manifest with a local key; verified at load | `test_manifest_signature_detects_edits`; `make verify` | the key lives on the same machine (it's local integrity, not PKI) |
| T4 | Audit history rewritten | events table | append-only triggers + a SHA-256 hash chain over every event | `test_event_chain_detects_tampering` (dropping the trigger still breaks the chain) | whoever controls the DB can append fake *new* events; external anchoring (e.g. periodic export of the head hash) is the upgrade |
| T5 | Unauthorized promotion or rollback | API | bearer tokens per role; promotion needs `release_manager` plus separation of duties | `test_release_endpoints_require_auth_and_role`, `test_promotion_requires_evidence_role_and_separation_of_duties` | tokens are long-lived local secrets; no rotation and no MFA |
| T6 | Racing operators corrupt the pointer | deployment | compare-and-swap on `generation` inside one transaction with its audit event | `test_concurrent_swaps_exactly_one_wins`, chaos `promotion_race` | none known for a single DB |
| T7 | Chaos endpoints abused to degrade serving | API | off unless `SCW_ENABLE_CHAOS=1`; also needs the operator role; every injection is an audit event marked simulated | `test_chaos_endpoints_disabled_by_default` | an operator who enables chaos on a real deployment |
| T8 | Schema confusion / injection through features | `/v1/predict` | strict schema (types, ranges, categories, no extra fields); rejected before any model call | chaos `schema_break`, `test_schema_rejects` | semantic attacks with valid-but-adversarial inputs (out of scope) |
| T9 | Sensitive data in logs or metrics | telemetry | features are never logged; metric labels are bounded (arm, version, status); auth headers redacted | `test_metrics_have_bounded_labels` | `requests.features_json` in SQLite holds the synthetic inputs for drift analysis; a real deployment would minimize or encrypt it |
| T10 | Secrets committed | git | gitleaks pre-commit and pre-push hooks; `var/` and `.env*` gitignored; a full-history scan before publishing | `.githooks/`, `.gitleaks.toml` | GitHub Free has no server-side push protection |
| T11 | Resource exhaustion by a slow candidate | serving | per-call timeout (500 ms); separate champion and candidate thread pools (bulkhead); fallback to the champion | chaos `candidate_timeout` (all HTTP 200) | many concurrent slow requests can still saturate the candidate lane |
| T12 | Container breakout or privilege | Docker | non-root uid 10001, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, 127.0.0.1-only ports | container smoke test (`id` → uid 10001) | kernel-level escapes |
| T13 | Cloud blast radius (if deployed) | AWS path | deployer policy scoped to named resources and one region; SG ingress from a single CIDR (0.0.0.0/0 rejected by validation); IMDSv2; no SSH, SSM only; budget alerts | `infra/aws/*.tf`, `deployer-policy.json` (validated, **not applied**) | `ec2:*` Describe/Create on `*` is needed for VPC creation |

## Trust boundaries

1. Caller → gateway (HTTP, localhost only).
2. Gateway → artifact store (filesystem, verified by hash).
3. Operator → release endpoints (bearer token, role).
4. Training → registry (the manifest is signed at registration, not trusted from the trainer).

Interop artifacts (e.g. from Morrowglass) enter at boundary 4 and get no special trust.
