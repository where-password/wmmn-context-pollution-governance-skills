from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from .db import SQLiteStore
from .models import (
    AccessScope,
    AgentContextToken,
    AuthorizedContext,
    MemoryStatus,
    SearchRequest,
    SensitivityLevel,
    TOKEN_AUDIENCE,
)
from .profiles import ProfileRegistry


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def token_signing_payload(token: AgentContextToken) -> bytes:
    payload = token.model_dump(mode="json")
    payload.pop("signature", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_token(token: AgentContextToken, secret: bytes) -> str:
    return hmac.new(secret, token_signing_payload(token), hashlib.sha256).hexdigest()


def issue_token(
    *, secret: bytes, principal_id: str, task_id: str, grant_id: str,
    workspace_id: str, ttl_seconds: int = 300, now: datetime | None = None,
) -> AgentContextToken:
    now = _utc(now or datetime.now(timezone.utc))
    unsigned = AgentContextToken(
        jti=str(uuid.uuid4()), principal_id=principal_id, task_id=task_id,
        grant_id=grant_id, workspace_id=workspace_id, issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds), signature="placeholder",
    )
    return unsigned.model_copy(update={"signature": sign_token(unsigned, secret)})


def intersect_optional(requested: list[str], allowed: list[str]) -> list[str]:
    if not allowed:
        return list(requested)
    if not requested:
        return list(allowed)
    result = [item for item in requested if item in set(allowed)]
    return result or ["__deny_all__"]


class TokenValidator:
    def __init__(self, store: SQLiteStore, *, secret: bytes, clock_skew_seconds: int = 30) -> None:
        self.store = store
        self.secret = secret
        self.clock_skew_seconds = clock_skew_seconds

    def validate(
        self, token: AgentContextToken, *, action: str = "search",
        now: datetime | None = None, consume_query: bool = True,
    ) -> AuthorizedContext:
        now = _utc(now or datetime.now(timezone.utc))
        issued_at = _utc(token.issued_at)
        expires_at = _utc(token.expires_at)
        if token.alg != "HMAC-SHA256" or token.audience != TOKEN_AUDIENCE:
            raise PermissionError("unsupported token algorithm or audience")
        if issued_at > now + timedelta(seconds=self.clock_skew_seconds):
            raise PermissionError("token issued in the future")
        if expires_at < now - timedelta(seconds=self.clock_skew_seconds):
            raise PermissionError("token expired")
        if not hmac.compare_digest(sign_token(token, self.secret), token.signature):
            raise PermissionError("invalid token signature")
        grant = self.store.get_grant(token.grant_id)
        if grant is None:
            raise PermissionError("grant not found")
        if grant.revoked:
            raise PermissionError("grant revoked")
        if _utc(grant.expires_at) < now - timedelta(seconds=self.clock_skew_seconds):
            raise PermissionError("grant expired")
        if token.expires_at > grant.expires_at:
            raise PermissionError("token expiry exceeds grant expiry")
        if token.principal_id != grant.principal_id or token.workspace_id != grant.workspace_id:
            raise PermissionError("token is not bound to the grant principal/workspace")
        if action not in grant.actions:
            raise PermissionError(f"grant does not allow action: {action}")
        if not self.store.register_token_jti(
            jti=token.jti, principal_id=token.principal_id,
            grant_id=token.grant_id, expires_at=token.expires_at,
        ):
            raise PermissionError("token replay detected")
        remaining = self.store.consume_query_budget(token.grant_id) if consume_query else grant.remaining_queries
        return AuthorizedContext(
            token=token, grant=grant, effective_scope=grant.scope,
            max_sensitivity=grant.scope.max_sensitivity, query_budget_remaining=remaining,
        )

    def authorize_search(
        self, token: AgentContextToken, request: SearchRequest, *,
        profiles: ProfileRegistry, now: datetime | None = None,
    ) -> AccessScope:
        context = self.validate(token, action="search", now=now)
        grant = context.grant
        requested_payload = request.payload_filters
        if requested_payload:
            profile_ids = request.domains or grant.scope.domains or ["novel", "generic"]
            allowed_fields: set[str] = set()
            for profile_id in profile_ids:
                try:
                    profile = profiles.get(profile_id, "1.0.0")
                except KeyError:
                    continue
                allowed_fields |= profile.indexable_fields
            disallowed = set(requested_payload) - allowed_fields
            if disallowed:
                raise PermissionError(f"payload_filters contain non-whitelisted fields: {sorted(disallowed)}")
            for key, value in requested_payload.items():
                granted = grant.scope.payload_filters.get(key)
                if granted is not None and isinstance(granted, list) and value not in granted:
                    raise PermissionError(f"payload filter escaped grant: {key}")
        return AccessScope(
            project_ids=intersect_optional(request.project_ids, grant.scope.project_ids),
            domains=intersect_optional(request.domains, grant.scope.domains),
            memory_types=intersect_optional(request.memory_types, grant.scope.memory_types),
            statuses=list(grant.scope.statuses), max_sensitivity=grant.scope.max_sensitivity,
            allow_global_kb=bool(grant.scope.allow_global_kb and request.include_global_kb),
            payload_filters={**grant.scope.payload_filters, **requested_payload},
        )


def sensitivity_rank(value: SensitivityLevel | str) -> int:
    order = {
        SensitivityLevel.PUBLIC: 0, SensitivityLevel.INTERNAL: 1,
        SensitivityLevel.PRIVATE: 2, SensitivityLevel.SECRET: 3,
    }
    return order[SensitivityLevel(value)]


def status_allowed(status: MemoryStatus | str, allowed: list[MemoryStatus]) -> bool:
    return MemoryStatus(status) in set(allowed)
