# 2026-10-23 — Ringer lane defaults proposal

[observed] This is a reading of two weeks of shadow logs, not a measured routing experiment.
[agent-inferred] The table below proposes defaults for Travis to confirm or reject by task type.
No default, model, effort, registry evidence, or Jev criterion is changed by this proposal.

## Proposed decisions

`default` below means **effort unrecorded / CLI default**, not a known fixed effort.
Tokens are median **first-attempt worker tokens**, including failures; n counts first attempts.
Agreement means the logged Jev pick names the actual executed engine/model/effort, among rows
with a pick. It measures agreement with the operator, not correctness.

| Task type | Proposed lane / decision | First-try PASS | Median tokens | n | Jev agreement | Reason |
|---|---|---:|---:|---:|---:|---|
| code-feature | `codex-astra` (gpt-6-astra/default), provisional | 49/57 (86.0%) | 52,718 | 57 | 0/27 | Largest eligible cell; lower token median than the high-effort cells. Observational evidence only. |
| code-fix | `codex-astra` (gpt-6-astra/default), provisional | 5/6 (83.3%) | 35,876 | 6 | 0/5 | Small eligible sample; failures remain in the denominator. |
| docs | `codex-astra` (gpt-6-astra/default), provisional | 4/4 (100%) | 32,747 | 4 | 0/4 | Only eligible docs cell; no evidence this is cheaper than untested write-only lanes. |
| code-review | **New candidate** `codex-sol-6.1-medium` (gpt-6.1-sol/medium), provisional | 7/9 (77.8%) | 64,569 | 9 | 0/9 | Lower median than high effort (104,760; 5/6 PASS). Neither sample establishes superiority. Requires a distinct registry lane, not relabeling gpt-5.6-sol. |
| probe | **No default**; retain explicit per-spec selection | low: 2/3 (66.7%); medium: 11/19 (57.9%) | low: 27,244; medium: 53,418 | 3 / 19 | 0/3; 0/19 | Different workloads and broken checks; low n=3 is an eligibility floor, not sufficient confidence. Haiku n=2 is THIN. |
| research | **No default — THIN** | gpt-6.1-sol/medium: 1/1 (100%) | 52,476 | 1 | 0/1 | No eligible lane cell. |
| test-hardening | **No default — all cells THIN** | Three separate lanes: 1/1 each | 65,506 / 72,674 / 73,951 | 1 each | 0/0; 0/1; 0/1 | Pooling three different lanes into n=3 would conceal the lack of evidence. |
| format-conversion | **No new default — no observations** | unknown | unknown | 0 THIN | undefined | No in-window cell. |

[agent-inferred] These are conservative candidates for a controlled rollout, not an assertion of
an optimal lane. Four-of-four PASS is compatible with substantial underlying failure risk. Even
57 observations share projects, checks, and operator selection; they are not 57 independent
randomized comparisons. Do not infer savings or a success improvement from this table.

## What was measured

[observed] Source code: commit `064b64fdb4b657b358c7691a08ecbd1df0bdfe1c` of Travis's fork.
Sources are the MacBook's `~/.ringer/runs.jsonl` and the Mini's same path (read through the
existing `mini` SSH alias). The window starts **2026-10-09 00:00 America/Los_Angeles (PDT)**
and ends at the read snapshot on October 23. The latest available attempt is **October 23
17:35:56 PDT**, or `2026-10-24T00:35:56.833789+00:00`; this is not October 24 local work.
There is no claim of coverage for the rest of October 23 or continuous daily collection.

- MacBook: 196 in-window attempts. Full-file SHA-256 at read:
  `6f74d8b1f3a8828975ed4690f910cda34a25a170813a0304ca9f3218e25e5d31`.
- Mini: 5 in-window attempts (latest October 13). Full-file SHA-256:
  `af029382b5a6842a362cf1bdd65611930a664e0a74abedbfc286c38d4c278559`.
- Combined: **201 attempts**, **176 first attempts**, **25 retry attempts** excluded from
  first-try denominators and medians. No mocks, cross-source duplicate attempts, conflicting
  duplicate payloads, duplicate first attempts, or groups missing a first attempt were found.
- **136/176 first attempts have a shadow pick; 40 have none.** There is one first attempt
  with no model attribution and no token count. Its token median is unknown, never zero.
- Group by `(run_id, task_key)`; pick the non-retry row. Repeated task keys in different runs
  are separate observations, including re-dispatches of related work. These can be correlated.
- PASS means the harness's logged check passed. It is not an independent production-quality
  assessment. FAIL, ERROR, and TIMEOUT count as first-try failures.

Raw logs/specs, customer data, and machine paths are not copied into this repo.

## Shadow picks are not executed-lane outcomes

