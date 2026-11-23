"""Generic Memory Core V1.2.

This package is intentionally independent from the existing chat_etl_v1
production path. External embedding and Milvus adapters are optional and are
not invoked by the local test suite.
"""

from .readonly_milvus import ReadOnlyMilvusIndex
from .routing import RouteCapability, RouteCapabilityError
from .models import (
    AccessGrant,
    AccessScope,
    AgentContextToken,
    ConsolidationInput,
    ConsolidationResult,
    MemoryDeleteInput,
    MemoryIndexDoc,
    MemoryRecord,
    MemoryStatus,
    MemoryWriteInput,
    SearchHit,
    SearchRequest,
    SynthesisResult,
)

__all__ = [
    "AccessGrant",
    "AccessScope",
    "AgentContextToken",
    "ConsolidationInput",
    "ConsolidationResult",
    "MemoryDeleteInput",
    "MemoryIndexDoc",
    "MemoryRecord",
    "MemoryStatus",
    "MemoryWriteInput",
    "SearchHit",
    "SearchRequest",
    "ReadOnlyMilvusIndex",
    "RouteCapability",
    "RouteCapabilityError",
    "SynthesisResult",
]
