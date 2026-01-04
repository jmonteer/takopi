from takopi.voice import (
    TRANSCRIPT_HEADER,
    build_transcript_message,
    extract_voice_input,
    truncate_transcript,
)


def test_extract_voice_input_voice() -> None:
    msg = {
        "voice": {
            "file_id": "voice123",
            "duration": 12,
            "file_size": 345,
            "mime_type": "audio/ogg",
        }
    }

    voice = extract_voice_input(msg)

    assert voice is not None
    assert voice.kind == "voice"
    assert voice.file_id == "voice123"
    assert voice.duration == 12
    assert voice.file_size == 345
    assert voice.mime_type == "audio/ogg"
    assert voice.file_name is None


def test_extract_voice_input_audio() -> None:
    msg = {
        "audio": {
            "file_id": "audio123",
            "duration": 22,
            "file_size": 789,
            "mime_type": "audio/mpeg",
            "file_name": "clip.mp3",
        }
    }

    audio = extract_voice_input(msg)

    assert audio is not None
    assert audio.kind == "audio"
    assert audio.file_id == "audio123"
    assert audio.duration == 22
    assert audio.file_size == 789
    assert audio.mime_type == "audio/mpeg"
    assert audio.file_name == "clip.mp3"


def test_extract_voice_input_missing() -> None:
    assert extract_voice_input({}) is None


def test_truncate_transcript_with_suffix() -> None:
    transcript = "abcdef"

    truncated, did_truncate = truncate_transcript(transcript, max_len=5, suffix="...")

    assert did_truncate is True
    assert truncated == "ab..."


def test_build_transcript_message_spoiler_offsets() -> None:
    transcript = "a\U0001F4A9b"

    message, entities, truncated = build_transcript_message(transcript, max_len=200)

    prefix = f"{TRANSCRIPT_HEADER}\n"
    assert truncated is False
    assert message == f"{prefix}{transcript}"
    assert len(entities) == 1

    entity = entities[0]
    assert entity["type"] == "spoiler"
    assert entity["offset"] == len(prefix.encode("utf-16-le")) // 2
    assert entity["length"] == len(transcript.encode("utf-16-le")) // 2
