from __future__ import annotations

from dataclasses import dataclass
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

import anyio

from .config import ConfigError
from .telegram import BotClient
from .utils.subprocess import manage_subprocess

DEFAULT_PROMPT_TEMPLATE = "Voice transcription:\n{transcript}"
DEFAULT_MAX_DURATION_SEC = 300
DEFAULT_TRANSCRIBE_TIMEOUT_SEC = 60
DEFAULT_BACKEND = "cmd"
MAX_TELEGRAM_TEXT_LEN = 4096
TRANSCRIPT_HEADER = "Transcript:"
TRANSCRIPT_TRUNCATION_SUFFIX = "..."
FFMPEG_TIMEOUT_SEC = 30

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceInput:
    kind: Literal["voice", "audio"]
    file_id: str
    duration: int | None
    file_size: int | None
    mime_type: str | None
    file_name: str | None


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    enabled: bool
    max_duration_sec: int
    prompt_template: str
    backend: str
    transcribe_cmd: list[str]
    transcribe_timeout_sec: int
    language: str | None


class VoiceError(RuntimeError):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


def extract_voice_input(msg: dict[str, Any]) -> VoiceInput | None:
    voice = msg.get("voice")
    if isinstance(voice, dict):
        return _extract_voice_payload(voice, kind="voice")
    audio = msg.get("audio")
    if isinstance(audio, dict):
        return _extract_voice_payload(audio, kind="audio")
    return None


def truncate_transcript(
    transcript: str,
    *,
    max_len: int,
    suffix: str = TRANSCRIPT_TRUNCATION_SUFFIX,
) -> tuple[str, bool]:
    if max_len <= 0:
        return "", bool(transcript)
    if len(transcript) <= max_len:
        return transcript, False
    if max_len <= len(suffix):
        return transcript[:max_len], True
    return transcript[: max_len - len(suffix)] + suffix, True


def build_transcript_message(
    transcript: str,
    *,
    header: str = TRANSCRIPT_HEADER,
    max_len: int = MAX_TELEGRAM_TEXT_LEN,
) -> tuple[str, list[dict[str, int]], bool]:
    prefix = f"{header}\n"
    remaining = max_len - len(prefix)
    shown, truncated = truncate_transcript(transcript, max_len=remaining)
    message = f"{prefix}{shown}"
    return message, [], truncated


async def resolve_user_prompt(
    msg: dict[str, Any],
    *,
    cfg: VoiceConfig,
    bot: BotClient,
) -> str | None:
    text = msg.get("text")
    if isinstance(text, str):
        return text

    voice_input = extract_voice_input(msg)
    if voice_input is None:
        return None

    chat_id = _get_chat_id(msg)
    user_msg_id = _get_message_id(msg)
    if chat_id is None or user_msg_id is None:
        return None

    if not cfg.enabled:
        await _send_voice_reply(
            bot, chat_id, user_msg_id, "voice notes are disabled"
        )
        return None

    if (
        voice_input.duration is not None
        and voice_input.duration > cfg.max_duration_sec
    ):
        await _send_voice_reply(bot, chat_id, user_msg_id, "voice note too long")
        return None

    transcribing_id = await _send_transcribing(bot, chat_id, user_msg_id)
    try:
        transcript = await transcribe_voice_input(voice_input, cfg=cfg, bot=bot)
    except VoiceError as exc:
        await _send_voice_error(
            bot,
            chat_id,
            user_msg_id,
            transcribing_id,
            exc.user_message,
        )
        return None

    await _edit_transcript_message(
        bot,
        chat_id,
        user_msg_id,
        transcribing_id,
        transcript,
    )

    return cfg.prompt_template.format(transcript=transcript)


async def transcribe_voice_input(
    voice_input: VoiceInput,
    *,
    cfg: VoiceConfig,
    bot: BotClient,
) -> str:
    download_start = time.monotonic()
    file_info = await bot.get_file(voice_input.file_id)
    file_path = None
    if isinstance(file_info, dict):
        raw_path = file_info.get("file_path")
        if isinstance(raw_path, str) and raw_path:
            file_path = raw_path
    if not file_path:
        raise VoiceError("could not download voice note")

    data = await bot.download_file(file_path)
    if not data:
        raise VoiceError("could not download voice note")
    download_elapsed = time.monotonic() - download_start

    suffix = Path(file_path).suffix
    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_dir = Path(tmp_dir)
        input_path = temp_dir / f"input{suffix}"
        output_path = temp_dir / "voice.wav"
        input_path.write_bytes(data)

        ffmpeg_start = time.monotonic()
        await _run_ffmpeg(input_path, output_path)
        ffmpeg_elapsed = time.monotonic() - ffmpeg_start

        transcribe_start = time.monotonic()
        transcript = await _run_transcribe_cmd(output_path, cfg)
        transcribe_elapsed = time.monotonic() - transcribe_start

    logger.debug(
        "[voice] download=%.2fs ffmpeg=%.2fs transcribe=%.2fs",
        download_elapsed,
        ffmpeg_elapsed,
        transcribe_elapsed,
    )

    transcript = _normalize_transcript(transcript)
    if not transcript:
        raise VoiceError("transcription failed")
    return transcript


