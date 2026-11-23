from __future__ import annotations

import re
from collections import Counter
from itertools import combinations
from dataclasses import dataclass, replace
from typing import Any, Iterable

from .contracts import Candidate, EvidenceSpan, OpenQuestion
from .util import sha256_json


MIN_REUSABLE_CONTENT_CHARS = 12
_ASCII_WORD = re.compile(r"[a-z][a-z0-9_.-]{2,}", re.IGNORECASE)
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_CONTEXT_DEPENDENT = re.compile(r"^(?:这|那|此|上述|前述|该|其|这些|那些|这块|这条)|模糊地带|相关事项|有关内容")
_TASK_PROMISE = re.compile(
    r"(?:暂不.{0,50}(?:冻结|完成|定稿).{0,60}先.{0,40}(?:拍板|定死|确认))"
    r"|(?:(?:补上|更新|修改|完成|实现|拆离|剥离|写出).{0,110}(?:后|之后).{0,70}(?:冻结|进入|实现|开始))"
    r"|(?:后.{0,30}(?:就|即可).{0,30}(?:冻结|进入|实现))"
    r"|(?:正式冻结|骨架实现|下一步|当前阶段|本轮|先做|随后|接着)",
    re.IGNORECASE,
)

_STOP_ASCII = {
    "apply", "plan", "v1", "v1.5", "v2", "canonical", "sqlite", "milvus",
    "and", "the", "with", "from", "for", "this", "that", "must", "shall", "should", "only", "all", "not",
}
_STOP_CJK = {
    "必须", "需要", "不要", "不能", "应当", "应该", "要求", "决定", "确定", "采用",
    "保留", "删除", "优先", "希望", "偏好", "同意", "赞同", "可以", "进行", "作为",
    "以及", "一个", "这个", "这些", "那种", "直接", "统一", "规划", "文档", "候选",
    "长期", "审查", "写入", "恢复", "迁移", "数据库", "只负责", "只允许", "唯一允许",
    "骨架实现", "长期候选", "人工审查",
}
_GENERIC_PARENT_IDENTIFIERS = {"sqlite", "canonical", "milvus", "route", "schema", "apply", "plan", "read", "write", "database", "skill", "memory", "core"}
_INDEPENDENT_CONSTRAINT = re.compile(r"必须|禁止|不能|不要|不得|严禁|仅|只|要求|允许|通过|不得", re.IGNORECASE)
_INDEPENDENT_DECISION = re.compile(
    r"决定|确定|采用|拍板|定死|保留|删除|统一|暂不|不再|放弃|切换|改为|优先|希望|偏好|同意|赞同|倾向",
    re.IGNORECASE,
)
_SUPPORTING_CONTEXT = re.compile(r"工程细节|技术细节|实现细节|附录|参考|待办|可选|后续|next steps?|implementation details?|appendix|reference", re.IGNORECASE)
_TYPE_PRIORITY = {"constraint": 4, "decision": 3, "preference": 2, "task": 1, "fact": 0, "open_question": 0}


@dataclass(frozen=True)
class GroupedCandidate:
    group_id: str
    candidate_id: str
    claim_key: str
    workspace_id: str
    project_id: str
    conversation_id: str
    topic_segment_id: str
    domain_profile: str
    profile_version: str
    candidate_type: str
    proposed_memory_type: str
    content: str
    summary: str
    member_candidate_ids: tuple[str, ...]
    member_summaries: tuple[dict[str, Any], ...]
    child_claims: tuple[dict[str, Any], ...]
    hierarchy_role: str
    validation_status: str
    evidence: tuple[EvidenceSpan, ...]
    merge_reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    extraction_methods: tuple[str, ...]
    extraction_method: str
    heuristic_rule: str | None
    decision_origin: str | None
    parent_group_ids: tuple[str, ...]
    suggested_action: str
    overlap_with: tuple[dict[str, Any], ...] = ()
    review_status: str = "pending_review"
    record_status: str = "grouped_candidate"
    suggestion_only: bool = True
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "candidate_id": self.candidate_id,
            "claim_key": self.claim_key,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "topic_segment_id": self.topic_segment_id,
            "domain_profile": self.domain_profile,
            "profile_version": self.profile_version,
            "candidate_type": self.candidate_type,
            "proposed_memory_type": self.proposed_memory_type,
            "content": self.content,
            "summary": self.summary,
            "member_candidate_ids": list(self.member_candidate_ids),
            "member_summaries": list(self.member_summaries),
            "child_claims": list(self.child_claims),
            "hierarchy_role": self.hierarchy_role,
            "validation_status": self.validation_status,
            "member_count": len(self.member_candidate_ids),
            "evidence": [item.to_dict() for item in self.evidence],
            "merge_reasons": list(self.merge_reasons),
            "risk_flags": list(self.risk_flags),
            "extraction_methods": list(self.extraction_methods),
            "extraction_method": self.extraction_method,
            "heuristic_rule": self.heuristic_rule,
            "decision_origin": self.decision_origin,
            "parent_group_ids": list(self.parent_group_ids),
            "overlap_with": list(self.overlap_with),
            "suggested_action": self.suggested_action,
            "suggestion_only": self.suggestion_only,
            "review_status": self.review_status,
            "record_status": self.record_status,
            "promotion_eligible": self.promotion_eligible,
        }


@dataclass(frozen=True)
class ParentGroup:
    parent_group_id: str
    parent_type: str
    claim_key: str
    title: str
    content: str
    summary: str
    member_group_ids: tuple[str, ...]
    member_parent_group_ids: tuple[str, ...]
    parent_group_ids: tuple[str, ...]
    member_candidate_ids: tuple[str, ...]
    evidence: tuple[EvidenceSpan, ...]
    merge_reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    suggested_action: str = "high_priority_review"
    review_status: str = "pending_review"
    suggestion_only: bool = True
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_group_id": self.parent_group_id,
            "parent_type": self.parent_type,
            "claim_key": self.claim_key,
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "member_group_ids": list(self.member_group_ids),
            "member_parent_group_ids": list(self.member_parent_group_ids),
            "parent_group_ids": list(self.parent_group_ids),
            "member_candidate_ids": list(self.member_candidate_ids),
            "member_group_count": len(self.member_group_ids),
            "evidence": [item.to_dict() for item in self.evidence],
            "merge_reasons": list(self.merge_reasons),
            "risk_flags": list(self.risk_flags),
            "suggested_action": self.suggested_action,
            "review_status": self.review_status,
            "suggestion_only": self.suggestion_only,
            "promotion_eligible": self.promotion_eligible,
        }


