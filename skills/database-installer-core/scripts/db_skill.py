from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any

from runtime_preflight import build_runtime_plan

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = SKILL_ROOT / "assets" / "demo" / "demo_manifest.json"
DEFAULT_WORKSPACE = Path.cwd() / "database-installer-workspace"
DEFAULT_MILVUS_COMPOSE = SKILL_ROOT / "assets" / "milvus" / "docker-compose.yml"
RUNTIME_LOCK = SKILL_ROOT / "assets" / "runtime" / "requirements-runtime.lock"
VENDOR_ROOT = SKILL_ROOT / "vendor"
VENDOR_CORE = VENDOR_ROOT / "memory_core"
VENDOR_MIGRATIONS = VENDOR_ROOT / "migrations"
MEMORY_CORE_SRC = Path(os.environ.get("MEMORY_CORE_SRC", "")).resolve() if os.environ.get("MEMORY_CORE_SRC") else None
if MEMORY_CORE_SRC and MEMORY_CORE_SRC.is_dir() and str(MEMORY_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(MEMORY_CORE_SRC))
elif VENDOR_CORE.is_dir() and VENDOR_MIGRATIONS.is_dir():
    if str(VENDOR_ROOT) not in sys.path:
        sys.path.insert(0, str(VENDOR_ROOT))
    os.environ.setdefault("MEMORY_CORE_MIGRATIONS_DIR", str(VENDOR_MIGRATIONS))
MEMORY_CORE_SOURCE = Path(os.environ.get("MEMORY_CORE_SOURCE", "")).resolve() if os.environ.get("MEMORY_CORE_SOURCE") else (
    MEMORY_CORE_SRC.parent if MEMORY_CORE_SRC and MEMORY_CORE_SRC.name == "src" else None
)

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_CONFIRM = 3
EXIT_VERIFY = 4
EXIT_ERROR = 5
HIDDEN_COMMANDS = {"demo", "init"}

CORE_IMPORT_ERROR: Exception | None = None
try:
    from memory_core.skill_api import (
        INSTALLER_API_VERSION,
        SKILL_API_VERSION,
        ConnectPlan,
        DemoPlan,
        InstallPlan,
        RestorePlan,
        SecretScanResult,
        SkillApiBlocked,
        UpgradePlan,
        LocalMilvusInstallPlan,
        MilvusBootstrapPlan,
        ProfileAttachPlan,
        connect_apply,
        connect_plan,
        demo_apply,
        demo_plan,
        doctor,
        export_profile,
        install_apply,
        install_plan,
        installer_backup,
        load_connection_profile,
        local_milvus_install_apply,
        local_milvus_install_plan,
        milvus_bootstrap_apply,
        milvus_bootstrap_plan,
        milvus_connect_apply,
        milvus_connect_plan,
        milvus_reconcile_status,
        milvus_status,
        apply_handoff_ingest_local,
        build_context_pack,
        cleanup_local_grants,
        get_memory,
        health_check,
        plan_handoff_ingest,
        query_context,
        resolve_written_memory_map_from_canonical,
        restore_apply,
        restore_plan,
        search_memory,
        sync_milvus_outbox,
        sample_apply,
        sample_plan,
        scan_for_secrets,
        self_test,
        smoke,
        status,
        uninstall_runtime,
        upgrade_apply,
        upgrade_plan,
    )
except Exception as exc:  # Import failure must still yield a dependency repair plan.
    CORE_IMPORT_ERROR = exc


def _runtime_plan(workspace_root: str | Path = DEFAULT_WORKSPACE) -> dict[str, Any]:
    return build_runtime_plan(
        workspace_root=workspace_root,
        requirements_lock=RUNTIME_LOCK,
        memory_core_source=MEMORY_CORE_SOURCE,
    )


def _emit(payload: dict[str, Any], *, json_mode: bool, secret_refs: tuple[str, ...] = ()) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if CORE_IMPORT_ERROR is None:
        scan = scan_for_secrets(text, secret_refs=secret_refs)
        if scan.status == "warning":
            payload = {
                **payload,
                "security_receipt": {
                    "status": "warning",
                    "warnings": list(scan.warnings),
                },
            }
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if scan.status == "blocked":
            safe_receipt = {
                "command": str(payload.get("command", "unknown")),
                "status": "blocked",
                "error": {"code": "secret_scan_blocked", "message": "输出包含敏感模式，已拒绝回显和落盘"},
                "safe_receipt": {
                    "operation_id": str(payload.get("result", {}).get("plan_id", "redacted")),
                    "command": str(payload.get("command", "unknown")),
                    "check_id": "secret_scan",
                    "redaction_counts": scan.redaction_counts,
                },
            }
            print(json.dumps(safe_receipt, ensure_ascii=False, sort_keys=True) if json_mode else json.dumps(safe_receipt, ensure_ascii=False, indent=2, sort_keys=True))
            return
    if json_mode:
        print(text)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _resolve_milvus_sync_profile(profile_path: str | Path, milvus_profile_path: str | Path | None) -> Path:
    if milvus_profile_path is not None:
        target = Path(milvus_profile_path).expanduser().resolve()
    else:
        profile_file = Path(profile_path).expanduser().resolve()
        try:
            raw = json.loads(profile_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise SkillApiBlocked("profile is not valid UTF-8 JSON") from exc
        if isinstance(raw, dict) and raw.get("contract_version") == "skill-profile-v1":
            if raw.get("backend") != "milvus":
                raise SkillApiBlocked("profile backend must be milvus for index sync")
            target = profile_file
        else:
            connection = load_connection_profile(profile_file)
            target = (connection.workspace_root / "config" / "milvus-profile.json").resolve()
            if not target.is_relative_to(connection.workspace_root.resolve()):
                raise SkillApiBlocked("milvus profile must stay inside workspace_root")
    if not target.is_file():
        raise SkillApiBlocked("milvus profile does not exist")
    return target


PERSONAL_PRESET_VERSION = "database-skill-personal-preset-v1"
_PRESET_FIELDS = {
    "profile", "project_id", "source_id", "conversation_id", "handoff_dir",
    "milvus_profile", "max_sensitivity", "owner", "route_file",
}
_PRESET_PATH_FIELDS = {"profile", "handoff_dir", "milvus_profile", "route_file"}


def _load_personal_preset(preset_path: str | Path) -> dict[str, Any]:
    path = Path(preset_path).expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError("personal preset must be an existing regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("personal preset is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or raw.get("contract_version") != PERSONAL_PRESET_VERSION:
        raise ValueError("personal preset contract_version is unsupported")
    unknown = set(raw) - _PRESET_FIELDS - {"contract_version"}
    if unknown:
        raise ValueError("personal preset contains unknown fields")
    result: dict[str, Any] = {}
    for name, value in raw.items():
        if name == "contract_version":
            continue
        if name in _PRESET_PATH_FIELDS:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"personal preset {name} must be a non-empty path")
            candidate = Path(value).expanduser()
            result[name] = candidate if candidate.is_absolute() else (path.parent / candidate).resolve()
        else:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"personal preset {name} must be a non-empty string")
            result[name] = value
    return result


