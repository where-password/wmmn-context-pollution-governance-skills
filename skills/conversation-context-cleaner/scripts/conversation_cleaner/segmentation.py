from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from .contracts import Message, TopicSegment
from .util import sha256_json


_TOPIC_MARKER = re.compile(r"^\s*(?:\[\[topic:\s*(?P<key1>[^\]]+)\]\]|#topic:\s*(?P<key2>.+?)\s*$)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
_TRIVIAL_ACKS = {
    "继续", "请继续", "继续执行", "好的", "好", "收到", "明白", "了解", "确认",
    "同意", "可以", "嗯", "行", "是", "对", "ok", "okay", "yes", "开始吧", "执行",
}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _topic_marker(text: str) -> str | None:
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    match = _TOPIC_MARKER.match(first_line)
    if not match:
        return None
    key = (match.group("key1") or match.group("key2") or "").strip()
    return key or None


def _explicit_topic_key(message: Message) -> str | None:
    value: Any = message.metadata.get("topic_key")
    if isinstance(value, str) and value.strip():
        return value.strip()[:120]
    return _topic_marker(message.text)


def _is_explicit_boundary(message: Message) -> bool:
    value = message.metadata.get("topic_boundary")
    return value is True or (isinstance(value, str) and value.casefold() in {"true", "yes", "1"})


def _summary(messages: list[Message]) -> str:
    first_user = next((item for item in messages if item.role == "user"), messages[0])
    first_line = first_user.text.splitlines()[0] if first_user.text.splitlines() else first_user.text
    text = _TOPIC_MARKER.sub("", first_line).strip()
    text = _WHITESPACE.sub(" ", text)
    return text[:120] or f"{len(messages)} messages"


def _normalized_text(text: str) -> str:
    normalized = _WHITESPACE.sub("", _TOPIC_MARKER.sub("", text)).casefold()
    return re.sub(r"[，,。.!！?？；;\s]", "", normalized)


def _is_short_clarification(text: str) -> bool:
    compact = _normalized_text(text)
    if len(compact) > 240:
        return False
    return "?" in text or "？" in text or any(token in text for token in ("是指", "还是", "是否", "需要确认"))


def _segment_is_trivial(segment: TopicSegment, messages_by_id: dict[str, Message]) -> bool:
    user_messages: list[Message] = []
    assistant_messages: list[Message] = []
    for message_id in segment.message_ids:
        message = messages_by_id.get(message_id)
        if message is None:
            return False
        if message.role == "user":
            user_messages.append(message)
        else:
            assistant_messages.append(message)
    if not user_messages:
        return False
    if not all(_normalized_text(message.text) in _TRIVIAL_ACKS for message in user_messages):
        return False
    for message in assistant_messages:
        compact = _normalized_text(message.text)
        if compact in _TRIVIAL_ACKS or _is_short_clarification(message.text):
            continue
        return False
    return True


def _rebuild_segment(segment: TopicSegment, messages: list[Message], *, summary: str) -> TopicSegment:
    source_ids = tuple(item.source_unit_id for item in messages)
    segment_id = "seg_" + sha256_json(
        {
            "conversation_id": segment.conversation_id,
            "topic_key": segment.topic_key,
            "source_unit_ids": list(source_ids),
        }
    )[:24]
    return replace(
        segment,
        topic_segment_id=segment_id,
        message_ids=tuple(item.message_id for item in messages),
        source_unit_ids=source_ids,
        start_source_unit_id=source_ids[0],
        end_source_unit_id=source_ids[-1],
        summary=summary,
    )


def _merge_trivial_segments(segments: list[TopicSegment], messages: list[Message]) -> list[TopicSegment]:
    messages_by_id = {item.message_id: item for item in messages}
    result = sorted(
        segments,
        key=lambda item: (
            item.conversation_id,
            messages_by_id[item.message_ids[0]].sequence,
            item.message_ids[0],
        ),
    )
    index = 0
    while index < len(result):
        segment = result[index]
        if not _segment_is_trivial(segment, messages_by_id):
            index += 1
            continue
        trivial_messages = [messages_by_id[mid] for mid in segment.message_ids]
        audit = {
            "merged_segment_id": segment.topic_segment_id,
            "reason": "only_acknowledgement_messages",
            "original_boundary_reasons": list(segment.boundary_reasons),
            "message_ids": list(segment.message_ids),
        }
        if index > 0:
            previous = result[index - 1]
            previous_messages = [messages_by_id[mid] for mid in previous.message_ids]
            merged = _rebuild_segment(previous, previous_messages + trivial_messages, summary=previous.summary)
            result[index - 1] = replace(
                merged,
                boundary_reasons=tuple(previous.boundary_reasons) + ("trivial_tail_merged",),
                merged_segment_ids=previous.merged_segment_ids + (segment.topic_segment_id,),
                merge_audit=previous.merge_audit + (audit,),
            )
            del result[index]
        elif index + 1 < len(result):
            following = result[index + 1]
            following_messages = [messages_by_id[mid] for mid in following.message_ids]
            merged = _rebuild_segment(following, trivial_messages + following_messages, summary=following.summary)
            result[index + 1] = replace(
                merged,
                boundary_reasons=("trivial_leading_merged",) + tuple(following.boundary_reasons),
                merged_segment_ids=(segment.topic_segment_id,) + following.merged_segment_ids,
                merge_audit=(audit,) + following.merge_audit,
            )
            del result[index]
        else:
            index += 1
    return result

def segment_messages(messages: list[Message], *, topic_gap_seconds: int = 1800) -> list[TopicSegment]:
    by_conversation: dict[str, list[Message]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    segments: list[TopicSegment] = []
    for conversation_id, conversation_messages in sorted(by_conversation.items()):
        current: list[Message] = []
        current_key = ""
        current_reasons: list[str] = []
        segment_ordinal = 0
        previous: Message | None = None

        def flush() -> None:
            nonlocal current, current_key, current_reasons
            if not current:
                return
            source_ids = tuple(item.source_unit_id for item in current)
            segment_id = "seg_" + sha256_json(
                {
                    "conversation_id": conversation_id,
                    "topic_key": current_key,
                    "source_unit_ids": list(source_ids),
                }
            )[:24]
            segments.append(
                TopicSegment(
                    topic_segment_id=segment_id,
                    conversation_id=conversation_id,
                    topic_key=current_key,
                    message_ids=tuple(item.message_id for item in current),
                    source_unit_ids=source_ids,
                    start_source_unit_id=source_ids[0],
                    end_source_unit_id=source_ids[-1],
                    summary=_summary(current),
                    boundary_reasons=tuple(current_reasons),
                )
            )
            current = []
            current_key = ""
            current_reasons = []

        for message in conversation_messages:
            explicit_key = _explicit_topic_key(message)
            reasons: list[str] = []
            if not current:
                reasons.append("conversation_start")
            else:
                assert previous is not None
                if _is_explicit_boundary(message):
                    reasons.append("explicit_topic_boundary")
                if explicit_key is not None and explicit_key != current_key:
                    reasons.append("topic_key_changed")
                gap = (_parse_utc(message.timestamp_utc) - _parse_utc(previous.timestamp_utc)).total_seconds()
                if gap > topic_gap_seconds:
                    reasons.append("time_gap_exceeded")

            if current and reasons:
                flush()
                segment_ordinal += 1
                reasons = ["topic_boundary_after_start", *reasons]

            if not current:
                current_reasons = reasons or ["conversation_start"]
                current_key = explicit_key or f"{conversation_id}:topic:{segment_ordinal + 1:03d}"
            current.append(message)
            previous = message

        flush()
    message_order = {message.message_id: (message.conversation_id, message.sequence) for message in messages}
    segments.sort(key=lambda item: (item.conversation_id, message_order[item.message_ids[0]][1], item.message_ids[0]))
    return _merge_trivial_segments(segments, messages)