@dataclass(frozen=True)
class TaskItem:
    task_id: str
    source_candidate_id: str
    workspace_id: str
    project_id: str
    conversation_id: str
    topic_segment_id: str
    content: str
    summary: str
    task_kind: str
    task_state: str
    evidence: tuple[EvidenceSpan, ...]
    reason: str
    archived: bool = False
    resolution_candidate_id: str | None = None
    resolution_message_id: str | None = None
    confirmed_by_message_id: str | None = None
    needs_user_confirmation_reason: str | None = None
    suggested_action: str = "track_in_task_history"
    suggestion_only: bool = True
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_candidate_id": self.source_candidate_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "topic_segment_id": self.topic_segment_id,
            "content": self.content,
            "summary": self.summary,
            "task_kind": self.task_kind,
            "task_state": self.task_state,
            "evidence": [item.to_dict() for item in self.evidence],
            "reason": self.reason,
            "archived": self.archived,
            "resolution_candidate_id": self.resolution_candidate_id,
            "resolution_message_id": self.resolution_message_id,
            "confirmed_by_message_id": self.confirmed_by_message_id,
            "needs_user_confirmation_reason": self.needs_user_confirmation_reason,
            "suggested_action": self.suggested_action,
            "suggestion_only": self.suggestion_only,
            "promotion_eligible": self.promotion_eligible,
            "not_long_term_candidate": True,
        }


@dataclass(frozen=True)
class NoiseItem:
    noise_id: str
    source_candidate_id: str
    workspace_id: str
    project_id: str
    conversation_id: str
    topic_segment_id: str
    summary: str
    reason: str
    reason_detail: str
    source_message_id: str
    source_unit_id: str
    char_start: int
    char_end: int
    status: str = "noise"
    suggested_action: str = "inspect_then_discard"
    suggestion_only: bool = True
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "noise_id": self.noise_id,
            "source_candidate_id": self.source_candidate_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "topic_segment_id": self.topic_segment_id,
            "summary": self.summary,
            "reason": self.reason,
            "reason_detail": self.reason_detail,
            "source_message_id": self.source_message_id,
            "source_unit_id": self.source_unit_id,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "status": self.status,
            "suggested_action": self.suggested_action,
            "suggestion_only": self.suggestion_only,
            "promotion_eligible": self.promotion_eligible,
            "full_text_included": False,
        }


@dataclass(frozen=True)
class ConsolidationResult:
    raw_candidates: tuple[Candidate, ...]
    grouped_candidates: tuple[GroupedCandidate, ...]
    parent_groups: tuple[ParentGroup, ...]
    task_items: tuple[TaskItem, ...]
    noise_items: tuple[NoiseItem, ...]
    open_questions: tuple[OpenQuestion, ...]
    suppressed_audit: tuple[dict[str, Any], ...]
    merge_edge_count: int

    def stats(self) -> dict[str, Any]:
        suppressed_reasons = Counter(item["reason"] for item in self.suppressed_audit)
        nested_group_ids = {
            group_id
            for parent in self.parent_groups
            for group_id in parent.member_group_ids
        }
        top_level_parent_count = sum(not parent.parent_group_ids for parent in self.parent_groups)
        return {
            "raw_candidate_count": len(self.raw_candidates),
            "consolidated_group_count": len(self.grouped_candidates),
            "consolidated_candidate_count": len(self.grouped_candidates),
            "top_level_grouped_candidate_count": len(self.grouped_candidates) - len(nested_group_ids),
            "nested_group_count": len(nested_group_ids),
            "grouped_member_count": sum(len(item.member_candidate_ids) for item in self.grouped_candidates),
            "merged_group_count": sum(len(item.member_candidate_ids) > 1 for item in self.grouped_candidates),
            "experimental_group_count": sum(item.validation_status == "needs_more_real_domain_validation" for item in self.grouped_candidates),
            "parent_group_count": len(self.parent_groups),
            "top_level_parent_group_count": top_level_parent_count,
            "nested_parent_group_count": len(self.parent_groups) - top_level_parent_count,
            "top_level_review_group_count": len(self.grouped_candidates) - len(nested_group_ids) + top_level_parent_count,
            "task_item_count": len(self.task_items),
            "noise_item_count": len(self.noise_items),
            "open_question_count": len(self.open_questions),
            "suppressed_audit_count": len(self.suppressed_audit),
            "suppressed_reason_counts": dict(sorted(suppressed_reasons.items())),
            "merge_edge_count": self.merge_edge_count,
        }


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _clean_text(text: str) -> str:
    for marker in ("*", "`", ".", "-", "#", "_", "|", "[", "]", "(", ")"):
        text = text.replace("\\" + marker, marker)
    text = text.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _summary(text: str, limit: int = 140) -> str:
    cleaned = _clean_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _token_profile(text: str) -> tuple[frozenset[str], frozenset[str]]:
    lowered = _clean_text(text).casefold()
    ascii_tokens: set[str] = set()
    for match in _ASCII_WORD.finditer(lowered):
        token = match.group(0).strip("._-")
        if len(token) >= 3 and token not in _STOP_ASCII:
            ascii_tokens.add(token)
        for part in re.split(r"[./-]+", token):
            if len(part) >= 3 and part not in _STOP_ASCII:
                ascii_tokens.add(part)
            for subpart in re.split(r"_+", part):
                if len(subpart) >= 4 and subpart not in _STOP_ASCII:
                    ascii_tokens.add(subpart)
    cjk_tokens: set[str] = set()
    for run in _CJK_RUN.findall(lowered):
        for size in (2, 3):
            for index in range(0, max(0, len(run) - size + 1)):
                token = run[index:index + size]
                if size == 3 and token[0] in "的了和与或在把将被从到而":
                    continue
                if token not in _STOP_CJK:
                    cjk_tokens.add(token)
    return frozenset(ascii_tokens), frozenset(cjk_tokens)


def _types_compatible(left: Candidate, right: Candidate) -> bool:
    types = {left.candidate_type, right.candidate_type}
    if len(types) == 1:
        return True
    return types <= {"preference", "decision"}


