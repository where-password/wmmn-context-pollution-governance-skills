from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import Candidate, EvidenceSpan, Message, Scope, TopicSegment
from .handoff_contracts import (
    ASSERTION_SOURCES,
    IMPORTANCE_LEVELS,
    STATE_KINDS,
    VERIFICATION_STATES,
)
from .util import sha256_json


STRUCTURED_CONTRACT_VERSION = "conversation-memory-candidates-v1"
STRUCTURED_INFO_STRING = "memory-candidates-v1"
CITATION_ROLES = frozenset({"user", "assistant", "tool", "external", "document"})
_STRUCTURED_ASSERTION_SOURCES = frozenset(ASSERTION_SOURCES) | {"assistant_summary"}
_OPEN_FENCE = re.compile(r"^[ \t]*```[ \t]*memory-candidates-v1[ \t]*$")
_CLOSE_FENCE = re.compile(r"^[ \t]*```[ \t]*$")
_STATE_TO_CANDIDATE = {
    "decision": "decision",
    "preference": "preference",
    "task": "task",
    "constraint": "constraint",
    "fact": "fact",
    "result": "fact",
    "discussion": "fact",
    "hypothesis": "fact",
    "conflict": "fact",
}
_STATE_TO_MEMORY = {
    "decision": "decision",
    "preference": "preference",
    "task": "task",
    "constraint": "requirement",
    "fact": "fact",
    "result": "fact",
    "discussion": "fact",
    "hypothesis": "fact",
    "conflict": "issue",
}


class StructuredCandidate(Candidate):
    """Candidate carrying structured claim identity for the observation stage."""

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update({
            "claim_key": getattr(self, "claim_key", None),
            "scope_key": getattr(self, "scope_key", None),
            "state_kind": getattr(self, "state_kind", None),
            "observed_at": getattr(self, "observed_at", None),
            "effective_from": getattr(self, "effective_from", None),
            "effective_to": getattr(self, "effective_to", None),
            "subject": getattr(self, "subject", None),
            "entity_type": getattr(self, "entity_type", None),
            "entity_id": getattr(self, "entity_id", None),
            "domain_payload": getattr(self, "domain_payload", {}),
            "primary_evidence_message_id": getattr(self, "primary_evidence_message_id", None),
            "structured_assertion_source": getattr(self, "structured_assertion_source", None),
        })
        return data


@dataclass(frozen=True)
class AssistantStructuredIssue:
    code: str
    message: str
    message_id: str | None = None
    conversation_id: str | None = None
    line: int | None = None
    block_index: int | None = None
    claim_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "line": self.line,
            "block_index": self.block_index,
            "claim_index": self.claim_index,
        }


@dataclass(frozen=True)
class AssistantStructuredResult:
    candidates: tuple[Candidate, ...]
    issues: tuple[AssistantStructuredIssue, ...]
    structured_source_unit_ids: tuple[str, ...]


