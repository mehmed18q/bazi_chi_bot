"""Virtual opponent decisions, deliberately limited to public observations."""

from __future__ import annotations

import hashlib
import itertools
import json
import secrets
from dataclasses import dataclass
from functools import cache

from ..models import Game, GamePhase, GameStatus, GameType, MastermindGuess, WordGuess
from ..persistence.games import GameRepository
from ..rules import MASTERMIND_COLORS, evaluate_mastermind_guess, evaluate_word_guess
from ..word_bank import BOT_WORDS
from .matches import BOT_USER_ID, MatchService
from .prediction import predict_choice

WORD_LETTERS = "ابپتجچخدرزسشفغقکگلمناوی"


def _public_choice(options: list, seed: str | None):
    if seed is None:
        return secrets.choice(options)
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


@dataclass(frozen=True, slots=True)
class BotAdvance:
    game: Game
    messages: tuple[str, ...]


def choose_word_guess(length: int, history: tuple[WordGuess, ...], seed: str | None = None) -> str:
    """Use only the public word length and previous guesses/feedback."""
    tried = {item.text for item in history}
    candidates = [
        word
        for word in BOT_WORDS
        if len(word) == length
        and word not in tried
        and all(evaluate_word_guess(word, item.text) == item.feedback for item in history)
    ]
    public_seed = (
        f"{seed}:{length}:{[(item.text, item.feedback) for item in history]}"
        if seed is not None
        else None
    )
    if candidates:
        return _public_choice(candidates, public_seed)
    # Unknown words are still played without consulting their hidden text.
    for _ in range(100):
        if public_seed is None:
            guess = "".join(secrets.choice(WORD_LETTERS) for _ in range(length))
        else:
            digest = hashlib.shake_256(f"{public_seed}:{_}".encode()).digest(length)
            guess = "".join(WORD_LETTERS[value % len(WORD_LETTERS)] for value in digest)
        if guess not in tried:
            return guess
    return "ا" * length


def choose_code_guess(
    history: tuple[MastermindGuess, ...], seed: str | None = None
) -> tuple[str, ...]:
    """Knuth-style candidate filtering from revealed black/white feedback."""
    candidates = [
        code
        for code in itertools.product(MASTERMIND_COLORS, repeat=4)
        if all(
            evaluate_mastermind_guess(code, item.colors) == (item.black, item.white)
            for item in history
        )
    ]
    if not candidates:
        raise RuntimeError("Public Mastermind feedback is inconsistent")
    public_seed = (
        f"{seed}:{[(item.colors, item.black, item.white) for item in history]}"
        if seed is not None
        else None
    )
    return _public_choice(candidates, public_seed)


def choose_tic_tac_toe_cell(board: str, seed: str | None = None) -> int:
    """Perfect-information minimax, adapted from the project's FinalXO.cpp."""
    from ..rules import WINNING_LINES

    for line in WINNING_LINES:
        open_cells = [index for index in line if board[index] == "."]
        if len(open_cells) == 1 and sum(board[index] == "O" for index in line) == 2:
            return open_cells[0]

    @cache
    def score(position: str, bot_turn: bool) -> int:
        for line in WINNING_LINES:
            marks = {position[index] for index in line}
            if marks == {"O"}:
                return 1
            if marks == {"X"}:
                return -1
        if "." not in position:
            return 0
        possible = (
            score(
                position[:index] + ("O" if bot_turn else "X") + position[index + 1 :], not bot_turn
            )
            for index, mark in enumerate(position)
            if mark == "."
        )
        return max(possible) if bot_turn else min(possible)

    options = [
        (score(board[:index] + "O" + board[index + 1 :], False), index)
        for index, mark in enumerate(board)
        if mark == "."
    ]
    best = max(value for value, _ in options)
    return _public_choice(
        [index for value, index in options if value == best],
        f"{seed}:{board}" if seed is not None else None,
    )


