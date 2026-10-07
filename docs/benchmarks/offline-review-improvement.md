# Local offline review improvement

The runner is `scripts/run_coderai_local.py`; a standalone copy is available in
`/Users/aditya/Downloads/code-review-benchmark-main/offline/run_coderai_local.py`.
It obtains each diff with `gh pr diff`, invokes the installed workspace CoderAI
CLI with `--trust-project --yolo --quiet --exec`, and reads the final assistant
message from the session log. Reviews are injected as `tool=coderai` into an
isolated benchmark workspace. The existing extraction, deduplication, and judge
modules run unmodified, with `--tool coderai --force`.

Reviewers run in temporary directories containing only the diff. They do not
receive golden comments, evaluations, other tools' reviews, or benchmark files.
The prompt is a general review checklist: changed contracts, state/lifecycle,
performance, executable tests, docs/locales, and a final verification pass. It
requires concrete triggers and consequences and has no finding quota.

## Baseline diagnosis

The current evaluations have 108 false negatives across all categories and 13
PRs with no true positives. Test-gap recall is 0%; performance recall is 16.67%;
documentation-defect recall is 11.11%. There are 53 missed bug comments and nine
missed API comments. The runner computes Core metrics using the same category
set as the benchmark dashboard. The supplied P37.4/R37.6/F1 37.5 is the
all-category metric: the preserved file scores P37.36/R37.57/F1 37.46 exactly.
Core excludes style and speculative categories and scores P36.63/R39.87/F1
38.18. The requested F1 >46 target applies to all categories; Core is reported
as an additional diagnostic.


## Runtime fixes

`--thinking` previously set `CODERAI_THINKING`, whereas the configuration
resolver consumes `CODERAI_THINKING_ENABLED`. `--reasoning-effort` was parsed
but not forwarded to headless settings. Additionally, client creation ignored
explicit thinking configuration whenever a model override was supplied. These
issues prevented explicit thinking settings from reaching the client. Explicit
settings are now preserved while model-specific defaults still apply when
thinking is not explicitly configured. The API additionally rejects Luna reasoning when function tools are attached to
Chat Completions, and the compatibility fallback retries with effort `none`.
The runner now creates a reviewer role with an explicit empty tool allowlist,
since the complete diff is supplied directly. The session factory preserves an
empty allowlist and the soul passes it into schema selection. Real reasoning
usage is recorded with this role (11,265 tokens in the large Sentry review).

Luna's Chat Completions endpoint also rejects the literal effort `max`. The
maximum setting now maps to its accepted `xhigh` level. A direct SDK probe
confirmed the accepted values: none, low, medium, high, and xhigh. The initial
max-effort experiment was stopped after its logs revealed zero reasoning tokens;
it must not be described as an actual maximum-reasoning run.

## Reproduce

From the CoderAI repository:

```sh
.venv/bin/python scripts/run_coderai_local.py --five-failed
.venv/bin/python scripts/run_coderai_local.py
```

From the offline benchmark directory, specify the current CoderAI executable:

```sh
.venv/bin/python run_coderai_local.py \
  --coderai /Users/aditya/Desktop/CoderAI-main/.venv/bin/coderai \
  --five-failed
```

Each run directory saves the prompt, baseline snapshot, per-PR diffs, final
reviews, session logs, pipeline logs, and a `comparison.json`. Completed reviews
are cached; use a new directory when changing the prompt, model, reasoning
settings, or runtime. The default concurrency is one to respect the observed
200,000 token/minute provider limit. Rate-limit failures are retried.

The five selected PRs were shutouts from Sentry (two), Keycloak, Cal.com, and
Grafana. Their baseline is TP0/FP14/FN21 under Core. This is a diagnostic sample,
selected using baseline outcomes; it cannot establish a full leaderboard rank.

The default review supplies the complete PR diff without tools and enables xhigh
reasoning with two independent verifiers. `--chunk-bytes 12000` optionally groups complete file diffs. A final
pass checks findings against the complete diff. Temperature is omitted for Luna requests, including when reasoning is off,
because its endpoint rejects temperature zero. Candidate deduplication and judging remain
the unchanged benchmark stages. Use `--chunk-bytes 0` to disable grouping.

## Diagnostic five-PR results (Core)

