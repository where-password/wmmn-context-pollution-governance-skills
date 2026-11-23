from __future__ import annotations

from .contracts import Candidate, EvidenceSpan, EvidenceValidation, Message


def validate_evidence_span(
    evidence: EvidenceSpan,
    *,
    messages_by_source_unit: dict[str, Message],
) -> EvidenceValidation:
    message = messages_by_source_unit.get(evidence.source_unit_id)
    checks = {
        "source_unit_exists": message is not None,
        "message_id_matches": bool(message and message.message_id == evidence.message_id),
        "role_matches": bool(message and message.role == evidence.role),
        "source_unit_hash_matches": bool(message and message.source_unit_hash == evidence.source_unit_hash),
        "range_in_bounds": bool(
            message
            and 0 <= evidence.char_start < evidence.char_end <= len(message.text)
        ),
    }
    checks["quote_matches_source"] = bool(
        message
        and checks["range_in_bounds"]
        and message.text[evidence.char_start:evidence.char_end] == evidence.quote
    )
    checks["quote_non_empty"] = bool(evidence.quote.strip())
    errors = tuple(name for name, passed in checks.items() if not passed)
    return EvidenceValidation(valid=not errors, checks=checks, errors=errors)


def validate_candidate_evidence(
    candidate: Candidate,
    *,
    messages_by_source_unit: dict[str, Message],
) -> EvidenceValidation:
    if not candidate.evidence:
        return EvidenceValidation(valid=False, checks={"has_evidence": False}, errors=("has_evidence",))
    checks: dict[str, bool] = {"has_evidence": True}
    errors: list[str] = []
    for index, evidence in enumerate(candidate.evidence):
        result = validate_evidence_span(evidence, messages_by_source_unit=messages_by_source_unit)
        for key, value in result.checks.items():
            checks[f"evidence[{index}].{key}"] = value
        errors.extend(f"evidence[{index}].{error}" for error in result.errors)
    return EvidenceValidation(valid=not errors, checks=checks, errors=tuple(errors))