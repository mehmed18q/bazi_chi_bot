"""Shared routing policy for free and premium private-chat features."""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from ..game import GameError, GameService

FREE_CALLBACKS = frozenset({
    "menu:home", "menu:new", "menu:help", "menu:support", "menu:premium",
    "menu:invite",
    "menu:profile", "menu:resume", "menu:last", "menu:daily_challenge", "noop",
})
FREE_COMMANDS = frozenset({"start", "help", "games", "lastgame"})


async def is_free_event(service: GameService, event: Message | CallbackQuery) -> bool:
    """Allow free routes, including actions on ordinary solo and daily games."""
    if isinstance(event, CallbackQuery):
        callback = event.data or ""
        if callback in FREE_CALLBACKS or callback.startswith(
            ("payment:", "profile:", "setup:solo", "daily:")
        ):
            return True
        if callback.startswith("game:"):
            try:
                game_id = int(callback.split(":", 2)[1])
                game = await service.get_game(game_id)
            except (ValueError, IndexError, GameError):
                return False
            return game.is_solo and game.tournament_id is None
        return False

    command = (event.text or "").split(maxsplit=1)[0]
    if command.startswith("/"):
        return command[1:].split("@", 1)[0].lower() in FREE_COMMANDS
    if event.from_user is None:
        return False
    return any(
        game.is_solo and game.tournament_id is None
        for game in await service.queries.active_games(event.from_user.id)
    )
