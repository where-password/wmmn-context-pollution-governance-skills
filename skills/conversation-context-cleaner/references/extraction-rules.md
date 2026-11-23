# Deterministic Extraction Rules

V1 uses `deterministic-rules-v13` and is intentionally conservative. It extracts explicit line-start markers, structured assistant output, and a narrow user-only fallback; it does not infer from ordinary assistant prose.

| Marker | Input role | Candidate type | Review suggestion |
|---|---|---|---|
| `DECISION:`, `决定:`, `决策:` | user | decision | standard review |
| `PREFERENCE:`, `偏好:`, `用户偏好:`, `建议:` | user | preference | standard review |
| `REQUIREMENT:`, `CONSTRAINT:`, `要求:`, `约束:` | user | constraint | standard review |
| `FACT:`, `事实:` | user | fact | verify before decision |
| `PROPOSAL:`, `建议方案:`, `方案建议:` | assistant | solution | verify before decision |
| `OPEN_QUESTION:`, `OPEN QUESTION:`, `开放问题:`, `待确认:` | user | open question | keep open |

Topic segmentation happens before extraction:

- First message starts a segment.
- `metadata.topic_key` or a first-line topic marker starting a new key starts a new segment.
- `metadata.topic_boundary=true` starts a new segment.
- A timestamp gap larger than `--topic-gap-seconds` starts a new segment.

Evidence validation checks all of:

- source unit exists;
- message ID matches;
- role matches;
- source unit hash matches;
- character range is in bounds;
- quote equals the exact source text slice;
- quote is non-empty.

An invalid evidence span blocks the plan. A candidate or open question never survives with unverified evidence.

## Conservative heuristic fallback

When no explicit marker is present, V1 evaluates structural units from user messages: numbered items, bullet items, table rows, and complete prose sentences. Headings and blank lines act as boundaries. Caps are applied after structural-unit recognition. Assistant prose is never used to create heuristic facts.

The fallback classifies a user sentence only when it contains explicit language from one of these families:

- constraint / requirement: `必须、需要、不要、不能、应当、要求、禁止、只允许、只能、同意` and explicit English modals;
- decision: `决定、确定、采用、拍板、定死、保留、删除、统一、暂不、不再、放弃、允许、切换`;
- preference: `优先、希望、偏好、同意、赞同、倾向`;
- task: explicit implementation or cleanup verbs such as `增加、新增、更新、修改、拆分、剥离、合并、落盘、补充、补上、接入`; generic `实现` does not by itself create a task.

Questions and uncertain units containing `?`/`？`, `是否`, `能不能`, `有没有`, `还有没有`, `是不是`, numbered meta-question lead-ins such as `第 N 个问题`, `为什么`, `如何`, `可能`, `也许`, `不确定`, `待定`, or `待确认` are skipped. This intentionally prioritizes avoiding false facts over candidate recall. Short noise, metadata lead-ins, code fences, and alternative-only units such as `要么`/`或者` are skipped.

Limits and controls:

- numbered/bullet items and table rows count as one cap unit; multiple complete subclaims inside one unit are retained together;
- each candidate records `structure_unit_id` and the nearest heading/lead-in `structure_context`;
- prose is split only at `。！？!?；;`, never commas alone;
- default limit is 30 heuristic candidates per user message and 100 per run;
- both limits are configurable through `--heuristic-per-message-cap` and `--heuristic-total-cap`;
- quotes longer than 360 characters are skipped rather than truncated;
- duplicate normalized sentence text is suppressed;
- evidence keeps the original quote and exact character range, while `content` may remove Markdown escape noise;
- every heuristic candidate remains `pending_review`, has `manual_review_required`, and has `promotion_eligible=false`.


## Consolidation handoff

Extraction does not discard original candidates. `raw_candidates.jsonl` remains the evidence-preserving extraction record. The next deterministic stage may merge long-term candidates, route process tasks to `task_history.jsonl`, reject short/context-dependent noise, or suppress extraction-cap items into the audit. See `references/consolidation.md`.

## Structured assistant candidates

The optional assistant entry point is a fenced block whose info string is exactly:

`memory-candidates-v1`

The fenced block must contain one JSON object with:

- `contract_version="conversation-memory-candidates-v1"`
- non-empty `claims` array

Each claim supports the documented `claim_key`, `scope_key`, `state_kind`, `content`, `assertion_source`, `verification_state`, `importance`, and `citations` fields, with optional subject/entity, effective-time, and `domain_payload` fields.

Citations name a `message_id` and exact `quote`. The Cleaner verifies that the message exists in the same conversation and computes character spans itself; caller-reported spans are ignored. The block is never treated as ordinary assistant prose.

Verification rules:

- `user_decision`, `user_preference`, and `user_request` require at least one citation targeting a user message.
- `tool_verified` or `source_verified` requires a citation marked `tool`, `external`, or `document`; otherwise the candidate is downgraded to `unverified`.
- `assistant_inference` and assistant summary outputs remain pending/manual review and cannot become facts automatically. `assistant_summary` is accepted as a review-only alias and normalized to the existing `assistant_inference` assertion source.
- Invalid, multiple, unclosed, unknown-field, or empty-claim blocks block the plan with a message/block/claim location.
- All structured candidates remain suggestion-only, `quote_verified` or stricter evidence provenance is explicit, and `promotion_eligible=false`.
