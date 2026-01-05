from pathlib import Path

import pytest

from takopi.config import ConfigError
from takopi.voice import load_voice_config


def test_voice_config_defaults_when_missing() -> None:
    cfg = load_voice_config({}, Path("takopi.toml"))

    assert cfg.enabled is False
    assert cfg.max_duration_sec == 300
    assert cfg.transcribe_timeout_sec == 60
    assert cfg.backend == "cmd"
    assert cfg.transcribe_cmd == []
    assert cfg.language is None
    assert "{transcript}" in cfg.prompt_template


def test_voice_config_requires_transcript_placeholder() -> None:
    with pytest.raises(ConfigError, match="prompt_template"):
        load_voice_config(
            {"voice": {"prompt_template": "missing placeholder"}},
            Path("takopi.toml"),
        )


def test_voice_config_lang_defaults_when_placeholder_present() -> None:
    cfg = load_voice_config(
        {"voice": {"transcribe_cmd": ["stt", "--lang", "{lang}"]}},
        Path("takopi.toml"),
    )

    assert cfg.language == "en"


def test_voice_config_lang_respects_config() -> None:
    cfg = load_voice_config(
        {
            "voice": {
                "transcribe_cmd": ["stt", "--lang", "{lang}"],
                "language": "es",
            }
        },
        Path("takopi.toml"),
    )

    assert cfg.language == "es"


def test_voice_config_requires_transcribe_cmd_when_enabled() -> None:
    with pytest.raises(ConfigError, match="transcribe_cmd"):
        load_voice_config({"voice": {"enabled": True}}, Path("takopi.toml"))
