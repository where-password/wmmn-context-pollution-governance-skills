from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONVERSATION_SKILL_API_VERSION = "1.5.0"
CONVERSATION_CONTRACT_VERSION = "conversation-context-cleaner-v1.5.0"
CONTROLLED_INPUT_CONTRACT = "controlled_conversation_jsonl_v1_snapshot"
EXTRACTOR_VERSION = "deterministic-rules-v13"
STATE_RELATIVE_ROOT = Path("conversation-cleaner") / "v1"
ALLOWED_BOUNDARY_TYPES = ("conversation_closed", "task_terminal", "explicit_scan_boundary")


class ConversationCleanerError(RuntimeError):
    """Base error for the versioned conversation-cleaner API."""

    code = "conversation_cleaner_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class PathSafetyError(ConversationCleanerError):
    code = "unsafe_path"


class PlanBlockedError(ConversationCleanerError):
    code = "plan_blocked"


class PlanMismatchError(ConversationCleanerError):
    code = "plan_mismatch"


class IncompleteRunError(ConversationCleanerError):
    code = "incomplete_run"


@dataclass(frozen=True)
class Scope:
    workspace_id: str
    project_id: str
    source_id: str
    domain_profile: str
    profile_version: str
    conversation_ids: tuple[str, ...]
    boundary_type: str
    boundary_evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "domain_profile": self.domain_profile,
            "profile_version": self.profile_version,
            "conversation_ids": list(self.conversation_ids),
            "boundary_type": self.boundary_type,
            "boundary_evidence": self.boundary_evidence,
        }


@dataclass(frozen=True)
class Message:
    conversation_id: str
    message_id: str
    role: str
    timestamp: str
    timestamp_utc: str
    sequence: int
    text: str
    attachments: tuple[dict[str, Any], ...]
    event_type: str
    source_revision: str | None
    metadata: dict[str, Any]
    source_unit_hash: str
    revision_id: str
    source_unit_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "role": self.role,
            "timestamp": self.timestamp,
            "timestamp_utc": self.timestamp_utc,
            "sequence": self.sequence,
            "text": self.text,
            "attachments": list(self.attachments),
            "event_type": self.event_type,
            "source_revision": self.source_revision,
            "metadata": self.metadata,
            "source_unit_hash": self.source_unit_hash,
            "revision_id": self.revision_id,
            "source_unit_id": self.source_unit_id,
        }


@dataclass(frozen=True)
class TopicSegment:
    topic_segment_id: str
    conversation_id: str
    topic_key: str
    message_ids: tuple[str, ...]
    source_unit_ids: tuple[str, ...]
    start_source_unit_id: str
    end_source_unit_id: str
    summary: str
    boundary_reasons: tuple[str, ...]
    merged_segment_ids: tuple[str, ...] = ()
    merge_audit: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_segment_id": self.topic_segment_id,
            "conversation_id": self.conversation_id,
            "topic_key": self.topic_key,
            "message_ids": list(self.message_ids),
            "source_unit_ids": list(self.source_unit_ids),
            "start_source_unit_id": self.start_source_unit_id,
            "end_source_unit_id": self.end_source_unit_id,
            "summary": self.summary,
            "boundary_reasons": list(self.boundary_reasons),
            "merged_segment_ids": list(self.merged_segment_ids),
            "merge_audit": list(self.merge_audit),
        }


@dataclass(frozen=True)
class EvidenceSpan:
    evidence_id: str
    source_unit_id: str
    message_id: str
    role: str
    quote: str
    char_start: int
    char_end: int
    source_unit_hash: str
    verified: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_unit_id": self.source_unit_id,
            "message_id": self.message_id,
            "role": self.role,
            "quote": self.quote,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "source_unit_hash": self.source_unit_hash,
            "verified": self.verified,
            "status": self.status,
        }


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    workspace_id: str
    project_id: str
    conversation_id: str
    topic_segment_id: str
    domain_profile: str
    profile_version: str
    candidate_type: str
    proposed_memory_type: str
    content: str
    content_hash: str
    assertion_source: str
    verification_state: str
    epistemic_status: str
    authority_basis: str
    importance: str
    evidence: tuple[EvidenceSpan, ...]
    risk_flags: tuple[str, ...]
    suggested_action: str
    review_status: str
    record_status: str
    suggestion_only: bool = True
    extraction_method: str = "explicit_marker"
    heuristic_rule: str | None = None
    structure_kind: str = "sentence"
    structure_unit_id: str = ""
    structure_context: str = ""
    promotion_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "topic_segment_id": self.topic_segment_id,
            "domain_profile": self.domain_profile,
            "profile_version": self.profile_version,
            "candidate_type": self.candidate_type,
            "proposed_memory_type": self.proposed_memory_type,
            "content": self.content,
            "content_hash": self.content_hash,
            "assertion_source": self.assertion_source,
            "verification_state": self.verification_state,
            "epistemic_status": self.epistemic_status,
            "authority_basis": self.authority_basis,
            "importance": self.importance,
            "evidence": [item.to_dict() for item in self.evidence],
            "risk_flags": list(self.risk_flags),
            "suggested_action": self.suggested_action,
            "suggestion_only": self.suggestion_only,
            "review_status": self.review_status,
            "record_status": self.record_status,
            "extraction_method": self.extraction_method,
            "heuristic_rule": self.heuristic_rule,
            "structure_kind": self.structure_kind,
            "structure_unit_id": self.structure_unit_id,
            "structure_context": self.structure_context,
            "promotion_eligible": self.promotion_eligible,
        }


@dataclass(frozen=True)
class OpenQuestion:
    open_question_id: str
    candidate_id: str
    workspace_id: str
    project_id: str
    conversation_id: str
    topic_segment_id: str
    question: str
    content_hash: str
    evidence: tuple[EvidenceSpan, ...]
    status: str = "open"
    resolved: bool = False
    suggestion_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_question_id": self.open_question_id,
            "candidate_id": self.candidate_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "topic_segment_id": self.topic_segment_id,
            "question": self.question,
            "content_hash": self.content_hash,
            "evidence": [item.to_dict() for item in self.evidence],
            "status": self.status,
            "resolved": self.resolved,
            "suggestion_only": self.suggestion_only,
        }


@dataclass
class ParsedInput:
    raw_text: str
    source_sha256: str
    source_size: int
    messages: list[Message] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_lines: int = 0


@dataclass(frozen=True)
class EvidenceValidation:
    valid: bool
    checks: dict[str, bool]
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionResult:
    messages: tuple[Message, ...]
    segments: tuple[TopicSegment, ...]
    candidates: tuple[Candidate, ...]
    open_questions: tuple[OpenQuestion, ...]
    evidence_checks: dict[str, bool]
    warnings: tuple[str, ...]
    explicit_candidate_count: int = 0
    heuristic_candidate_count: int = 0
    questionish_skipped: int = 0
    heuristic_deduplicated: int = 0
    heuristic_limit_suppressed: int = 0
    heuristic_long_sentence_suppressed: int = 0
    suppressed_audit: tuple[dict[str, Any], ...] = ()
    heuristic_per_message_cap: int = 30
    heuristic_total_cap: int = 100


@dataclass(frozen=True)
class PlanBuild:
    plan: dict[str, Any]
    parsed: ParsedInput | None
    extraction: ExtractionResult | None
    consolidation: Any | None = None
