from __future__ import annotations

import re
import shutil
from pathlib import Path

import questionary
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from questionary.constants import DEFAULT_QUESTION_PREFIX
from questionary.question import Question
from questionary.styles import merge_styles_default
from rich import box
from rich.console import Console
from rich.panel import Panel

from .config import ConfigError, load_telegram_config
from .logging import suppress_logs
from .voice import DEFAULT_BACKEND, DEFAULT_MAX_DURATION_SEC, DEFAULT_PROMPT_TEMPLATE

VOICE_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
)


def _display_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _confirm(message: str, *, default: bool = True) -> bool | None:
    merged_style = merge_styles_default([None])
    status = {"answer": None, "complete": False}

    def get_prompt_tokens():
        tokens = [
            ("class:qmark", DEFAULT_QUESTION_PREFIX),
            ("class:question", f" {message} "),
        ]
        if not status["complete"]:
            tokens.append(("class:instruction", "(yes/no) "))
        if status["answer"] is not None:
            tokens.append(("class:answer", "yes" if status["answer"] else "no"))
        return to_formatted_text(tokens)

    def exit_with_result(event):
        status["complete"] = True
        event.app.exit(result=status["answer"])

    bindings = KeyBindings()

    @bindings.add(Keys.ControlQ, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _(event):
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @bindings.add("n")
    @bindings.add("N")
    def key_n(event):
        status["answer"] = False
        exit_with_result(event)

    @bindings.add("y")
    @bindings.add("Y")
    def key_y(event):
        status["answer"] = True
        exit_with_result(event)

    @bindings.add(Keys.ControlH)
    def key_backspace(event):
        status["answer"] = None

    @bindings.add(Keys.ControlM, eager=True)
    def set_answer(event):
        if status["answer"] is None:
            status["answer"] = default
        exit_with_result(event)

    @bindings.add(Keys.Any)
    def other(event):
        _ = event

    question = Question(
        PromptSession(get_prompt_tokens, key_bindings=bindings, style=merged_style).app
    )
    return question.ask()


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_toml_escape(value)}"' for value in values) + "]"


def _default_voice_model_path() -> Path:
    return Path.home() / ".takopi" / "models" / "ggml-base.en.bin"


def _default_voice_transcribe_cmd(
    model_path: Path, whisper_path: str | None
) -> list[str]:
    return [
        whisper_path or "whisper-cli",
        "-m",
        str(model_path),
        "-f",
        "{wav}",
        "-nt",
    ]


def _render_voice_config(transcribe_cmd: list[str]) -> list[str]:
    return [
        "[voice]",
        "enabled = true",
        f"max_duration_sec = {DEFAULT_MAX_DURATION_SEC}",
        f'prompt_template = "{_toml_escape(DEFAULT_PROMPT_TEMPLATE)}"',
        f'backend = "{DEFAULT_BACKEND}"',
        f"transcribe_cmd = {_toml_list(transcribe_cmd)}",
        '# language = "en"',
    ]


def _set_key(section: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^\\s*{re.escape(key)}\\s*=.*$")
    if pattern.search(section):
        return pattern.sub(f"{key} = {value}", section)
    trailing_match = re.search(r"(\\n*)\\Z", section)
    trailing = trailing_match.group(1) if trailing_match else ""
    section_body = section[:-len(trailing)] if trailing else section
    if section_body and not section_body.endswith("\\n"):
        section_body += "\\n"
    section_body += f"{key} = {value}\\n"
    return section_body + trailing


def _upsert_voice_section(text: str, transcribe_cmd: list[str]) -> str:
    match = re.search(r"(?m)^\\[voice\\]\\s*$", text)
    if not match:
        block = "\n".join(_render_voice_config(transcribe_cmd))
        return text.rstrip("\\n") + "\\n\\n" + block + "\\n"

    line_end = text.find("\\n", match.end())
    if line_end == -1:
        text = text + "\\n"
        start = len(text)
        end = len(text)
        section = ""
    else:
        start = line_end + 1
        next_match = re.search(r"(?m)^\\[[^\\]]+\\]\\s*$", text[start:])
        end = start + next_match.start() if next_match else len(text)
        section = text[start:end]

    section = _set_key(section, "enabled", "true")
    section = _set_key(section, "max_duration_sec", str(DEFAULT_MAX_DURATION_SEC))
    section = _set_key(
        section,
        "prompt_template",
        f'"{_toml_escape(DEFAULT_PROMPT_TEMPLATE)}"',
    )
    section = _set_key(section, "backend", f'"{DEFAULT_BACKEND}"')
    section = _set_key(section, "transcribe_cmd", _toml_list(transcribe_cmd))
    if not re.search(r"(?m)^\\s*language\\s*=", section):
        if not section.endswith("\\n"):
            section += "\\n"
        section += '# language = "en"\\n'

    return text[:start] + section + text[end:]


def interactive_voice_setup(*, force: bool) -> bool:
    console = Console()
    with suppress_logs():
        panel = Panel(
            "let's configure voice notes.",
            title="voice notes setup",
            border_style="yellow",
            padding=(1, 2),
            expand=False,
        )
        console.print(panel)

        try:
            _config, config_path = load_telegram_config()
        except ConfigError as exc:
            console.print(f"error: {exc}")
            return False

        config_text = config_path.read_text(encoding="utf-8")
        has_voice = re.search(r"(?m)^\\[voice\\]\\s*$", config_text) is not None
        if has_voice and not force:
            overwrite = _confirm("voice config already exists. overwrite?", default=False)
            if not overwrite:
                return False

        console.print("step 1: dependencies\n")
        ffmpeg_path = shutil.which("ffmpeg")
        whisper_path = shutil.which("whisper-cli")
        model_path = _default_voice_model_path()
        if ffmpeg_path:
            console.print(f"  ffmpeg: {ffmpeg_path}")
        else:
            console.print("  ffmpeg: not found")
        if whisper_path:
            console.print(f"  whisper-cli: {whisper_path}")
        else:
            console.print("  whisper-cli: not found")
        if not ffmpeg_path or not whisper_path:
            if shutil.which("brew"):
                console.print("  install: brew install ffmpeg whisper-cpp")
            else:
                console.print(
                    "  install ffmpeg and whisper-cpp (whisper-cli) and ensure they're on PATH."
                )
        if model_path.exists():
            console.print(f"  model: {_display_path(model_path)}")
        else:
            console.print(f"  model: {_display_path(model_path)} (missing)")
            console.print(f"  mkdir -p {_display_path(model_path.parent)}")
            console.print(f"  curl -L -o {_display_path(model_path)} {VOICE_MODEL_URL}")

        transcribe_cmd = _default_voice_transcribe_cmd(model_path, whisper_path)
        config_preview = _render_voice_config(transcribe_cmd)

        console.print("\nstep 2: update config\n")
        console.print(f"  {_display_path(config_path)}\n")
        for line in config_preview:
            console.print(f"  {line}")
        console.print("")

        save = _confirm(f"write voice config to {_display_path(config_path)}?")
        if not save:
            return False

        updated = _upsert_voice_section(config_text, transcribe_cmd)
        config_path.write_text(updated, encoding="utf-8")
        console.print(f"  voice config updated in {_display_path(config_path)}")
        return True