| Variant | TP | FP | FN | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Preserved baseline | 0 | 14 | 21 | 0.00% | 0.00% | 0.00 |
| v1, broad checklist | 9 | 15 | 12 | 37.50% | 42.86% | 40.00 |
| v4, local evidence checks | 8 | 14 | 13 | 36.36% | 38.10% | 37.21 |
| v5, file groups + verification | 13 | 38 | 8 | 25.49% | 61.90% | 36.11 |
| v6, stricter verification | 11 | 20 | 10 | 35.48% | 52.38% | 42.31 |
| v7, tool-free high reasoning | 10 | 14 | 11 | 41.67% | 47.62% | 44.44 |

The grouping experiment increased recall but introduced noise. The stricter
verification removes generic test-expansion requests, presentation preferences,
hypothetical callers, residual work in an optimization, and ordinary cache TTL
tradeoffs unless the diff establishes a concrete violated contract. It preserves
actual test setup/assertion defects, contract errors, and wasted computation.

`--drafts-from` reuses blind drafts when changing only verification. Give each
variant a separate run directory; the runner records submitted prompts and
checks model/prompt/effort provenance before reusing completed reviews. Primary
draft reuse also validates the original primary prompt and settings, and removes
old verification artifacts. `--prompt-file`, `--verification-prompt-file`,
`--verification-effort`, and `--skip-verification` support isolated experiments
without modifying the benchmark pipeline. New reports include both Core and
all-category scores.

## Full 50-PR results

| Variant | Core TP | FP | Core FN | Core precision | Core recall | Core F1 | All-category F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Preserved baseline | 63 | 109 | 95 | 36.63% | 39.87% | 38.18 | 37.46 |
| v6, grouped diff, reasoning off | 69 | 145 | 89 | 32.24% | 43.67% | 37.10 | 36.92 |
| v7, complete diff, tool-free high reasoning | 71 | 99 | 87 | 41.76% | 44.94% | 43.29 | 42.77 |
| v8, verification with additional discovery | 72 | 135 | 86 | 34.78% | 45.57% | 39.45 | 39.16 |
| v9, skeptical critique | 65 | 97 | 93 | 40.12% | 41.14% | 40.62 | 40.24 |
| v10, primary review only | 70 | 109 | 88 | 39.11% | 44.30% | 41.54 | 41.57 |
| v14b, direct confidence filter | 62 | 90 | 96 | 40.79% | 39.24% | 40.00 | 39.63 |
| v12, xhigh discovery and validation | 81 | 114 | 77 | 41.54% | 51.27% | 45.89 | 45.70 |
| v16, quality discovery plus validation | 66 | 119 | 92 | 35.68% | 41.77% | 38.48 | 41.30 |
| v17, broad quality supplement | 82 | 180 | 76 | 31.30% | 51.90% | 39.05 | 41.35 |
| v18, narrow behavior/quality supplement | 77 | 128 | 81 | 37.56% | 48.73% | 42.42 | 44.04 |
| v19, identifier-only supplement | 80 | 126 | 78 | 38.83% | 50.63% | 43.96 | 45.08 |
| v21, xhigh primary review only | 82 | 131 | 76 | 38.50% | 51.90% | 44.20 | 44.10 |
| v22, xhigh discovery / high validation | 78 | 116 | 80 | 40.21% | 49.37% | 44.32 | 44.20 |
| v15, high reasoning / 12 KB file groups | 71 | 122 | 87 | 36.79% | 44.94% | 40.46 | 40.54 |
| v20, functional supplement | 83 | 137 | 75 | 37.73% | 52.53% | 43.92 | 44.22 |

The extra discovery sweep recovered only one additional Core match while adding
36 false positives. It was rejected. Full-run all-category scores are the release criterion;
the five shutouts are a diagnostic sample. The v8 five-PR sample scored
TP11/FP15/FN10, F1 46.81; this does not establish the requested full-set target.

The benchmark dashboard currently contains peer results judged by other models.
A same-judge rank for Luna cannot be inferred from those leaderboards. Report
full-set all-category and Core F1 separately from any leaderboard rank.

