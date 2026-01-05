import pytest

from takopi.voice import VoiceConfig, VoiceError, resolve_user_prompt


class _FakeBot:
    def __init__(self) -> None:
        self._next_id = 1
        self.send_calls: list[dict] = []
        self.edit_calls: list[dict] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        payload = {
            "message_id": msg_id,
            "chat_id": chat_id,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
            "disable_notification": disable_notification,
            "entities": entities,
            "parse_mode": parse_mode,
        }
        self.send_calls.append(payload)
        return {"message_id": msg_id}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "entities": entities,
            "parse_mode": parse_mode,
        }
        self.edit_calls.append(payload)
        return {"message_id": message_id}


def _voice_cfg(*, max_duration_sec: int = 300) -> VoiceConfig:
    return VoiceConfig(
        enabled=True,
        max_duration_sec=max_duration_sec,
        prompt_template="Voice: {transcript}",
        backend="cmd",
        transcribe_cmd=["stub", "{wav}"],
        transcribe_timeout_sec=60,
        language=None,
    )


@pytest.mark.anyio
async def test_resolve_user_prompt_voice_success(monkeypatch) -> None:
    async def fake_transcribe(_voice_input, *, cfg, bot) -> str:
        return "hello world"

    monkeypatch.setattr("takopi.voice.transcribe_voice_input", fake_transcribe)

    bot = _FakeBot()
    msg = {
        "message_id": 10,
        "chat": {"id": 123},
        "voice": {"file_id": "voice123", "duration": 5},
    }

    prompt = await resolve_user_prompt(msg, cfg=_voice_cfg(), bot=bot)

    assert prompt == "Voice: hello world"
    assert bot.send_calls
    assert bot.send_calls[0]["text"] == "Transcribing..."
    assert bot.send_calls[0]["reply_to_message_id"] == 10
    assert bot.edit_calls
    assert bot.edit_calls[0]["message_id"] == bot.send_calls[0]["message_id"]
    assert bot.edit_calls[0]["entities"] == []


@pytest.mark.anyio
async def test_resolve_user_prompt_voice_too_long(monkeypatch) -> None:
    called = False

    async def fake_transcribe(_voice_input, *, cfg, bot) -> str:
        nonlocal called
        called = True
        return "unused"

    monkeypatch.setattr("takopi.voice.transcribe_voice_input", fake_transcribe)

    cfg = _voice_cfg(max_duration_sec=1)
    bot = _FakeBot()
    msg = {
        "message_id": 11,
        "chat": {"id": 123},
        "voice": {"file_id": "voice123", "duration": 5},
    }

    prompt = await resolve_user_prompt(msg, cfg=cfg, bot=bot)

    assert prompt is None
    assert called is False
    assert bot.edit_calls == []
    assert bot.send_calls[0]["text"] == "voice note too long"


@pytest.mark.anyio
async def test_resolve_user_prompt_transcribe_error_edits_message(monkeypatch) -> None:
    async def fake_transcribe(_voice_input, *, cfg, bot) -> str:
        raise VoiceError("transcription failed")

    monkeypatch.setattr("takopi.voice.transcribe_voice_input", fake_transcribe)

    bot = _FakeBot()
    msg = {
        "message_id": 12,
        "chat": {"id": 123},
        "voice": {"file_id": "voice123", "duration": 5},
    }

    prompt = await resolve_user_prompt(msg, cfg=_voice_cfg(), bot=bot)

    assert prompt is None
    assert bot.send_calls
    assert bot.edit_calls
    assert bot.edit_calls[0]["text"] == "transcription failed"