def _apply_preset_defaults(args: argparse.Namespace) -> None:
    preset_path = getattr(args, "preset", None)
    if not preset_path:
        return
    preset = _load_personal_preset(preset_path)
    for name, value in preset.items():
        if hasattr(args, name) and getattr(args, name) in (None, ""):
            setattr(args, name, str(value) if isinstance(value, Path) else value)


def _require_personal_args(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    for name in names:
        value = getattr(args, name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"--{name.replace('_', '-')} is required unless supplied by --preset")


def _preset_init_payload(args: argparse.Namespace) -> dict[str, Any]:
    profile_path = Path(args.profile).expanduser().resolve()
    connection = load_connection_profile(profile_path)
    workspace_root = Path(connection.workspace_root).resolve()
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else (workspace_root / "config" / "personal-preset.json").resolve()
    )
    if not output.is_relative_to(workspace_root):
        raise SkillApiBlocked("personal preset output must stay inside workspace_root")
    if output.is_symlink():
        raise SkillApiBlocked("personal preset output must not be a symlink")
    relative_profile = Path(os.path.relpath(profile_path, output.parent)).as_posix()
    payload = {
        "contract_version": PERSONAL_PRESET_VERSION,
        "profile": relative_profile,
        "project_id": connection.project_ids[0] if connection.project_ids else "",
        "source_id": "personal_conversation",
        "max_sensitivity": connection.max_sensitivity.value,
        "owner": "database-skill-sync",
    }
    if getattr(args, "route_file", None):
        route_path = Path(args.route_file).expanduser().resolve()
        if not route_path.is_relative_to(workspace_root):
            raise SkillApiBlocked("route file must stay inside workspace_root")
        if not route_path.is_file() or route_path.is_symlink():
            raise SkillApiBlocked("route file must be an existing regular file")
        payload["route_file"] = Path(os.path.relpath(route_path, output.parent)).as_posix()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous = output.read_text(encoding="utf-8") if output.is_file() else None
    if previous is not None and previous != rendered and not args.force:
        raise SkillApiBlocked("personal preset already exists; pass --force to replace it")
    changed = previous != rendered
    if changed:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8", newline="\n")
    return {
        "command": "preset init",
        "status": "generated" if changed else "already_generated",
        "changed": changed,
        "preset_path": str(output),
        "profile_path": str(profile_path),
        "contains_secret_values": False,
    }


def _profile_secret_refs(profile_path: str | Path) -> tuple[str, ...]:
    try:
        return tuple(load_connection_profile(profile_path).secret_refs)
    except Exception:
        return ()