class SoloOpponent:
    def __init__(self, matches: MatchService, games: GameRepository) -> None:
        self.matches = matches
        self.games = games

    async def _daily_secret(self, game: Game) -> object | None:
        if game.daily_challenge_date is None:
            return None
        async with self.games.database.connect() as connection:
            row = await (
                await connection.execute(
                    "SELECT secrets_json FROM daily_challenges WHERE challenge_date = ?",
                    (game.daily_challenge_date,),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("Daily challenge configuration is missing")
        return json.loads(row["secrets_json"])[game.hand_number - 1]

    async def advance(self, game_id: int) -> BotAdvance:
        """Advance all consecutive bot turns, including after a process restart."""
        messages: list[str] = []
        for _ in range(100):
            game = await self.games.get_game(game_id)
            if not game.is_solo or game.status is not GameStatus.ACTIVE:
                return BotAdvance(game, tuple(messages))
            if game.game_type is GameType.TIC_TAC_TOE:
                if game.next_player_id != BOT_USER_ID:
                    return BotAdvance(game, tuple(messages))
                result = await self.matches.place_mark(
                    game.id,
                    BOT_USER_ID,
                    choose_tic_tac_toe_cell(game.board, game.daily_challenge_date),
                    game.version,
                )
                if result.round_finished:
                    messages.append(
                        "🤖 ربات این دست را برد."
                        if result.point_winner_id == BOT_USER_ID
                        else "🤝 این دست مساوی شد."
                    )
            elif game.game_type is GameType.GOL_YA_POOCH:
                if game.phase is GamePhase.HIDING and game.hider_id == BOT_USER_ID:
                    daily_secret = await self._daily_secret(game)
                    await self.matches.hide_fist(
                        game.id,
                        BOT_USER_ID,
                        int(daily_secret)
                        if daily_secret is not None
                        else secrets.randbelow(game.fists) + 1,
                        game.version,
                    )
                    messages.append("🤖 ربات گل را پنهان کرد؛ نوبت حدس توست.")
                elif game.phase is GamePhase.GUESSING and game.guesser_id == BOT_USER_ID:
                    async with self.games.database.connect() as connection:
                        guess = int(await predict_choice(
                            connection, user_id=game.creator_id,
                            game_type=GameType.GOL_YA_POOCH,
                            options=tuple(str(i) for i in range(1, game.fists + 1)),
                            hand_number=game.hand_number,
                            total_hands=game.total_hands,
                        ))
                    result = await self.matches.guess_fist(
                        game.id, BOT_USER_ID, guess, game.version
                    )
                    messages.append(
                        f"🤖 ربات مشت {guess} را حدس زد؛ "
                        + (
                            "درست گفت."
                            if result.correct
                            else "اشتباه گفت؛ امتیاز این دست برای تو شد."
                        )
                    )
                else:
                    return BotAdvance(game, tuple(messages))
            elif game.game_type is GameType.WORD_GUESS:
                if game.phase is GamePhase.HIDING and game.hider_id == BOT_USER_ID:
                    daily_secret = await self._daily_secret(game)
                    await self.matches.choose_bot_word(
                        game.id,
                        str(daily_secret) if daily_secret is not None else None,
                    )
                    messages.append("🤖 ربات کلمه را انتخاب کرد؛ نوبت حدس توست.")
                elif game.phase is GamePhase.GUESSING and game.guesser_id == BOT_USER_ID:
                    # The hidden word is used here only for its publicly shown length.
                    length = len(game.word_secret or "")
                    guess = (
                        choose_word_guess(
                            length,
                            game.word_guesses,
                            f"{game.daily_challenge_date}:{game.hand_number}",
                        )
                        if game.daily_challenge_date is not None
                        else choose_word_guess(length, game.word_guesses)
                    )
                    result = await self.matches.guess_word(game.id, BOT_USER_ID, guess)
                    if result.round_finished:
                        messages.append(
                            "🤖 ربات کلمه را حدس زد."
                            if result.guessed_correctly
                            else "🤖 فرصت‌های ربات تمام شد؛ امتیاز این دور برای تو شد."
                        )
                    else:
                        messages.append(f"🤖 حدس ربات: {guess}")
                else:
                    return BotAdvance(game, tuple(messages))
            elif game.game_type is GameType.MASTERMIND:
                if game.phase is GamePhase.HIDING and game.hider_id == BOT_USER_ID:
                    daily_secret = await self._daily_secret(game)
                    code = (
                        tuple(daily_secret)
                        if daily_secret is not None
                        else tuple(secrets.choice(MASTERMIND_COLORS) for _ in range(4))
                    )
                    await self.matches.choose_mastermind_code(game.id, BOT_USER_ID, code)
                    messages.append("🤖 ربات کد را ساخت؛ نوبت حدس توست.")
                elif game.phase is GamePhase.GUESSING and game.guesser_id == BOT_USER_ID:
                    guess = choose_code_guess(
                        game.mastermind_guesses,
                        f"{game.daily_challenge_date}:{game.hand_number}"
                        if game.daily_challenge_date is not None
                        else None,
                    )
                    result = await self.matches.guess_mastermind(
                        game.id, BOT_USER_ID, guess, game.version
                    )
                    if result.round_finished:
                        messages.append(
                            "🤖 ربات کد را پیدا کرد."
                            if result.guessed_correctly
                            else "🤖 تلاش‌های ربات تمام شد؛ امتیاز این دور برای تو شد."
                        )
                    else:
                        messages.append(
                            f"🤖 تلاش {len(result.guesses)} ربات: "
                            f"⚫ {result.black} | ⚪ {result.white}"
                        )
                else:
                    return BotAdvance(game, tuple(messages))
            else:
                return BotAdvance(game, tuple(messages))
        raise RuntimeError("Virtual player exceeded safe turn limit")