The v15 five-PR benchmark was run from the Downloads offline directory with
12 KB file groups, genuine high reasoning, and high-effort validation. It scored
all-category TP13/FP19/FN9, P40.62/R59.09/F1 48.15. Core was TP12/FP19/FN9,
F1 46.15. The preserved all-category baseline for those PRs is TP0/FP14/FN22.
The matching full 50-PR run scored all-category TP75/FP122/FN98,
P38.07/R43.35/F1 40.54, so that configuration was rejected.
The later v25 combination reached all-category F1 46.07; see the current configuration below.

`--refine-from` appends a blind supplemental pass to previous final reviews.
The source review body and prompt hashes are recorded; neither evaluation data
nor golden comments are copied into reviewer input. Existing review text is
retained, with new items numbered after it. Broad supplements were rejected for
false positives; all-category precision remains part of the acceptance gate.

## Current reproducible configuration

The default is complete-diff xhigh discovery followed by two independent xhigh
verifiers. The first uses the v12 precision filter; the second uses the v23
revision. Both receive exactly the same discovery draft and complete diff; the
second never receives the first verifier's answer. Their outputs are appended
uniformly with continuous numbering. The unchanged benchmark deduplicates the
combined findings. There is no per-PR selection or gold-informed filtering.
The historical v12 and v23 runs share identical discovery drafts for all 50 PRs.

v23 full scored all-category TP84/FP116/FN89, P42.00/R48.55/F1 45.04.
v25 uniformly combined v12 and v23 and scored TP88/FP121/FN85,
P42.11/R50.87/F1 46.07 (Core TP84/FP121/FN74, F1 46.28).
This is a narrow measured margin, not evidence of stable repeat performance.
v24 is incomplete and is excluded from winner comparisons.

Run from the Downloads offline directory with its standalone runner (the source
runner also defaults to the same two-verifier configuration; it has an optional
PR-head context mode that defaults off and was not used in these results):

```sh
.venv/bin/python run_coderai_local.py \
  --coderai /Users/aditya/Desktop/CoderAI-main/.venv/bin/coderai \
  --run-dir results/coderai-dual-fresh-full
.venv/bin/python run_coderai_local.py \
  --coderai /Users/aditya/Desktop/CoderAI-main/.venv/bin/coderai \
  --five-failed --run-dir results/coderai-dual-final-five
```

Use `--single-verifier` for historical single-verifier experiments. Explicit
verification-prompt overrides and draft/refinement reuse retain single-verifier
semantics. Fresh default runs need no source results. Prompts, diff hashes,
configuration manifests, both verifier outputs, sessions and usage are saved.
Resuming a partial run rejects changed inputs/settings; completed cache entries
also re-fetch the diff and reject changes. Use a new directory after runtime
changes. Combine-from validates legacy prompt hashes, submitted verification
inputs and final assistant bodies against saved sessions, then checks both
source diffs against fresh `gh pr diff` output. Only blind review artifacts are
used as model inputs. Stages 2, 2.5 and 3 always run with `--tool coderai --force`.

Canonical results must be snapshotted before promotion. A complete validated full-set result
must meet all-category precision >=37.36% and F1 >46 before it can replace the
canonical result; repeated runs are needed to establish a stable margin. The
five baseline shutouts are diagnostic, not a release criterion or rank estimate.
Peer results judged by different models, including deepseek-flash (v4.1), do not
support a same-judge leaderboard ranking.

## Provenance revalidation

`results/coderai-dual-provenance-full` validates all 100 legacy source reviews,
re-fetches all 50 diffs and reproduces all 50 v25 combined bodies exactly.
GitHub varied only the length of some `index` blob hash abbreviations; matching
hash prefixes are accepted, while conflicting hashes, modes and source hunks
are rejected. Forced stages 2, 2.5 and 3 completed with zero errors (zero dedup
fallbacks). The repeated all-category score is TP90/FP123/FN83,
P42.25/R52.02/F1 46.63; Core TP86/FP123/FN72, F1 46.87. The changed
score for identical reviews demonstrates downstream extraction/judge variation,
not improved discovery. It supplies a second passing measurement of the combined
review set, but cannot establish that newly generated reviews reliably clear 46.
The independent fresh full run is recorded separately.

The current standalone runner's combination validator was also re-run across
all 50 PRs. All 100 v12/v23 source reviews validated, all 50 source diffs
matched fresh GitHub diffs, and uniform concatenation reproduced each saved
v25 body exactly. Per-PR hashes are recorded in
`results/coderai-v25-current-provenance-audit.json`.

