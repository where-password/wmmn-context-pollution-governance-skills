# Consolidation, Task Routing, Noise Filtering, and Suppression Audit

This stage is deterministic and review-only. It consumes raw candidates and produces a separate consolidated layer without altering `raw_candidates.jsonl` or its evidence.

## Candidate grouping

Long-term candidates are grouped with deterministic union-find:

- candidates from different conversations never merge;
- candidate types must be compatible: same type, or a `preference`/`decision` mix;
- subclaims from the same structural unit may merge while retaining that structural evidence;
- shared technical identifiers can merge candidates when at least two identifiers overlap sufficiently;
- a restricted same-source restatement rule may absorb a short claim only when it shares the exact same conversation, type, non-empty structure context and source message, is at most 45% of the longer claim's length, and repeats strong lexical signal;
- a shared CJK phrase may merge claims only when it is at least six characters long, contains at least four CJK characters, and has sufficient token overlap;
- overlapping three-character n-grams from one short generic phrase are not treated as multiple independent merge signals;
- otherwise a high total text-overlap threshold may merge statements;
- generic identifiers, modal phrases, and isolated short phrases such as `apply`, `plan`, `sqlite`, `只负责`, `只允许`, or repeated boilerplate are excluded from the merge signature.

A merged group records:

- stable `group_id` and `claim_key`;
- all member candidate IDs;
- all original evidence IDs and evidence spans;
- `merge_reasons`;
- deterministic synthesis `content` formed by numbered union of every distinct member claim;
- `decision_origin=user_decision` when explicit `拍板/决定` language governs `只允许/禁止/必须` constraints;
- `review_status=pending_review`, `promotion_eligible=false`.

Multi-member groups retain all members as `child_claims`. A same-source short restatement can merge into the core group while being excluded from the core summary when it adds no new claim. A separable user-authored constraint with explicit modal meaning remains an independent top-level constraint even if its structural source is a table row or an engineering-detail/appendix section. Only context-dependent or non-reusable support becomes an experimental `needs_more_real_domain_validation` group. When two groups are near-overlapping but cannot be merged safely, the review output records `overlap_with`; it must not silently present them as unrelated. Parent groups are derived from shared non-empty structure context or rare identifiers; no pilot-specific business terms are production triggers. Parent groups can contain child groups and nested child parent groups. Only the outermost parent is a top-level review item; descendant groups are recursively referenced and not duplicated.

A structural unit that matches multiple independent heuristic signal types is marked `mixed_signal_claim` and receives `split_review`. This is a review hint only; the deterministic layer does not auto-split or promote it.

Singleton long-term candidates remain groups of one with `singleton_no_merge`.

## Task routing

A candidate is routed to `task_history.jsonl` when it is an implementation task or a stage promise, including patterns such as:

- “补上/完成/更新 … 后就可以冻结/进入实现”;
- “暂不冻结 … 先拍板/定死”;
- explicit `下一步`, `当前阶段`, `本轮`, `先做`, `随后`, `接着`, `骨架实现`.

Task items are retained and state is resolved from evidence. Choice tasks score later user decisions/constraints by identifier overlap, CJK phrase evidence, message order, and explicit decision language; incidental short shared words are not sufficient.

- `completed` + `archived=true` when a later assistant completion report is followed by explicit user confirmation;
- `needs_user_confirmation` when only an assistant claim exists without user confirmation;
- `resolved_to_constraint` when a choice task is followed by a user decision sharing the same structural signals;
- otherwise `open`.

Resolved tasks retain their original evidence and link to the resolving candidate/message. Tasks never enter the grouped long-term candidate set automatically. Generic words such as `实现` do not by themselves route a candidate to task history; stage-promise patterns are evaluated separately.

## Noise filtering

Heuristic extraction first identifies structural units: headings/blank lines as boundaries, numbered items, bullet items, table rows, and complete prose sentences. Caps are applied afterward. Defaults are 30 per user message and 100 per run and can be overridden per plan.

Heuristic candidates are filtered when their normalized reusable content is shorter than 12 characters or when they are clearly context-dependent (`这/那/该/上述/模糊地带/相关事项`).

Noise items expose only a short summary, reason, source message, and character span. Full text is not included by default.

## Suppressed candidate audit

`suppressed_candidates.jsonl` records every candidate-like item that did not survive the pipeline:

- `per_message_cap`, `total_cap`, `duplicate_sentence`, `overlong_sentence`;
- `questionish_or_uncertain`;
- `task_routed`;
- `noise_filtered`.

Each record contains `suppressed_id`, stage, reason, short summary, source message/unit, character span, heuristic rule where applicable, and `full_text_included=false`.

## Review bundle

`review_bundle.jsonl` contains heterogeneous items:

- `parent_group`;
- top-level `grouped_candidate`;
- `task_item`;
- `noise_item`;
- `open_question`.

Statistics distinguish all consolidated groups from top-level grouped candidates and nested groups. Counts must satisfy:

- `top_level_review_group_count = top_level_grouped_candidate_count + top_level_parent_group_count`;
- `candidate_count = grouped_candidate_count` (compatibility alias for unparented review groups);
- `nested_group_count = nested_child_group_count` (compatibility alias).

Every item is `suggestion_only=true`. No decisions are recorded and no promotion path exists.
