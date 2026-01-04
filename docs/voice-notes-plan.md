# Voice Notes Feature Plan

## Goal

Enable Telegram voice notes and audio files to be used as user input by transcribing
them to text and then running the same pipeline as normal text messages in Takopi.

## Non-goals (v1)

- Streaming transcription or partial updates while recording.
- Speaker diarization or language auto-detection unless the backend provides it.
- Voice-triggered /cancel (users can still type /cancel).

## Confirmed decisions

- Input types: voice notes + audio files.
- Transcript echo: send transcript in a toggleable section (spoiler).
- Transcription UX: separate "transcribing..." message.
- Limits: max_duration_sec = 300 (5 minutes).
- Caption behavior: ignore captions for v1.
- Backend: simplest option only (command-based).
- Language: leave unset if possible; if required, default to English.
- Errors: terse user-facing messages only.

## Current code touch points (from repo)

- `src/takopi/bridge.py`
  - `poll_updates()` currently drops non-text messages.
  - `run_main_loop()` assumes `msg["text"]` and routes based on text.
  - `handle_message()` is the single path for running a prompt.
- `src/takopi/scheduler.py`
  - `ThreadJob` stores `text` only; queued jobs are text-only.
- `src/takopi/telegram.py`
  - No `getFile` or file download helpers yet.
- `src/takopi/cli.py`
  - Config validation lives here; config is an untyped dict.
- `src/takopi/utils/subprocess.py`
  - Use `manage_subprocess()` for ffmpeg/transcriber process management.

## Proposed design (aligned to current structure)

### 1) Config

Add a `[voice]` table in `~/.takopi/takopi.toml`, parsed in `cli._parse_bridge_config()`
into a new `VoiceConfig` dataclass.

Suggested fields:

- `enabled: bool` (default false)
- `max_duration_sec: int` (default 300)
- `prompt_template: str` (must include `{transcript}`)
- `backend: "cmd"` (v1)
- `transcribe_cmd: list[str]` (required when backend is cmd)
- `transcribe_timeout_sec: int` (default 60)
- `language: str | None` (optional, used for `{lang}` substitution)

If `[voice]` is missing, use defaults with `enabled=false` to keep current behavior.

Validation should follow existing patterns in `runners/*`:

- All fields must be correct types or raise `ConfigError` with `config_path`.
- If `enabled=true`, require `backend` and `transcribe_cmd`.
- Require `{transcript}` in `prompt_template` to avoid silent misconfig.
- If `transcribe_cmd` includes `{lang}` and `language` is unset, substitute `en`.

### 2) Telegram API helpers

Extend `TelegramClient` in `src/takopi/telegram.py`:

- `async def get_file(file_id: str) -> dict | None`
  - call `getFile` and return the result dict.
- `async def download_file(file_path: str) -> bytes | None`
  - GET `https://api.telegram.org/file/bot<TOKEN>/<file_path>`.
  - log errors with the existing redaction filter.

Update `BotClient` Protocol if `voice.py` relies on these methods, or pass the
concrete `TelegramClient` where needed.

Add tests in `tests/test_telegram_client.py` for `get_file`/`download_file` using
`httpx.MockTransport`.

### 3) Voice pipeline module

Add a new module, e.g. `src/takopi/voice.py`, to keep bridge changes small.

Suggested APIs:

- `extract_voice_input(msg: dict) -> VoiceInput | None`
  - support `message.voice` and `message.audio`.
  - capture `file_id`, `duration`, `file_size`, `mime_type` (and maybe `file_name`).
- `async def resolve_user_prompt(msg: dict, cfg: VoiceConfig, bot: BotClient) -> str | None`
  - if `msg["text"]` exists, return it.
  - if voice/audio and `voice.enabled`, run the pipeline and return the prompt.
  - send the "transcribing..." message and edit it into the transcript spoiler.
  - otherwise return None.

Pipeline steps inside `resolve_user_prompt()`:

1. Validate `duration` (max 300s).
2. `get_file(file_id)` -> `file_path` -> download bytes.
3. Write bytes to a temp dir (use `tempfile.TemporaryDirectory`).
4. Run ffmpeg to produce a 16kHz mono wav:
   - `ffmpeg -y -loglevel error -i input -ar 16000 -ac 1 output.wav`