def _write_plan(path: str, model: Any) -> None:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-db", description="database-installer-core V1")
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    doctor_cmd = sub.add_parser("doctor", help="Read-only environment and runtime repair check")
    doctor_cmd.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE))
    doctor_cmd.add_argument("--secret-ref", action="append", default=[])

    install = sub.add_parser("install", help="Create a new SQLite memory database")
    install_sub = install.add_subparsers(dest="install_action", required=True)
    install_plan_cmd = install_sub.add_parser("plan", help="Plan an empty SQLite installation")
    install_plan_cmd.add_argument("--workspace-root", required=True)
    install_plan_cmd.add_argument("--workspace-id", required=True)
    install_plan_cmd.add_argument("--project-id", required=True)
    install_plan_cmd.add_argument("--domain-profile", default="generic")
    install_plan_cmd.add_argument("--owner-principal-id", required=True)
    install_plan_cmd.add_argument("--plan-out")
    install_apply_cmd = install_sub.add_parser("apply", help="Apply the exact install plan")
    install_apply_cmd.add_argument("--plan-file", required=True)
    install_apply_cmd.add_argument("--confirm-token", required=True)

    connect = sub.add_parser("connect", help="Attach an existing SQLite memory database")
    connect_sub = connect.add_subparsers(dest="connect_action", required=True)
    connect_plan_cmd = connect_sub.add_parser("plan", help="Read-only validation before attaching SQLite")
    connect_plan_cmd.add_argument("--workspace-root", required=True)
    connect_plan_cmd.add_argument("--canonical-db", required=True)
    connect_plan_cmd.add_argument("--workspace-id", required=True)
    connect_plan_cmd.add_argument("--project-id", required=True)
    connect_plan_cmd.add_argument("--domain-profile", default="generic")
    connect_plan_cmd.add_argument("--owner-principal-id", required=True)
    connect_plan_cmd.add_argument("--environment", default="development")
    connect_plan_cmd.add_argument("--plan-out")
    connect_apply_cmd = connect_sub.add_parser("apply", help="Apply the exact connect plan")
    connect_apply_cmd.add_argument("--plan-file", required=True)
    connect_apply_cmd.add_argument("--confirm-token", required=True)

    self_test_cmd = sub.add_parser("self-test", help="Run a temporary SQLite test and delete all test data")
    self_test_cmd.add_argument("--manifest", default=str(DEFAULT_MANIFEST))

    sample = sub.add_parser("sample", help="Optional teaching/display sample; not an installation prerequisite")
    sample_sub = sample.add_subparsers(dest="sample_action", required=True)
    sample_plan_cmd = sample_sub.add_parser("plan", help="Plan an explicit persistent sample root")
    sample_plan_cmd.add_argument("--workspace-root", required=True)
    sample_plan_cmd.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    sample_plan_cmd.add_argument("--plan-out")
    sample_apply_cmd = sample_sub.add_parser("apply", help="Apply a previously reviewed sample plan")
    sample_apply_cmd.add_argument("--plan-file", required=True)
    sample_apply_cmd.add_argument("--confirm-token", required=True)

    milvus = sub.add_parser("milvus", help="Optional Milvus V1.5 Add-on")
    milvus_sub = milvus.add_subparsers(dest="milvus_action", required=True)
    milvus_status_cmd = milvus_sub.add_parser("status", help="Probe vector/embedding/collection/alias status")
    milvus_status_cmd.add_argument("--profile", required=True)
    milvus_status_cmd.add_argument("--route-file")
    milvus_status_cmd.add_argument("--milvus-profile")
    milvus_status_cmd.add_argument("--fake", action="store_true")
    milvus_status_cmd.add_argument("--uri-env", default="MILVUS_URI")
    milvus_status_cmd.add_argument("--embedding-key-env", default="EMBEDDING_API_KEY")

    milvus_connect = milvus_sub.add_parser("connect", help="Attach an existing non-production Milvus alias")
    milvus_connect_sub = milvus_connect.add_subparsers(dest="milvus_connect_action", required=True)
    milvus_connect_plan_cmd = milvus_connect_sub.add_parser("plan", help="Read-only Milvus attach plan")
    milvus_connect_plan_cmd.add_argument("--profile", required=True)
    milvus_connect_plan_cmd.add_argument("--route-file", required=True)
    milvus_connect_plan_cmd.add_argument("--milvus-profile")
    milvus_connect_plan_cmd.add_argument("--fake", action="store_true")
    milvus_connect_plan_cmd.add_argument("--plan-out")
    milvus_connect_apply_cmd = milvus_connect_sub.add_parser("apply", help="Write the local Milvus profile")
    milvus_connect_apply_cmd.add_argument("--plan-file", required=True)
    milvus_connect_apply_cmd.add_argument("--confirm-token", required=True)

    milvus_sync_cmd = milvus_sub.add_parser("sync", help="Process pending Outbox events into FTS and Milvus")
    milvus_sync_cmd.add_argument("--profile")
    milvus_sync_cmd.add_argument("--preset")
    milvus_sync_cmd.add_argument("--milvus-profile")
    milvus_sync_cmd.add_argument("--fake", action="store_true")
    milvus_sync_cmd.add_argument("--limit", type=int, default=100)
    milvus_sync_cmd.add_argument("--owner")
    milvus_sync_cmd.add_argument("--lease-seconds", type=int, default=60)
    milvus_sync_cmd.add_argument("--uri-env", default="MILVUS_URI")
    milvus_sync_cmd.add_argument("--embedding-key-env", default="EMBEDDING_API_KEY")
    milvus_sync_cmd.add_argument("--embedding-base-url-env", default="EMBEDDING_BASE_URL")
    milvus_sync_cmd.add_argument("--embedding-model-env", default="EMBEDDING_MODEL")
    milvus_sync_cmd.add_argument("--embedding-dim-env", default="EMBEDDING_DIM")

    milvus_install = milvus_sub.add_parser("install", help="Plan an isolated fixed-version local Milvus")
    milvus_install_sub = milvus_install.add_subparsers(dest="milvus_install_action", required=True)
    milvus_install_plan_cmd = milvus_install_sub.add_parser("plan", help="Detect Docker/WSL2 and plan Compose startup")
    milvus_install_plan_cmd.add_argument("--workspace-root", required=True)
    milvus_install_plan_cmd.add_argument("--compose-template", default=str(DEFAULT_MILVUS_COMPOSE))
    milvus_install_plan_cmd.add_argument("--plan-out")
    milvus_install_apply_cmd = milvus_install_sub.add_parser("apply", help="Explicitly start the isolated Compose project")
    milvus_install_apply_cmd.add_argument("--plan-file", required=True)
    milvus_install_apply_cmd.add_argument("--confirm-token", required=True)

    milvus_bootstrap = milvus_sub.add_parser("bootstrap", help="Create an isolated test collection/alias and reconcile")
    milvus_bootstrap_sub = milvus_bootstrap.add_subparsers(dest="milvus_bootstrap_action", required=True)
    milvus_bootstrap_plan_cmd = milvus_bootstrap_sub.add_parser("plan", help="Plan isolated collection/alias initialization")
    milvus_bootstrap_plan_cmd.add_argument("--profile", required=True)
    milvus_bootstrap_plan_cmd.add_argument("--route-file", required=True)
    milvus_bootstrap_plan_cmd.add_argument("--backend", choices=("fake", "real"), default="fake")
    milvus_bootstrap_plan_cmd.add_argument("--plan-out")
    milvus_bootstrap_apply_cmd = milvus_bootstrap_sub.add_parser("apply", help="Apply isolated bootstrap and reconcile")
    milvus_bootstrap_apply_cmd.add_argument("--profile", required=True)
    milvus_bootstrap_apply_cmd.add_argument("--plan-file", required=True)
    milvus_bootstrap_apply_cmd.add_argument("--confirm-token", required=True)
    milvus_bootstrap_apply_cmd.add_argument("--backend", choices=("fake", "real"), default="fake")

    restore = sub.add_parser("restore", help="Restore SQLite from a verified backup")
    restore_sub = restore.add_subparsers(dest="restore_action", required=True)
    restore_plan_cmd = restore_sub.add_parser("plan", help="Verify a backup and plan restore")
    restore_plan_cmd.add_argument("--profile", required=True)
    restore_plan_cmd.add_argument("--backup", required=True)
    restore_plan_cmd.add_argument("--plan-out")
    restore_apply_cmd = restore_sub.add_parser("apply", help="Apply restore with pre-restore backup and rollback")
    restore_apply_cmd.add_argument("--profile", required=True)
    restore_apply_cmd.add_argument("--plan-file", required=True)
    restore_apply_cmd.add_argument("--confirm-token", required=True)
    restore_apply_cmd.add_argument("--backup-dir")

    upgrade = sub.add_parser("upgrade", help="Apply forward-only SQLite schema migrations")
    upgrade_sub = upgrade.add_subparsers(dest="upgrade_action", required=True)
    upgrade_plan_cmd = upgrade_sub.add_parser("plan", help="Inspect schema and plan forward migration")
    upgrade_plan_cmd.add_argument("--profile", required=True)
    upgrade_plan_cmd.add_argument("--plan-out")
    upgrade_apply_cmd = upgrade_sub.add_parser("apply", help="Apply upgrade with pre-upgrade backup and rollback")
    upgrade_apply_cmd.add_argument("--profile", required=True)
    upgrade_apply_cmd.add_argument("--plan-file", required=True)
    upgrade_apply_cmd.add_argument("--confirm-token", required=True)
    upgrade_apply_cmd.add_argument("--backup-dir")

    handoff = sub.add_parser("handoff", help="Read-only conversation handoff planning")
    handoff_sub = handoff.add_subparsers(dest="handoff_action", required=True)
    handoff_plan_cmd = handoff_sub.add_parser("plan", help="Validate handoff artifacts and produce a read-only ingest plan")
    handoff_plan_cmd.add_argument("--profile")
    handoff_plan_cmd.add_argument("--preset")
    handoff_plan_cmd.add_argument("--handoff-dir")
    handoff_plan_cmd.add_argument("--written-memory-map", help="Optional explicit JSON map for diagnostics or legacy overrides")
    handoff_plan_cmd.add_argument("--source-id")
    handoff_plan_cmd.add_argument("--project-id")
    handoff_plan_cmd.add_argument("--conversation-id")
    handoff_apply_cmd = handoff_sub.add_parser("apply", help="Apply a handoff after explicit plan confirmation")
    handoff_apply_cmd.add_argument("--profile")
    handoff_apply_cmd.add_argument("--preset")
    handoff_apply_cmd.add_argument("--handoff-dir")
    handoff_apply_cmd.add_argument("--written-memory-map", help="Optional explicit JSON map for diagnostics or legacy overrides")
    handoff_apply_cmd.add_argument("--source-id")
    handoff_apply_cmd.add_argument("--project-id")
    handoff_apply_cmd.add_argument("--conversation-id")
    handoff_apply_cmd.add_argument("--principal-id")
    handoff_apply_cmd.add_argument("--ttl-seconds", type=int, default=300)
    handoff_apply_cmd.add_argument("--confirm")

    search_cmd = sub.add_parser("search", help="Search scoped memory and return ranked context only")
    search_cmd.add_argument("--profile")
    search_cmd.add_argument("--preset")
    search_cmd.add_argument("--query", required=True)
    search_cmd.add_argument("--project-id")
    search_cmd.add_argument("--top-k", type=int, default=10)
    search_cmd.add_argument("--max-sensitivity", choices=("public", "internal", "private", "secret"))
    search_cmd.add_argument("--max-content-chars", type=int, default=2000)

    context_cmd = sub.add_parser("context", help="Build scoped context for a caller-owned LLM")
    context_cmd.add_argument("--profile")
    context_cmd.add_argument("--preset")
    context_cmd.add_argument("--query", required=True)
    context_cmd.add_argument("--project-id")
    context_cmd.add_argument("--top-k", type=int, default=10)
    context_cmd.add_argument("--max-sensitivity", choices=("public", "internal", "private", "secret"))
    context_cmd.add_argument("--max-content-chars", type=int, default=2000)

    context_pack_cmd = sub.add_parser("context-pack", help="Build a bounded Context Pack for a caller-owned LLM")
    context_pack_cmd.add_argument("--profile")
    context_pack_cmd.add_argument("--preset")
    context_pack_cmd.add_argument("--query", required=True)
    context_pack_cmd.add_argument("--project-id")
    context_pack_cmd.add_argument("--token-budget", type=int, default=3500)
    context_pack_cmd.add_argument("--deep", action="store_true", help="Use the 8,000-token peak budget")
    context_pack_cmd.add_argument("--max-sensitivity", choices=("public", "internal", "private", "secret"))

    context_auto_cmd = sub.add_parser("context-auto", help="Route a natural-language query to the configured memory projects")
    context_auto_cmd.add_argument("--profile")
    context_auto_cmd.add_argument("--preset")
    context_auto_cmd.add_argument("--route-file")
    context_auto_cmd.add_argument("--query", required=True)
    context_auto_cmd.add_argument("--token-budget", type=int, default=3500)
    context_auto_cmd.add_argument("--top-k", type=int, default=10)
    context_auto_cmd.add_argument("--max-sensitivity", choices=("public", "internal", "private", "secret"))

    get_memory_cmd = sub.add_parser("get-memory", help="Read one scoped canonical memory")
    get_memory_cmd.add_argument("--profile")
    get_memory_cmd.add_argument("--preset")
    get_memory_cmd.add_argument("--memory-id", required=True)
    get_memory_cmd.add_argument("--project-id")
    get_memory_cmd.add_argument("--max-sensitivity", choices=("public", "internal", "private", "secret"))
    get_memory_cmd.add_argument("--max-content-chars", type=int, default=4000)
    get_memory_cmd.add_argument("--include-evidence-quote", action="store_true")

    health_cmd = sub.add_parser("health", help="Run scoped read-only database health checks")
    health_cmd.add_argument("--profile")
    health_cmd.add_argument("--preset")
    health_cmd.add_argument("--project-id")

    mcp_cmd = sub.add_parser("mcp", help="Run the read-only MCP stdio server")
    mcp_cmd.add_argument("--profile")
    mcp_cmd.add_argument("--preset")
    mcp_cmd.add_argument("--project-id")
    mcp_cmd.add_argument("--route-file")

    preset_parser = sub.add_parser("preset", help="Personal preset operations")
    preset_sub = preset_parser.add_subparsers(dest="preset_action", required=True)
    preset_init = preset_sub.add_parser("init", help="Generate a secret-free personal preset")
    preset_init.add_argument("--profile", required=True)
    preset_init.add_argument("--output")
    preset_init.add_argument("--route-file")
    preset_init.add_argument("--force", action="store_true")

    maintenance_parser = sub.add_parser("maintenance", help="Local audit-retention maintenance")
    maintenance_sub = maintenance_parser.add_subparsers(dest="maintenance_action", required=True)
    grants_cleanup = maintenance_sub.add_parser("grants", help="Remove old expired or revoked grants")
    grants_cleanup.add_argument("--profile")
    grants_cleanup.add_argument("--preset")
    grants_cleanup.add_argument("--retention-days", type=int, default=7)

    start_cmd = sub.add_parser("start", help="SQLite V1 has no managed daemon")
    start_cmd.add_argument("--profile")
    stop_cmd = sub.add_parser("stop", help="SQLite V1 has no managed daemon")
    stop_cmd.add_argument("--profile")

    smoke_cmd = sub.add_parser("smoke", help="Write/read/idempotency/cleanup smoke")
    smoke_cmd.add_argument("--profile", required=True)

    status_cmd = sub.add_parser("status", help="Show installation and capability status")
    status_cmd.add_argument("--profile", required=True)

    profile = sub.add_parser("profile", help="Connection profile operations")
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    profile_export = profile_sub.add_parser("export", help="Export portable connection profile")
    profile_export.add_argument("--profile", required=True)
    profile_export.add_argument("--output", required=True)

    backup = sub.add_parser("backup", help="SQLite canonical backup")
    backup_sub = backup.add_subparsers(dest="backup_action", required=True)
    backup_create = backup_sub.add_parser("create", help="Create SQLite backup")
    backup_create.add_argument("--profile", required=True)
    backup_create.add_argument("--backup-dir")

    uninstall = sub.add_parser("uninstall", help="Remove runtime state; preserve data and backups")
    uninstall.add_argument("--profile", required=True)

    for name in sorted(HIDDEN_COMMANDS):
        hidden = sub.add_parser(name, help=argparse.SUPPRESS)
        hidden.add_argument("legacy_args", nargs=argparse.REMAINDER)

    def visible_subactions():
        return [action for action in sub._choices_actions if action.help != argparse.SUPPRESS]

    sub._get_subactions = visible_subactions
    return parser