def _longest_common_substring_length(left: str, right: str) -> tuple[int, str]:
    if not left or not right:
        return 0, ""
    previous = [0] * (len(right) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_char in enumerate(left, start=1):
        current = [0] * (len(right) + 1)
        for right_index, right_char in enumerate(right, start=1):
            if left_char != right_char:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best_length:
                best_length = current[right_index]
                best_end = right_index
        previous = current
    return best_length, right[best_end - best_length:best_end]


def _same_source_restatement(
    left: Candidate,
    right: Candidate,
    profiles: dict[str, tuple[frozenset[str], frozenset[str]]],
) -> str | None:
    """Return a reason when a short claim restates a longer same-unit claim."""
    if (
        left.conversation_id != right.conversation_id
        or left.candidate_type != right.candidate_type
        or not left.structure_context
        or left.structure_context != right.structure_context
    ):
        return None
    left_messages = {item.message_id for item in left.evidence}
    right_messages = {item.message_id for item in right.evidence}
    if not left_messages or not right_messages or not (left_messages & right_messages):
        return None
    left_text = re.sub(r"\s+", "", _clean_text(left.content))
    right_text = re.sub(r"\s+", "", _clean_text(right.content))
    if not left_text or not right_text:
        return None
    if len(left_text) <= len(right_text):
        short_profile, long_profile = profiles[left.candidate_id], profiles[right.candidate_id]
        short_length, long_length = len(left_text), len(right_text)
    else:
        short_profile, long_profile = profiles[right.candidate_id], profiles[left.candidate_id]
        short_length, long_length = len(right_text), len(left_text)
    if short_length < 12 or short_length > max(1, long_length) * 0.45:
        return None
    shared_ascii = {token for token in short_profile[0] & long_profile[0] if len(token) >= 4}
    shared_cjk = short_profile[1] & long_profile[1]
    short_cjk_overlap = len(shared_cjk) / max(1, len(short_profile[1]))
    if shared_ascii or (len(shared_cjk) >= 3 and short_cjk_overlap >= 0.12):
        return "same-source short restatement"
    return None


def _pair_edge(
    left: Candidate,
    right: Candidate,
    profiles: dict[str, tuple[frozenset[str], frozenset[str]]],
    cjk_df: Counter[str],
) -> tuple[tuple[str, ...], float]:
    if left.conversation_id != right.conversation_id:
        return (), 0.0
    if not _types_compatible(left, right):
        return (), 0.0
    if left.structure_unit_id and left.structure_unit_id == right.structure_unit_id:
        return ("same structural unit",), 1.2
    restatement = _same_source_restatement(left, right, profiles)
    if restatement:
        return (restatement,), 0.9
    left_ascii, left_cjk = profiles[left.candidate_id]
    right_ascii, right_cjk = profiles[right.candidate_id]
    shared_ascii = sorted(left_ascii & right_ascii)
    ascii_overlap = len(shared_ascii) / max(1, min(len(left_ascii), len(right_ascii)))
    if len(shared_ascii) >= 2 and ascii_overlap >= 0.5:
        return (f"shared identifiers: {', '.join(shared_ascii[:6])}",), 1.0 + min(0.3, 0.05 * len(shared_ascii))

    shared_cjk = left_cjk & right_cjk
    cjk_overlap = len(shared_cjk) / max(1, min(len(left_cjk), len(right_cjk)))
    shared_phrase_length, shared_phrase = _longest_common_substring_length(
        _clean_text(left.content),
        _clean_text(right.content),
    )
    cjk_character_count = len(re.findall(r"[\u3400-\u9fff]", shared_phrase))
    if shared_phrase_length >= 6 and cjk_character_count >= 4 and cjk_overlap >= 0.12:
        return (f"shared CJK phrase ({shared_phrase_length} chars): {shared_phrase[:24]}",), 0.6

    shared_total = len(shared_ascii) + len(shared_cjk)
    total_overlap = shared_total / max(1, min(len(left_ascii) + len(left_cjk), len(right_ascii) + len(right_cjk)))
    if shared_total >= 4 and total_overlap >= 0.45:
        return (f"text overlap: {total_overlap:.2f}",), 0.5 + 0.5 * total_overlap
    return (), 0.0


def _pair_merge_reasons(
    left: Candidate,
    right: Candidate,
    profiles: dict[str, tuple[frozenset[str], frozenset[str]]],
    cjk_df: Counter[str],
) -> tuple[str, ...]:
    return _pair_edge(left, right, profiles, cjk_df)[0]


def _core_member_ids(members: list[Candidate], edge_weights: dict[tuple[str, str], float]) -> set[str]:
    ids = sorted(item.candidate_id for item in members)
    if len(ids) <= 1:
        return set(ids)
    non_supporting = {
        item.candidate_id
        for item in members
        if item.structure_kind != "table_row" and not _SUPPORTING_CONTEXT.search(item.structure_context)
    }
    if 2 <= len(non_supporting) < len(ids):
        return non_supporting
    for size in range(len(ids), 1, -1):
        best: tuple[str, ...] = ()
        best_score = -1.0
        for combo in combinations(ids, size):
            if all(edge_weights.get(tuple(sorted((left, right))), 0.0) > 0 for left, right in combinations(combo, 2)):
                score = sum(edge_weights[tuple(sorted((left, right)))] for left, right in combinations(combo, 2))
                if score > best_score:
                    best = combo
                    best_score = score
        if best:
            return set(best)
    return set(ids)



def _task_kind(candidate: Candidate) -> str | None:
    if candidate.candidate_type == "task":
        return "implementation_task"
    if _TASK_PROMISE.search(_clean_text(candidate.content)):
        return "stage_promise"
    return None


def _noise_reason(candidate: Candidate) -> tuple[str, str] | None:
    if candidate.extraction_method != "heuristic_fallback":
        return None
    normalized = re.sub(r"\s+", "", _clean_text(candidate.content))
    if len(normalized) < MIN_REUSABLE_CONTENT_CHARS:
        return (
            "short_context_dependent_fragment",
            f"normalized_content_length={len(normalized)} < min_reusable_content_chars={MIN_REUSABLE_CONTENT_CHARS}",
        )
    if len(normalized) < 24 and _CONTEXT_DEPENDENT.search(_clean_text(candidate.content)):
        return (
            "context_dependent_fragment",
            "short content contains a deictic or context-dependent reference without enough reusable scope",
        )
    return None


def _suppression_record(
    *,
    candidate: Candidate,
    reason: str,
    reason_detail: str,
) -> dict[str, Any]:
    first = candidate.evidence[0]
    payload = {
        "stage": "consolidation",
        "reason": reason,
        "summary": _summary(candidate.content),
        "candidate_id": candidate.candidate_id,
        "source_message_id": first.message_id,
        "source_unit_id": first.source_unit_id,
        "topic_segment_id": candidate.topic_segment_id,
        "char_start": first.char_start,
        "char_end": first.char_end,
        "heuristic_rule": candidate.heuristic_rule,
        "reason_detail": reason_detail,
        "full_text_included": False,
    }
    payload["suppressed_id"] = "sup_" + sha256_json(payload)[:24]
    return payload


def _group_type(members: list[Candidate]) -> str:
    return max((member.candidate_type for member in members), key=lambda item: _TYPE_PRIORITY.get(item, 0))


def _group_memory_type(candidate_type: str) -> str:
    return {
        "constraint": "requirement",
        "decision": "decision",
        "preference": "preference",
        "task": "task",
        "fact": "fact",
        "open_question": "open_question",
    }.get(candidate_type, candidate_type)


def _synthesize_group_content(members: list[Candidate], candidate_type: str) -> str:
    ordered = sorted(members, key=lambda item: item.candidate_id)
    clauses: list[str] = []
    seen: set[str] = set()
    for member in ordered:
        clause = _clean_text(member.content).strip("；;。 ")
        key = re.sub(r"[\W_]+", "", clause.casefold(), flags=re.UNICODE)
        if not key or key in seen:
            continue
        seen.add(key)
        clauses.append(clause)
    if len(clauses) == 1:
        return clauses[0]
    label = {
        "constraint": "合并约束",
        "decision": "合并决策",
        "preference": "合并偏好",
        "task": "合并任务",
    }.get(candidate_type, "合并候选")
    return label + "：" + "；".join(f"({index}) {clause}" for index, clause in enumerate(clauses, start=1))


def _decision_origin(members: list[Candidate], candidate_type: str) -> str | None:
    text = _clean_text(" ".join(item.content for item in members))
    if candidate_type == "constraint" and re.search(r"拍板|决定", text) and re.search(r"只允许|禁止|必须|不得", text):
        return "user_decision"
    if candidate_type == "decision" and re.search(r"拍板|决定|统一|采用|赞同新增", text):
        return "user_decision"
    return None


def _group_evidence(members: list[Candidate]) -> tuple[EvidenceSpan, ...]:
    evidence_by_id: dict[str, EvidenceSpan] = {}
    for member in members:
        for evidence in member.evidence:
            evidence_by_id[evidence.evidence_id] = evidence
    return tuple(sorted(evidence_by_id.values(), key=lambda item: (item.message_id, item.char_start, item.evidence_id)))


def _group_context(group: GroupedCandidate) -> str:
    contexts = [item.get("structure_context", "") for item in group.child_claims if item.get("structure_context")]
    if not contexts:
        return ""
    return Counter(contexts).most_common(1)[0][0]


def _context_is_leadin(context: str) -> bool:
    return bool(context and context.rstrip().endswith((":", "：")))


def _parent_context(parent: ParentGroup, groups_by_id: dict[str, GroupedCandidate], parents_by_id: dict[str, ParentGroup]) -> str:
    contexts: list[str] = []
    for group_id in parent.member_group_ids:
        group = groups_by_id.get(group_id)
        if group:
            context = _group_context(group)
            if context:
                contexts.append(context)
    for parent_id in parent.member_parent_group_ids:
        child_parent = parents_by_id.get(parent_id)
        if child_parent:
            context = _parent_context(child_parent, groups_by_id, parents_by_id)
            if context:
                contexts.append(context)
    return Counter(contexts).most_common(1)[0][0] if contexts else ""


def _parent_content(parent: ParentGroup) -> str:
    return parent.content


_HIERARCHY_PREFIX = re.compile(
    r"^(?:共享结构父组|relationship parent|parent group|父组)\s*[:：]\s*",
    re.IGNORECASE,
)


def _strip_hierarchy_prefix(text: str) -> str:
    value = text.strip()
    while True:
        updated = _HIERARCHY_PREFIX.sub("", value, count=1)
        if updated == value:
            return value
        value = updated.strip()


def _make_parent(
    *,
    member_groups: list[GroupedCandidate],
    member_parents: list[ParentGroup],
    reasons: tuple[str, ...],
    parent_type: str,
) -> ParentGroup:
    groups_by_id = {item.group_id: item for item in member_groups}
    parents_by_id = {item.parent_group_id: item for item in member_parents}
    member_group_ids = tuple(item.group_id for item in sorted(member_groups, key=lambda item: item.group_id))
    member_parent_ids = tuple(item.parent_group_id for item in sorted(member_parents, key=lambda item: item.parent_group_id))
    member_candidate_ids = tuple(
        sorted(
            {cid for item in member_groups for cid in item.member_candidate_ids}
            | {cid for item in member_parents for cid in item.member_candidate_ids}
        )
    )
    parent_id = "pgrp_" + sha256_json(
        {
            "member_group_ids": list(member_group_ids),
            "member_parent_group_ids": list(member_parent_ids),
            "reasons": list(reasons),
        }
    )[:24]
    contexts = sorted(
        {
            context
            for context in (
                [_group_context(item) for item in member_groups]
                + [_parent_context(item, groups_by_id, parents_by_id) for item in member_parents]
            )
            if context
        }
    )
    title = contexts[0] if len(contexts) == 1 else "Related structural claim groups"
    ordered_statements = [
        item.content for item in sorted(member_groups, key=lambda item: item.group_id)
    ] + [
        item.content for item in sorted(member_parents, key=lambda item: item.parent_group_id)
    ]
    content = "；".join(_strip_hierarchy_prefix(item) for item in ordered_statements)
    evidence_by_id: dict[str, EvidenceSpan] = {}
    for group in member_groups:
        for evidence in group.evidence:
            evidence_by_id[evidence.evidence_id] = evidence
    for parent in member_parents:
        for evidence in parent.evidence:
            evidence_by_id[evidence.evidence_id] = evidence
    evidence = tuple(sorted(evidence_by_id.values(), key=lambda item: (item.message_id, item.char_start, item.evidence_id)))
    return ParentGroup(
        parent_group_id=parent_id,
        parent_type=parent_type,
        claim_key="claim:" + sha256_json({"parent_group_id": parent_id})[:24],
        title=title,
        content=content,
        summary=_summary(content, 220),
        member_group_ids=member_group_ids,
        member_parent_group_ids=member_parent_ids,
        parent_group_ids=(),
        member_candidate_ids=member_candidate_ids,
        evidence=evidence,
        merge_reasons=reasons,
        risk_flags=("parent_group", "manual_review_required"),
    )


def _build_parent_groups(groups: list[GroupedCandidate]) -> list[ParentGroup]:
    eligible = [
        item for item in groups
        if item.validation_status != "needs_more_real_domain_validation"
        and item.hierarchy_role != "independent_group"
    ]
    profiles = {item.group_id: _token_profile(item.content) for item in eligible}
    token_df: Counter[str] = Counter()
    for _, (ascii_tokens, cjk_tokens) in profiles.items():
        token_df.update(set(ascii_tokens) | {token for token in cjk_tokens if len(token) == 3})
    union = _UnionFind(item.group_id for item in eligible)
    relationship_edges: list[tuple[str, str, tuple[str, ...]]] = []
    for index, left in enumerate(eligible):
        for right in eligible[index + 1:]:
            if left.conversation_id != right.conversation_id:
                continue
            all_shared_identifiers = {
                token for token in profiles[left.group_id][0] & profiles[right.group_id][0]
                if len(token) >= 5
            }
            shared_identifiers = {
                token for token in all_shared_identifiers if token_df[token] <= 2
            }
            if not all_shared_identifiers:
                continue
            relationship_identifiers = shared_identifiers - _GENERIC_PARENT_IDENTIFIERS
            relationship_identifiers = shared_identifiers - _GENERIC_PARENT_IDENTIFIERS
            left_context = _group_context(left)
            right_context = _group_context(right)
            same_leadin = bool(left_context and left_context == right_context and _context_is_leadin(left_context))
            type_pair = {left.candidate_type, right.candidate_type}
            mixed_decision_scope = same_leadin and ("constraint" in type_pair) and bool(type_pair & {"decision", "preference"})
            shared_constraints = left.candidate_type == right.candidate_type == "constraint"
            if (mixed_decision_scope and all_shared_identifiers) or (shared_constraints and relationship_identifiers):
                signal_identifiers = all_shared_identifiers if mixed_decision_scope else relationship_identifiers
                reasons = (f"shared rare identifiers: {', '.join(sorted(signal_identifiers)[:6])}",)
                if same_leadin:
                    reasons = (f"shared structure context: {left_context[:120]}",) + reasons
                union.union(left.group_id, right.group_id)
                relationship_edges.append((left.group_id, right.group_id, reasons))
    by_root: dict[str, list[GroupedCandidate]] = {}
    for group in eligible:
        by_root.setdefault(union.find(group.group_id), []).append(group)
    relationship_parents: list[ParentGroup] = []
    group_to_parent: dict[str, str] = {}
    for members in by_root.values():
        if len(members) < 2:
            continue
        member_ids = {item.group_id for item in members}
        reasons = tuple(
            sorted(
                {
                    reason
                    for left_id, right_id, edge_reasons in relationship_edges
                    if left_id in member_ids and right_id in member_ids
                    for reason in edge_reasons
                }
            )
        )
        parent = _make_parent(
            member_groups=members,
            member_parents=[],
            reasons=reasons,
            parent_type="relationship",
        )
        relationship_parents.append(parent)
        for group_id in parent.member_group_ids:
            group_to_parent[group_id] = parent.parent_group_id

    # Stage 2: shared lead-in context parent can contain leaf groups and prior parent groups.
    groups_by_id = {item.group_id: item for item in eligible}
    parents_by_id = {item.parent_group_id: item for item in relationship_parents}
    nodes: list[tuple[str, GroupedCandidate | ParentGroup]] = []
    for group in eligible:
        if group.group_id not in group_to_parent:
            nodes.append((group.group_id, group))
    for parent in relationship_parents:
        nodes.append((parent.parent_group_id, parent))
    node_union = _UnionFind(item[0] for item in nodes)
    context_edges: list[tuple[str, str, str]] = []
    for index, (left_id, left_node) in enumerate(nodes):
        for right_id, right_node in nodes[index + 1:]:
            left_conv = left_node.conversation_id if isinstance(left_node, GroupedCandidate) else next(
                (groups_by_id[gid].conversation_id for gid in left_node.member_group_ids if gid in groups_by_id),
                "",
            )
            right_conv = right_node.conversation_id if isinstance(right_node, GroupedCandidate) else next(
                (groups_by_id[gid].conversation_id for gid in right_node.member_group_ids if gid in groups_by_id),
                "",
            )
            if not left_conv or left_conv != right_conv:
                continue
            left_context = _group_context(left_node) if isinstance(left_node, GroupedCandidate) else _parent_context(left_node, groups_by_id, parents_by_id)
            right_context = _group_context(right_node) if isinstance(right_node, GroupedCandidate) else _parent_context(right_node, groups_by_id, parents_by_id)
            if left_context and left_context == right_context and _context_is_leadin(left_context):
                node_union.union(left_id, right_id)
                context_edges.append((left_id, right_id, left_context))
    context_roots: dict[str, list[tuple[str, GroupedCandidate | ParentGroup]]] = {}
    for node in nodes:
        context_roots.setdefault(node_union.find(node[0]), []).append(node)
    context_parents: list[ParentGroup] = []
    for members in context_roots.values():
        if len(members) < 2:
            continue
        member_groups = [node for _, node in members if isinstance(node, GroupedCandidate)]
        member_parents = [node for _, node in members if isinstance(node, ParentGroup)]
        reasons = tuple(sorted({f"shared structure context: {reason[:120]}" for left_id, right_id, reason in context_edges if left_id in {item[0] for item in members} and right_id in {item[0] for item in members}}))
        context_parents.append(
            _make_parent(
                member_groups=member_groups,
                member_parents=member_parents,
                reasons=reasons,
                parent_type="shared_context",
            )
        )
    all_parents = relationship_parents + context_parents
    child_parent_ids = {pid for parent in context_parents for pid in parent.member_parent_group_ids}
    all_parents = [
        replace(parent, parent_group_ids=tuple(sorted({p.parent_group_id for p in context_parents if parent.parent_group_id in p.member_parent_group_ids})))
        if parent.parent_group_id in child_parent_ids
        else parent
        for parent in all_parents
    ]
    return all_parents

def _make_group(
    members: list[Candidate],
    merge_reasons: tuple[str, ...],
    *,
    hierarchy_role: str,
    validation_status: str,
    suggested_action: str,
    included_ids: set[str] | None = None,
) -> GroupedCandidate:
    ordered = sorted(members, key=lambda item: item.candidate_id)
    candidate_type = _group_type(ordered)
    representative = ordered[0]
    summary_members = [item for item in ordered if included_ids is None or item.candidate_id in included_ids]
    synthesized_content = _synthesize_group_content(summary_members or ordered, candidate_type)
    evidence = _group_evidence(ordered)
    member_ids = tuple(item.candidate_id for item in ordered)
    group_id = "grp_" + sha256_json(
        {
            "member_candidate_ids": list(member_ids),
            "candidate_type": candidate_type,
            "validation_status": validation_status,
        }
    )[:24]
    claim_key = "claim:" + sha256_json(
        {"group_id": group_id, "candidate_type": candidate_type, "member_candidate_ids": list(member_ids)}
    )[:24]
    methods = tuple(sorted({item.extraction_method for item in ordered}))
    heuristic_rules = {item.heuristic_rule for item in ordered if item.heuristic_rule}
    risks = {
        risk for item in ordered for risk in item.risk_flags
    } | {"manual_review_required"}
    if len(ordered) > 1:
        risks.add("merged_group")
    if validation_status == "needs_more_real_domain_validation":
        risks.update({"experimental_supporting_group", "needs_more_real_domain_validation"})
    if "mixed_signal_claim" in risks and suggested_action == "standard_review":
        suggested_action = "split_review"
    return GroupedCandidate(
        group_id=group_id,
        candidate_id=group_id,
        claim_key=claim_key,
        workspace_id=representative.workspace_id,
        project_id=representative.project_id,
        conversation_id=representative.conversation_id,
        topic_segment_id=representative.topic_segment_id,
        domain_profile=representative.domain_profile,
        profile_version=representative.profile_version,
        candidate_type=candidate_type,
        proposed_memory_type=_group_memory_type(candidate_type),
        content=synthesized_content,
        summary=_summary(synthesized_content, 220),
        member_candidate_ids=member_ids,
        member_summaries=tuple(
            {
                "candidate_id": item.candidate_id,
                "candidate_type": item.candidate_type,
                "summary": _summary(item.content),
                "extraction_method": item.extraction_method,
                "heuristic_rule": item.heuristic_rule,
                "source_message_ids": [evidence.message_id for evidence in item.evidence],
                "structure_context": item.structure_context,
                "validation_status": validation_status,
            }
            for item in ordered
        ),
        child_claims=tuple(
            {
                "candidate_id": item.candidate_id,
                "candidate_type": item.candidate_type,
                "summary": _summary(item.content),
                "structure_kind": item.structure_kind,
                "structure_context": item.structure_context,
                "included_in_parent_summary": included_ids is None or item.candidate_id in included_ids,
                "validation_status": validation_status,
                "evidence_ids": [evidence.evidence_id for evidence in item.evidence],
            }
            for item in ordered
        ),
        hierarchy_role=hierarchy_role,
        validation_status=validation_status,
        evidence=evidence,
        merge_reasons=merge_reasons if len(ordered) > 1 else ("singleton_no_merge",),
        risk_flags=tuple(sorted(risks)),
        extraction_methods=methods,
        extraction_method=methods[0] if len(methods) == 1 else "mixed",
        heuristic_rule=next(iter(heuristic_rules)) if len(heuristic_rules) == 1 else None,
        decision_origin=_decision_origin(ordered, candidate_type),
        parent_group_ids=(),
        suggested_action=suggested_action,
    )


def _restatement_duplicate_ids(
    members: list[Candidate],
    profiles: dict[str, tuple[frozenset[str], frozenset[str]]],
) -> set[str]:
    duplicates: set[str] = set()
    for index, left in enumerate(members):
        for right in members[index + 1:]:
            if not _same_source_restatement(left, right, profiles):
                continue
            duplicate = min(
                (left, right),
                key=lambda item: (
                    len(re.sub(r"\s+", "", _clean_text(item.content))),
                    item.candidate_id,
                ),
            )
            duplicates.add(duplicate.candidate_id)
    return duplicates


def _independent_claim(members: list[Candidate]) -> bool:
    if not members:
        return False
    candidate_types = {item.candidate_type for item in members}
    if not (candidate_types == {"constraint"} or candidate_types <= {"decision", "preference"}):
        return False
    for item in members:
        if not item.evidence or any(evidence.role != "user" for evidence in item.evidence):
            return False
        normalized = re.sub(r"\s+", "", _clean_text(item.content))
        if len(normalized) < 24:
            return False
        if item.candidate_type == "constraint":
            has_signal = bool(_INDEPENDENT_CONSTRAINT.search(item.content))
        else:
            has_signal = bool(_INDEPENDENT_DECISION.search(item.content))
        if not has_signal:
            return False
        if _CONTEXT_DEPENDENT.search(_clean_text(item.content)) and len(normalized) < 60:
            return False
    return True


def _build_groups(
    members: list[Candidate],
    merge_reasons: tuple[str, ...],
    profiles: dict[str, tuple[frozenset[str], frozenset[str]]],
    cjk_df: Counter[str],
) -> list[GroupedCandidate]:
    ordered = sorted(members, key=lambda item: item.candidate_id)
    edge_weights = {
        tuple(sorted((left.candidate_id, right.candidate_id))): _pair_edge(
            left, right, profiles, cjk_df
        )[1]
        for index, left in enumerate(ordered)
        for right in ordered[index + 1:]
    }
    core_ids = _core_member_ids(ordered, edge_weights)
    core_members = [item for item in ordered if item.candidate_id in core_ids]
    support_members = [item for item in ordered if item.candidate_id not in core_ids]
    duplicate_ids = _restatement_duplicate_ids(ordered, profiles)
    core_group_ids = core_ids | duplicate_ids
    core_group_members = [item for item in ordered if item.candidate_id in core_group_ids]
    remaining_support = [item for item in support_members if item.candidate_id not in duplicate_ids]
    summary_core_ids = core_ids - duplicate_ids
    if not summary_core_ids and core_members:
        canonical = min(
            core_members,
            key=lambda item: (len(re.sub(r"\s+", "", _clean_text(item.content))), item.candidate_id),
        )
        summary_core_ids = {canonical.candidate_id}
    core_reasons = merge_reasons + (("same-source short restatement",) if duplicate_ids else ())
    groups = [
        _make_group(
            core_group_members,
            core_reasons,
            hierarchy_role="group_parent" if len(core_group_members) > 1 else "group_leaf",
            validation_status="standard",
            suggested_action="high_priority_review" if len(core_members) > 1 else "standard_review",
            included_ids=summary_core_ids,
        )
    ]
    if remaining_support:
        support_reasons = merge_reasons + ("supporting_context_split",)
        independent = _independent_claim(remaining_support)
        groups.append(
            _make_group(
                remaining_support,
                support_reasons,
                hierarchy_role="independent_group" if independent else "supporting_group",
                validation_status="standard" if independent else "needs_more_real_domain_validation",
                suggested_action="standard_review" if independent else "review_supporting_evidence_no_promotion",
            )
        )
    return groups

_CHOICE_MARKERS = re.compile(r"二选一|要么|或者|either|options?", re.IGNORECASE)
_COMPLETION_MARKERS = re.compile(r"已完成|已更新|已修订|已纳入|已冻结|已实现|完成并通过|测试已通过|completed|ready", re.IGNORECASE)
_USER_CONFIRMATION = re.compile(r"确认|同意|采用|拍板|决定|可以开始|继续|已接受|accepted", re.IGNORECASE)


_RESOLUTION_STOP_ASCII = {
    "and", "the", "with", "from", "for", "this", "that", "must", "shall", "should",
    "only", "all", "not", "apply", "plan", "v1", "v1.5", "v2",
}


def _resolution_identifiers(text: str) -> frozenset[str]:
    lowered = _clean_text(text).casefold()
    identifiers: set[str] = set()
    for match in _ASCII_WORD.finditer(lowered):
        token = match.group(0).strip("._-")
        if len(token) >= 3 and token not in _RESOLUTION_STOP_ASCII:
            identifiers.add(token)
        for part in re.split(r"[./-]+", token):
            if len(part) >= 3 and part not in _RESOLUTION_STOP_ASCII:
                identifiers.add(part)
        for part in re.split(r"_+", token):
            if len(part) >= 3 and part not in _RESOLUTION_STOP_ASCII:
                identifiers.add(part)
    return frozenset(identifiers)


def _task_resolution_score(task: Candidate, other: Candidate) -> float:
    shared_identifiers = _resolution_identifiers(task.content) & _resolution_identifiers(other.content)
    task_profile = _token_profile(task.content)
    other_profile = _token_profile(other.content)
    shared_cjk = task_profile[1] & other_profile[1]
    shared_trigrams = {token for token in shared_cjk if len(token) == 3}
    shared_bigrams = {token for token in shared_cjk if len(token) == 2}
    phrase_length, _ = _longest_common_substring_length(_clean_text(task.content), _clean_text(other.content))
    score = (
        8.0 * len(shared_identifiers)
        + 3.0 * len(shared_trigrams)
        + 1.0 * len(shared_bigrams)
        + max(0, phrase_length - 3)
    )
    if other.assertion_source in {"user_decision", "user_request"} or other.candidate_type in {"constraint", "decision"}:
        score += 1.0
    return score


def _resolve_task(
    candidate: Candidate,
    *,
    messages: list[Message],
    raw_candidates: tuple[Candidate, ...],
) -> dict[str, Any]:
    source_sequence = next(
        (
            message.sequence
            for message in messages
            if message.message_id == candidate.evidence[0].message_id
        ),
        10**9,
    )
    if _CHOICE_MARKERS.search(candidate.content):
        matches: list[tuple[float, int, str, Candidate]] = []
        for other in raw_candidates:
            if other.candidate_id == candidate.candidate_id or other.candidate_type == "task":
                continue
            other_sequence = next(
                (message.sequence for message in messages if message.message_id == other.evidence[0].message_id),
                -1,
            )
            if other_sequence <= source_sequence:
                continue
            if other.assertion_source not in {"user_decision", "user_request"} and other.candidate_type not in {"constraint", "decision"}:
                continue
            score = _task_resolution_score(candidate, other)
            if score >= 8.0:
                matches.append((score, other_sequence, other.candidate_id, other))
        if matches:
            _, _, _, resolution = max(matches, key=lambda item: (item[0], -item[1], item[2]))
            return {
                "task_state": "resolved_to_constraint",
                "archived": True,
                "resolution_candidate_id": resolution.candidate_id,
                "resolution_message_id": resolution.evidence[0].message_id,
                "suggested_action": "track_linked_constraint",
            }
    assistant_completion = next(
        (
            message
            for message in messages
            if message.role == "assistant"
            and message.sequence > source_sequence
            and _COMPLETION_MARKERS.search(message.text)
        ),
        None,
    )
    if assistant_completion is not None:
        user_confirmation = next(
            (
                message
                for message in messages
                if message.role == "user"
                and message.sequence > assistant_completion.sequence
                and _USER_CONFIRMATION.search(message.text)
            ),
            None,
        )
        if user_confirmation is not None:
            return {
                "task_state": "completed",
                "archived": True,
                "resolution_message_id": assistant_completion.message_id,
                "confirmed_by_message_id": user_confirmation.message_id,
                "suggested_action": "archive_task_history",
            }
        return {
            "task_state": "needs_user_confirmation",
            "archived": False,
            "resolution_message_id": assistant_completion.message_id,
            "needs_user_confirmation_reason": "assistant reported completion without a later explicit user confirmation",
            "suggested_action": "request_user_confirmation",
        }
    if candidate.assertion_source.startswith("assistant"):
        return {
            "task_state": "needs_user_confirmation",
            "archived": False,
            "needs_user_confirmation_reason": "assistant-originated task has no user confirmation",
            "suggested_action": "request_user_confirmation",
        }
    return {"task_state": "open", "archived": False, "suggested_action": "track_in_task_history"}


def _annotate_overlaps(groups: list[GroupedCandidate]) -> list[GroupedCandidate]:
    profiles = {group.group_id: _token_profile(group.content) for group in groups}
    token_df: Counter[str] = Counter()
    for ascii_tokens, _ in profiles.values():
        token_df.update(ascii_tokens)
    relations: dict[str, list[dict[str, str]]] = {group.group_id: [] for group in groups}
    for index, left in enumerate(groups):
        for right in groups[index + 1:]:
            if left.conversation_id != right.conversation_id or left.candidate_type != right.candidate_type:
                continue
            left_ascii, left_cjk = profiles[left.group_id]
            right_ascii, right_cjk = profiles[right.group_id]
            shared_ascii = {
                token
                for token in left_ascii & right_ascii
                if len(token) >= 5
                and token not in _GENERIC_PARENT_IDENTIFIERS
                and token_df[token] <= 2
            }
            shared_cjk = left_cjk & right_cjk
            left_contexts = {item.get("structure_context", "") for item in left.child_claims if item.get("structure_context")}
            right_contexts = {item.get("structure_context", "") for item in right.child_claims if item.get("structure_context")}
            shared_contexts = left_contexts & right_contexts
            reason: str | None = None
            if shared_ascii:
                reason = "shared rare identifiers: " + ", ".join(sorted(shared_ascii)[:6])
            elif shared_contexts and len(shared_cjk) >= 2:
                reason = "shared structure context and repeated phrases"
            if reason is None:
                continue
            relations[left.group_id].append({"group_id": right.group_id, "reason": reason})
            relations[right.group_id].append({"group_id": left.group_id, "reason": reason})
    return [
        replace(group, overlap_with=tuple(sorted(relations[group.group_id], key=lambda item: item["group_id"])))
        for group in groups
    ]


def consolidate_candidates(
    *,
    candidates: Iterable[Candidate],
    open_questions: Iterable[OpenQuestion],
    extraction_suppressed_audit: Iterable[dict[str, Any]],
    messages: Iterable[Message] = (),
) -> ConsolidationResult:
    raw_candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))
    questions = tuple(sorted(open_questions, key=lambda item: item.open_question_id))
    suppressed = list(extraction_suppressed_audit)
    task_items: list[TaskItem] = []
    noise_items: list[NoiseItem] = []
    long_term: list[Candidate] = []

    for candidate in raw_candidates:
        if candidate.record_status == "open_question" or candidate.candidate_type == "open_question":
            continue
        task_kind = _task_kind(candidate)
        if task_kind is not None:
            task_id = "task_" + sha256_json({"source_candidate_id": candidate.candidate_id})[:24]
            resolution = _resolve_task(candidate, messages=list(messages), raw_candidates=raw_candidates)
            task_items.append(
                TaskItem(
                    task_id=task_id,
                    source_candidate_id=candidate.candidate_id,
                    workspace_id=candidate.workspace_id,
                    project_id=candidate.project_id,
                    conversation_id=candidate.conversation_id,
                    topic_segment_id=candidate.topic_segment_id,
                    content=candidate.content,
                    summary=_summary(candidate.content),
                    task_kind=task_kind,
                    task_state=resolution["task_state"],
                    evidence=candidate.evidence,
                    reason="process task or stage promise routed out of long-term candidates",
                    archived=resolution.get("archived", False),
                    resolution_candidate_id=resolution.get("resolution_candidate_id"),
                    resolution_message_id=resolution.get("resolution_message_id"),
                    confirmed_by_message_id=resolution.get("confirmed_by_message_id"),
                    needs_user_confirmation_reason=resolution.get("needs_user_confirmation_reason"),
                    suggested_action=resolution.get("suggested_action", "track_in_task_history"),
                )
            )
            suppressed.append(
                _suppression_record(
                    candidate=candidate,
                    reason="task_routed",
                    reason_detail=f"routed to task history as {task_kind}",
                )
            )
            continue
        noise = _noise_reason(candidate)
        if noise is not None:
            reason, detail = noise
            first = candidate.evidence[0]
            noise_id = "noise_" + sha256_json({"source_candidate_id": candidate.candidate_id})[:24]
            noise_items.append(
                NoiseItem(
                    noise_id=noise_id,
                    source_candidate_id=candidate.candidate_id,
                    workspace_id=candidate.workspace_id,
                    project_id=candidate.project_id,
                    conversation_id=candidate.conversation_id,
                    topic_segment_id=candidate.topic_segment_id,
                    summary=_summary(candidate.content),
                    reason=reason,
                    reason_detail=detail,
                    source_message_id=first.message_id,
                    source_unit_id=first.source_unit_id,
                    char_start=first.char_start,
                    char_end=first.char_end,
                )
            )
            suppressed.append(
                _suppression_record(
                    candidate=candidate,
                    reason="noise_filtered",
                    reason_detail=detail,
                )
            )
            continue
        long_term.append(candidate)

    profiles = {item.candidate_id: _token_profile(item.content) for item in long_term}
    cjk_df: Counter[str] = Counter()
    for _, cjk_tokens in profiles.values():
        cjk_df.update({token for token in cjk_tokens if len(token) == 3})
    union = _UnionFind(item.candidate_id for item in long_term)
    edge_reasons: list[tuple[str, str, str]] = []
    for left_index, left in enumerate(long_term):
        for right in long_term[left_index + 1:]:
            reasons = _pair_merge_reasons(left, right, profiles, cjk_df)
            if reasons:
                union.union(left.candidate_id, right.candidate_id)
                edge_reasons.extend((left.candidate_id, right.candidate_id, reason) for reason in reasons)

    members_by_root: dict[str, list[Candidate]] = {}
    for candidate in long_term:
        members_by_root.setdefault(union.find(candidate.candidate_id), []).append(candidate)
    groups: list[GroupedCandidate] = []
    for members in members_by_root.values():
        member_ids = {item.candidate_id for item in members}
        reasons = tuple(
            sorted(
                {
                    reason
                    for left_id, right_id, reason in edge_reasons
                    if left_id in member_ids and right_id in member_ids
                }
            )
        )
        groups.extend(_build_groups(members, reasons, profiles, cjk_df))
    groups.sort(key=lambda item: item.group_id)
    groups = _annotate_overlaps(groups)
    parent_groups = _build_parent_groups(groups)
    parent_links: dict[str, list[str]] = {}
    for parent in parent_groups:
        for group_id in parent.member_group_ids:
            parent_links.setdefault(group_id, []).append(parent.parent_group_id)
    groups = [replace(group, parent_group_ids=tuple(sorted(parent_links.get(group.group_id, [])))) for group in groups]
    task_items.sort(key=lambda item: item.task_id)
    noise_items.sort(key=lambda item: item.noise_id)
    suppressed.sort(key=lambda item: item["suppressed_id"])
    return ConsolidationResult(
        raw_candidates=raw_candidates,
        grouped_candidates=tuple(groups),
        parent_groups=tuple(parent_groups),
        task_items=tuple(task_items),
        noise_items=tuple(noise_items),
        open_questions=questions,
        suppressed_audit=tuple(suppressed),
        merge_edge_count=len(edge_reasons),
    )