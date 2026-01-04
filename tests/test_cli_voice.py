from pathlib import Path

from takopi.backends import EngineBackend
from takopi.runners.mock import Return, ScriptRunner


def test_parse_bridge_config_defaults_voice(monkeypatch) -> None:
    from takopi import cli

    class DummyBot:
        async def close(self) -> None:
            return None

    def build_runner(_config, _config_path):
        return ScriptRunner([Return(answer="ok")], engine="codex", resume_value="sid")

    backend = EngineBackend(
        id="codex",
        build_runner=build_runner,
        cli_cmd="codex",
    )

    monkeypatch.setattr(cli, "list_backends", lambda: [backend])
    monkeypatch.setattr(cli.shutil, "which", lambda _cmd: "/usr/bin/codex")
    monkeypatch.setattr(cli, "TelegramClient", lambda _token: DummyBot())

    cfg = cli._parse_bridge_config(
        final_notify=True,
        default_engine_override=None,
        config={"bot_token": "token", "chat_id": 123, "default_engine": "codex"},
        config_path=Path("takopi.toml"),
        token="token",
        chat_id=123,
    )

    assert cfg.voice.enabled is False
