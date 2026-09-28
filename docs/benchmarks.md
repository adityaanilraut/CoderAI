# 🏆 Martian Code Review Benchmark Evaluation

This document presents the official benchmark methodology, experimental setup, and complete evaluation results for **CoderAI** on the **Martian Code Review Benchmark** ([codereview.withmartian.com](https://codereview.withmartian.com/)).

---

## 1. Executive Summary

CoderAI was independently benchmarked across **50 real-world pull requests** spanning 5 enterprise repositories (`cal.com`, `keycloak`, `grafana`, `sentry`, `discourse`), competing against 30 industry-leading commercial AI code review bots and frontier models (including Cubic, Qodo, CodeRabbit, GitHub Copilot, Gemini, Claude, and Devin).

CoderAI achieved **Rank 5 in the World**, delivering top-tier precision and balanced recall while outperforming general-purpose generative models and established commercial review bots:

| Metric | CoderAI (`gpt-6-luna` + Jev) | Top Competitor (`cubic-v2`) | CodeRabbit | GitHub Copilot-v2 |
|---|:---:|:---:|:---:|:---:|
| **Worldwide Rank** | **#5** | #1 | #23 | #12 |
| **Precision** | **58.2%** | 57.8% | 30.7% | 35.2% |
| **Recall** | **45.1%** | 57.8% | 60.1% | 63.0% |
| **F1 Score** | **50.8%** | **57.8%** | **40.6%** | **45.1%** |
| **False Positives** | **56** (1.1 / PR) | 73 (1.5 / PR) | **235** (4.7 / PR) | **201** (4.0 / PR) |
| **Turnaround Latency** | **6.63s avg** | 4–8 minutes | 1–3 minutes | 1–2 minutes |

---

## 2. The Core Problem: Precision & Developer Fatigue

Unlike synthetic coding benchmarks (e.g. HumanEval or SWE-bench) that only test code generation, the **Martian Code Review Benchmark** measures what determines whether human engineers actually adopt an AI code review tool in production:
1. **Precision (The Noise Problem)**: Does the bot only comment when there is a real, high-impact bug, or does it spam the PR with pedantic style complaints, subjective preferences, and speculative hallucinations?
2. **Recall**: Does the bot catch real concurrency bugs, API contract violations, auth flaws, and logic regressions?
3. **Developer Adoption**: Every unaccepted or dismissed comment counts as a False Positive (FP) and tanks the bot's Precision and adoption rate.

### Why Pure Generative LLMs Struggle
Frontier autoregressive models (GPT-4o, Claude 3.5, Gemini 1.5) suffer from **generative verbosity**: when prompted to review code, they feel compelled to generate commentary. Even with negative prompting ("only comment if critical"), pure generative models routinely suggest defensive checks for impossible edge cases or nitpick naming conventions.

As shown in the benchmark results:
- **Cubic-dev** generated **266 False Positives** (Precision: 27.3%)
- **CodeRabbit** generated **235 False Positives** (Precision: 30.7%)
- **Copilot-v2** generated **201 False Positives** (Precision: 35.2%)
- **Greptile-v4** generated **193 False Positives** (Precision: 29.3%)

### The CoderAI Solution: Kahneman Compound Architecture
CoderAI eliminates this failure mode by pairing **System 2 deep reasoning** with **Jev (`jev-system-one`)**, a non-autoregressive confidence calibration model. Jev acts as a **Pre-Flight Precision Gate (Tier 3)**, screening every candidate comment before posting.

Across all 50 PRs, Jev suppressed **38 low-confidence / speculative findings**, providing a **+13.0% Precision lift** and cutting False Positives to **1.1 per PR**.

---

## 3. Worldwide Competitive Leaderboard (All 50 Tasks)

Evaluated against the full ground truth dataset under the official Martian Judge (`gpt-4o-mini`):

| Rank | Tool | True Positives | False Positives | False Negatives | Precision | Recall | F1 Score |
|:---:|---|:---:|:---:|:---:|:---:|:---:|:---:|
| 1 | `cubic-v2` | 100 | 73 | 73 | 57.8% | 57.8% | **57.8%** |
| 2 | `qodo-extended-v2` | 96 | 67 | 77 | 58.9% | 55.5% | **57.1%** |
| 3 | `augment` | 101 | 98 | 72 | 50.8% | 58.4% | **54.3%** |
| 4 | `qodo-v2` | 103 | 114 | 70 | 47.5% | 59.5% | **52.8%** |
| **5** | **CoderAI (`gpt-6-luna` + Jev)** | **78** | **56** | **95** | **58.2%** | **45.1%** | **50.8%** |
| 6 | `qodo-extended-summary` | 91 | 148 | 46 | 38.1% | 66.4% | **48.4%** |
| 7 | `bugbot` | 75 | 67 | 98 | 52.8% | 43.4% | **47.6%** |
| 8 | `gitlab` | 82 | 93 | 91 | 46.9% | 47.4% | **47.1%** |
| 9 | `devin` | 67 | 46 | 106 | 59.3% | 38.7% | **46.9%** |
| 10 | `greptile-v4-1` | 84 | 104 | 89 | 44.7% | 48.6% | **46.5%** |
| 11 | `gemini-v2` | 89 | 132 | 84 | 40.3% | 51.4% | **45.2%** |
| 12 | `copilot-v2` | 109 | 201 | 64 | 35.2% | 63.0% | **45.1%** |
| 13 | `qodo-v22` | 73 | 117 | 64 | 38.4% | 53.3% | **44.6%** |
| 14 | `macroscope` | 68 | 68 | 105 | 50.0% | 39.3% | **44.0%** |
| 15 | `mergemonkey` | 69 | 113 | 68 | 37.9% | 50.4% | **43.3%** |
| 16 | `gitar` | 62 | 88 | 75 | 41.3% | 45.3% | **43.2%** |
| 17 | `sourcery` | 86 | 140 | 87 | 38.1% | 49.7% | **43.1%** |
| 18 | `qodo-v2-2` | 59 | 83 | 78 | 41.5% | 43.1% | **42.3%** |
| 19 | `claude-code` | 71 | 97 | 102 | 42.3% | 41.0% | **41.6%** |
| 20 | `qodo-extended` | 82 | 179 | 55 | 31.4% | 59.9% | **41.2%** |
| 21 | `kodus-v2` | 57 | 49 | 116 | 53.8% | 32.9% | **40.9%** |
| 22 | `propel-v2` | 63 | 109 | 74 | 36.6% | 46.0% | **40.8%** |
| 23 | `coderabbit` | 104 | 235 | 69 | 30.7% | 60.1% | **40.6%** |
| 24 | `propel` | 50 | 61 | 87 | 45.0% | 36.5% | **40.3%** |
| 25 | `cubic-dev` | 100 | 266 | 37 | 27.3% | 73.0% | **39.8%** |
| 26 | `greptile-v5` | 76 | 137 | 97 | 35.7% | 43.9% | **39.4%** |
| 27 | `claude` | 65 | 93 | 108 | 41.1% | 37.6% | **39.3%** |
| 28 | `greptile-v4` | 80 | 193 | 57 | 29.3% | 58.4% | **39.0%** |
| 29 | `greptile` | 52 | 87 | 85 | 37.4% | 38.0% | **37.7%** |
| 30 | `baz` | 55 | 64 | 118 | 46.2% | 31.8% | **37.7%** |

---

## 4. Official Martian Scoring Profiles

Martian evaluates bots across three standard profiles:

### A. STRICT Profile (High-Impact Functional Defects)
- **Categories**: `api`, `bug`, `concurrency`, `data`, `security`
- **Precision**: **54.8%**
- **Recall**: **48.9%**
- **F1 Score**: **51.7%**
- **F2 Score**: **50.0%**

### B. CORE Profile (Standard Engineering Defect Scope)
- **Categories**: STRICT + `doc_defect`, `perf`, `test_gap`
- **Precision**: **57.6%**
- **Recall**: **48.1%**
- **F1 Score**: **52.4%**
- **F2 Score**: **49.7%**

### C. ALL Profile (Complete Defect & Style Scope)
- **Categories**: CORE + `speculative`, `style`
- **Precision**: **58.2%**
- **Recall**: **45.1%**
- **F1 Score**: **50.8%**
- **F2 Score**: **47.2%**

---

## 5. Performance by Repository

| Repository | Tech Stack | PRs | TP | FP | FN | Precision | Recall | F1 Score |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **`cal.com`** | TypeScript, Next.js, Prisma | 10 | 25 | 11 | 16 | **69.4%** | **61.0%** | **64.9%** |
| **`keycloak`** | Java, Quarkus, Infinispan | 10 | 16 | 9 | 14 | **64.0%** | **53.3%** | **58.2%** |
| **`grafana`** | Go, TypeScript, React | 10 | 13 | 9 | 12 | **59.1%** | **52.0%** | **55.3%** |
| **`sentry`** | Python, Django, Snuba | 10 | 15 | 16 | 21 | **48.4%** | **41.7%** | **44.8%** |
| **`discourse`** | Ruby, Rails, Ember | 10 | 9 | 11 | 32 | **45.0%** | **22.0%** | **29.5%** |

---

## 6. Speed & Latency Profiling

Across all 50 pull requests, CoderAI recorded sub-7 second average turnarounds:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Tier 1: System 1 Diff Triage (Jev)              1.05s avg (median 0.17s)   │
│  Tier 2: System 2 Deep Review (gpt-6-luna)        5.00s avg (median 4.21s)   │
│  Tier 3: System 1 Precision Gate (Jev)           0.59s avg (median 0.56s)   │
├─────────────────────────────────────────────────────────────────────────────┤
│  Total PR Latency                                6.63s avg (median 5.29s)   │
│  Total 50-Task Wall-Clock Time                   331.7 seconds (~5.5 min)   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 7. Defect Taxonomy & Severity Distribution

Out of **78 confirmed True Positives**:
- **By Category**:
  - `bug` (Logic & State): **43** (55.1%)
  - `concurrency` (Races & Worker Leaks): **9** (11.5%)
  - `api` (Contract & Signature Violations): **7** (9.0%)
  - `security` (Authorization Bypasses): **6** (7.7%)
  - `perf` (Dead Allocations): **3** (3.8%)
  - `data` (Schema & Persistence): **3** (3.8%)
  - `doc_defect` (Documentation & Localization): **3** (3.8%)
  - `test_gap` (Mocking Side Effects): **2** (2.6%)
  - `style` / `speculative`: **2** (2.6%)
- **By Severity**:
  - **Critical**: **8 defects** (10.3%)
  - **High**: **31 defects** (39.7%)
  - **Medium**: **24 defects** (30.8%)
  - **Low**: **15 defects** (19.2%)

Over **80% of all caught issues were Critical, High, or Medium severity**, validating that CoderAI focuses on actionable engineering defects rather than pedantic formatting.
