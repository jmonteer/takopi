from __future__ import annotations

from ..voice import VoiceConfig, load_voice_config, resolve_user_prompt

PLUGIN_ID = "voice"

__all__ = [
    "PLUGIN_ID",
    "VoiceConfig",
    "interactive_voice_setup",
    "load_voice_config",
    "resolve_user_prompt",
]


def interactive_voice_setup(*, force: bool) -> bool:
    from ..voice_onboarding import interactive_voice_setup as _interactive_voice_setup

    return _interactive_voice_setup(force=force)