def transcribe_cmd_needs_lang(cmd: list[str]) -> bool:
    return any("{lang}" in part for part in cmd)


def load_voice_config(config: dict[str, Any], config_path: Path) -> VoiceConfig:
    voice = config.get("voice")
    if voice is None:
        voice = {}
    if not isinstance(voice, dict):
        raise ConfigError(
            f"Invalid `voice` config in {config_path}; expected a table."
        )

    enabled = _optional_bool(
        voice.get("enabled"),
        "voice.enabled",
        config_path,
        default=False,
    )
    max_duration_sec = _optional_int(
        voice.get("max_duration_sec"),
        "voice.max_duration_sec",
        config_path,
        default=DEFAULT_MAX_DURATION_SEC,
    )
    transcribe_timeout_sec = _optional_int(
        voice.get("transcribe_timeout_sec"),
        "voice.transcribe_timeout_sec",
        config_path,
        default=DEFAULT_TRANSCRIBE_TIMEOUT_SEC,
    )

    backend_value = _optional_str(
        voice.get("backend"),
        "voice.backend",
        config_path,
    )
    backend = backend_value or DEFAULT_BACKEND
    if backend != DEFAULT_BACKEND:
        raise ConfigError(
            f"Invalid `voice.backend` in {config_path}; expected {DEFAULT_BACKEND!r}."
        )

    prompt_template = _optional_template(
        voice.get("prompt_template"),
        "voice.prompt_template",
        config_path,
        default=DEFAULT_PROMPT_TEMPLATE,
    )
    if "{transcript}" not in prompt_template:
        raise ConfigError(
            f"Invalid `voice.prompt_template` in {config_path}; "
            "expected '{transcript}' placeholder."
        )

    transcribe_cmd = _optional_str_list(
        voice.get("transcribe_cmd"),
        "voice.transcribe_cmd",
        config_path,
        default=[],
    )

    if enabled and not transcribe_cmd:
        raise ConfigError(
            f"Invalid `voice.transcribe_cmd` in {config_path}; "
            "expected a non-empty list of strings."
        )

    language = _optional_str(
        voice.get("language"),
        "voice.language",
        config_path,
    )
    if transcribe_cmd_needs_lang(transcribe_cmd) and language is None:
        language = "en"

    return VoiceConfig(
        enabled=enabled,
        max_duration_sec=max_duration_sec,
        prompt_template=prompt_template,
        backend=backend,
        transcribe_cmd=transcribe_cmd,
        transcribe_timeout_sec=transcribe_timeout_sec,
        language=language,
    )


def _extract_voice_payload(
    payload: dict[str, Any], *, kind: Literal["voice", "audio"]
) -> VoiceInput | None:
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    return VoiceInput(
        kind=kind,
        file_id=file_id,
        duration=_coerce_int(payload.get("duration")),
        file_size=_coerce_int(payload.get("file_size")),
        mime_type=_coerce_str(payload.get("mime_type")),
        file_name=_coerce_str(payload.get("file_name")),
    )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value or None


def _get_chat_id(msg: dict[str, Any]) -> int | None:
    chat = msg.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if isinstance(chat_id, bool) or not isinstance(chat_id, int):
        return None
    return chat_id


def _get_message_id(msg: dict[str, Any]) -> int | None:
    msg_id = msg.get("message_id")
    if isinstance(msg_id, bool) or not isinstance(msg_id, int):
        return None
    return msg_id


async def _send_transcribing(
    bot: BotClient, chat_id: int, user_msg_id: int
) -> int | None:
    msg = await bot.send_message(
        chat_id=chat_id,
        text="Transcribing...",
        reply_to_message_id=user_msg_id,
        disable_notification=True,
    )
    if msg is None:
        return None
    msg_id = msg.get("message_id")
    return int(msg_id) if isinstance(msg_id, int) else None


