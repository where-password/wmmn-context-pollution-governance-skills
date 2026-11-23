from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .assistant_structured import parse_structured_assistant_candidates
from .contracts import (
    Candidate,
    EvidenceSpan,
    ExtractionResult,
    Message,
    OpenQuestion,
    Scope,
    TopicSegment,
)
from .util import sha256_json
from .validation import validate_candidate_evidence, validate_evidence_span


DEFAULT_HEURISTIC_PER_MESSAGE_CAP = 30
DEFAULT_HEURISTIC_TOTAL_CAP = 100
MAX_HEURISTIC_QUOTE_CHARS = 360
MIN_HEURISTIC_QUOTE_CHARS = 8

_OPEN_QUESTION_PATTERN = re.compile(
    r"^\s*(?:OPEN_QUESTION|OPEN QUESTION|开放问题|待确认)\s*[:：]\s*(?P<content>.+?)\s*$",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[。！？!?；;](?:[\"'”’）)\]]*)")
_BULLET_PREFIX = re.compile(r"^\s*(?:\\?\*|\\?-|\\?\+|\d+\\?\.)\s*")
_NUMBERED_PREFIX = re.compile(r"^\s*\d+\\?\.\s*")
_HEADING = re.compile(r"^\s*\\?#{1,6}(?:\s|$)")
_CODE_FENCE = re.compile(r"^\s*\\?```")
_TABLE_ROW = re.compile(r"^\s*\\?\|")
_META_PREFIX = re.compile(r"^\s*(?:(?:问题|收益|角度|示例|例如|比如|注意|说明)\s*[:：]|(?:以下|如下)(?:是|为|包括)?)")
_ALTERNATIVE_PREFIX = re.compile(r"^\s*(?:要么|或者|另一种|备选|可选方案)")
_QUESTIONISH = re.compile(
    r"[?？]|(?:是否|能不能|可不可以|会不会|要不要|为什么|怎么|如何|多少|哪一|哪些|何时|谁)"
    r"|(?:有没有|还有没有|是不是|还是)"
    r"|(?:吗|么)(?=[，,。.!！?？]|$)|(?:可能|也许|不确定|待定|待确认|暂不确定)"
    r"|(?:(?:但|然而|可是).{0,80}(?:又写|又规定|又要求|尚未|未同步|矛盾|冲突|不一致))"
    r"|(?:(?:前序|前述|上述).{0,50}(?:尚未|未同步|未更新|未纳入))"
    r"|(?:however|but).{0,120}(?:also (?:states|requires)|not yet|inconsistent|conflict)",
    re.IGNORECASE,
)
_REQUIREMENT = re.compile(
    r"必须|务必|不得|不能|不要|禁止|严禁|只允许|只能(?!说|算|认为|作为|看作)|只负责|仅用于|应当|应该|应以|要求|需要|拒绝|不允许|白名单"
    r"|\b(?:must|shall|required|need to|do not|don't|never|only allow|forbid|prohibit)\b",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"决定|确定|采用|拍板|定死|保留|删除|统一|暂不|不再|放弃|允许|切换|改为"
    r"|\b(?:decide|decision|adopt|keep|retain|delete|unify|standardize|switch)\b",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(
    r"优先|希望|偏好|同意|赞同|倾向"
    r"|\b(?:prefer|priority|hope|agree)\b",
    re.IGNORECASE,
)
_TASK = re.compile(
    r"增加|新增|更新|修改|拆分|剥离|合并|落盘|补充|补上|接入|移除|下沉|重命名|先做|先把|再执行"
    r"|\b(?:add|update|split|merge|remove|rename|implement|refactor)\b",
    re.IGNORECASE,
)

_RULES: tuple[tuple[str, re.Pattern[str], dict[str, Any]], ...] = (
    (
        "decision",
        re.compile(r"^\s*(?:DECISION|决定|决策)\s*[:：]\s*(?P<content>.+?)\s*$", re.IGNORECASE),
        {
            "candidate_type": "decision",
            "proposed_memory_type": "decision",
            "assertion_source": "user_decision",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "authority_basis": "user_authoritative_statement",
            "importance": "high",
            "suggested_action": "standard_review",
            "risk_flags": ("manual_review_required",),
        },
    ),
    (
        "preference",
        re.compile(r"^\s*(?:PREFERENCE|偏好|用户偏好|建议)\s*[:：]\s*(?P<content>.+?)\s*$", re.IGNORECASE),
        {
            "candidate_type": "preference",
            "proposed_memory_type": "preference",
            "assertion_source": "user_preference",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "authority_basis": "user_authoritative_statement",
            "importance": "medium",
            "suggested_action": "standard_review",
            "risk_flags": ("manual_review_required",),
        },
    ),
    (
        "requirement",
        re.compile(r"^\s*(?:REQUIREMENT|CONSTRAINT|要求|约束)\s*[:：]\s*(?P<content>.+?)\s*$", re.IGNORECASE),
        {
            "candidate_type": "constraint",
            "proposed_memory_type": "requirement",
            "assertion_source": "user_request",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "authority_basis": "user_authoritative_statement",
            "importance": "high",
            "suggested_action": "standard_review",
            "risk_flags": ("manual_review_required",),
        },
    ),
    (
        "fact",
        re.compile(r"^\s*(?:FACT|事实)\s*[:：]\s*(?P<content>.+?)\s*$", re.IGNORECASE),
        {
            "candidate_type": "fact",
            "proposed_memory_type": "fact",
            "assertion_source": "user_report",
            "verification_state": "quote_verified",
            "epistemic_status": "user_asserted",
            "authority_basis": "user_assertion",
            "importance": "medium",
            "suggested_action": "verify_before_decision",
            "risk_flags": ("unverified_evidence", "manual_review_required"),
        },
    ),
    (
        "assistant_proposal",
        re.compile(r"^\s*(?:PROPOSAL|建议方案|方案建议)\s*[:：]\s*(?P<content>.+?)\s*$", re.IGNORECASE),
        {
            "candidate_type": "solution",
            "proposed_memory_type": "solution",
            "assertion_source": "assistant_proposal",
            "verification_state": "quote_verified",
            "epistemic_status": "assistant_proposed",
            "authority_basis": "assistant_suggestion",
            "importance": "medium",
            "suggested_action": "verify_before_decision",
            "risk_flags": ("assistant_candidate_requires_review", "manual_review_required"),
        },
    ),
)

_HEURISTIC_SPECS: dict[str, dict[str, Any]] = {
    "constraint": {
        "candidate_type": "constraint",
        "proposed_memory_type": "requirement",
        "assertion_source": "user_request",
        "verification_state": "quote_verified",
        "epistemic_status": "user_asserted",
        "authority_basis": "user_authoritative_statement",
        "importance": "high",
        "suggested_action": "standard_review",
        "risk_flags": ("heuristic_candidate", "manual_review_required"),
    },
    "decision": {
        "candidate_type": "decision",
        "proposed_memory_type": "decision",
        "assertion_source": "user_decision",
        "verification_state": "quote_verified",
        "epistemic_status": "user_asserted",
        "authority_basis": "user_authoritative_statement",
        "importance": "high",
        "suggested_action": "standard_review",
        "risk_flags": ("heuristic_candidate", "manual_review_required"),
    },
    "preference": {
        "candidate_type": "preference",
        "proposed_memory_type": "preference",
        "assertion_source": "user_preference",
        "verification_state": "quote_verified",
        "epistemic_status": "user_asserted",
        "authority_basis": "user_authoritative_statement",
        "importance": "medium",
        "suggested_action": "standard_review",
        "risk_flags": ("heuristic_candidate", "manual_review_required"),
    },
    "task": {
        "candidate_type": "task",
        "proposed_memory_type": "task",
        "assertion_source": "user_request",
        "verification_state": "quote_verified",
        "epistemic_status": "user_asserted",
        "authority_basis": "user_authoritative_statement",
        "importance": "medium",
        "suggested_action": "standard_review",
        "risk_flags": ("heuristic_candidate", "manual_review_required"),
    },
}


@dataclass(frozen=True)
class _HeuristicHit:
    message: Message
    segment: TopicSegment
    start: int
    end: int
    content: str
    raw_quote: str
    kind: str
    score: int
    sequence: int
    structure_kind: str
    structure_unit_id: str
    structure_context: str



@dataclass
class _UnitRecord:
    message: Message
    segment: TopicSegment
    start: int
    end: int
    structure_kind: str
    structure_context: str
    structure_unit_id: str
    summary: str
    hits: list[_HeuristicHit]

    @property
    def score(self) -> int:
        return max((item.score for item in self.hits), default=0)


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while end > start and text[end - 1] == "\\":
        end -= 1
        while end > start and text[end - 1].isspace():
            end -= 1
    return start, end


def _iter_lines(text: str):
    offset = 0
    for line in text.splitlines(keepends=True):
        clean = line.rstrip("\r\n")
        yield offset, clean
        offset += len(line)
    if offset < len(text):
        yield offset, text[offset:]


def _iter_structure_units(text: str):
    """Yield structural units and their nearest heading/lead-in context."""
    current_context = ""
    for line_offset, line in _iter_lines(text):
        line_without_slash = line.rstrip().rstrip("\\").rstrip()
        if not line_without_slash.strip():
            continue
        if _HEADING.match(line):
            current_context = _clean_display_text(line_without_slash.lstrip(" \\#"))
            continue
        if _CODE_FENCE.match(line):
            continue
        if re.match(r"^\s*\\?\|\s*:?-{3,}", line):
            continue
        stripped = line_without_slash.strip()
        bullet = _BULLET_PREFIX.match(line)
        is_table = bool(_TABLE_ROW.match(line))
        if not bullet and not is_table and stripped.endswith((": ", "：")):
            current_context = _clean_display_text(stripped)
            continue
        if is_table:
            start, end = _trim_span(line, 0, len(line))
            if start < end:
                yield line_offset + start, line_offset + end, "table_row", current_context
            continue
        if bullet:
            unit_kind = "numbered_item" if _NUMBERED_PREFIX.match(line) else "bullet_item"
            start, end = _trim_span(line, bullet.end(), len(line))
            if start < end:
                yield line_offset + start, line_offset + end, unit_kind, current_context
            continue
        cursor = 0
        for match in _SENTENCE_END.finditer(line, 0):
            start, end = _trim_span(line, cursor, match.end())
            if start < end:
                yield line_offset + start, line_offset + end, "sentence", current_context
            cursor = match.end()
        if cursor < len(line):
            start, end = _trim_span(line, cursor, len(line))
            if start < end:
                yield line_offset + start, line_offset + end, "sentence", current_context


def _split_unit_subclaims(text: str, start: int, end: int, structure_kind: str):
    if structure_kind == "sentence":
        yield start, end
        return
    cursor = start
    for match in _SENTENCE_END.finditer(text, start, end):
        sub_start, sub_end = _trim_span(text, cursor, match.end())
        if sub_start < sub_end:
            yield sub_start, sub_end
        cursor = match.end()
    if cursor < end:
        sub_start, sub_end = _trim_span(text, cursor, end)
        if sub_start < sub_end:
            yield sub_start, sub_end

def _unescape_markdown(text: str) -> str:
    for marker in ("*", "`", ".", "-", "#", "_", "|", "[", "]", "(", ")"):
        text = text.replace("\\" + marker, marker)
    return text


def _clean_display_text(text: str) -> str:
    text = _unescape_markdown(text).replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_key(text: str) -> str:
    cleaned = _clean_display_text(text).casefold()
    return re.sub(r"[\W_]+", "", cleaned, flags=re.UNICODE)


def _overlaps(start: int, end: int, spans: set[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def _evidence(
    *,
    message: Message,
    content: str,
    start: int,
    end: int,
    kind: str,
) -> EvidenceSpan:
    evidence_id = "ev_" + sha256_json(
        {
            "source_unit_id": message.source_unit_id,
            "kind": kind,
            "char_start": start,
            "char_end": end,
            "quote": content,
        }
    )[:24]
    return EvidenceSpan(
        evidence_id=evidence_id,
        source_unit_id=message.source_unit_id,
        message_id=message.message_id,
        role=message.role,
        quote=content,
        char_start=start,
        char_end=end,
        source_unit_hash=message.source_unit_hash,
        verified=True,
        status="verified",
    )


def _content_hash(
    *,
    content: str,
    candidate_type: str,
    epistemic_status: str,
    evidence: tuple[EvidenceSpan, ...],
) -> str:
    return sha256_json(
        {
            "content": content,
            "candidate_type": candidate_type,
            "epistemic_status": epistemic_status,
            "evidence": [
                {
                    "source_unit_id": item.source_unit_id,
                    "message_id": item.message_id,
                    "char_start": item.char_start,
                    "char_end": item.char_end,
                }
                for item in evidence
            ],
        }
    )


def _candidate(
    *,
    scope: Scope,
    message: Message,
    segment: TopicSegment,
    kind: str,
    evidence_kind: str,
    content: str,
    start: int,
    end: int,
    spec: dict[str, Any],
    extraction_method: str,
    heuristic_rule: str | None = None,
    quote: str | None = None,
    structure_kind: str = "sentence",
    structure_unit_id: str = "",
    structure_context: str = "",
    extra_risk_flags: tuple[str, ...] = (),
) -> Candidate:
    evidence_quote = quote if quote is not None else content
    evidence = (
        _evidence(message=message, content=evidence_quote, start=start, end=end, kind=evidence_kind),
    )
    content_hash = _content_hash(
        content=content,
        candidate_type=spec["candidate_type"],
        epistemic_status=spec["epistemic_status"],
        evidence=evidence,
    )
    candidate_id = "cand_" + sha256_json(
        {
            "workspace_id": scope.workspace_id,
            "project_id": scope.project_id,
            "conversation_id": message.conversation_id,
            "topic_segment_id": segment.topic_segment_id,
            "content_hash": content_hash,
            "extraction_method": extraction_method,
            "heuristic_rule": heuristic_rule,
        }
    )[:24]
    return Candidate(
        candidate_id=candidate_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        conversation_id=message.conversation_id,
        topic_segment_id=segment.topic_segment_id,
        domain_profile=scope.domain_profile,
        profile_version=scope.profile_version,
        candidate_type=spec["candidate_type"],
        proposed_memory_type=spec["proposed_memory_type"],
        content=content,
        content_hash=content_hash,
        assertion_source=spec["assertion_source"],
        verification_state=spec["verification_state"],
        epistemic_status=spec["epistemic_status"],
        authority_basis=spec["authority_basis"],
        importance=spec["importance"],
        evidence=evidence,
        risk_flags=tuple(sorted(set(spec["risk_flags"]) | set(extra_risk_flags))),
        suggested_action=spec["suggested_action"],
        review_status="pending_review",
        record_status="candidate",
        extraction_method=extraction_method,
        heuristic_rule=heuristic_rule,
        structure_kind=structure_kind,
        structure_unit_id=structure_unit_id,
        structure_context=structure_context,
        promotion_eligible=False,
    )


def _open_question(
    *,
    scope: Scope,
    message: Message,
    segment: TopicSegment,
    question: str,
    start: int,
    end: int,
) -> tuple[Candidate, OpenQuestion]:
    evidence = (_evidence(message=message, content=question, start=start, end=end, kind="open_question"),)
    content_hash = _content_hash(
        content=question,
        candidate_type="open_question",
        epistemic_status="open_question",
        evidence=evidence,
    )
    candidate_id = "cand_" + sha256_json(
        {
            "workspace_id": scope.workspace_id,
            "project_id": scope.project_id,
            "conversation_id": message.conversation_id,
            "topic_segment_id": segment.topic_segment_id,
            "content_hash": content_hash,
            "extraction_method": "explicit_marker",
        }
    )[:24]
    candidate = Candidate(
        candidate_id=candidate_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        conversation_id=message.conversation_id,
        topic_segment_id=segment.topic_segment_id,
        domain_profile=scope.domain_profile,
        profile_version=scope.profile_version,
        candidate_type="open_question",
        proposed_memory_type="open_question",
        content=question,
        content_hash=content_hash,
        assertion_source="user_request",
        verification_state="quote_verified",
        epistemic_status="open_question",
        authority_basis="unresolved_user_question",
        importance="medium",
        evidence=evidence,
        risk_flags=("open_question_unresolved", "manual_review_required"),
        suggested_action="keep_open",
        review_status="open",
        record_status="open_question",
        extraction_method="explicit_marker",
        heuristic_rule=None,
        promotion_eligible=False,
    )
    open_question = OpenQuestion(
        open_question_id="oq_" + content_hash[:24],
        candidate_id=candidate_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        conversation_id=message.conversation_id,
        topic_segment_id=segment.topic_segment_id,
        question=question,
        content_hash=content_hash,
        evidence=evidence,
    )
    return candidate, open_question


def _heuristic_signal_kinds(text: str) -> tuple[str, ...]:
    return tuple(
        kind
        for kind, pattern in (
            ("constraint", _REQUIREMENT),
            ("decision", _DECISION),
            ("preference", _PREFERENCE),
            ("task", _TASK),
        )
        if pattern.search(text)
    )


def _classify_heuristic(text: str) -> tuple[str, int] | None:
    if _META_PREFIX.match(text) or _ALTERNATIVE_PREFIX.match(text):
        return None
    scores = (
        ("constraint", 4, _REQUIREMENT),
        ("decision", 3, _DECISION),
        ("preference", 2, _PREFERENCE),
        ("task", 2, _TASK),
    )
    for kind, score, pattern in scores:
        if pattern.search(text):
            return kind, score
    return None


def _short_summary(text: str, limit: int = 120) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def _suppression_record(
    *,
    reason: str,
    message: Message,
    segment: TopicSegment,
    summary: str,
    char_start: int,
    char_end: int,
    heuristic_rule: str | None,
    reason_detail: str,
    structure_kind: str,
    structure_unit_id: str,
    structure_context: str,
) -> dict[str, Any]:
    payload = {
        "stage": "extraction",
        "reason": reason,
        "summary": _short_summary(summary),
        "source_message_id": message.message_id,
        "source_unit_id": message.source_unit_id,
        "topic_segment_id": segment.topic_segment_id,
        "char_start": char_start,
        "char_end": char_end,
        "heuristic_rule": heuristic_rule,
        "reason_detail": reason_detail,
        "structure_kind": structure_kind,
        "structure_unit_id": structure_unit_id,
        "structure_context": structure_context,
        "full_text_included": False,
    }
    payload["suppressed_id"] = "sup_" + sha256_json(payload)[:24]
    return payload


def extract_candidates(
    *,
    messages: list[Message],
    segments: list[TopicSegment],
    scope: Scope,
    per_message_cap: int = DEFAULT_HEURISTIC_PER_MESSAGE_CAP,
    total_cap: int = DEFAULT_HEURISTIC_TOTAL_CAP,
) -> ExtractionResult:
    message_by_source_unit = {item.source_unit_id: item for item in messages}
    segment_by_source_unit = {
        source_unit_id: segment
        for segment in segments
        for source_unit_id in segment.source_unit_ids
    }
    structured_result = parse_structured_assistant_candidates(
        messages=messages,
        segments=segments,
        scope=scope,
    )
    structured_source_unit_ids = set(structured_result.structured_source_unit_ids)
    candidates: dict[str, Candidate] = {
        candidate.candidate_id: candidate for candidate in structured_result.candidates
    }
    open_questions: dict[str, OpenQuestion] = {}
    consumed_spans: dict[str, set[tuple[int, int]]] = {}
    warning_messages: list[str] = [
        (
            f"assistant_structured[{issue.code}] at line {issue.line or 0}"
            f" claim {issue.claim_index or 0}: {issue.message}"
        )
        for issue in structured_result.issues
    ]
    questionish_skipped = 0
    heuristic_deduplicated = 0
    heuristic_limit_suppressed = 0
    heuristic_long_sentence_suppressed = 0

    # Pass 1: explicit markers have priority and reserve their source spans.
    for message in messages:
        if message.source_unit_id in structured_source_unit_ids:
            continue
        consumed = consumed_spans.setdefault(message.source_unit_id, set())
        segment = segment_by_source_unit[message.source_unit_id]
        for line_offset, line in _iter_lines(message.text):
            match = _OPEN_QUESTION_PATTERN.match(line)
            if match:
                start, end = _trim_span(line, match.start("content"), match.end("content"))
                question = line[start:end]
                if question:
                    start += line_offset
                    end += line_offset
                    candidate, open_question = _open_question(
                        scope=scope,
                        message=message,
                        segment=segment,
                        question=question,
                        start=start,
                        end=end,
                    )
                    candidates[candidate.candidate_id] = candidate
                    open_questions[open_question.open_question_id] = open_question
                    consumed.add((start, end))
                continue

            for kind, pattern, spec in _RULES:
                match = pattern.match(line)
                if not match:
                    continue
                if kind == "assistant_proposal" and message.role != "assistant":
                    continue
                if kind != "assistant_proposal" and message.role != "user":
                    continue
                start, end = _trim_span(line, match.start("content"), match.end("content"))
                content = line[start:end]
                if not content:
                    continue
                start += line_offset
                end += line_offset
                candidate = _candidate(
                    scope=scope,
                    message=message,
                    segment=segment,
                    kind=kind,
                    evidence_kind=kind,
                    content=content,
                    start=start,
                    end=end,
                    spec=spec,
                    extraction_method="explicit_marker",
                    structure_kind="line",
                )
                candidates[candidate.candidate_id] = candidate
                consumed.add((start, end))
                break

    # Pass 2: conservative user-only fallback. Cap accounting is per structural
    # unit; every high-value subclaim inside a selected unit is retained.
    by_message_units: dict[str, list[_UnitRecord]] = {}
    seen_keys: dict[str, _HeuristicHit] = {}
    suppressed_audit: list[dict[str, Any]] = []
    for message in messages:
        if message.role != "user":
            continue
        segment = segment_by_source_unit[message.source_unit_id]
        consumed = consumed_spans.get(message.source_unit_id, set())
        unit_records: list[_UnitRecord] = []
        for start, end, structure_kind, structure_context in _iter_structure_units(message.text):
            unit_id = "unit_" + sha256_json(
                {
                    "source_unit_id": message.source_unit_id,
                    "start": start,
                    "end": end,
                    "structure_kind": structure_kind,
                }
            )[:24]
            unit_text = message.text[start:end]
            unit_hits: list[_HeuristicHit] = []
            for sub_start, sub_end in _split_unit_subclaims(message.text, start, end, structure_kind):
                if _overlaps(sub_start, sub_end, consumed):
                    continue
                raw_quote = message.text[sub_start:sub_end]
                content = _clean_display_text(raw_quote)
                if len(re.sub(r"\s+", "", content)) < MIN_HEURISTIC_QUOTE_CHARS:
                    continue
                if (
                    structure_kind in {"bullet_item", "numbered_item"}
                    and len(content) < 50
                    and content.endswith("）")
                    and not _REQUIREMENT.search(content)
                    and not re.search(r"拍板|决定|采用", content)
                ):
                    continue
                classified = _classify_heuristic(content)
                if classified is None:
                    continue
                kind, score = classified
                if _QUESTIONISH.search(raw_quote):
                    questionish_skipped += 1
                    suppressed_audit.append(
                        _suppression_record(
                            reason="questionish_or_uncertain",
                            message=message,
                            segment=segment,
                            summary=content,
                            char_start=sub_start,
                            char_end=sub_end,
                            heuristic_rule=kind,
                            reason_detail="question/uncertain heuristic subclaim was skipped rather than classified",
                            structure_kind=structure_kind,
                            structure_unit_id=unit_id,
                            structure_context=structure_context,
                        )
                    )
                    continue
                if len(raw_quote) > MAX_HEURISTIC_QUOTE_CHARS:
                    heuristic_long_sentence_suppressed += 1
                    suppressed_audit.append(
                        _suppression_record(
                            reason="overlong_sentence",
                            message=message,
                            segment=segment,
                            summary=content,
                            char_start=sub_start,
                            char_end=sub_end,
                            heuristic_rule=kind,
                            reason_detail=f"quote length {len(raw_quote)} exceeds {MAX_HEURISTIC_QUOTE_CHARS}",
                            structure_kind=structure_kind,
                            structure_unit_id=unit_id,
                            structure_context=structure_context,
                        )
                    )
                    continue
                key = _dedupe_key(content)
                if not key:
                    continue
                hit = _HeuristicHit(
                    message=message,
                    segment=segment,
                    start=sub_start,
                    end=sub_end,
                    content=content,
                    raw_quote=raw_quote,
                    kind=kind,
                    score=score,
                    sequence=message.sequence,
                    structure_kind=structure_kind,
                    structure_unit_id=unit_id,
                    structure_context=structure_context,
                )
                existing = seen_keys.get(key)
                if existing is not None:
                    heuristic_deduplicated += 1
                    suppressed_audit.append(
                        _suppression_record(
                            reason="duplicate_sentence",
                            message=message,
                            segment=segment,
                            summary=content,
                            char_start=sub_start,
                            char_end=sub_end,
                            heuristic_rule=kind,
                            reason_detail=f"duplicate of {existing.message.message_id}:{existing.start}-{existing.end}",
                            structure_kind=structure_kind,
                            structure_unit_id=unit_id,
                            structure_context=structure_context,
                        )
                    )
                    if score > existing.score:
                        seen_keys[key] = hit
                    continue
                seen_keys[key] = hit
                unit_hits.append(hit)
            if unit_hits:
                unit_records.append(
                    _UnitRecord(
                        message=message,
                        segment=segment,
                        start=start,
                        end=end,
                        structure_kind=structure_kind,
                        structure_context=structure_context,
                        structure_unit_id=unit_id,
                        summary=_clean_display_text(unit_text),
                        hits=unit_hits,
                    )
                )
        by_message_units[message.source_unit_id] = unit_records

    per_message_selected_units: list[_UnitRecord] = []
    for message in messages:
        units = by_message_units.get(message.source_unit_id, [])
        units.sort(key=lambda item: (-item.score, item.start))
        selected_units = units[:per_message_cap]
        for unit in units[per_message_cap:]:
            suppressed_audit.append(
                _suppression_record(
                    reason="per_message_cap",
                    message=unit.message,
                    segment=unit.segment,
                    summary=unit.summary,
                    char_start=unit.start,
                    char_end=unit.end,
                    heuristic_rule="composite",
                    reason_detail=f"per-message structural-unit cap is {per_message_cap}",
                    structure_kind=unit.structure_kind,
                    structure_unit_id=unit.structure_unit_id,
                    structure_context=unit.structure_context,
                )
            )
        heuristic_limit_suppressed += max(0, len(units) - len(selected_units))
        per_message_selected_units.extend(selected_units)

    per_message_selected_units.sort(
        key=lambda item: (-item.score, item.message.conversation_id, item.message.sequence, item.start)
    )
    selected_units = per_message_selected_units[:total_cap]
    for unit in per_message_selected_units[total_cap:]:
        suppressed_audit.append(
            _suppression_record(
                reason="total_cap",
                message=unit.message,
                segment=unit.segment,
                summary=unit.summary,
                char_start=unit.start,
                char_end=unit.end,
                heuristic_rule="composite",
                reason_detail=f"run-level structural-unit cap is {total_cap}",
                structure_kind=unit.structure_kind,
                structure_unit_id=unit.structure_unit_id,
                structure_context=unit.structure_context,
            )
        )
    heuristic_limit_suppressed += max(0, len(per_message_selected_units) - len(selected_units))
    selected_hits = [hit for unit in selected_units for hit in unit.hits]
    selected_hits.sort(key=lambda item: (item.message.conversation_id, item.sequence, item.start))

    for hit in selected_hits:
        candidate = _candidate(
            scope=scope,
            message=hit.message,
            segment=hit.segment,
            kind=hit.kind,
            evidence_kind=f"heuristic_{hit.kind}",
            content=hit.content,
            start=hit.start,
            end=hit.end,
            spec=_HEURISTIC_SPECS[hit.kind],
            extraction_method="heuristic_fallback",
            heuristic_rule=hit.kind,
            quote=hit.raw_quote,
            structure_kind=hit.structure_kind,
            structure_unit_id=hit.structure_unit_id,
            structure_context=hit.structure_context,
            extra_risk_flags=(
                ("mixed_signal_claim",)
                if len(_heuristic_signal_kinds(hit.content)) >= 2
                and len(re.sub(r"\s+", "", hit.content)) >= 40
                else ()
            ),
        )
        candidates[candidate.candidate_id] = candidate

    sorted_candidates = tuple(sorted(candidates.values(), key=lambda item: item.candidate_id))
    sorted_questions = tuple(sorted(open_questions.values(), key=lambda item: item.open_question_id))
    evidence_checks: dict[str, bool] = {}
    for issue_index, issue in enumerate(structured_result.issues, start=1):
        evidence_checks[
            f"assistant_structured.{issue_index}.{issue.line or 0}.{issue.claim_index or 0}.valid"
        ] = False
    for candidate in sorted_candidates:
        validation = validate_candidate_evidence(candidate, messages_by_source_unit=message_by_source_unit)
        evidence_checks[f"{candidate.candidate_id}.valid"] = validation.valid
        if not validation.valid:
            warning_messages.append(f"evidence validation failed for {candidate.candidate_id}")
    for question in sorted_questions:
        validation = validate_evidence_span(
            question.evidence[0], messages_by_source_unit=message_by_source_unit
        )
        evidence_checks[f"{question.open_question_id}.valid"] = validation.valid
        if not validation.valid:
            warning_messages.append(f"evidence validation failed for {question.open_question_id}")

    explicit_count = sum(item.extraction_method in {"explicit_marker", "assistant_structured"} for item in sorted_candidates)
    heuristic_count = sum(item.extraction_method == "heuristic_fallback" for item in sorted_candidates)
    if not sorted_candidates:
        warning_messages.append("no explicit markers or conservative heuristic candidates were found")
    if questionish_skipped:
        warning_messages.append(f"{questionish_skipped} question/uncertain sentences skipped by heuristic")
    if heuristic_deduplicated:
        warning_messages.append(f"{heuristic_deduplicated} duplicate heuristic sentences suppressed")
    if heuristic_limit_suppressed:
        warning_messages.append(f"{heuristic_limit_suppressed} heuristic candidates suppressed by conservative limits")
    if heuristic_long_sentence_suppressed:
        warning_messages.append(f"{heuristic_long_sentence_suppressed} overlong heuristic sentences skipped")

    return ExtractionResult(
        messages=tuple(messages),
        segments=tuple(segments),
        candidates=sorted_candidates,
        open_questions=sorted_questions,
        evidence_checks=evidence_checks,
        warnings=tuple(warning_messages),
        explicit_candidate_count=explicit_count,
        heuristic_candidate_count=heuristic_count,
        questionish_skipped=questionish_skipped,
        heuristic_deduplicated=heuristic_deduplicated,
        heuristic_limit_suppressed=heuristic_limit_suppressed,
        heuristic_long_sentence_suppressed=heuristic_long_sentence_suppressed,
        suppressed_audit=tuple(sorted(suppressed_audit, key=lambda item: item["suppressed_id"])),
        heuristic_per_message_cap=per_message_cap,
        heuristic_total_cap=total_cap,
    )
