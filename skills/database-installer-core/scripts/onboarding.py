from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

MESSAGE = """已读取成功。这个工具包含两个相互独立的 Skill：

- 对话清理：只处理你当前 Agent 中的对话，不处理微信、飞书等普通聊天记录。它会把 Agent 对话整理成更清楚、方便保存和使用的内容。
- 数据库 Skill：建立并维护本地数据库，用来保存需要长期保留的信息，也可以保存整理后的 Agent 对话，从而相对降低后续对话的上下文污染。完整功能需要 Embedding API、Docker 等基础环境，你的 Agent 可以协助检测、安装和配置；遇到系统权限或重启要求时会提示你处理。

两个 Skill 可以单独使用：已有数据库时可以只用对话清理；只整理内容时也可以不用数据库 Skill。建议两个一起使用，长期管理效果更完整。你想做什么，直接告诉我就行。

---

wmmn · 我密码呢
"""
MESSAGE_ID = "wmmn-context-pollution-governance-first-run-v1"
WATERMARK = {"mark": "wmmn", "author": "我密码呢"}


def _resolve_codex_home(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _state_path(codex_home: Path) -> Path:
    return codex_home / "onboarding" / "conversation-memory-skills-v1.json"


def show_once(codex_home: Path) -> dict[str, object]:
    marker = _state_path(codex_home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return {
            "shown": False,
            "already_shown": True,
            "message_id": MESSAGE_ID,
            "watermark": WATERMARK,
            "state_path": str(marker),
            "message": None,
        }

    payload = {
        "version": 1,
        "message_id": MESSAGE_ID,
        "watermark": WATERMARK,
        "shown_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        os.write(descriptor, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(descriptor)

    return {
        "shown": True,
        "already_shown": False,
        "message_id": MESSAGE_ID,
        "watermark": WATERMARK,
        "state_path": str(marker),
        "message": MESSAGE,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Show the WMMN Context Pollution Governance first-run orientation once.")
    parser.add_argument("--codex-home", default=None, help="Codex home used to store the one-time marker.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result.")
    args = parser.parse_args(argv)

    result = show_once(_resolve_codex_home(args.codex_home))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["shown"]:
        print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
