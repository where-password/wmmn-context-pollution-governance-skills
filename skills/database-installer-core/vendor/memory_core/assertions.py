from __future__ import annotations

from .models import AssertionSource, MemoryStatus, VerificationState


STRONG_VERIFICATION = {
    VerificationState.SOURCE_VERIFIED,
    VerificationState.TOOL_VERIFIED,
}

USER_CONTROLLED_SOURCES = {
    AssertionSource.USER_DECISION,
    AssertionSource.USER_PREFERENCE,
    AssertionSource.USER_REQUEST,
}


def validate_assertion_policy(
    *,
    assertion_source: AssertionSource,
    verification_state: VerificationState,
    status: MemoryStatus,
) -> None:
    """Validate orthogonal assertion provenance and verification semantics."""
    if status != MemoryStatus.ACTIVE:
        return
    if (
        assertion_source == AssertionSource.ASSISTANT_INFERENCE
        and verification_state not in STRONG_VERIFICATION
    ):
        raise ValueError(
            "assistant_inference cannot be active without source_verified or tool_verified; "
            "explicit user adoption must be reclassified as user_decision"
        )
    if (
        assertion_source == AssertionSource.ASSISTANT_PROPOSAL
        and verification_state not in {VerificationState.USER_CONFIRMED, *STRONG_VERIFICATION}
    ):
        raise ValueError(
            "assistant_proposal cannot be active without user_confirmed, source_verified, or tool_verified"
        )
    if (
        assertion_source == AssertionSource.USER_REPORT
        and verification_state == VerificationState.LEGACY_UNCLASSIFIED
    ):
        raise ValueError("user_report active memories must state an explicit verification_state")
    if (
        assertion_source == AssertionSource.PRIMARY_SOURCE
        and verification_state not in {VerificationState.SOURCE_VERIFIED, VerificationState.TOOL_VERIFIED}
    ):
        raise ValueError("primary_source active memories require source_verified or tool_verified")
    if (
        assertion_source == AssertionSource.EXTERNAL_DOCUMENT
        and verification_state not in {VerificationState.SOURCE_VERIFIED, VerificationState.TOOL_VERIFIED}
    ):
        raise ValueError("external_document active memories require source_verified or tool_verified")
    if (
        assertion_source == AssertionSource.TOOL_OUTPUT
        and verification_state not in {VerificationState.TOOL_VERIFIED, VerificationState.SOURCE_VERIFIED}
    ):
        raise ValueError("tool_output active memories require tool_verified or source_verified")


def is_unverified_user_report(
    assertion_source: AssertionSource,
    verification_state: VerificationState,
) -> bool:
    return (
        assertion_source == AssertionSource.USER_REPORT
        and verification_state in {VerificationState.UNVERIFIED, VerificationState.QUOTE_VERIFIED}
    )
