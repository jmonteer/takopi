from pathlib import Path

from takopi.voice_reply import (
    VoiceReplyConfig,
    build_voice_reply_summary,
    load_voice_reply_config,
)


def test_load_voice_reply_config_parses_list_command() -> None:
    cfg = load_voice_reply_config(
        {"voice_reply": {"command": ["echo", "hi"]}}, Path("takopi.toml")
    )
    assert cfg is not None
    assert cfg.command == ["echo", "hi"]
    assert cfg.max_seconds == 30
    assert cfg.wpm == 150


def test_load_voice_reply_config_parses_string_command() -> None:
    cfg = load_voice_reply_config(
        {"voice_reply": {"command": "echo hi"}}, Path("takopi.toml")
    )
    assert cfg is not None
    assert cfg.command == "echo hi"


def test_load_voice_reply_config_disabled_returns_none() -> None:
    cfg = load_voice_reply_config(
        {"voice_reply": {"enabled": False}}, Path("takopi.toml")
    )
    assert cfg is None


def test_build_voice_reply_summary_prefers_bullets() -> None:
    config = VoiceReplyConfig(command=["echo", "hi"])
    summary = build_voice_reply_summary("- shipped feature\n- fixed tests", config)
    assert summary is not None
    assert summary.splitlines()[0] == "hey boss"
    assert "- shipped feature" in summary
    assert "- fixed tests" in summary


def test_build_voice_reply_summary_limits_words() -> None:
    config = VoiceReplyConfig(command=["echo", "hi"], max_seconds=30, wpm=60)
    text = "Sentence one. Sentence two. Sentence three. Sentence four."
    summary = build_voice_reply_summary(text, config)
    assert summary is not None
    lines = summary.splitlines()[1:]
    bullet_words = " ".join(line.lstrip("- ").strip() for line in lines).split()
    assert len(bullet_words) <= 30


def test_build_voice_reply_summary_redacts_paths() -> None:
    config = VoiceReplyConfig(command=["echo", "hi"])
    text = "Updated src/takopi/bridge.py and /Users/juan/project/app/main.py."
    summary = build_voice_reply_summary(text, config)
    assert summary is not None
    assert "src/takopi/bridge.py" not in summary
    assert "/Users/juan/project/app/main.py" not in summary
