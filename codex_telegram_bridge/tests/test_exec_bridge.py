import asyncio
import os

import pytest

from codex_telegram_bridge.exec_bridge import extract_session_id, truncate_for_telegram


def test_extract_session_id_finds_uuid_v7() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    text = f"resume session `{uuid}` please"

    assert extract_session_id(text) == uuid


def test_truncate_for_telegram_preserves_resume_line() -> None:
    uuid = "019b66fc-64c2-7a71-81cd-081c504cfeb2"
    md = ("x" * 10_000) + f"\nresume: `{uuid}`"

    out = truncate_for_telegram(md, 400)

    assert len(out) <= 400
    assert uuid in out
    assert out.rstrip().endswith(f"resume: `{uuid}`")


class _FakeBot:
    def __init__(self) -> None:
        self._next_id = 1
        self.send_calls: list[dict] = []
        self.edit_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = False,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "disable_notification": disable_notification,
                "entities": entities,
                "parse_mode": parse_mode,
            }
        )
        msg_id = self._next_id
        self._next_id += 1
        return {"message_id": msg_id}

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        entities: list[dict] | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        self.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "entities": entities,
                "parse_mode": parse_mode,
            }
        )
        return {"message_id": message_id}

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.delete_calls.append({"chat_id": chat_id, "message_id": message_id})
        return True


class _FakeRunner:
    def __init__(self, *, answer: str, saw_agent_message: bool = True) -> None:
        self._answer = answer
        self._saw_agent_message = saw_agent_message

    async def run_serialized(self, *_args, **_kwargs) -> tuple[str, str, bool]:
        return ("019b66fc-64c2-7a71-81cd-081c504cfeb2", self._answer, self._saw_agent_message)


def test_final_notify_sends_loud_final_message() -> None:
    from codex_telegram_bridge.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    runner = _FakeRunner(answer="ok")
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        ignore_backlog=True,
        progress_edit_every_s=999.0,
        final_notify=True,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            semaphore=asyncio.Semaphore(1),
            chat_id=123,
            user_msg_id=10,
            text="hi",
            resume_session=None,
        )
    )

    assert len(bot.send_calls) == 2
    assert bot.send_calls[0]["disable_notification"] is True
    assert bot.send_calls[1]["disable_notification"] is False


def test_new_final_message_forces_notification_when_too_long_to_edit() -> None:
    from codex_telegram_bridge.exec_bridge import BridgeConfig, _handle_message

    bot = _FakeBot()
    runner = _FakeRunner(answer="x" * 10_000)
    cfg = BridgeConfig(
        bot=bot,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        chat_id=123,
        ignore_backlog=True,
        progress_edit_every_s=999.0,
        final_notify=False,
        startup_msg="",
        max_concurrency=1,
    )

    asyncio.run(
        _handle_message(
            cfg,
            semaphore=asyncio.Semaphore(1),
            chat_id=123,
            user_msg_id=10,
            text="hi",
            resume_session=None,
        )
    )

    assert len(bot.send_calls) == 2
    assert bot.send_calls[0]["disable_notification"] is True
    assert bot.send_calls[1]["disable_notification"] is False


def test_codex_runner_cancellation_terminates_subprocess(tmp_path, monkeypatch) -> None:
    from codex_telegram_bridge.exec_bridge import CodexExecRunner

    pid_file = tmp_path / "pid"
    codex_path = tmp_path / "codex"
    codex_path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import time\n"
        "\n"
        "pid_file = os.environ.get('CODEX_FAKE_PID_FILE')\n"
        "if pid_file:\n"
        "    with open(pid_file, 'w', encoding='utf-8') as f:\n"
        "        f.write(str(os.getpid()))\n"
        "        f.flush()\n"
        "\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)
    monkeypatch.setenv("CODEX_FAKE_PID_FILE", str(pid_file))

    runner = CodexExecRunner(codex_cmd=str(codex_path), workspace=None, extra_args=[])

    async def run_and_cancel() -> None:
        task = asyncio.create_task(runner.run("hello", session_id=None))

        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert pid_file.exists()

        pid = int(pid_file.read_text(encoding="utf-8").strip())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(200):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)

        raise AssertionError("cancelled codex subprocess is still running")

    asyncio.run(run_and_cancel())
