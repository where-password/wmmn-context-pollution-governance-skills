from __future__ import annotations

import re
import unicodedata
from typing import Any, Protocol

from .assertions import validate_assertion_policy
from .models import (
    AssertionSource,
    DedupPolicy,
    MemoryStatus,
    MemoryWriteInput,
    VerificationState,
)


class DomainProfile(Protocol):
    profile_id: str
    version: str
    domain: str
    allowed_memory_types: set[str]
    allowed_subtypes: set[str]
    indexable_fields: set[str]

    def validate_item(self, item: MemoryWriteInput) -> None: ...
    def validate_assertion(self, item: MemoryWriteInput, status: MemoryStatus) -> None: ...
    def validate_assertion_state(
        self, *, assertion_source: AssertionSource, verification_state: VerificationState,
        memory_type: str, status: MemoryStatus,
    ) -> None: ...
    def validate_payload(self, subtype: str | None, payload: dict[str, Any]) -> dict[str, Any]: ...
    def normalize_subject(self, subject: str | None, entity_type: str | None) -> str: ...
    def normalize_content(self, content: str) -> str: ...
    def default_dedup_policy(self, memory_type: str, subtype: str | None) -> DedupPolicy: ...
    def embedding_text(self, item: MemoryWriteInput, normalized_subject: str) -> str: ...
    def extract_indexable_payload(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def synthesis_template(self, task_type: str) -> str: ...


def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


class GenericProfile:
    profile_id = "generic"
    version = "1.0.0"
    domain = "generic"
    allowed_memory_types = {
        "fact", "requirement", "constraint", "decision", "task", "procedure",
        "issue", "solution", "artifact", "relationship", "event", "preference",
        "open_question",
    }
    allowed_subtypes: set[str] = set()
    indexable_fields = {"tags", "entity_names", "category", "memory_category", "importance"}

    def validate_item(self, item: MemoryWriteInput) -> None:
        if item.memory_type not in self.allowed_memory_types:
            raise ValueError(f"unsupported memory_type: {item.memory_type}")
        if item.subtype and self.allowed_subtypes and item.subtype not in self.allowed_subtypes:
            raise ValueError(f"unsupported subtype: {item.subtype}")
        if item.profile_version != self.version:
            raise ValueError(f"profile_version must be {self.version}")

    def validate_payload(self, subtype: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("domain_payload must be an object")
        return dict(payload)

    def validate_assertion_state(
        self, *, assertion_source: AssertionSource, verification_state: VerificationState,
        memory_type: str, status: MemoryStatus,
    ) -> None:
        validate_assertion_policy(
            assertion_source=assertion_source,
            verification_state=verification_state,
            status=status,
        )

    def validate_assertion(self, item: MemoryWriteInput, status: MemoryStatus) -> None:
        self.validate_assertion_state(
            assertion_source=item.assertion_source,
            verification_state=item.verification_state,
            memory_type=item.memory_type,
            status=status,
        )

    def normalize_subject(self, subject: str | None, entity_type: str | None) -> str:
        if not subject:
            return ""
        return _normalize_text(subject).casefold()

    def normalize_content(self, content: str) -> str:
        return _normalize_text(content)

    def default_dedup_policy(self, memory_type: str, subtype: str | None) -> DedupPolicy:
        if memory_type == "event" or subtype in {"timeline_event"}:
            return DedupPolicy.NO_AUTO_MERGE
        return DedupPolicy.CONTENT_ONLY

    def embedding_text(self, item: MemoryWriteInput, normalized_subject: str) -> str:
        payload = self.extract_indexable_payload(item.domain_payload)
        payload_text = " ".join(str(payload[key]) for key in sorted(payload))
        return " ".join(part for part in [item.memory_type, item.subtype or "", normalized_subject, item.content, payload_text] if part)

    def extract_indexable_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: payload[key] for key in sorted(payload) if key in self.indexable_fields}

    def synthesis_template(self, task_type: str) -> str:
        return f"Synthesize a grounded {task_type} answer from workspace memory evidence."


class NovelProfile(GenericProfile):
    profile_id = "novel"
    version = "1.0.0"
    domain = "novel"
    allowed_memory_types = {"fact", "relationship", "decision", "event", "open_question"}
    allowed_subtypes = {
        "narrative_fact", "character_relation", "plot_decision", "timeline_event",
        "unresolved_thread", "world_rule",
    }
    indexable_fields = {
        "memory_category", "importance", "entity_names", "tags", "chapter_index",
        "chapter_title", "retention_reason", "story_function",
    }

    _TYPE_SUBTYPES = {
        "fact": {"narrative_fact", "world_rule"},
        "relationship": {"character_relation"},
        "decision": {"plot_decision"},
        "event": {"timeline_event"},
        "open_question": {"unresolved_thread"},
    }
    _ALLOWED_CATEGORIES = {
        "narrative_fact": {
            "character_state", "environment_or_prop_state", "future_payoff_event",
            "relationship_change", "time_anchor", "unresolved_thread",
            "world_rule", "key_decision",
        },
        "character_relation": {"relationship_change"},
        "plot_decision": {"key_decision"},
        "timeline_event": {"time_anchor", "future_payoff_event"},
        "unresolved_thread": {"unresolved_thread"},
        "world_rule": {"world_rule"},
    }

    def validate_item(self, item: MemoryWriteInput) -> None:
        super().validate_item(item)
        if item.subtype not in self.allowed_subtypes:
            raise ValueError(f"novel subtype is required and must be one of {sorted(self.allowed_subtypes)}")
        allowed = self._TYPE_SUBTYPES.get(item.memory_type, set())
        if item.subtype not in allowed:
            raise ValueError(f"subtype {item.subtype} is invalid for memory_type {item.memory_type}")
        self.validate_payload(item.subtype, item.domain_payload)

    def validate_payload(self, subtype: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        payload = super().validate_payload(subtype, payload)
        category = payload.get("memory_category")
        if category is not None and subtype in self._ALLOWED_CATEGORIES:
            if category not in self._ALLOWED_CATEGORIES[subtype]:
                raise ValueError(f"memory_category {category} is invalid for subtype {subtype}")
        if category == "environment_or_prop_state" and not payload.get("story_function"):
            raise ValueError("environment_or_prop_state requires story_function")
        return payload

    def validate_assertion_state(
        self, *, assertion_source: AssertionSource, verification_state: VerificationState,
        memory_type: str, status: MemoryStatus,
    ) -> None:
        super().validate_assertion_state(
            assertion_source=assertion_source,
            verification_state=verification_state,
            memory_type=memory_type,
            status=status,
        )
        if status == MemoryStatus.ACTIVE and memory_type in {"fact", "relationship", "event"}:
            if assertion_source not in {
                AssertionSource.PRIMARY_SOURCE,
                AssertionSource.EXTERNAL_DOCUMENT,
                AssertionSource.TOOL_OUTPUT,
            }:
                raise ValueError(
                    "novel factual memories require primary_source, external_document, or tool_output"
                )
            if verification_state not in {
                VerificationState.SOURCE_VERIFIED,
                VerificationState.TOOL_VERIFIED,
            }:
                raise ValueError(
                    "novel factual memories require source_verified or tool_verified evidence"
                )

    def validate_assertion(self, item: MemoryWriteInput, status: MemoryStatus) -> None:
        self.validate_assertion_state(
            assertion_source=item.assertion_source,
            verification_state=item.verification_state,
            memory_type=item.memory_type,
            status=status,
        )

    def default_dedup_policy(self, memory_type: str, subtype: str | None) -> DedupPolicy:
        if subtype == "timeline_event" or memory_type == "event":
            return DedupPolicy.NO_AUTO_MERGE
        return DedupPolicy.CONTENT_ONLY

    def normalize_subject(self, subject: str | None, entity_type: str | None) -> str:
        normalized = super().normalize_subject(subject, entity_type)
        if entity_type in {"person", "character"}:
            normalized = re.sub(r"(先生|女士|小姐|同学|老师)$", "", normalized).strip()
        return normalized

    def synthesis_template(self, task_type: str) -> str:
        return (
            "Build a continuity-aware novel answer. Preserve relationship direction, "
            "timeline uncertainty, unresolved threads, and distinguish primary facts from context."
        )


_PROFILES: dict[tuple[str, str], DomainProfile] = {
    ("generic", GenericProfile.version): GenericProfile(),
    ("novel", NovelProfile.version): NovelProfile(),
}


class ProfileRegistry:
    def __init__(self, profiles: list[DomainProfile] | None = None) -> None:
        self._profiles: dict[tuple[str, str], DomainProfile] = dict(_PROFILES)
        for profile in profiles or []:
            self.register(profile)

    def register(self, profile: DomainProfile) -> None:
        self._profiles[(profile.profile_id, profile.version)] = profile

    def get(self, profile_id: str, version: str) -> DomainProfile:
        try:
            return self._profiles[(profile_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown domain profile: {profile_id}@{version}") from exc
