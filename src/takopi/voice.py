from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import ConfigError

DEFAULT_PROMPT_TEMPLATE = "Voice transcription:\n{transcript}"
DEFAULT_MAX_DURATION_SEC = 300
DEFAULT_TRANSCRIBE_TIMEOUT_SEC = 60
DEFAULT_BACKEND = "cmd"
MAX_TELEGRAM_TEXT_LEN = 4096
TRANSCRIPT_HEADER = "Transcript (tap to reveal):"
TRANSCRIPT_TRUNCATION_SUFFIX = "..."


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
    entities: list[dict[str, int]] = []
    if shown:
        entities.append(
            {
                "type": "spoiler",
                "offset": _utf16_len(prefix),
                "length": _utf16_len(shown),
            }
        )
    return message, entities, truncated


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


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


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
