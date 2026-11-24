---
name: conversation-context-cleaner
description: Clean controlled conversation JSONL into review-only candidates, open questions, verified evidence, and audit packages. Use for offline deterministic conversation cleaning; do not use for business Q&A, database installation, production canonical writes, Milvus writes, or alias changes.
---

# Conversation Context Cleaner

## First-load user orientation

On the first local invocation after installation, run `python <skill-root>/scripts/onboarding.py --json` before the user's substantive task. If the result has `shown=true`, present its `message` as the first short user-facing response, then continue with the user's task. If `shown=false`, do not mention onboarding again. The marker is shared by both Skills and must never be repeated per turn or per conversation. If command execution is unavailable, use the same message from the repository README once and do not repeat it.

Use this Skill to turn one explicitly supplied controlled conversation JSONL file into a review-ready candidate package. The V1 implementation is deterministic and standard-library-only. It does not call an LLM, scan directories automatically, read `.env` or `.secrets`, or write production systems.

## Product position and V1.5.0 freeze

This is an independent Skill and the conversation-preprocessing module for the database Skill. It produces review-only candidates, state artifacts, database handoff proposals, approval-ledger records, approved-batch artifacts, and approval execution plans. It is not the database writer.

V1.5.0 adds personal presets and controlled auto-approval on top of the frozen handoff/approval boundaries. The deterministic extractor remains `deterministic-rules-v13`. Handoff/approval artifacts are not canonical memory, do not authorize database writes by themselves, and must be independently revalidated by the database integration boundary.

## Non-negotiable boundaries

- Accept one explicit `--input` file under an explicit `--input-root`; never discover files or recurse.
- Require explicit `workspace_id`, `project_id`, `source_id`, domain/profile, conversation allowlist, boundary type, and boundary evidence.
- `clean plan` is read-only. It prints a plan and must not create the workspace or run directory.
- `clean apply` accepts only the exact saved plan and its exact confirmation token. Apply writes only below `<workspace-root>/conversation-cleaner/v1/`.
- Treat output as evidence-backed suggestions. Do not record approve/reject decisions or close open questions.
- Never read `.env`, `.secrets`, secret stores, production canonical databases, Milvus collections, or aliases.
- Never add a real LLM, database installer, business-answer, embedding, or network path to V1 without a new API version and a new safety review.

## Workflow

1. Run `doctor` against the intended isolated workspace. Add `--input-root/--input` when checking an input file.
2. Run `clean plan ... --json` and inspect `status`, `counts`, `checks`, `warnings`, and `errors`.
3. Persist the exact JSON plan outside the command; the command itself does not write it.
4. Review the plan and run `clean apply --plan <plan.json> --confirm <confirmation_token>`.
5. Use `status` to verify the isolated run exists and `review export` to create a portable review package containing grouped candidates, task history, noise items, suppressed audit, open questions, and the audit report.
6. Use `handoff export` only with an explicit run, observations file, scope, and source. Treat the resulting handoff as a proposal.
7. Record explicit approval decisions with `handoff review decide`; only the latest `approve` can enter an approved batch.
8. Optional: use `handoff review auto-approve` only for quote-verified user decisions with no supersede/conflict/assistant-inference/risk marker; all other items remain pending for human review.
9. Generate an approved batch with `handoff review export-approved`; the database boundary must still run the independent approval execution checks before writing.

Stop on any blocked plan or approval. Fix the input, scope, or approval record and regenerate; do not hand-edit plan identity, confirmation values, hashes, or ledger records.

All review/handoff outputs remain suggestion-only. Revoke before execution removes pending authorization; revoke after execution produces a deletion/tombstone intent and must not physically erase history.

## Extractor rules

V1 extracts explicit line-start markers first:

- `DECISION:` / `决定:` / `决策:` -> decision candidate.
- `PREFERENCE:` / `偏好:` / `用户偏好:` / `建议:` -> user preference candidate.
- `REQUIREMENT:` / `CONSTRAINT:` / `要求:` / `约束:` -> requirement candidate.
- `FACT:` / `事实:` -> unverified user-report candidate.
- `PROPOSAL:` / `建议方案:` / `方案建议:` from an assistant -> review-only solution candidate.
- `OPEN_QUESTION:` / `OPEN QUESTION:` / `开放问题:` / `待确认:` -> both a review candidate and an open question that remains open.

If no explicit marker exists, V1 uses a conservative deterministic fallback only for user messages. It first recognizes Markdown structural units: headings and blank lines as boundaries, numbered items, bullet items, table rows, and complete prose sentences. It then evaluates units containing explicit modal, decision, preference, or task language such as `必须、需要、不要、不能、应当、要求、决定、确定、采用、保留、删除、优先、希望、偏好、同意`. Questions, uncertain statements, short noise, and alternative-only units are skipped. Defaults are 30 heuristic candidates per user message and 100 per run, configurable through `--heuristic-per-message-cap` and `--heuristic-total-cap`; evidence quotes are capped at 360 characters.

Heuristic candidates are cleared content plus exact raw evidence: the display text may remove Markdown escapes, while the evidence quote and character spans must still locate the original source exactly. Every heuristic candidate is `pending_review`, carries `manual_review_required`, and has `promotion_eligible=false`.

## Consolidation and triage

After extraction, V1.5.0 keeps every original candidate in `raw_candidates.jsonl` and builds a separate consolidation layer:

- `candidates.jsonl`: grouped long-term candidates. Groups are formed by deterministic type compatibility plus same structural units, shared technical identifiers, sufficiently long shared CJK phrases, explicit same-source restatements, or high text overlap. Short generic repeated phrases alone must not merge unrelated claims. Merged groups retain every source evidence span and record merge reasons and member candidate IDs. Multi-member `content` is a deterministic synthesis of all member claims, never a copy of one representative. A near-related but safely unmerged group exposes `overlap_with` with the related group ID and reason.
- `task_history.jsonl`: process tasks and stage promises. States include `open`, `completed`/archived, `needs_user_confirmation`, and `resolved_to_constraint`. Tasks are never deleted and never become long-term candidates automatically.
- `noise_items.jsonl`: short or context-dependent heuristic fragments filtered by minimum reuse rules. Noise items expose a summary, reason, and source message but not full text.
- `suppressed_candidates.jsonl`: extraction-cap, question/uncertain, task-routed, and noise-filtered items with a short summary, reason, source message, and `full_text_included=false`.
- `parent_groups.jsonl`: higher-level deterministic groups derived from shared structure context or rare identifiers. Parent groups link child groups without using pilot-specific business words as production rules.
- `open_questions.jsonl` and the review bundle remain separate; open-question candidates are not grouped as long-term candidates.

Continuous segments containing only acknowledgements such as `继续`/`收到` and, optionally, a short assistant clarification are merged into the adjacent topic. Original segment boundary reasons are retained in `merge_audit`.

Merged groups expose all `child_claims`. A short same-source restatement may stay inside the core group without duplicating the core summary. A user-authored, reusable constraint, decision, or preference with an explicit type signal that is structurally separable remains standard even when it came from a table row or an engineering-detail section. Only genuinely context-dependent fragments remain experimental `needs_more_real_domain_validation` groups. Near-overlap that cannot be merged safely is reported through `overlap_with` rather than hidden. Parent groups may contain child groups and child parent groups, preserving all evidence. `decision_origin=user_decision` is metadata on the same grouped constraint, not a second candidate.

The review bundle shows each top-level parent/group once. Child groups and nested parent groups are recursively referenced inside their top-level parent and are not repeated as top-level review items. A parent group is the only review entry for its subtree. `review_stats.json` distinguishes `consolidated_group_count` (all hierarchy levels), `top_level_grouped_candidate_count`, `nested_group_count`, and `top_level_review_group_count`; `candidate_count` and `nested_child_group_count` remain compatibility aliases.

Choice-like tasks resolve to the strongest later user decision by shared identifiers, CJK evidence and message order, not merely the first incidental phrase overlap. Resolved tasks remain archived in task history with `resolved_to_constraint` and a resolution candidate/message link. A long structural unit that contains multiple independent heuristic signal types is marked `mixed_signal_claim` and receives `split_review`; it is never auto-split or promoted.

All grouped candidates, parent groups, tasks, noise suggestions, and open questions remain `suggestion_only=true`; no review decision, promotion, canonical write, Milvus write, or alias change is performed.

## Commands

Use `scripts/context-cleaner.cmd` on Windows or `python scripts/context_cleaner_cli.py` cross-platform. Every command supports `--json`.

- `doctor --workspace-root <PATH> [--input-root <PATH> --input <FILE>] --json`
- `clean plan --input-root <PATH> --input <FILE> --workspace-root <PATH> --workspace-id <ID> --project-id <ID> --source-id <ID> --domain-profile <NAME> --profile-version <VERSION> --conversation-id <ID> --boundary-type <TYPE> --boundary-evidence <TEXT> [--preset <JSON>] --json`
- `clean apply --plan <PLAN_JSON> --confirm <TOKEN> --json`
- `status --workspace-root <PATH> --json`
- `review export --workspace-root <PATH> --run-id <RUN_ID> [--output-dir <PATH>] --json`
- `handoff export --workspace-root <ROOT> --run-id <RUN_ID> --observations-root <ROOT> --observations <FILE> --workspace-id <ID> --project-id <ID> --domain-profile <NAME> --profile-version <VERSION> --source-id <ID> --json`
- `handoff review decide --workspace-root <ROOT> --handoff-id <ID> --idempotency-key <KEY> --decision approve|reject|defer|revoke --reviewer-id <ID> --reason <TEXT> --reviewed-at <RFC3339> --json`
- `handoff review auto-approve --workspace-root <ROOT> --handoff-id <ID> [--reviewer-id <ID>] [--reviewed-at <RFC3339>] --json`
- `handoff review status --workspace-root <ROOT> --handoff-id <ID> --json`
- `handoff review export-approved --workspace-root <ROOT> --handoff-id <ID> --json`

Read `references/commands.md` for operational details, `references/contracts.md` for schemas and artifact contracts, `references/extraction-rules.md` for deterministic extraction behavior, `references/consolidation.md` for merge/task/noise/suppression rules, `references/handoff-contract.md` and `references/handoff-review.md` for handoff/approval boundaries, `references/approval-execution.md` for independent pre-write checks and post-write revocation, and `references/safety.md` for production and secret boundaries.

## Validation

From the Skill root, run:

`python -m unittest discover -s tests -v`

The tests must stay isolated and removable. They must continue to prove no writes from `clean plan`, stale-plan rejection after source changes, duplicate-apply no-op behavior, evidence tamper rejection, secret blocking, open-question preservation, and no production-side effects.
