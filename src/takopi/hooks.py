from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .model import EngineId
from .telegram import BotClient


@dataclass(frozen=True, slots=True)
class FinalReplyContext:
    bot: BotClient
    chat_id: int
    user_message_id: int
    final_message_id: int | None
    answer: str
    ok: bool
    engine: EngineId
    elapsed_s: float


FinalReplyHook = Callable[[FinalReplyContext], Awaitable[None]]