5. Run transcription command (cmd backend):
   - replace placeholders in `transcribe_cmd` with `{wav}` and `{lang}`.
   - if `{lang}` is present and `language` is unset, substitute `en`.
   - capture stdout; treat empty stdout as a failure.
6. Post-process transcript (strip, collapse whitespace).
7. Build prompt with `prompt_template.format(transcript=...)`.

Use `manage_subprocess()` and `drain_stderr()` to ensure child processes are
terminated on cancellation.

### 4) Bridge integration and queueing

Update `bridge.py` so transcription happens inside the queued job, preserving
per-thread FIFO behavior.

Recommended changes:

- `poll_updates()`:
  - allow messages that contain `text`, `voice`, or `audio`.
  - keep the `chat_id` filter unchanged.
- `ThreadJob` in `scheduler.py`:
  - replace `text: str` with `msg: dict` (or a minimal dataclass holding
    the fields needed for routing and transcription).
- `run_main_loop()`:
  - use `msg["text"]` only for routing (`/cancel`, engine overrides, resume tokens).
  - captions are ignored; voice/audio must use replies or the default engine.
  - pass the full message to the job so `resolve_user_prompt()` runs inside
    `run_job()` or `_thread_worker()`.
- `run_job()`:
  - call `resolve_user_prompt()` before `handle_message()`.
  - on `None` (unsupported input or disabled), reply with a short message.
  - on transcription failure, reply with a short error and return (or let
    `resolve_user_prompt()` send the error if it owns the UX).

Transcription should happen before `handle_message()` so the prompt is a string.

### 5) UX and error handling

User experience flow (voice/audio message, not a reply):

- When the queued job starts, send a reply to the user message:
  - `Transcribing...`
- After transcription succeeds, edit that reply into:
  - `Transcript (tap to reveal):\n<transcript>`
  - apply a Telegram spoiler entity covering only the transcript text.
  - if the transcript exceeds Telegram message limits, truncate the displayed
    transcript but keep the full transcript for the prompt.
- Then run `handle_message()` normally so progress and final messages behave
  exactly like text input.
- Keep the transcript message in chat (do not delete it).

Reply/resume behavior:

- If the voice/audio message is a reply to a bot message containing a resume
  line, use the same thread routing as text replies.
- If the voice/audio message is a reply to a running progress message, wait for
  the resume token and then run in that thread. The `Transcribing...` reply is
  sent only when the queued job starts.

Routing behavior:

- `/cancel` and engine overrides are only parsed from `msg["text"]`.
- Voice/audio messages do not parse captions for routing; they use the default
  engine unless they are replies to a resume line.

Error messaging (terse, reply to the user message):

- "voice notes are disabled"
- "voice note too long"
- "could not download voice note"
- "could not decode audio"
- "transcription failed"

Log timings for download, ffmpeg, and transcription (debug-level only).

### 6) Tests to add or update

- Update all `BridgeConfig(...)` uses to include the new voice config field.
- Add unit tests for:
  - voice input extraction (voice/audio keys).
  - duration limit checks.
  - prompt template rendering.
  - spoiler transcript formatting (entity offsets).
  - transcribing message edit behavior (send -> edit).
- Add integration-style tests (mocked HTTP and stubbed transcriber) for:
  - `getFile` -> file download -> wav -> transcript -> prompt.
  - queue behavior with voice messages (transcription inside thread queue).

### 7) Docs

- Update `readme.md` with:
  - new `[voice]` config example.
  - dependencies: `ffmpeg` and the chosen STT backend.
  - troubleshooting notes (duration limit 5 minutes, missing backend).
  - transcript UX (spoiler message).

## Proposed implementation order

1. Define `VoiceConfig` and validation in `cli.py` (or a small `voice.py` helper).
2. Add Telegram `get_file` and `download_file` helpers + tests.
3. Add `voice.py` pipeline and unit tests.
4. Refactor `bridge.py` + `scheduler.py` to pass message objects and resolve
   prompts inside queued jobs.
5. Add UX polish (transcribing message -> transcript spoiler) and logging.
6. Update docs and changelog.