The preserved original canonical dataset, candidates, dedup groups and evaluations
are in `results/coderai-original-canonical`, with a SHA-256 manifest. Never replace
that snapshot during subsequent promotions.

## Final five baseline shutouts

The final saved runner, including the 1800-second timeout and timeout diagnostics,
was executed from the Downloads offline directory with `--five-failed`. Each
current `gh pr diff` was re-checked against the fresh cached blind reviews, and
all three benchmark stages were forced. The latest all-category result is
TP12/FP17/FN10, P41.38/R54.55/F1 47.06, versus preserved baseline TP0/FP14/FN22.
Core is TP11/FP17/FN10, P39.29/R52.38/F1 44.90. All three stages completed with
zero errors and deduplication had zero fallbacks. An earlier pipeline pass on
these same fresh reviews scored TP12/FP16/FN10, F1 48.00; it is retained in
`comparison-before-timeout-fix.json`. The latest whole pass is reported without
selecting per-PR outcomes between passes. The first pass (also F1 47.06) remains
in `comparison-first-pass.json` and `first-pass-evaluation`.

| PR | TP | FP | FN | All-category F1 |
| --- | ---: | ---: | ---: | ---: |
| ai-code-review-evaluation/sentry-greptile/pull/5 | 2 | 8 | 4 | 25.00 |
| getsentry/sentry/pull/93824 | 3 | 4 | 2 | 50.00 |
| keycloak/keycloak/pull/33832 | 3 | 1 | 1 | 75.00 |
| calcom/cal.com/pull/22532 | 2 | 4 | 2 | 40.00 |
| grafana/grafana/pull/103633 | 2 | 0 | 1 | 80.00 |

Artifacts: `results/coderai-dual-final-five/comparison.json`,
`five-case-scores.json`, `blind-input-audit.json` and `final-configuration.json`.
This baseline-selected five-case sample does not establish full-set performance
or rank. The promoted whole-set provenance run is recorded in
`results/coderai-canonical-promotion.json`; original artifacts remain in the
preserved snapshot. Other tools, gold comments and metadata are unchanged.
The runner prefers that original snapshot for subsequent baseline comparisons.

A separate fresh five-case run was completed with the same default configuration
in `results/coderai-dual-final-five-repeat`. It scored all-category
TP13/FP12/FN9, P52.00/R59.09/F1 55.32; Core TP12/FP12/FN9, F1 53.33. Per-PR
all-category counts were sentry-greptile/5 2/4/4, sentry/93824 4/1/1,
keycloak/33832 3/2/1, cal.com/22532 1/4/3, and grafana/103633 3/1/0
(TP/FP/FN). The earlier complete fresh run scored 12/17/10, F1 47.06.
Both runs used all five PRs uniformly; this visible variation reinforces that
the baseline-selected five-case sample is diagnostic only. The repeat audit
verified 15 prompts and nonzero-reasoning calls, no assistant function calls,
zero stage errors, and zero dedup fallbacks. The original canonical snapshot's
SHA-256 manifest still matches. See that run's `comparison.json`,
`five-case-scores.json`, `blind-input-audit.json`, and
`final-configuration.json`.

The fresh full run first encountered a 900-second discovery timeout on
`discourse-graphite/4`; 49 reviews were saved in that incomplete attempt. It
was resumed with the unchanged model, prompts, xhigh effort and empty tool
allowlist after the default timeout was raised to 1800 seconds. The completed
run is `results/coderai-dual-fresh-full`: all 50 PRs have fresh diffs and
reviews, all 150 model calls recorded nonzero reasoning, and the blind-input
audit found no benchmark gold comments or evaluations in reviewer inputs.
Stages 2, 2.5 and 3 completed. Its all-category result is TP83/FP122/FN90,
P40.49/R47.98/F1 43.92; Core is TP77/FP122/FN81, F1 43.14. This independent
fresh review run does not clear the F1 46 target, so the passing v25 and
provenance-recheck scores do not establish a stable margin above 46. Keep the
preserved canonical baseline unchanged until repeated fresh full runs meet both
the precision and F1 gates. Timeout progress and session diagnostics remain
saved, and partial output was not injected as a review.