def _issue(
    code: str,
    message: str,
    *,
    source: Message | None = None,
    line: int | None = None,
    block_index: int | None = None,
    claim_index: int | None = None,
) -> AssistantStructuredIssue:
    return AssistantStructuredIssue(
        code=code,
        message=message,
        message_id=source.message_id if source is not None else None,
        conversation_id=source.conversation_id if source is not None else None,
        line=line,
        block_index=block_index,
        claim_index=claim_index,
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _datetime_text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-compatible values") from exc
    return dict(value)


def _extract_blocks(message: Message) -> tuple[list[tuple[int, int, str]], list[AssistantStructuredIssue]]:
    lines = message.text.splitlines()
    blocks: list[tuple[int, int, str]] = []
    issues: list[AssistantStructuredIssue] = []
    index = 0
    while index < len(lines):
        if not _OPEN_FENCE.fullmatch(lines[index]):
            index += 1
            continue
        start_line = index + 1
        body: list[str] = []
        index += 1
        while index < len(lines) and not _CLOSE_FENCE.fullmatch(lines[index]):
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            issues.append(_issue("unclosed_structured_block", "structured block has no closing fence", source=message, line=start_line))
            break
        blocks.append((start_line, index + 1, "\n".join(body)))
        index += 1
    if len(blocks) > 1:
        issues.append(_issue("multiple_structured_blocks", "assistant message contains more than one structured block", source=message, line=blocks[0][0] if blocks else None))
        return [], issues
    return blocks, issues


def _message_indexes(messages: list[Message]) -> tuple[dict[tuple[str, str], Message], dict[str, list[Message]]]:
    by_conversation_message: dict[tuple[str, str], Message] = {}
    by_id: dict[str, list[Message]] = {}
    for message in messages:
        by_conversation_message[(message.conversation_id, message.message_id)] = message
        by_id.setdefault(message.message_id, []).append(message)
    return by_conversation_message, by_id


def _citation_evidence(
    citation: dict[str, Any],
    *,
    source: Message,
    by_conversation_message: dict[tuple[str, str], Message],
    by_id: dict[str, list[Message]],
    claim_index: int,
    citation_index: int,
) -> tuple[EvidenceSpan, str, str]:
    allowed = {"message_id", "quote", "role", "locator_json"}
    unknown = set(citation) - allowed
    if unknown:
        raise ValueError(f"citation contains unknown fields: {', '.join(sorted(unknown))}")
    if "message_id" not in citation or "quote" not in citation:
        raise ValueError("citation is missing message_id or quote")
    message_id = _required_text(citation["message_id"], "citation.message_id")
    quote = _required_text(citation["quote"], "citation.quote")
    citation_role = citation.get("role")
    if citation_role is not None and citation_role not in CITATION_ROLES:
        raise ValueError("citation.role is invalid")
    locator = citation.get("locator_json", {})
    _json_object(locator, "citation.locator_json")

    target = by_conversation_message.get((source.conversation_id, message_id))
    if target is None:
        if any(item.conversation_id != source.conversation_id for item in by_id.get(message_id, [])):
            raise ValueError("citation.message_id belongs to another conversation")
        raise ValueError("citation.message_id does not exist")
    if citation_role in {"user", "assistant"} and target.role != citation_role:
        raise ValueError("citation.role does not match the target message")
    start = target.text.find(quote)
    if start < 0:
        raise ValueError("citation.quote is not an exact substring of the target message")
    end = start + len(quote)
    evidence_id = "ev_" + sha256_json(
        {
            "message_id": target.message_id,
            "source_unit_id": target.source_unit_id,
            "quote": quote,
            "char_start": start,
            "char_end": end,
            "claim_index": claim_index,
            "citation_index": citation_index,
        }
    )[:24]
    return (
        EvidenceSpan(
            evidence_id=evidence_id,
            source_unit_id=target.source_unit_id,
            message_id=target.message_id,
            role=target.role,
            quote=quote,
            char_start=start,
            char_end=end,
            source_unit_hash=target.source_unit_hash,
            verified=True,
            status="verified",
        ),
        target.role,
        citation_role or target.role,
    )


def _claim_candidate(
    claim: dict[str, Any],
    *,
    source: Message,
    segment: TopicSegment,
    scope: Scope,
    by_conversation_message: dict[tuple[str, str], Message],
    by_id: dict[str, list[Message]],
    claim_index: int,
) -> Candidate:
    required = {
        "claim_key", "scope_key", "state_kind", "content", "assertion_source",
        "verification_state", "importance", "citations",
    }
    optional = {"subject", "entity_type", "entity_id", "effective_from", "effective_to", "domain_payload"}
    missing = required - set(claim)
    unknown = set(claim) - required - optional
    if missing:
        raise ValueError(f"claim is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"claim contains unknown fields: {', '.join(sorted(unknown))}")
    claim_key = _required_text(claim["claim_key"], "claim.claim_key")
    scope_key = _required_text(claim["scope_key"], "claim.scope_key")
    content = _required_text(claim["content"], "claim.content")
    state_kind = claim["state_kind"]
    if state_kind not in STATE_KINDS:
        raise ValueError("claim.state_kind is invalid")
    assertion_input = claim["assertion_source"]
    if assertion_input not in _STRUCTURED_ASSERTION_SOURCES:
        raise ValueError("claim.assertion_source is invalid")
    assertion_source = "assistant_inference" if assertion_input == "assistant_summary" else assertion_input
    verification_state = claim["verification_state"]
    if verification_state not in VERIFICATION_STATES:
        raise ValueError("claim.verification_state is invalid")
    importance = claim["importance"]
    if importance not in IMPORTANCE_LEVELS:
        raise ValueError("claim.importance is invalid")
    citations = claim["citations"]
    if not isinstance(citations, list) or not citations:
        raise ValueError("claim.citations must be a non-empty array")
    effective_from = _datetime_text(claim.get("effective_from"), "claim.effective_from", optional=True)
    effective_to = _datetime_text(claim.get("effective_to"), "claim.effective_to", optional=True)
    if effective_from and effective_to and datetime.fromisoformat(effective_to.replace("Z", "+00:00")) < datetime.fromisoformat(effective_from.replace("Z", "+00:00")):
        raise ValueError("claim.effective_to must not precede effective_from")
    domain_payload = _json_object(claim.get("domain_payload", {}), "claim.domain_payload")
    subject = _optional_text(claim.get("subject"), "claim.subject")
    entity_type = _optional_text(claim.get("entity_type"), "claim.entity_type")
    entity_id = _optional_text(claim.get("entity_id"), "claim.entity_id")

    evidences: list[EvidenceSpan] = []
    evidence_roles: list[tuple[str, str]] = []
    for citation_index, raw_citation in enumerate(citations):
        if not isinstance(raw_citation, dict):
            raise ValueError("citation must be an object")
        evidence, target_role, citation_role = _citation_evidence(
            raw_citation,
            source=source,
            by_conversation_message=by_conversation_message,
            by_id=by_id,
            claim_index=claim_index,
            citation_index=citation_index,
        )
        evidences.append(evidence)
        evidence_roles.append((target_role, citation_role))

    if assertion_source in {"user_decision", "user_preference", "user_request"} and not any(
        target_role == "user" for target_role, _ in evidence_roles
    ):
        raise ValueError("user-controlled assertion_source requires at least one user citation")

    risk_flags = {"structured_assistant_candidate", "manual_review_required"}
    effective_verification = verification_state
    if verification_state in {"tool_verified", "source_verified"} and not any(
        citation_role in {"tool", "external", "document"} for _, citation_role in evidence_roles
    ):
        effective_verification = "unverified"
        risk_flags.add("verification_downgraded")
    if assertion_source in {"assistant_inference", "assistant_proposal"}:
        risk_flags.add("assistant_inference_requires_review")
    if assertion_input == "assistant_summary":
        risk_flags.add("assistant_summary_requires_review")
    if state_kind in {"discussion", "hypothesis", "conflict"}:
        risk_flags.add("review_state_kind")

    candidate_type = _STATE_TO_CANDIDATE[state_kind]
    memory_type = _STATE_TO_MEMORY[state_kind]
    evidence_tuple = tuple(evidences)
    content_hash = sha256_json(
        {
            "content": content,
            "candidate_type": candidate_type,
            "assertion_source": assertion_source,
            "verification_state": effective_verification,
            "evidences": [item.to_dict() for item in evidence_tuple],
        }
    )
    candidate_id = "cand_" + sha256_json(
        {
            "contract_version": STRUCTURED_CONTRACT_VERSION,
            "conversation_id": source.conversation_id,
            "message_id": source.message_id,
            "claim_key": claim_key,
            "scope_key": scope_key,
            "content_hash": content_hash,
        }
    )[:24]
    suggested_action = "verify_before_decision" if assertion_source in {"assistant_inference", "assistant_proposal"} else "standard_review"
    candidate = StructuredCandidate(
        candidate_id=candidate_id,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        conversation_id=source.conversation_id,
        topic_segment_id=segment.topic_segment_id,
        domain_profile=scope.domain_profile,
        profile_version=scope.profile_version,
        candidate_type=candidate_type,
        proposed_memory_type=memory_type,
        content=content,
        content_hash=content_hash,
        assertion_source=assertion_source,
        verification_state=effective_verification,
        epistemic_status="assistant_inferred" if assertion_source in {"assistant_inference", "assistant_proposal"} else "user_asserted",
        authority_basis="structured_assistant_output",
        importance=importance,
        evidence=evidence_tuple,
        risk_flags=tuple(sorted(risk_flags)),
        suggested_action=suggested_action,
        review_status="pending_review",
        record_status="candidate",
        extraction_method="assistant_structured",
        heuristic_rule=None,
        structure_kind="assistant_structured_claim",
        structure_unit_id=candidate_id,
        structure_context=json.dumps(
            {
                "claim_key": claim_key,
                "scope_key": scope_key,
                "state_kind": state_kind,
                "assertion_source_input": assertion_input,
                "citations": [
                    {"message_id": item.message_id, "role": item.role, "quote": item.quote}
                    for item in evidence_tuple
                ],
                "subject": subject,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "domain_payload": domain_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        promotion_eligible=False,
    )
    object.__setattr__(candidate, "claim_key", claim_key)
    object.__setattr__(candidate, "scope_key", scope_key)
    object.__setattr__(candidate, "state_kind", state_kind)
    object.__setattr__(candidate, "observed_at", by_conversation_message[(source.conversation_id, evidences[0].message_id)].timestamp)
    object.__setattr__(candidate, "effective_from", effective_from)
    object.__setattr__(candidate, "effective_to", effective_to)
    object.__setattr__(candidate, "subject", subject)
    object.__setattr__(candidate, "entity_type", entity_type)
    object.__setattr__(candidate, "entity_id", entity_id)
    object.__setattr__(candidate, "domain_payload", domain_payload)
    object.__setattr__(candidate, "primary_evidence_message_id", evidences[0].message_id)
    object.__setattr__(candidate, "structured_assertion_source", assertion_input)
    return candidate


def parse_structured_assistant_candidates(
    *,
    messages: list[Message],
    segments: list[TopicSegment],
    scope: Scope,
) -> AssistantStructuredResult:
    """Parse explicit assistant memory-candidate fences without inference or I/O."""

    by_conversation_message, by_id = _message_indexes(messages)
    segment_by_source_unit = {
        source_unit_id: segment
        for segment in segments
        for source_unit_id in segment.source_unit_ids
    }
    candidates: list[Candidate] = []
    issues: list[AssistantStructuredIssue] = []
    structured_source_unit_ids: list[str] = []
    for message in sorted(messages, key=lambda item: (item.conversation_id, item.sequence, item.message_id)):
        if message.role != "assistant":
            continue
        blocks, block_issues = _extract_blocks(message)
        issues.extend(block_issues)
        if block_issues:
            structured_source_unit_ids.append(message.source_unit_id)
            continue
        if not blocks:
            continue
        structured_source_unit_ids.append(message.source_unit_id)
        start_line, end_line, body = blocks[0]
        try:
            document = json.loads(body)
        except ValueError:
            issues.append(_issue("invalid_structured_json", "structured block is not valid JSON", source=message, line=start_line, block_index=1))
            continue
        if not isinstance(document, dict):
            issues.append(_issue("invalid_structured_json", "structured block must contain an object", source=message, line=start_line, block_index=1))
            continue
        allowed_top = {"contract_version", "claims"}
        unknown_top = set(document) - allowed_top
        if unknown_top:
            issues.append(_issue("unknown_structured_field", "structured block contains unknown top-level fields", source=message, line=start_line, block_index=1))
            continue
        if document.get("contract_version") != STRUCTURED_CONTRACT_VERSION:
            issues.append(_issue("invalid_structured_contract", "structured block contract_version is invalid", source=message, line=start_line, block_index=1))
            continue
        claims = document.get("claims")
        if not isinstance(claims, list) or not claims:
            issues.append(_issue("empty_structured_claims", "structured block claims must be a non-empty array", source=message, line=start_line, block_index=1))
            continue
        segment = segment_by_source_unit.get(message.source_unit_id)
        if segment is None:
            issues.append(_issue("structured_segment_missing", "structured message has no topic segment", source=message, line=start_line, block_index=1))
            continue
        for claim_index, claim in enumerate(claims, start=1):
            if not isinstance(claim, dict):
                issues.append(_issue("invalid_structured_claim", "claim must be an object", source=message, line=start_line, block_index=1, claim_index=claim_index))
                continue
            try:
                candidate = _claim_candidate(
                    claim,
                    source=message,
                    segment=segment,
                    scope=scope,
                    by_conversation_message=by_conversation_message,
                    by_id=by_id,
                    claim_index=claim_index,
                )
            except ValueError as exc:
                issues.append(_issue("invalid_structured_claim", str(exc), source=message, line=start_line, block_index=1, claim_index=claim_index))
                continue
            candidates.append(candidate)
    candidates.sort(key=lambda item: item.candidate_id)
    issues.sort(key=lambda item: (item.conversation_id or "", item.message_id or "", item.line or 0, item.claim_index or 0, item.code))
    return AssistantStructuredResult(tuple(candidates), tuple(issues), tuple(sorted(set(structured_source_unit_ids))))
