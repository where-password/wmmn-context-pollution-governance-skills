from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SKILL_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = SKILL_ROOT / "vendor"
VENDOR_CORE = VENDOR_ROOT / "memory_core"
VENDOR_MIGRATIONS = VENDOR_ROOT / "migrations"
if VENDOR_CORE.is_dir() and VENDOR_MIGRATIONS.is_dir():
    if str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    os.environ.setdefault("MEMORY_CORE_MIGRATIONS_DIR", str(VENDOR_MIGRATIONS))

from context_router import build_auto_context  # noqa: E402
from memory_core.skill_api import (  # noqa: E402
    SkillApiBlocked,
    SkillBackend,
    build_context_pack,
    create_default_embedding_client,
    create_readonly_milvus_from_profile,
    get_memory,
    health_check,
    load_database_profile,
    search_memory,
)

SERVER_NAME = "database-installer-core"
SERVER_VERSION = "0.6.0"
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}


class MemoryDatabaseMcpServer:
    def __init__(self, profile_path: str | Path, *, project_id: str | None = None, route_file: str | Path | None = None) -> None:
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.project_id = project_id
        self.route_file = Path(route_file).expanduser().resolve() if route_file else None
        self._read_index: Any | None = None
        self._embedding: Any | None = None
        self._adapters_loaded = False

    def _adapters(self) -> tuple[Any | None, Any | None]:
        if self._adapters_loaded:
            return self._read_index, self._embedding
        profile = load_database_profile(self.profile_path)
        if profile.backend == SkillBackend.MILVUS:
            self._read_index = create_readonly_milvus_from_profile(profile)
            self._embedding = create_default_embedding_client()
        self._adapters_loaded = True
        return self._read_index, self._embedding

    @staticmethod
    def _tools() -> list[dict[str, Any]]:
        common_scope = {
            "project_id": {"type": "string"},
            "max_sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "private", "secret"],
            },
        }
        return [
            {
                "name": "context",
                "description": "Build a bounded Context Pack from canonical memory. The caller's LLM writes the final answer.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "token_budget": {
                            "type": "integer",
                            "minimum": 1000,
                            "maximum": 8000,
                            "default": 3500,
                        },
                        **common_scope,
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "context_auto",
                "description": "Use first for any memory-dependent question. Route natural language to the configured operational conversation, course knowledge, or both, and return labeled context without writing.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "token_budget": {
                            "type": "integer",
                            "minimum": 1000,
                            "maximum": 8000,
                            "default": 3500,
                        },
                        "max_sensitivity": {
                            "type": "string",
                            "enum": ["public", "internal", "private", "secret"],
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "search",
                "description": "Return scoped ranked memory hits only. No final answer is generated.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                        **common_scope,
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_memory",
                "description": "Read one active memory in the profile scope. Evidence quotes are hidden by default.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "minLength": 1},
                        "max_content_chars": {
                            "type": "integer",
                            "minimum": 100,
                            "maximum": 4000,
                            "default": 4000,
                        },
                        "include_evidence_quote": {"type": "boolean", "default": False},
                        **common_scope,
                    },
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "health",
                "description": "Run scoped read-only database health checks.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "context":
                read_index, embedding = self._adapters()
                result = build_context_pack(
                    self.profile_path,
                    query=str(arguments.get("query", "")),
                    project_id=arguments.get("project_id") or self.project_id,
                    token_budget=int(arguments.get("token_budget", 3500)),
                    max_sensitivity=arguments.get("max_sensitivity"),
                    read_index=read_index,
                    embedding=embedding,
                ).model_dump(mode="json")
            elif name == "context_auto":
                read_index, embedding = self._adapters()
                result = build_auto_context(
                    self.profile_path,
                    query=str(arguments.get("query", "")),
                    route_file=self.route_file,
                    token_budget=int(arguments.get("token_budget", 3500)),
                    max_sensitivity=arguments.get("max_sensitivity"),
                    read_index=read_index,
                    embedding=embedding,
                )
            elif name == "search":
                read_index, embedding = self._adapters()
                result = search_memory(
                    self.profile_path,
                    query=str(arguments.get("query", "")),
                    project_id=arguments.get("project_id") or self.project_id,
                    top_k=int(arguments.get("top_k", 10)),
                    max_sensitivity=arguments.get("max_sensitivity"),
                    read_index=read_index,
                    embedding=embedding,
                ).model_dump(mode="json")
            elif name == "get_memory":
                result = get_memory(
                    self.profile_path,
                    memory_id=str(arguments.get("memory_id", "")),
                    project_id=arguments.get("project_id") or self.project_id,
                    max_sensitivity=arguments.get("max_sensitivity"),
                    max_content_chars=int(arguments.get("max_content_chars", 4000)),
                    include_evidence_quote=bool(arguments.get("include_evidence_quote", False)),
                ).model_dump(mode="json", exclude_none=True)
            elif name == "health":
                result = health_check(
                    self.profile_path,
                    project_id=arguments.get("project_id") or self.project_id,
                ).model_dump(mode="json")
            else:
                raise SkillApiBlocked("unknown MCP tool")
        except Exception as exc:
            message = str(exc) if isinstance(exc, SkillApiBlocked) else type(exc).__name__
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"status": "blocked", "tool": name, "error": message},
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
                }
            ],
            "isError": False,
        }

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "invalid JSON-RPC request"},
            }
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self._tools()},
            }
        if method == "tools/call":
            if not isinstance(params, dict):
                return self._rpc_error(request_id, -32602, "tools/call params must be an object")
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._rpc_error(request_id, -32602, "tools/call requires name and object arguments")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": self.call_tool(name, arguments),
            }
        return self._rpc_error(request_id, -32601, "method not found")

    @staticmethod
    def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def run_stdio(profile_path: str | Path, *, project_id: str | None = None, route_file: str | Path | None = None) -> int:
    server = MemoryDatabaseMcpServer(profile_path, project_id=project_id, route_file=route_file)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError:
            response = server._rpc_error(None, -32700, "parse error")
        else:
            response = server.handle_message(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MCP stdio server for database-installer-core")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--route-file")
    args = parser.parse_args(argv)
    return run_stdio(args.profile, project_id=args.project_id, route_file=args.route_file)


if __name__ == "__main__":
    raise SystemExit(main())