async def _send_voice_reply(
    bot: BotClient, chat_id: int, user_msg_id: int, text: str
) -> None:
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_to_message_id=user_msg_id,
    )


async def _send_voice_error(
    bot: BotClient,
    chat_id: int,
    user_msg_id: int,
    transcribing_id: int | None,
    text: str,
) -> None:
    if transcribing_id is not None:
        edited = await bot.edit_message_text(
            chat_id=chat_id,
            message_id=transcribing_id,
            text=text,
        )
        if edited is not None:
            return
    await _send_voice_reply(bot, chat_id, user_msg_id, text)


async def _edit_transcript_message(
    bot: BotClient,
    chat_id: int,
    user_msg_id: int,
    transcribing_id: int | None,
    transcript: str,
) -> None:
    text, entities, _ = build_transcript_message(transcript)
    if transcribing_id is not None:
        edited = await bot.edit_message_text(
            chat_id=chat_id,
            message_id=transcribing_id,
            text=text,
            entities=entities,
        )
        if edited is not None:
            return
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        entities=entities,
        reply_to_message_id=user_msg_id,
        disable_notification=True,
    )


def _normalize_transcript(text: str) -> str:
    return " ".join(text.split()).strip()


def _format_transcribe_cmd(
    cmd: list[str], *, wav_path: Path, language: str | None
) -> list[str]:
    lang = language or ""
    return [
        part.replace("{wav}", str(wav_path)).replace("{lang}", lang) for part in cmd
    ]


async def _run_ffmpeg(input_path: Path, output_path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-ar",
        "16000",
        "-ac",
        "1",
        str(output_path),
    ]
    try:
        rc, _, stderr = await _run_process(cmd, timeout_s=FFMPEG_TIMEOUT_SEC)
    except TimeoutError as exc:
        raise VoiceError("could not decode audio") from exc
    if rc != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        logger.debug(
            "[voice] ffmpeg failed rc=%s stderr=%s",
            rc,
            stderr.decode("utf-8", errors="replace"),
        )
        raise VoiceError("could not decode audio")


async def _run_transcribe_cmd(wav_path: Path, cfg: VoiceConfig) -> str:
    cmd = _format_transcribe_cmd(
        cfg.transcribe_cmd,
        wav_path=wav_path,
        language=cfg.language,
    )
    try:
        rc, stdout, stderr = await _run_process(
            cmd, timeout_s=cfg.transcribe_timeout_sec
        )
    except TimeoutError as exc:
        raise VoiceError("transcription failed") from exc
    if rc != 0:
        logger.debug(
            "[voice] transcribe failed rc=%s stderr=%s",
            rc,
            stderr.decode("utf-8", errors="replace"),
        )
        raise VoiceError("transcription failed")
    text = stdout.decode("utf-8", errors="replace")
    if not text.strip():
        raise VoiceError("transcription failed")
    return text


async def _run_process(cmd: list[str], *, timeout_s: int) -> tuple[int, bytes, bytes]:
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    async with manage_subprocess(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        async with anyio.create_task_group() as tg:
            if proc.stdout is not None:
                tg.start_soon(_read_stream, proc.stdout, stdout_buf)
            if proc.stderr is not None:
                tg.start_soon(_read_stream, proc.stderr, stderr_buf)
            with anyio.fail_after(timeout_s):
                await proc.wait()
    rc = proc.returncode or 0
    return rc, bytes(stdout_buf), bytes(stderr_buf)


async def _read_stream(stream: anyio.abc.ByteReceiveStream, buf: bytearray) -> None:
    while True:
        try:
            chunk = await stream.receive(65536)
        except anyio.EndOfStream:
            return
        buf.extend(chunk)




def _optional_bool(
    value: Any, key: str, config_path: Path, *, default: bool
) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"Invalid `{key}` in {config_path}; expected a boolean.")


def _optional_int(
    value: Any, key: str, config_path: Path, *, default: int
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Invalid `{key}` in {config_path}; expected an integer.")
    if value < 1:
        raise ConfigError(f"Invalid `{key}` in {config_path}; must be >= 1.")
    return value


def _optional_str(value: Any, key: str, config_path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Invalid `{key}` in {config_path}; expected a string.")
    stripped = value.strip()
    return stripped or None


def _optional_template(
    value: Any, key: str, config_path: Path, *, default: str
) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"Invalid `{key}` in {config_path}; expected a string.")
    return value


def _optional_str_list(
    value: Any, key: str, config_path: Path, *, default: list[str]
) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"Invalid `{key}` in {config_path}; expected a list of strings.")
    return list(value)
