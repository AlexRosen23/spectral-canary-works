# Interview defense: 15 system-design and incident-response questions

Each answer points to code or a recorded artifact, so it can be checked rather than taken on trust.

**1. How do you decide a model is safe to deploy?**
With four sequential gates (`RELEASE_POLICY.md`). Offline non-inferiority and integrity
(`gates.run_offline_gate`); shadow on identical traffic with zero user impact; a 10% sticky canary with
statistical guardrails; and human promotion with separation of duties. "Better on one metric" is never
enough: the candidate has to be non-inferior on PR-AUC, recall and F1 within explicit margins, with no slice
regressing past tolerance.

**2. How do you know it's degrading in production when labels are delayed?**
Label-free signals first: the flag-rate difference between randomized arms, with a CI, plus score
divergence. Labeled evidence follows once enough labels arrive: paired McNemar on candidate-served frauds.
Below 20 labeled positives per arm the system records `insufficient` and makes no accuracy claim
(`monitor.evaluate_canary_window`).

**3. Why a paired test instead of comparing arm recall?**
Ten percent of 6%-fraud traffic produces few labeled frauds. Scoring the champion on the same
candidate-served requests removes between-request variance, so the test has power at small n
(`ARCHITECTURE_DECISIONS.md` ADR-5). The cost is one extra model call on 10% of traffic. The arm-vs-arm
Newcombe CI is still reported.

**4. Walk me through the rollback.**
One SQLite transaction does a compare-and-swap on `deployment.generation`, sets the release state, and
appends the `rollback.executed` event with its evidence. The in-memory snapshot is replaced. Fifty real
predictions plus readiness confirm the champion is serving. The postmortem is generated from the event log
(`controller.Controller.rollback`). It's idempotent, so a duplicate request is a recorded no-op.

**5. What happens to in-flight requests during a swap?**
Each request captures one immutable `Snapshot` (pointer + model objects) at its start and finishes on it.
Models aren't unloaded while referenced. `test_in_flight_requests_survive_pointer_swaps` hammers the gateway
from 4 threads during a rollback and asserts zero errors.

**6. Two operators click approve and rollback at the same moment.**
The CAS on generation lets exactly one win; the loser sees `ConflictError`, re-reads, and either no-ops or
refuses. Chaos `promotion_race` and `test_concurrent_swaps_exactly_one_wins` (8 threads) cover it.

**7. The process crashes mid-canary. Then what?**
All state is in the DB. On restart `recover()` resumes window counters if the release heartbeat is recent;
past `stuck_after_seconds` it reverts traffic to the champion (`release.stuck_reverted`). It also reconciles a
pointer that says canary for a release that is no longer active. Chaos `restart_recovery` uses a fake clock.

**8. How do you stop a corrupted or malicious artifact from ever serving?**
skops with a type allowlist, so no pickle execution. A SHA-256 recorded in an immutable row and re-checked on
every load, before promotion and before rollback onto a target. HMAC-signed manifests. Chaos
`corrupted_artifact`: a byte-flipped artifact is blocked at the gate, and a truncated one is refused at
registration.

**9. Why didn't the offline gate catch v3?**
It structurally couldn't. The defect (immature labels for card-not-present transactions above $1,200)
affects a segment holding about 3% of frozen-test positives, so offline PR-AUC moved only −0.020, inside the
0.03 margin. Under the simulated surge that segment is about a quarter of the positives. That gap is the
reason canaries exist. The fix is upstream: a label-maturity check in training-data validation.

**10. Why not Kubernetes?**
One CPU model behind one gateway gets model-level canaries from the application. A cluster adds a control
plane, ingress, RBAC and upgrades for a single replica (ADR-2). The gateway is nearly stateless, so moving it
later is mechanical. The only state to move is the window counter.

**11. How would you scale this to 10× traffic?**
Measure first (`LOAD_TEST_RESULTS.md` names the bottlenecks). Then: N gateway replicas (the snapshot already
re-reads the shared pointer); a single controller leader for guardrail windows; Postgres for
registry/pointer/events; telemetry through a queue into columnar storage; and a model server for heavier
models.

**12. A candidate gets slow. Do you roll back on latency?**
Only when it's attributable to the candidate. The breach needs the candidate's model-call p95 over 50 ms
**and** more than 6× the champion's in the same window. On this shared laptop the same v2 artifact measured
p95 6.4 ms in one run and 37.7 ms in another, purely from machine load, so an absolute budget alone would be
judging the laptop, not the model. Timeouts (500 ms)
fall back to the champion via a separate thread pool (bulkhead), so users still get answers.

**13. Feature drift fires. Do you roll back?**
No. Drift is an alert, because both arms see the same shifted traffic. Rollback decisions compare candidate
against champion on that traffic. Chaos `distribution_shift` shows PSI "major" with no traffic movement.

**14. How do you make results reproducible and auditable?**
A seeded generator with a committed dataset hash; configs hashed into manifests; the threshold chosen on
validation only; test-prediction hashes re-checked at gate time *and* inside the Linux Docker build (the
same hashes as macOS); a hash-chained append-only event log; and a policy SHA-256 on every release event.
`test_replay_is_deterministic` replays the full story twice and compares every decision and every piece of
labeled evidence.

**15. What would you not claim from this project?**
Production scale, uptime, real fraud performance, or cloud deployment. The data is synthetic, traffic is
simulated (labeled as such everywhere), latency comes from a shared, loaded laptop, and the AWS path is
implemented but not provisioned. What is real: the trained models, the inference, routing, statistics,
rollback execution, chaos outcomes, and the load-test measurements on this machine.