def _not_applicable(command: str) -> dict[str, Any]:
    return {
        "command": command,
        "status": "not_applicable",
        "error": {
            "code": "not_applicable",
            "capability": "runtime",
            "message": "SQLite V1 不使用自有 daemon；start/stop 不适用，未执行任何进程操作",
        },
    }


def _blocked_capability(command: str, capability: str) -> dict[str, Any]:
    return {
        "command": command,
        "status": "blocked",
        "error": {
            "code": "blocked_capability",
            "capability": capability,
            "message": "当前 profile 未启用该能力",
        },
    }


def _import_blocked_payload(exc: Exception, workspace_root: str | Path = DEFAULT_WORKSPACE) -> dict[str, Any]:
    plan = _runtime_plan(workspace_root)
    return {
        "status": "blocked",
        "error": {
            "code": "runtime_dependency_missing",
            "message": "Python 依赖或 memory_core 包尚未就绪；请先审阅 repair_plan。",
            "detail": type(exc).__name__,
        },
        "repair_plan": plan,
    }


def _explicit_handoff_manifest(raw_value: str) -> tuple[Path, dict[str, Any]]:
    raw = Path(raw_value).expanduser()
    if any(part == ".." for part in raw.parts):
        raise SkillApiBlocked("handoff directory must not contain parent traversal")
    if raw.is_symlink():
        raise SkillApiBlocked("handoff directory must not be a symlink")
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked("handoff directory is missing") from exc
    if not root.is_dir():
        raise SkillApiBlocked("handoff path must be an existing directory")
    manifest_path = root / "handoff_manifest.json"
    if manifest_path.is_symlink():
        raise SkillApiBlocked("handoff_manifest.json must not be a symlink")
    try:
        resolved_manifest = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked("handoff_manifest.json is missing") from exc
    if not resolved_manifest.is_relative_to(root) or not resolved_manifest.is_file():
        raise SkillApiBlocked("handoff_manifest.json escapes its directory")
    try:
        manifest = json.loads(resolved_manifest.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SkillApiBlocked("handoff_manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise SkillApiBlocked("handoff_manifest.json must be a JSON object")
    handoff_id = manifest.get("handoff_id")
    conversation_ids = manifest.get("conversation_ids")
    if not isinstance(handoff_id, str) or not handoff_id.strip():
        raise SkillApiBlocked("handoff_manifest.json handoff_id must be a non-empty string")
    if not isinstance(conversation_ids, list) or not all(isinstance(value, str) for value in conversation_ids):
        raise SkillApiBlocked("handoff_manifest.json conversation_ids must be an array of strings")
    return root, manifest


def _explicit_written_memory_map(raw_value: str | None) -> tuple[dict[str, Any] | None, str]:
    if raw_value is None:
        return None, "none"
    text = raw_value
    if raw_value.lstrip().startswith("{"):
        source_kind = "inline"
    else:
        candidate = Path(raw_value).expanduser()
        try:
            candidate_exists = candidate.exists()
        except OSError as exc:
            raise SkillApiBlocked("written-memory-map path is invalid") from exc
        if candidate_exists:
            if candidate.is_symlink():
                raise SkillApiBlocked("written-memory-map file must not be a symlink")
            if not candidate.is_file():
                raise SkillApiBlocked("written-memory-map must be an explicit JSON file or inline JSON")
            source_kind = "file"
            try:
                text = candidate.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                raise SkillApiBlocked("written-memory-map file must be UTF-8 JSON") from exc
        else:
            source_kind = "inline"
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SkillApiBlocked("written-memory-map must be valid JSON") from exc
    if not isinstance(value, dict):
        raise SkillApiBlocked("written-memory-map must be a JSON object")
    return value, source_kind


def _handoff_plan_context(args: argparse.Namespace) -> dict[str, Any]:
    try:
        profile = load_connection_profile(args.profile)
    except SkillApiBlocked:
        raise
    except Exception as exc:
        raise SkillApiBlocked("connection profile is invalid") from exc

    projects = tuple(profile.project_ids)
    if args.project_id:
        project_id = args.project_id
        if project_id not in projects:
            raise SkillApiBlocked("requested project_id is not in the connection profile")
    elif len(projects) == 1:
        project_id = projects[0]
    else:
        raise SkillApiBlocked("connection profile has multiple projects; --project-id is required")
    if not project_id.strip():
        raise SkillApiBlocked("project_id must be non-empty")
    if not profile.owner_principal_id or not profile.owner_principal_id.strip():
        raise SkillApiBlocked("connection profile owner_principal_id is required")
    if not args.source_id.strip():
        raise SkillApiBlocked("source_id must be non-empty")

    handoff_dir, manifest = _explicit_handoff_manifest(args.handoff_dir)
    conversation_id = args.conversation_id
    if conversation_id and conversation_id not in manifest["conversation_ids"]:
        raise SkillApiBlocked("requested conversation_id is not present in handoff_manifest.json")
    explicit_written_memory_map, explicit_map_source = _explicit_written_memory_map(args.written_memory_map)
    canonical_db = _profile_canonical_db(profile)
    written_memory_map, map_source = resolve_written_memory_map_from_canonical(
        canonical_db=canonical_db,
        handoff_dir=handoff_dir,
        workspace_id=profile.workspace_id,
        project_id=project_id,
        domain_profile=profile.domain_profile,
        profile_version=profile.profile_version,
        principal_id=profile.owner_principal_id,
        explicit_written_memory_map=explicit_written_memory_map,
        explicit_source=explicit_map_source,
    )
    plan = plan_handoff_ingest(
        handoff_dir=handoff_dir,
        handoff_id=manifest["handoff_id"],
        workspace_id=profile.workspace_id,
        project_id=project_id,
        domain_profile=profile.domain_profile,
        profile_version=profile.profile_version,
        source_id=args.source_id,
        written_memory_map=written_memory_map,
    )
    if conversation_id and any(write.conversation_id != conversation_id for write in plan.writes):
        raise SkillApiBlocked("--conversation-id does not match every planned write")
    return {
        "profile": profile,
        "canonical_db": canonical_db,
        "handoff_dir": handoff_dir,
        "manifest": manifest,
        "project_id": project_id,
        "conversation_id": conversation_id,
        "written_memory_map": written_memory_map,
        "map_source": map_source,
        "plan": plan,
    }


def _profile_canonical_db(profile: Any) -> Path:
    raw = Path(profile.canonical_db).expanduser()
    workspace_root = Path(profile.workspace_root).expanduser()
    if raw.is_absolute():
        candidate = raw
    else:
        if ".." in raw.parts:
            raise SkillApiBlocked("profile canonical_db must not contain parent traversal")
        candidate = workspace_root / raw
    if candidate.is_symlink():
        raise SkillApiBlocked("profile canonical_db must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SkillApiBlocked("profile canonical_db does not exist") from exc
    if not resolved.is_file():
        raise SkillApiBlocked("profile canonical_db must be an existing SQLite file")
    if not raw.is_absolute():
        try:
            workspace_resolved = workspace_root.resolve(strict=True)
        except OSError as exc:
            raise SkillApiBlocked("profile workspace_root does not exist") from exc
        if not resolved.is_relative_to(workspace_resolved):
            raise SkillApiBlocked("profile canonical_db escapes workspace_root")
    return resolved


def _handoff_plan_payload(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    context = _handoff_plan_context(args)
    profile = context["profile"]
    plan = context["plan"]
    counts = plan.counts.model_dump(mode="json")
    required_token = f"handoff-apply:{plan.plan_id}"
    payload = {
        "command": "handoff plan",
        "status": "planned",
        "handoff_id": plan.handoff_id,
        "plan_id": plan.plan_id,
        "plan_version": plan.contract_version,
        "skill_api_version": SKILL_API_VERSION,
        "scope": {
            "workspace_id": plan.workspace_id,
            "project_id": plan.project_id,
            "domain_profile": plan.domain_profile,
            "profile_version": plan.profile_version,
            "source_id": plan.source_id,
            "owner_principal_id": profile.owner_principal_id,
            "conversation_id": context["conversation_id"],
        },
        "counts": counts,
        "written_memory_map_source": context["map_source"],
        "confirmation_summary": {
            "action": "ingest_conversation_handoff",
            "handoff_id": plan.handoff_id,
            "plan_id": plan.plan_id,
            "counts": counts,
            "contains_deletes": counts["deletes"] > 0,
            "writes_validated_via_skill_api": True,
            "requires_confirmation": True,
            "provider": "local_ephemeral",
        },
        "required_confirmation_token": required_token,
        "apply_available": True,
        "apply_status": "confirmation_required",
        "message": "审阅计划后，使用 required_confirmation_token 作为 handoff apply 的 --confirm 参数执行。",
        "read_only": True,
    }
    return payload, context


def _blocked_handoff_confirmation(context: dict[str, Any], *, required: bool) -> dict[str, Any]:
    plan = context["plan"]
    return {
        "command": "handoff apply",
        "status": "blocked",
        "handoff_id": plan.handoff_id,
        "plan_id": plan.plan_id,
        "confirmation_matched": False,
        "apply_available": True,
        "error": {
            "code": "confirmation_required" if required else "confirmation_mismatch",
            "message": "handoff apply 需要完整匹配当前计划的 --confirm token；计划变化后旧 token 自动失效。",
        },
    }


def _handoff_apply_payload(applied: Any) -> dict[str, Any]:
    result = applied.apply_result
    return {
        "command": "handoff apply",
        "status": applied.status,
        "handoff_id": result.handoff_id,
        "plan_id": result.plan_id,
        "confirmation_matched": True,
        "counts": result.counts.model_dump(mode="json"),
        "write_results": [item.model_dump(mode="json") for item in result.write_results],
        "delete_results": [item.model_dump(mode="json") for item in result.delete_results],
        "already_applied": [item.model_dump(mode="json") for item in result.already_applied],
        "blocked_items": [item.model_dump(mode="json") for item in result.blocked_items],
        "pending_post_write_revocations": [
            item.model_dump(mode="json") for item in result.pending_post_write_revocations
        ],
        "before_counts": result.before_counts,
        "after_counts": result.after_counts,
        "stopped_on_item": result.stopped_on_item.model_dump(mode="json") if result.stopped_on_item else None,
        "failure": result.failure.model_dump(mode="json") if result.failure else None,
        "applied_before_stop": result.applied_before_stop.model_dump(mode="json") if result.applied_before_stop else None,
        "authorization": applied.authorization.model_dump(mode="json"),
        "apply_contract_version": result.contract_version,
        "apply_available": True,
        "read_only": False,
    }


def _execute_handoff_apply(args: argparse.Namespace) -> tuple[dict[str, Any], int, tuple[str, ...]]:
    context = _handoff_plan_context(args)
    profile = context["profile"]
    plan = context["plan"]
    secret_refs = tuple(profile.secret_refs)
    expected_confirmation = f"handoff-apply:{plan.plan_id}"
    if not args.confirm:
        return _blocked_handoff_confirmation(context, required=True), EXIT_BLOCKED, secret_refs
    if not hmac.compare_digest(args.confirm.encode("utf-8"), expected_confirmation.encode("utf-8")):
        return _blocked_handoff_confirmation(context, required=False), EXIT_BLOCKED, secret_refs
    if args.ttl_seconds < 1 or args.ttl_seconds > 3600:
        raise SkillApiBlocked("ttl_seconds must be between 1 and 3600")

    profile_owner = profile.owner_principal_id
    if args.principal_id is not None and args.principal_id != profile_owner:
        raise SkillApiBlocked("principal_id is outside the connection profile owner scope")
    principal_id = args.principal_id if args.principal_id is not None else profile_owner
    if not principal_id or not principal_id.strip():
        raise SkillApiBlocked("principal_id must be non-empty")

    canonical_db = context["canonical_db"]
    applied = apply_handoff_ingest_local(
        canonical_db=canonical_db,
        handoff_dir=context["handoff_dir"],
        handoff_id=context["manifest"]["handoff_id"],
        workspace_id=profile.workspace_id,
        project_id=context["project_id"],
        domain_profile=profile.domain_profile,
        profile_version=profile.profile_version,
        source_id=args.source_id,
        written_memory_map=context["written_memory_map"],
        principal_id=principal_id,
        ttl_seconds=args.ttl_seconds,
    )
    payload = _handoff_apply_payload(applied)
    return payload, EXIT_OK if applied.status == "completed" else EXIT_VERIFY, secret_refs

def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    parser = _parser()
    args, extras = parser.parse_known_args(raw)
    if extras and args.command not in HIDDEN_COMMANDS:
        parser.error("unrecognized arguments: " + " ".join(extras))

    try:
        _apply_preset_defaults(args)
        personal_commands = {"search", "context", "context-pack", "context-auto", "get-memory", "health", "mcp"}
        if args.command in personal_commands:
            _require_personal_args(args, ("profile",))
        elif args.command == "handoff" and args.handoff_action in {"plan", "apply"}:
            _require_personal_args(args, ("profile", "handoff_dir", "source_id"))
        elif args.command == "milvus" and args.milvus_action == "sync":
            _require_personal_args(args, ("profile",))
        elif args.command == "maintenance" and args.maintenance_action == "grants":
            _require_personal_args(args, ("profile",))
    except ValueError as exc:
        _emit(
            {"command": args.command, "status": "blocked", "error": {"code": "preset_blocked", "message": str(exc)}},
            json_mode=json_mode,
        )
        return EXIT_BLOCKED

    if CORE_IMPORT_ERROR is not None:
        requested_root = getattr(args, "workspace_root", DEFAULT_WORKSPACE)
        _emit({"command": args.command, **_import_blocked_payload(CORE_IMPORT_ERROR, requested_root)}, json_mode=json_mode)
        return EXIT_BLOCKED

    try:
        if args.command == "preset" and args.preset_action == "init":
            _emit(_preset_init_payload(args), json_mode=json_mode)
            return EXIT_OK

        if args.command == "maintenance" and args.maintenance_action == "grants":
            result = cleanup_local_grants(args.profile, retention_days=args.retention_days)
            _emit(
                {"command": "maintenance grants", "result": result.model_dump(mode="json")},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "doctor":
            repair_plan = _runtime_plan(args.workspace_root)
            result = doctor(args.workspace_root, secret_refs=tuple(args.secret_ref), repair_plan=repair_plan)
            _emit({"command": "doctor", "result": result.model_dump(mode="json")}, json_mode=json_mode, secret_refs=tuple(args.secret_ref))
            return EXIT_OK if result.status == "ok" else EXIT_BLOCKED

        if args.command == "install" and args.install_action == "plan":
            result = install_plan(
                workspace_root=args.workspace_root,
                workspace_id=args.workspace_id,
                project_id=args.project_id,
                domain_profile=args.domain_profile,
                owner_principal_id=args.owner_principal_id,
            )
            if args.plan_out:
                _write_plan(args.plan_out, result)
            _emit({"command": "install plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
        if args.command == "install" and args.install_action == "apply":
            plan = InstallPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
            result = install_apply(plan, confirmation_token=args.confirm_token)
            _emit({"command": "install apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK

        if args.command == "connect" and args.connect_action == "plan":
            result = connect_plan(
                workspace_root=args.workspace_root,
                canonical_db=args.canonical_db,
                workspace_id=args.workspace_id,
                project_id=args.project_id,
                domain_profile=args.domain_profile,
                owner_principal_id=args.owner_principal_id,
                environment=args.environment,
            )
            if args.plan_out:
                _write_plan(args.plan_out, result)
            _emit({"command": "connect plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
        if args.command == "connect" and args.connect_action == "apply":
            plan = ConnectPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
            result = connect_apply(plan, confirmation_token=args.confirm_token)
            _emit({"command": "connect apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK

        if args.command == "self-test":
            result = self_test(args.manifest)
            _emit({"command": "self-test", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "passed" else EXIT_VERIFY

        if args.command == "sample" and args.sample_action == "plan":
            result = sample_plan(workspace_root=args.workspace_root, manifest_path=args.manifest)
            if args.plan_out:
                _write_plan(args.plan_out, result)
            payload = {"command": "sample plan", "sample_only": True, "result": result.model_dump(mode="json")}
            _emit(payload, json_mode=json_mode)
            return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
        if args.command == "sample" and args.sample_action == "apply":
            plan = DemoPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
            result = sample_apply(plan, confirmation_token=args.confirm_token)
            payload = {"command": "sample apply", "sample_only": True, "result": result.model_dump(mode="json")}
            _emit(payload, json_mode=json_mode)
            return EXIT_OK

        if args.command == "milvus":
            if args.milvus_action == "status":
                result = milvus_status(
                    args.profile,
                    route_path=args.route_file,
                    milvus_profile_path=args.milvus_profile,
                    fake=args.fake,
                    milvus_uri_env=args.uri_env,
                    embedding_key_env=args.embedding_key_env,
                )
                _emit({"command": "milvus status", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_BLOCKED if result.status == "blocked" else EXIT_OK

            if args.milvus_action == "connect" and args.milvus_connect_action == "plan":
                result = milvus_connect_plan(
                    args.profile,
                    route_path=args.route_file,
                    milvus_profile_path=args.milvus_profile,
                    fake=args.fake,
                )
                if args.plan_out:
                    _write_plan(args.plan_out, result)
                _emit({"command": "milvus connect plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
            if args.milvus_action == "connect" and args.milvus_connect_action == "apply":
                plan = ProfileAttachPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
                result = milvus_connect_apply(plan, confirmation_token=args.confirm_token)
                _emit({"command": "milvus connect apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_OK

            if args.milvus_action == "sync":
                milvus_profile = _resolve_milvus_sync_profile(args.profile, args.milvus_profile)
                result = sync_milvus_outbox(
                    milvus_profile,
                    fake=args.fake,
                    owner=args.owner or "database-skill-sync",
                    limit=args.limit,
                    lease_seconds=args.lease_seconds,
                    uri_env=args.uri_env,
                    embedding_key_env=args.embedding_key_env,
                    embedding_base_url_env=args.embedding_base_url_env,
                    embedding_model_env=args.embedding_model_env,
                    embedding_dim_env=args.embedding_dim_env,
                )
                _emit(
                    {"command": "milvus sync", "result": result.model_dump(mode="json")},
                    json_mode=json_mode,
                    secret_refs=_profile_secret_refs(args.profile),
                )
                return EXIT_OK if result.status in {"completed", "nothing_to_do"} else EXIT_VERIFY

            if args.milvus_action == "install" and args.milvus_install_action == "plan":
                result = local_milvus_install_plan(
                    workspace_root=args.workspace_root,
                    compose_template=args.compose_template,
                )
                if args.plan_out:
                    _write_plan(args.plan_out, result)
                _emit({"command": "milvus install plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
            if args.milvus_action == "install" and args.milvus_install_action == "apply":
                plan = LocalMilvusInstallPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
                result = local_milvus_install_apply(plan, confirmation_token=args.confirm_token)
                _emit({"command": "milvus install apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_OK

            if args.milvus_action == "bootstrap" and args.milvus_bootstrap_action == "plan":
                result = milvus_bootstrap_plan(args.profile, route_path=args.route_file, backend=args.backend)
                if args.plan_out:
                    _write_plan(args.plan_out, result)
                _emit({"command": "milvus bootstrap plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
                return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
            if args.milvus_action == "bootstrap" and args.milvus_bootstrap_action == "apply":
                plan = MilvusBootstrapPlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
                result = milvus_bootstrap_apply(
                    args.profile,
                    plan,
                    confirmation_token=args.confirm_token,
                    backend=args.backend,
                )
                payload = {"command": "milvus bootstrap apply", "result": result.model_dump(mode="json")}
                payload["reconcile"] = milvus_reconcile_status(result)
                _emit(payload, json_mode=json_mode)
                return EXIT_OK

        if args.command == "handoff" and args.handoff_action == "plan":
            payload, context = _handoff_plan_payload(args)
            _emit(payload, json_mode=json_mode, secret_refs=tuple(context["profile"].secret_refs))
            return EXIT_OK
        if args.command == "handoff" and args.handoff_action == "apply":
            payload, exit_code, secret_refs = _execute_handoff_apply(args)
            _emit(payload, json_mode=json_mode, secret_refs=secret_refs)
            return exit_code

        if args.command == "context-pack":
            result = build_context_pack(
                args.profile,
                query=args.query,
                project_id=args.project_id,
                token_budget=8000 if args.deep else args.token_budget,
                max_sensitivity=args.max_sensitivity,
            )
            _emit(
                {"command": "context-pack", "result": result.model_dump(mode="json")},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "search":
            result = search_memory(
                args.profile,
                query=args.query,
                project_id=args.project_id,
                top_k=args.top_k,
                max_sensitivity=args.max_sensitivity,
                max_content_chars=args.max_content_chars,
            )
            _emit(
                {"command": "search", "result": result.model_dump(mode="json")},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "context":
            result = query_context(
                args.profile,
                query=args.query,
                project_id=args.project_id,
                top_k=args.top_k,
                max_sensitivity=args.max_sensitivity,
                max_content_chars=args.max_content_chars,
            )
            _emit(
                {"command": "context", "result": result.model_dump(mode="json")},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "context-auto":
            from context_router import build_auto_context

            result = build_auto_context(
                args.profile,
                query=args.query,
                route_file=args.route_file,
                token_budget=args.token_budget,
                top_k=args.top_k,
                max_sensitivity=args.max_sensitivity,
            )
            _emit(
                {"command": "context-auto", "result": result},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "get-memory":
            result = get_memory(
                args.profile,
                memory_id=args.memory_id,
                project_id=args.project_id,
                max_sensitivity=args.max_sensitivity,
                max_content_chars=args.max_content_chars,
                include_evidence_quote=args.include_evidence_quote,
            )
            _emit(
                {"command": "get-memory", "result": result.model_dump(mode="json", exclude_none=True)},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            return EXIT_OK

        if args.command == "health":
            result = health_check(args.profile, project_id=args.project_id)
            _emit(
                {"command": "health", "result": result.model_dump(mode="json")},
                json_mode=json_mode,
                secret_refs=_profile_secret_refs(args.profile),
            )
            if result.status in {"ok", "warning"}:
                return EXIT_OK
            return EXIT_BLOCKED if result.status == "blocked" else EXIT_VERIFY

        if args.command == "mcp":
            from mcp_server import run_stdio

            return run_stdio(args.profile, project_id=args.project_id, route_file=args.route_file)

        if args.command == "start" or args.command == "stop":
            _emit(_not_applicable(args.command), json_mode=json_mode)
            return EXIT_BLOCKED

        if args.command == "restore" and args.restore_action == "plan":
            result = restore_plan(args.profile, backup_path=args.backup)
            if args.plan_out:
                _write_plan(args.plan_out, result)
            _emit({"command": "restore plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
        if args.command == "restore" and args.restore_action == "apply":
            plan = RestorePlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
            result = restore_apply(
                args.profile,
                plan,
                confirmation_token=args.confirm_token,
                backup_dir=args.backup_dir,
            )
            _emit({"command": "restore apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK

        if args.command == "upgrade" and args.upgrade_action == "plan":
            result = upgrade_plan(args.profile)
            if args.plan_out:
                _write_plan(args.plan_out, result)
            _emit({"command": "upgrade plan", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "planned" else EXIT_BLOCKED
        if args.command == "upgrade" and args.upgrade_action == "apply":
            plan = UpgradePlan.model_validate_json(Path(args.plan_file).resolve().read_text(encoding="utf-8"))
            result = upgrade_apply(
                args.profile,
                plan,
                confirmation_token=args.confirm_token,
                backup_dir=args.backup_dir,
            )
            _emit({"command": "upgrade apply", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK

        if args.command == "smoke":
            result = smoke(args.profile)
            _emit({"command": "smoke", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status == "ok" else EXIT_BLOCKED
        if args.command == "status":
            result = status(args.profile)
            _emit({"command": "status", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK if result.status in {"ok", "warning"} else EXIT_BLOCKED
        if args.command == "profile" and args.profile_action == "export":
            result = export_profile(args.profile, args.output)
            _emit({"command": "profile export", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK
        if args.command == "backup" and args.backup_action == "create":
            result = installer_backup(args.profile, args.backup_dir)
            _emit({"command": "backup create", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK
        if args.command == "uninstall":
            result = uninstall_runtime(args.profile)
            _emit({"command": "uninstall", "result": result.model_dump(mode="json")}, json_mode=json_mode)
            return EXIT_OK

        if args.command == "demo":
            result = self_test(DEFAULT_MANIFEST)
            payload = {
                "command": "demo",
                "status": "compatibility_warning",
                "warning": "demo 仅为隐藏兼容命令，不保留 Demo 数据；请使用 self-test 或 sample。",
                "result": result.model_dump(mode="json"),
            }
            _emit(payload, json_mode=json_mode)
            return EXIT_OK if result.status == "passed" else EXIT_VERIFY

        if args.command in HIDDEN_COMMANDS:
            capability = "vector" if args.command == "milvus" else "runtime"
            _emit(_blocked_capability(args.command, capability), json_mode=json_mode)
            return EXIT_BLOCKED
        parser.error("unsupported command")
    except SkillApiBlocked as exc:
        _emit({"command": args.command, "status": "blocked", "error": {"code": "blocked", "message": str(exc)}}, json_mode=json_mode)
        return EXIT_BLOCKED
    except Exception as exc:
        _emit({"command": args.command, "status": "error", "error": {"code": "runtime_error", "message": type(exc).__name__}}, json_mode=json_mode)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
