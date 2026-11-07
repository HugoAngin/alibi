# Task

Filled in by `lead` on 2026-11-07 from the team's revised build specification, `alibi/SPEC.md`. That file is the full source of truth for scope, contracts, evaluation and the demo. This page is the short version every agent reads.

## Task text

AgentVerse 2026, Finance Track. Help a specific person make a better financial decision, with evidence a professional can check and human oversight for consequential actions.

We build **Alibi**, a user-controlled financial passport. It links bank transactions to the marketplace sales, refunds, fees and settlement records that explain them, keeps that evidence ready in the background, and when a bank sends a Request for Context (RFC) it investigates, challenges weak explanations and prepares the smallest supported response. The account holder approves exactly what is shared. A bank analyst verifies and reviews what arrives.

The person is **Maya**, a student who resells clothes. Her account is restricted while her bank asks her to explain a credit. She must decide which records to share. The analyst makes a separate review decision.

One complete workflow: maintain Maya's history, surface a proactive alert, accept a bank RFC, investigate using both connections (merchant and bank), prepare a reviewable draft, deliver the approved package to a bank-side verifier and analyst, then reuse the same persisted passport for a different question from a second recipient without re-importing records or reusing consent.

Canonical synthetic amounts, used everywhere: **£2,400 sales − £180 refunds − £60 fees = £2,160 credit**, stored as `240000`, `18000`, `6000`, `216000` pence. See `shared/CONTRACTS.md`.

## Judging criteria

From the team's transcription of the Finance Track reveal (slides 16–19). Full table in `alibi/SPEC.md` under "Judging rubric".

| Weight | Criterion | What to show |
| --- | --- | --- |
| 25% | Traceable reasoning | Claim → source versions → lookup → separately computed arithmetic. A new record changing the conclusion. |
| 25% | Testing on unseen data | Sealed comparison against a rules baseline and a one-pass Gemini baseline, with case counts, uncertainty, cost and a failure. |
| 20% | Relevance and impact | Maya's disclosure decision. Records present before the request. An unsupported explanation prevented. Reuse for a second recipient. |
| 15% | Risk controls and oversight | A live backend refusal. Scoped access, audit entries, holder and analyst decisions. |
| 15% | Ambition and technical quality | Live Gemini tool selection, four roles against a persistent passport, measured value of adaptive retrieval. |

All teams are scored from the deck. Finalists present for four minutes plus two of questions.

## Deadline and what to submit

- Hacking ends **13:00 on Sunday 8 Nov** (Devfolio). The team's own submission deadline is **12:30 London time**; `lead` submits between 12:15 and 12:30 and does not rely on any later public timetable.
- Submit a **public code repository with a README**, preferably a runnable demo, and a **deck** showing the user, data, benchmark, results, limits and controls.
- Pre-submit checks and the Devfolio fields are in `docs/SUBMISSION.md`.

## Rules on tools, data and pre-written code

- Work only with labelled synthetic records and simulated services. No real financial records, account details, logins or money movement.
- Both connection layers are simulated services, not live integrations. Do not claim that the app or the inference provider cannot read data sent for processing.
- Gemini is the LLM (`google-genai` is in `pyproject.toml`; the key name is `GEMINI_API_KEY` in `.env`). Pinned model IDs: `gemini-3.8-flash` for routine calls and `gemini-4-argon` for the frontier role, as set in `alibi/llm/config.py`. If Gemini is unavailable on stage, play a clearly labelled recorded run.
- Devfolio's Code of Conduct is the only published rule on AI tools and pre-written code. This repo held only workflow tooling before the task.
- Do not weaken the baselines to make Alibi win, and never replace a failed agent path with a hard-coded success.

## What judges want

Notes from talking to the Jane Street, Google DeepMind and NVIDIA people. Not collected yet. The human owning sponsor time writes their answers here, in their words where possible.
