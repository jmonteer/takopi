from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import anyio
from anyio import EndOfStream

from .config import ConfigError
from .hooks import FinalReplyContext, FinalReplyHook
from .render import render_markdown
from .telegram import BotClient
from .utils.subprocess import manage_subprocess, wait_for_process

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "hey boss"
_BULLET_RE = re.compile(r"^(?:[-*]|\d+[.)])\s+(.*)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_CODE_RE = re.compile(r"`[^`]+`")
_URL_RE = re.compile(r"https?://\\S+")
_ABS_PATH_RE = re.compile(r"(?<!\\w)(?:~\\/|/|[A-Za-z]:\\\\)\\S+")
_REL_PATH_RE = re.compile(r"(?<!\\w)(?:[\\w.-]+/)+[\\w.-]+(?::\\d+)?")


@dataclass(frozen=True)
class VoiceReplyConfig:
    command: list[str] | str
    timeout_s: float = 20.0
    max_seconds: int = 30
    wpm: int = 150
    max_bullets: int = 5
    reply_to_final: bool = True
    high_level: bool = True


def _parse_bool(
    raw: dict[str, Any], key: str, default: bool, *, section: str
) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool):
        return value
    raise ConfigError(f"Invalid `{section}.{key}`; expected a boolean.")


def _parse_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    section: str,
) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Invalid `{section}.{key}`; expected an integer.")
    if value < minimum:
        raise ConfigError(
            f"Invalid `{section}.{key}`; expected >= {minimum}."
        )
    return value


def _parse_float(
    raw: dict[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    section: str,
) -> float:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Invalid `{section}.{key}`; expected a number.")
    value = float(value)
    if value < minimum:
        raise ConfigError(
            f"Invalid `{section}.{key}`; expected >= {minimum}."
        )
    return value


def load_voice_reply_config(
    config: dict[str, Any], config_path: Path
) -> VoiceReplyConfig | None:
    section = "voice_reply"
    raw = config.get(section)
    if raw is None:
        raw = config.get("voice_note")
        if raw is not None:
            section = "voice_note"
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Invalid `{section}` config in {config_path}; expected a table."
        )
    if not _parse_bool(raw, "enabled", True, section=section):
        return None

    command = raw.get("command")
    if command is None:
        raise ConfigError(
            f"Missing `{section}.command` in {config_path}; expected a command."
        )
    if isinstance(command, str):
        if not command.strip():
            raise ConfigError(
                f"Invalid `{section}.command` in {config_path}; expected a command."
            )
        command_value: list[str] | str = command
    elif isinstance(command, list) and command:
        cleaned: list[str] = []
        for item in command:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(
                    f"Invalid `{section}.command` in {config_path}; expected strings."
                )
            cleaned.append(item)
        command_value = cleaned
    else:
        raise ConfigError(
            f"Invalid `{section}.command` in {config_path}; expected a command."
        )

    return VoiceReplyConfig(
        command=command_value,
        timeout_s=_parse_float(raw, "timeout_s", 20.0, minimum=1.0, section=section),
        max_seconds=_parse_int(raw, "max_seconds", 30, minimum=5, section=section),
        wpm=_parse_int(raw, "wpm", 150, minimum=60, section=section),
        max_bullets=_parse_int(raw, "max_bullets", 5, minimum=1, section=section),
        reply_to_final=_parse_bool(raw, "reply_to_final", True, section=section),
        high_level=_parse_bool(raw, "high_level", True, section=section),
    )


def _max_words(config: VoiceReplyConfig) -> int:
    words = int(config.max_seconds * config.wpm / 60)
    return max(1, words)


def _extract_candidates(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bullets: list[str] = []
    for line in lines:
        match = _BULLET_RE.match(line)
        if match:
            bullets.append(match.group(1).strip())
    if bullets:
        return bullets
    if not lines:
        return []
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    if not joined:
        return []
    sentences = [s.strip() for s in _SENTENCE_RE.split(joined) if s.strip()]
    return sentences or [joined]


def _sanitize_bullet(text: str) -> str:
    text = _CODE_RE.sub("", text)
    text = _URL_RE.sub("a link", text)
    text = _ABS_PATH_RE.sub("files", text)
    text = _REL_PATH_RE.sub("files", text)
    text = re.sub(r"\\bfiles\\b(?:\\s+\\bfiles\\b)+", "files", text)
    text = re.sub(r"\\s+", " ", text)
    text = text.strip(" ,;:-")
    if text in {"file", "files"}:
        return "updated files"
    return text


def _limit_bullets(
    candidates: list[str], *, max_words: int, max_bullets: int
) -> list[str]:
    bullets: list[str] = []
    total_words = 0
    for candidate in candidates:
        if len(bullets) >= max_bullets:
            break
        words = candidate.split()
        if not words:
            continue
        remaining = max_words - total_words
        if remaining <= 0:
            break
        if len(words) > remaining:
            candidate = " ".join(words[:remaining])
            bullets.append(candidate)
            break
        bullets.append(candidate)
        total_words += len(words)
    return bullets


def build_voice_reply_summary(text: str, config: VoiceReplyConfig) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        rendered, _ = render_markdown(raw)
    except Exception:
        rendered = raw
    candidates = _extract_candidates(rendered)
    if config.high_level:
        candidates = [
            cleaned
            for candidate in candidates
            if (cleaned := _sanitize_bullet(candidate))
        ]
    if not candidates:
        return None
    bullets = _limit_bullets(
        candidates, max_words=_max_words(config), max_bullets=config.max_bullets
    )
    if not bullets:
        return None
    lines = [SUMMARY_PREFIX]
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)


def _build_command(
    command: list[str] | str, *, text_path: Path, out_path: Path
) -> list[str]:
    if isinstance(command, str):
        replaced = (
            command.replace("{text}", str(text_path)).replace("{out}", str(out_path))
        )
        return ["sh", "-lc", replaced]
    return [
        item.replace("{text}", str(text_path)).replace("{out}", str(out_path))
        for item in command
    ]


async def _drain_stream(
    stream: anyio.abc.ByteReceiveStream, buffer: bytearray, max_bytes: int
) -> None:
    while True:
        try:
            chunk = await stream.receive(65536)
        except EndOfStream:
            return
        if not chunk:
            return
        if len(buffer) < max_bytes:
            remaining = max_bytes - len(buffer)
            buffer.extend(chunk[:remaining])


async def _run_voice_command(
    cmd: list[str], *, env: dict[str, str], timeout_s: float
) -> tuple[bool, bytes, bytes]:
    async with manage_subprocess(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as proc:
        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("voice command missing stdout/stderr pipes")
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        async with anyio.create_task_group() as tg:
            tg.start_soon(_drain_stream, proc.stdout, stdout_buf, 4096)
            tg.start_soon(_drain_stream, proc.stderr, stderr_buf, 4096)
            timed_out = await wait_for_process(proc, timeout_s)
        if timed_out:
            logger.warning("[voice_reply] command timed out after %.1fs", timeout_s)
            return False, bytes(stdout_buf), bytes(stderr_buf)
        rc = proc.returncode or 0
    return rc == 0, bytes(stdout_buf), bytes(stderr_buf)


async def maybe_send_voice_reply(
    bot: BotClient,
    *,
    chat_id: int,
    user_message_id: int,
    final_message_id: int | None,
    answer: str,
    config: VoiceReplyConfig | None,
) -> None:
    if config is None:
        return
    summary = build_voice_reply_summary(answer, config)
    if not summary:
        return
    reply_to = (
        final_message_id or user_message_id if config.reply_to_final else user_message_id
    )

    try:
        with TemporaryDirectory(prefix="takopi-voice-") as tmpdir:
            tmp_path = Path(tmpdir)
            text_path = tmp_path / "summary.txt"
            out_path = tmp_path / "voice.ogg"
            text_path.write_text(summary, encoding="utf-8")
            cmd = _build_command(config.command, text_path=text_path, out_path=out_path)
            env = os.environ.copy()
            env["TAKOPI_VOICE_TEXT"] = str(text_path)
            env["TAKOPI_VOICE_OUT"] = str(out_path)
            ok, stdout_buf, stderr_buf = await _run_voice_command(
                cmd, env=env, timeout_s=config.timeout_s
            )
            if not ok:
                logger.warning(
                    "[voice_reply] command failed stdout=%r stderr=%r",
                    stdout_buf.decode("utf-8", errors="replace"),
                    stderr_buf.decode("utf-8", errors="replace"),
                )
                return
            if not out_path.exists():
                logger.warning("[voice_reply] output missing: %s", out_path)
                return
            if out_path.stat().st_size <= 0:
                logger.warning("[voice_reply] output empty: %s", out_path)
                return
            await bot.send_voice(
                chat_id=chat_id,
                voice_path=out_path,
                reply_to_message_id=reply_to,
                disable_notification=False,
            )
    except Exception:
        logger.exception("[voice_reply] failed to send voice note")


def build_voice_reply_hook(config: VoiceReplyConfig) -> FinalReplyHook:
    async def hook(ctx: FinalReplyContext) -> None:
        if not ctx.ok:
            return
        if not ctx.answer.strip():
            return
        await maybe_send_voice_reply(
            ctx.bot,
            chat_id=ctx.chat_id,
            user_message_id=ctx.user_message_id,
            final_message_id=ctx.final_message_id,
            answer=ctx.answer,
            config=config,
        )

    return hook