[observed] The registry still maps `codex-sol`, `codex-sol-medium`, and `codex-sol-high` to
**gpt-5.6-sol**. Many actual attempts ran **gpt-6.1-sol** or **gpt-6-astra** at explicit medium
or high effort. The astra registry entry has no effort flag, so astra/high and astra/medium
are different executed configurations. An identical family name is insufficient agreement.
Claude versioned model stamps are normalized to their haiku/sonnet family for agreement only.

| Task type | First attempts | Shadow picks | Pick distribution |
|---|---:|---:|---|
| code-feature | 100 | 62 | codex-sol 61; codex-astra 1 |
| code-fix | 26 | 25 | codex-sol 22; codex-astra 2; codex-sol-high 1 |
| code-review | 17 | 17 | codex-sol-high 16; codex-sol 1 |
| docs | 5 | 5 | codex-sol 5 |
| probe | 24 | 24 | codex-sol 13; codex-astra 7; claude-haiku 2; claude-sonnet 1; codex-sol-high 1 |
| research | 1 | 1 | codex-sol-high 1 |
| test-hardening | 3 | 2 | codex-sol 2 |

Overall agreement is **2/136 (1.5%)**, both Haiku probes. The one executed gpt-5.6-sol/default
and one medium row have no shadow pick and are THIN. Thus the 104 picks of codex-sol cannot be
credited with those other models' success or token use. The 19 codex-sol-high and 10 astra picks
also do not establish counterfactual outcomes. There are **no** medium, gemini, or format-conversion
picks in this window. This is a stale lane-question/model mismatch to resolve before changing routing.

## All executed cells

[observed] Every n<3 cell is THIN and supports no default. `0/0` agreement means no pick was
logged, not 0% agreement. Unknown token values are excluded, with coverage stated explicitly.

| Task type | Executed engine/model/effort | First PASS | Median tokens | n | Jev equals executed |
|---|---|---:|---:|---:|---:|
| code-feature | claude/claude-sonnet-5-5/default | 0/1 (0.0%) | 145,239 | 1 THIN | 0/1 |
| code-feature | codex/(blank)/default | 0/1 (0.0%) | unknown (0/1 logged) | 1 THIN | 0/0 |
| code-feature | codex/gpt-5.6-sol/default | 1/1 (100.0%) | 40,951 | 1 THIN | 0/0 |
| code-feature | codex/gpt-5.6-sol/medium | 1/1 (100.0%) | 41,020 | 1 THIN | 0/0 |
| code-feature | codex/gpt-6-astra/default | 49/57 (86.0%) | 52,718 | 57 | 0/27 |
| code-feature | codex/gpt-6-astra/high | 7/16 (43.8%) | 98,569 | 16 | 0/11 |
| code-feature | codex/gpt-6-astra/medium | 11/14 (78.6%) | 48,690 | 14 | 0/14 |
| code-feature | codex/gpt-6.1-sol/high | 6/6 (100.0%) | 113,398 | 6 | 0/6 |
| code-feature | codex/gpt-6.1-sol/low | 0/1 (0.0%) | 21,799 | 1 THIN | 0/1 |
| code-feature | codex/gpt-6.1-sol/medium | 2/2 (100.0%) | 85,104 | 2 THIN | 0/2 |
| code-fix | codex/gpt-6-astra/default | 5/6 (83.3%) | 35,876 | 6 | 0/5 |
| code-fix | codex/gpt-6-astra/high | 0/1 (0.0%) | 35,846 | 1 THIN | 0/1 |
| code-fix | codex/gpt-6-astra/medium | 2/2 (100.0%) | 28,994 | 2 THIN | 0/2 |
| code-fix | codex/gpt-6.1-sol/default | 2/2 (100.0%) | 87,145 | 2 THIN | 0/2 |
| code-fix | codex/gpt-6.1-sol/high | 4/6 (66.7%) | 85,278 | 6 | 0/6 |
| code-fix | codex/gpt-6.1-sol/low | 1/1 (100.0%) | 12,577 | 1 THIN | 0/1 |
| code-fix | codex/gpt-6.1-sol/medium | 5/8 (62.5%) | 56,254 | 8 | 0/8 |
| code-review | codex/gpt-6.1-sol/default | 2/2 (100.0%) | 97,914 | 2 THIN | 0/2 |
| code-review | codex/gpt-6.1-sol/high | 5/6 (83.3%) | 104,760 | 6 | 0/6 |
| code-review | codex/gpt-6.1-sol/medium | 7/9 (77.8%) | 64,569 | 9 | 0/9 |
| docs | codex/gpt-6-astra/default | 4/4 (100.0%) | 32,747 | 4 | 0/4 |
| docs | codex/gpt-6.1-sol/medium | 1/1 (100.0%) | 32,223 | 1 THIN | 0/1 |
| probe | claude/claude-haiku-4-5-20251001/default | 1/2 (50.0%) | 44,502 | 2 THIN | 2/2 |
| probe | codex/gpt-6.1-sol/low | 2/3 (66.7%) | 27,244 | 3 | 0/3 |
| probe | codex/gpt-6.1-sol/medium | 11/19 (57.9%) | 53,418 | 19 | 0/19 |
| research | codex/gpt-6.1-sol/medium | 1/1 (100.0%) | 52,476 | 1 THIN | 0/1 |
| test-hardening | codex/gpt-6-astra/default | 1/1 (100.0%) | 65,506 | 1 THIN | 0/0 |
| test-hardening | codex/gpt-6.1-sol/high | 1/1 (100.0%) | 72,674 | 1 THIN | 0/1 |
| test-hardening | codex/gpt-6.1-sol/medium | 1/1 (100.0%) | 73,951 | 1 THIN | 0/1 |

