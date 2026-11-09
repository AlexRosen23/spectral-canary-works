# Drift, monitoring definitions and limitations

**How do you know a model is degrading?** You look at three kinds of evidence, in order of how quickly they
arrive and how much they can prove.

| signal | needs labels? | arrives | what it can claim | used as |
|---|---|---|---|---|
| Feature drift: PSI per feature vs the training split | no | immediately | "the inputs no longer look like training data" | **alert only** |
| Prediction drift: flag rate per arm, score JS divergence | no | immediately | "the candidate behaves differently from the champion on comparable traffic" | guardrail (with CI) |
| Labeled quality: paired recall on candidate-served frauds | yes (delayed, partial) | after label delay | "the candidate misses frauds the champion catches" | guardrail (McNemar p < 0.01) |

## Definitions

- **PSI**, with deciles from the training split: `Σ (a−e)·ln(a/e)`, with bins clipped at 1e-4. Thresholds
  (industry rule of thumb, not a law): < 0.10 stable, 0.10–0.25 moderate, > 0.25 major. Categorical and
  boolean features use their category frequencies. Fewer than 500 rows prints a caveat.
- **Jensen–Shannon divergence** of candidate vs champion score histograms (20 bins on [0,1], base 2, range
  [0,1]), computed on identical requests during shadow.
- **Flag rate** is the share of requests at or above the model's own threshold. The comparison is arm vs arm
  in the same window, with a Newcombe 95% CI on the difference.
- **Recall (labeled)** is computed on requests whose ground-truth label has arrived. In the simulation labels
  arrive 120 requests later and 20% never arrive, so labeled metrics always lag and always cover a subset.
  The UI hides buckets with fewer than 5 labeled frauds rather than plotting noise.

## Why drift never triggers a rollback on its own

In a canary both arms see the same traffic. Feature drift therefore says nothing about whether the candidate
is worse than the champion, only that the world moved. The chaos scenario `distribution_shift` checks this:
shifted traffic with no candidate reaches PSI "major" and moves no traffic. A drift alert is a reason to
start a retraining or investigation, not to swap models.

## Offline truth vs production estimates

- **Offline truth:** the frozen test split has every label. Model cards, `make eval` and the offline gate
  report these numbers with 95% intervals.
- **Production estimates:** live traffic has delayed, partial labels. The guardrail makes a quality claim
  only when the labeled sample is large enough (≥ 20 labeled positives per arm) and the test is significant.
  Otherwise the evaluation records `insufficient` and promotion stays blocked.

## Known limitations (stated plainly)

1. **Synthetic data.** The dataset comes from a documented generator (see `DATA_CARD.md`). The metrics
   describe this generator and nothing about real fraud. The upside is known ground truth, which lets the
   demo deliver delayed labels; the downside is that real fraud has adversaries and feedback loops this does
   not model.
2. **The offline gate cannot see what the frozen split under-represents.** v3's defect (immature labels for
   card-not-present transactions above $1,200) sits in a segment holding about 3% of test positives, so
   offline it costs only −0.020 PR-AUC, inside the margin. Under the simulated surge that segment holds about
   a quarter of the positives. The canary caught it; the offline gate structurally could not. The follow-up
   is a label-maturity check in training-data validation.
3. **Small samples.** A 10% canary sees about 6% fraud in baseline traffic, so labeled evidence accumulates
   slowly (25 labeled candidate-arm frauds after 6,000 routed requests in a measured run). That is why promotion needs labels, and why the
   label-free flag-rate guardrail exists.
4. **Clustered samples.** Routing is sticky per card and one card sends many requests, so requests are
   not independent. Treating them as independent made a CI far too narrow and rolled back a healthy
   candidate during the c=32 load test (146 requests from 38 cards, which happened to contain no risky
   transactions). The guardrails now test one observation per card. Cards within an arm can still be
   correlated in time (bursty fraud rings), which this does not model.
5. **Multiple testing.** Guardrails are re-evaluated every window without alpha-spending. The strict
   p < 0.01 per evaluation limits but does not eliminate inflated false-alarm risk over many windows. A
   sequential test (e.g. mSPRT) is the principled upgrade.
6. **Latency figures come from a shared, heavily loaded laptop.** The load average was recorded during
   runs and reached 50–95 because other workloads were running. They are useful for relative comparison
   (arms in the same window), not as capacity planning numbers.
7. **Single-process controller.** Guardrail windows are counted in the API process. Several replicas would
   need the counter moved into the shared store (see `ARCHITECTURE_DECISIONS.md`).
8. **Calibration under shift is not guaranteed.** Isotonic calibration is fit on training folds. Under the
   surge, base rates rise from 6.2% to 41.4% (measured), so the scores are no longer calibrated probabilities.