## Metric limitations and checks

[observed] `scripts/efficiency_report.py` groups by actual model/effort, but its `_tokens`
helper converts missing values to zero. The table above keeps that one unknown cell unknown.
Its baseline aggregate was independently run over the selected first attempts: 176 total,
with feature/astra/default 49 PASS of 57 and median 52,718 confirmed. Every cell's n, PASS
count, and known token median was cross-checked against that helper.

[observed] `scripts/jev_backtest.py` asks Jev again for historical specs, potentially making
new metered calls when the cache misses. Its estimates substitute other tasks' lane averages
for unexecuted picks; its `actual_lane` lookup also cannot name the new gpt-6.1-sol cells from
this registry. No new backtest calls were made. The original October 9 backtest remains historical
context, not evidence that this window's hypothetical picks would have succeeded.

[observed] Probe checks include missing expected files, harness timeout, and browser acceptance
failures. One Haiku failure counted the harness's `worker.log` as an unexpected second file.
Probe is also overloaded: write-only smoke, test/build verification, and live browser diagnosis.
A logged PASS may certify a diagnostic artifact even when it reports a product failure.
Feature includes one setup ERROR and one TIMEOUT; fixes include one TIMEOUT. These are not
removed to improve rates. Without independent check adjudication, model-caused failure rates
cannot be separated from harness and product failures.

[observed] A substantial part of the sample comes from related shop and report workflows.
71/176 first attempts were logged on October 16 UTC alone. Sibling tasks, repeated dispatches,
operator difficulty selection, and changes to runner/checks confound comparisons. Reported worker
tokens are not dollars, subscription usage limits, or normalized model compute. No dollar saving,
causal routing benefit, or cross-model efficiency ratio is claimed.

## Proposed registry criteria edits (not applied)

[agent-inferred] After confirmation, make these edits in a separate implementation PR:

1. Preserve gpt-5.6-sol lane identity. Add explicit gpt-6.1-sol/medium (and high only if needed)
   entries with distinct names, current model IDs, and explicit effort; do not silently reinterpret
   old picks. Keep unknown/default effort distinct from medium/high until runner config is verified.
2. Change astra's criterion from only “hard, judgment-heavy” work to include the confirmed
   code-feature, code-fix, and docs candidates. State that this is an observed baseline choice,
   not proof that a cheaper lane would fail. Refresh its evidence with this window and counts.
3. For review, describe bounded repo reviews with shell/test access in the new medium entry.
   Keep high effort as an explicit operator choice for difficult reviews; do not claim its n=6
   justifies a general default. The 16 stale sol-high shadow picks do not settle this decision.
4. Split probe criteria by required capability: pure write-only smoke, shell/build verification,
   and live/browser diagnosis. Keep `task_type=probe` manual until labels and checks support a
   comparable sample. Preserve the claude lanes' Write-only capability boundary.
5. Leave research, test-hardening, format-conversion, Haiku, Sonnet, and all other THIN cells
   without a newly inferred default. Lack of medium picks does not justify deleting medium while
   the registry does not represent the medium model actually used.
6. Log the effective engine/model/effort, registry revision, and pick identity together for the
   next controlled comparison. Validate checks first; separate task type from capability and
   difficulty. A future promotion should compare eligible lanes on similar tasks, include retries
   and end-to-end cost, and keep a holdout rather than credit an unexecuted shadow prediction.

## Review decision

[agent-inferred] Confirm or reject each row in “Proposed decisions”: provisional astra/default
for code-feature, code-fix, and docs; a new gpt-6.1-sol/medium lane for code-review; and no new
default for probe, research, test-hardening, or format-conversion. Confirmation authorizes a
follow-up registry/routing change; this document itself does not enact one.
