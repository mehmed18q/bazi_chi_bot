"""Transactional matches use cases."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Sequence

import aiosqlite

from ..db import Database
from ..errors import (
    CannotJoinOwnGame,
    DailyChallengeClosed,
    DailyChallengeRequiresActivation,
    GameNotFound,
    InvalidFist,
    InvalidGameSetup,
    InviteUnavailable,
    NotYourTurn,
    StaleAction,
)
from ..models import (
    Game,
    GamePhase,
    GameStatus,
    GameType,
    MastermindGuess,
    MastermindGuessResult,
    MastermindSelection,
    MoveResult,
    TurnResult,
    WordGuess,
    WordGuessResult,
)
from ..persistence.games import _locked_game
from ..persistence.mappers import _game_from_row
from ..persistence.scores import award_point, record_draw, record_match_result
from ..rules import (
    ALLOWED_FISTS,
    ALLOWED_HAND_COUNTS,
    MASTERMIND_COLORS,
    MASTERMIND_LENGTH,
    MASTERMIND_MAX_ATTEMPTS,
    _require_player,
    _require_version,
    evaluate_mastermind_guess,
    evaluate_word_guess,
    normalize_mastermind_code,
    normalize_word,
    play_tic_tac_toe,
)

BOT_USER_ID = -1


class MatchService:
    def __init__(
        self,
        database: Database,
        choose_first_hider: Callable[[Sequence[int]], int] = secrets.choice,
        *,
        activation_exempt_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.database = database
        self.choose_first_hider = choose_first_hider
        self.activation_exempt_ids = activation_exempt_ids

    async def create_game(
        self,
        creator_id: int,
        fists: int,
        total_hands: int,
        game_type: GameType = GameType.GOL_YA_POOCH,
    ) -> Game:
        try:
            game_type = GameType(game_type)
        except ValueError as error:
            raise InvalidGameSetup from error
        valid_fists = (
            fists in ALLOWED_FISTS or fists == MASTERMIND_MAX_ATTEMPTS
            if game_type is GameType.MASTERMIND
            else fists in ALLOWED_FISTS
        )
        if not valid_fists or total_hands not in ALLOWED_HAND_COUNTS:
            raise InvalidGameSetup
        now = int(time.time())
        async with self.database.transaction() as connection:
            existing = await (
                await connection.execute(
                    """
                    SELECT * FROM games
                    WHERE creator_id = ? AND status = 'waiting' AND game_type = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (creator_id, game_type.value),
                )
            ).fetchone()
            if existing is not None:
                game = _game_from_row(existing)
                if game.fists == fists and game.total_hands == total_hands:
                    return game
                await connection.execute(
                    """
                    UPDATE games SET status = 'cancelled', phase = 'cancelled',
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND status = 'waiting'
                    """,
                    (now, game.id),
                )

            for _ in range(5):
                token = secrets.token_urlsafe(8)
                try:
                    cursor = await connection.execute(
                        """
                        INSERT INTO games (
                            invite_token, creator_id, fists, total_hands,
                            created_at, updated_at, game_type
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (token, creator_id, fists, total_hands, now, now, game_type.value),
                    )
                    game_id = cursor.lastrowid
                    break
                except aiosqlite.IntegrityError as error:
                    if "invite_token" not in str(error):
                        raise
            else:
                raise RuntimeError("Could not allocate a unique invite token")
            row = await (
                await connection.execute("SELECT * FROM games WHERE id = ?", (game_id,))
            ).fetchone()
        return _game_from_row(row)

    async def join_game(self, invite_token: str, player_id: int) -> Game:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT * FROM games WHERE invite_token = ?", (invite_token,)
                )
            ).fetchone()
            if row is None:
                raise GameNotFound
            game = _game_from_row(row)
            if game.is_solo or player_id == BOT_USER_ID:
                raise InviteUnavailable
            if player_id == game.creator_id:
                raise CannotJoinOwnGame
            if game.status is not GameStatus.WAITING or game.player2_id is not None:
                raise InviteUnavailable

            hider_id = self.choose_first_hider((game.creator_id, player_id))
            if hider_id not in (game.creator_id, player_id):
                raise RuntimeError("First-hider selector returned a non-player")
            guesser_id = player_id if hider_id == game.creator_id else game.creator_id
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET
                    player2_id = ?, first_hider_id = ?, hider_id = ?, guesser_id = ?,
                    status = 'active', phase = ?, version = version + 1,
                    next_player_id = ?, round_starter_id = ?,
                    challenge_asker_id = ?, challenge_respondent_id = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'waiting' AND player2_id IS NULL
                """,
                (
                    player_id,
                    hider_id,
                    hider_id,
                    guesser_id,
                    "choice"
                    if game.game_type is GameType.TRUTH_OR_DARE
                    else ("guessing" if game.game_type is GameType.TIC_TAC_TOE else "hiding"),
                    hider_id if game.game_type is GameType.TIC_TAC_TOE else None,
                    hider_id if game.game_type is GameType.TIC_TAC_TOE else None,
                    game.creator_id if game.game_type is GameType.TRUTH_OR_DARE else None,
                    player_id if game.game_type is GameType.TRUTH_OR_DARE else None,
                    now,
                    game.id,
                ),
            )
            if cursor.rowcount != 1:
                raise InviteUnavailable
            updated = await (
                await connection.execute("SELECT * FROM games WHERE id = ?", (game.id,))
            ).fetchone()
        return _game_from_row(updated)

    async def create_solo_game(
        self,
        creator_id: int,
        fists: int,
        total_hands: int,
        game_type: GameType,
        *,
        daily_date: str | None = None,
        bot_starts: bool | None = None,
    ) -> Game:
        """Start an active match with the virtual player; no invite is issued."""
        try:
            game_type = GameType(game_type)
        except ValueError as error:
            raise InvalidGameSetup from error
        if game_type is GameType.TRUTH_OR_DARE or creator_id == BOT_USER_ID:
            raise InvalidGameSetup
        valid_fists = (
            fists == MASTERMIND_MAX_ATTEMPTS
            if game_type is GameType.MASTERMIND
            else fists in ALLOWED_FISTS
        )
        if not valid_fists or total_hands not in ALLOWED_HAND_COUNTS:
            raise InvalidGameSetup
        first = (
            (BOT_USER_ID if bot_starts else creator_id)
            if bot_starts is not None
            else self.choose_first_hider((creator_id, BOT_USER_ID))
        )
        if first not in (creator_id, BOT_USER_ID):
            raise RuntimeError("First-hider selector returned a non-player")
        now = int(time.time())
        async with self.database.transaction() as connection:
            if daily_date is not None:
                challenge = await (
                    await connection.execute(
                        "SELECT * FROM daily_challenges WHERE challenge_date = ?", (daily_date,)
                    )
                ).fetchone()
                if challenge is None or not challenge["starts_at"] <= now < challenge["ends_at"]:
                    raise DailyChallengeClosed
                if (
                    challenge["game_type"] != game_type.value
                    or challenge["fists"] != fists
                    or challenge["total_hands"] != total_hands
                    or bool(challenge["bot_starts"]) != bot_starts
                ):
                    raise InvalidGameSetup
                activated = await (
                    await connection.execute(
                        "SELECT is_activated FROM users WHERE telegram_id = ?", (creator_id,)
                    )
                ).fetchone()
                if activated is None or (
                    not activated["is_activated"]
                    and creator_id not in self.activation_exempt_ids
                ):
                    raise DailyChallengeRequiresActivation
                existing = await (
                    await connection.execute(
                        "SELECT * FROM games WHERE daily_challenge_date = ? AND creator_id = ?",
                        (daily_date, creator_id),
                    )
                ).fetchone()
                if existing is not None:
                    return _game_from_row(existing)
            await connection.execute(
                """
                INSERT OR IGNORE INTO users (
                    id, telegram_id, username, first_name, display_name,
                    nickname, nickname_is_custom, created_at, updated_at
                ) VALUES (
                    (SELECT COALESCE(MAX(id), 0) + 1 FROM users),
                    -1, NULL, 'ربات', '🤖 ربات', '🤖 ربات', 1, ?, ?
                )
                """,
                (now, now),
            )
            await connection.execute(
                "INSERT OR IGNORE INTO user_stats (telegram_id) VALUES (?)", (BOT_USER_ID,)
            )
            cursor = await connection.execute(
                """
                INSERT INTO games (
                    invite_token, creator_id, player2_id, fists, total_hands,
                    first_hider_id, hider_id, guesser_id, status, phase,
                    next_player_id, round_starter_id, is_solo, game_type, daily_challenge_date,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    secrets.token_urlsafe(8), creator_id, BOT_USER_ID, fists, total_hands,
                    first, first, BOT_USER_ID if first == creator_id else creator_id,
                    "guessing" if game_type is GameType.TIC_TAC_TOE else "hiding",
                    first if game_type is GameType.TIC_TAC_TOE else None,
                    first if game_type is GameType.TIC_TAC_TOE else None,
                    game_type.value,
                    daily_date,
                    now, now,
                ),
            )
            return await _locked_game(connection, cursor.lastrowid)

    async def cancel_waiting(self, game_id: int, user_id: int, expected_version: int) -> Game:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_version(game, expected_version)
            if game.creator_id != user_id or game.status is not GameStatus.WAITING:
                raise NotYourTurn
            now = int(time.time())
            await connection.execute(
                """
                UPDATE games SET status = 'cancelled', phase = 'cancelled',
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (now, game.id, game.version),
            )
            updated = await _locked_game(connection, game.id)
        return updated

    async def hide_fist(self, game_id: int, user_id: int, fist: int, expected_version: int) -> Game:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.game_type is not GameType.GOL_YA_POOCH
                or game.phase is not GamePhase.HIDING
                or game.hider_id != user_id
            ):
                raise NotYourTurn
            if not 1 <= fist <= game.fists:
                raise InvalidFist
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET hidden_fist = ?, phase = 'guessing',
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'hiding'
                """,
                (fist, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            updated = await _locked_game(connection, game.id)
        return updated

    async def guess_fist(
        self, game_id: int, user_id: int, fist: int, expected_version: int
    ) -> TurnResult:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.game_type is not GameType.GOL_YA_POOCH
                or game.phase is not GamePhase.GUESSING
                or game.guesser_id != user_id
            ):
                raise NotYourTurn
            if not 1 <= fist <= game.fists:
                raise InvalidFist
            if game.hidden_fist is None or game.hider_id is None:
                raise RuntimeError("Guessing phase has incomplete role state")

            hidden_fist = game.hidden_fist
            correct = fist == hidden_fist
            point_winner_id = game.guesser_id if correct else game.hider_id
            player1_score = game.player1_score + (point_winner_id == game.creator_id)
            player2_score = game.player2_score + (point_winner_id == game.player2_id)
            match_finished = game.hand_number == game.total_hands
            now = int(time.time())

            if match_finished:
                winner_id = game.creator_id if player1_score > player2_score else game.player2_id
                if winner_id is None:
                    raise RuntimeError("Finished game has no second player")
                loser_id = game.player2_id if winner_id == game.creator_id else game.creator_id
                await connection.execute(
                    """
                    UPDATE games SET
                        player1_score = ?, player2_score = ?, hidden_fist = NULL,
                        winner_id = ?, loser_id = ?, status = ?, phase = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'guessing'
                    """,
                    (
                        player1_score,
                        player2_score,
                        winner_id,
                        loser_id,
                        "finished" if game.is_solo else "choice",
                        "finished" if game.is_solo else "choice",
                        now,
                        game.id,
                        game.version,
                    ),
                )
                await record_match_result(connection, winner_id, loser_id)
            else:
                await connection.execute(
                    """
                    UPDATE games SET
                        hand_number = hand_number + 1,
                        player1_score = ?, player2_score = ?, hidden_fist = NULL,
                        hider_id = guesser_id, guesser_id = hider_id,
                        phase = 'hiding', version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'guessing'
                    """,
                    (player1_score, player2_score, now, game.id, game.version),
                )

            await connection.execute(
                """
                UPDATE user_stats SET
                    correct_guesses = correct_guesses + ?,
                    wrong_guesses = wrong_guesses + ?
                WHERE telegram_id = ?
                """,
                (int(correct), int(not correct), game.guesser_id),
            )
            if not game.is_solo:
                await award_point(connection, game.id, point_winner_id, "round", game.hand_number)
            elif match_finished and winner_id == game.creator_id:
                await award_point(connection, game.id, winner_id, "round", game.hand_number)
            updated = await _locked_game(connection, game.id)
        return TurnResult(
            game=updated,
            hidden_fist=hidden_fist,
            guessed_fist=fist,
            correct=correct,
            point_winner_id=point_winner_id,
            match_finished=match_finished,
        )

    async def choose_word(self, game_id: int, user_id: int, text: str) -> Game:
        word = normalize_word(text)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.game_type is not GameType.WORD_GUESS
                or game.status is not GameStatus.ACTIVE
                or game.phase is not GamePhase.HIDING
                or game.hider_id != user_id
                or game.word_secret is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET word_secret = ?, word_attempts = 0,
                    word_guesses_json = '[]', phase = 'guessing',
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'hiding'
                """,
                (word, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            return await _locked_game(connection, game.id)

    async def guess_word(self, game_id: int, user_id: int, text: str) -> WordGuessResult:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.game_type is not GameType.WORD_GUESS
                or game.status is not GameStatus.ACTIVE
                or game.phase is not GamePhase.GUESSING
                or game.guesser_id != user_id
                or game.word_secret is None
            ):
                raise NotYourTurn

            secret = game.word_secret
            guess = normalize_word(text, expected_length=len(secret))
            feedback = evaluate_word_guess(secret, guess)
            guesses = (*game.word_guesses, WordGuess(guess, feedback))
            attempts = len(guesses)
            guessed_correctly = guess == secret
            round_finished = guessed_correctly or attempts >= len(secret)
            serialized_guesses = json.dumps(
                [{"text": item.text, "feedback": item.feedback} for item in guesses],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            now = int(time.time())

            if not round_finished:
                cursor = await connection.execute(
                    """
                    UPDATE games SET word_attempts = ?, word_guesses_json = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'guessing'
                    """,
                    (attempts, serialized_guesses, now, game.id, game.version),
                )
                if cursor.rowcount != 1:
                    raise StaleAction
                updated = await _locked_game(connection, game.id)
                return WordGuessResult(
                    game=updated,
                    guess=guess,
                    feedback=feedback,
                    secret=secret,
                    guesses=guesses,
                    guessed_correctly=False,
                    round_finished=False,
                    match_finished=False,
                    point_winner_id=None,
                )

            if game.hider_id is None or game.guesser_id is None or game.player2_id is None:
                raise RuntimeError("Word round has incomplete player roles")
            point_winner_id = game.guesser_id if guessed_correctly else game.hider_id
            player1_score = game.player1_score + int(point_winner_id == game.creator_id)
            player2_score = game.player2_score + int(point_winner_id == game.player2_id)
            match_finished = game.hand_number == game.total_hands

            if match_finished:
                winner_id = game.creator_id if player1_score > player2_score else game.player2_id
                loser_id = game.opponent_of(winner_id)
                cursor = await connection.execute(
                    """
                    UPDATE games SET word_attempts = ?, word_guesses_json = ?,
                        player1_score = ?, player2_score = ?,
                        winner_id = ?, loser_id = ?, status = ?, phase = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'guessing'
                    """,
                    (
                        attempts,
                        serialized_guesses,
                        player1_score,
                        player2_score,
                        winner_id,
                        loser_id,
                        "finished" if game.is_solo else "choice",
                        "finished" if game.is_solo else "choice",
                        now,
                        game.id,
                        game.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleAction
                await record_match_result(connection, winner_id, loser_id)
            else:
                cursor = await connection.execute(
                    """
                    UPDATE games SET hand_number = hand_number + 1,
                        player1_score = ?, player2_score = ?,
                        word_secret = NULL, word_attempts = 0, word_guesses_json = '[]',
                        hider_id = guesser_id, guesser_id = hider_id,
                        phase = 'hiding', version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'guessing'
                    """,
                    (player1_score, player2_score, now, game.id, game.version),
                )
                if cursor.rowcount != 1:
                    raise StaleAction

            if not game.is_solo:
                await award_point(connection, game.id, point_winner_id, "round", game.hand_number)
            elif match_finished and winner_id == game.creator_id:
                await award_point(connection, game.id, winner_id, "round", game.hand_number)
            updated = await _locked_game(connection, game.id)
        return WordGuessResult(
            game=updated,
            guess=guess,
            feedback=feedback,
            secret=secret,
            guesses=guesses,
            guessed_correctly=guessed_correctly,
            round_finished=True,
            match_finished=match_finished,
            point_winner_id=point_winner_id,
        )

    async def choose_mastermind_code(
        self, game_id: int, user_id: int, colors: Sequence[str] | str
    ) -> Game:
        code = normalize_mastermind_code(colors)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.game_type is not GameType.MASTERMIND
                or game.status is not GameStatus.ACTIVE
                or game.phase is not GamePhase.HIDING
                or game.hider_id != user_id
                or game.mastermind_secret is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET mastermind_secret_json = ?, mastermind_attempts = 0,
                    mastermind_guesses_json = '[]', mastermind_draft_json = '[]',
                    phase = 'guessing', version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'hiding'
                """,
                (json.dumps(code, separators=(",", ":")), now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            return await _locked_game(connection, game.id)

    async def _record_mastermind_guess(
        self,
        connection: aiosqlite.Connection,
        game: Game,
        guess: tuple[str, ...],
    ) -> MastermindGuessResult:
        if (
            game.game_type is not GameType.MASTERMIND
            or game.status is not GameStatus.ACTIVE
            or game.phase is not GamePhase.GUESSING
            or game.mastermind_secret is None
            or game.hider_id is None
            or game.guesser_id is None
            or game.player2_id is None
        ):
            raise NotYourTurn

        black, white = evaluate_mastermind_guess(game.mastermind_secret, guess)
        guesses = (*game.mastermind_guesses, MastermindGuess(guess, black, white))
        attempts = len(guesses)
        guessed_correctly = black == MASTERMIND_LENGTH
        round_finished = guessed_correctly or attempts >= MASTERMIND_MAX_ATTEMPTS
        serialized_guesses = json.dumps(
            [
                {"colors": item.colors, "black": item.black, "white": item.white}
                for item in guesses
            ],
            separators=(",", ":"),
        )
        now = int(time.time())

        if not round_finished:
            cursor = await connection.execute(
                """
                UPDATE games SET mastermind_attempts = ?, mastermind_guesses_json = ?,
                    mastermind_draft_json = '[]', version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'guessing'
                """,
                (attempts, serialized_guesses, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            updated = await _locked_game(connection, game.id)
            return MastermindGuessResult(
                game=updated,
                guess=guess,
                black=black,
                white=white,
                guesses=guesses,
                guessed_correctly=False,
                round_finished=False,
                match_finished=False,
                point_winner_id=None,
                secret=game.mastermind_secret,
            )

        point_winner_id = game.guesser_id if guessed_correctly else game.hider_id
        player1_score = game.player1_score + int(point_winner_id == game.creator_id)
        player2_score = game.player2_score + int(point_winner_id == game.player2_id)
        match_finished = game.hand_number == game.total_hands

        if match_finished:
            winner_id = game.creator_id if player1_score > player2_score else game.player2_id
            loser_id = game.opponent_of(winner_id)
            cursor = await connection.execute(
                """
                UPDATE games SET mastermind_attempts = ?, mastermind_guesses_json = ?,
                    mastermind_draft_json = '[]', player1_score = ?, player2_score = ?,
                    winner_id = ?, loser_id = ?, status = ?, phase = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'guessing'
                """,
                (
                    attempts,
                    serialized_guesses,
                    player1_score,
                    player2_score,
                    winner_id,
                    loser_id,
                    "finished" if game.is_solo else "choice",
                    "finished" if game.is_solo else "choice",
                    now,
                    game.id,
                    game.version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            await record_match_result(connection, winner_id, loser_id)
        else:
            cursor = await connection.execute(
                """
                UPDATE games SET hand_number = hand_number + 1,
                    player1_score = ?, player2_score = ?,
                    mastermind_secret_json = NULL, mastermind_attempts = 0,
                    mastermind_guesses_json = '[]', mastermind_draft_json = '[]',
                    hider_id = guesser_id, guesser_id = hider_id,
                    phase = 'hiding', version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'guessing'
                """,
                (player1_score, player2_score, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction

        if not game.is_solo:
            await award_point(connection, game.id, point_winner_id, "round", game.hand_number)
        elif match_finished and winner_id == game.creator_id:
            await award_point(connection, game.id, winner_id, "round", game.hand_number)
        updated = await _locked_game(connection, game.id)
        return MastermindGuessResult(
            game=updated,
            guess=guess,
            black=black,
            white=white,
            guesses=guesses,
            guessed_correctly=guessed_correctly,
            round_finished=True,
            match_finished=match_finished,
            point_winner_id=point_winner_id,
            secret=game.mastermind_secret,
        )

    async def guess_mastermind(
        self,
        game_id: int,
        user_id: int,
        colors: Sequence[str] | str,
        expected_version: int | None = None,
    ) -> MastermindGuessResult:
        guess = normalize_mastermind_code(colors)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if expected_version is not None:
                _require_version(game, expected_version)
            if game.guesser_id != user_id:
                raise NotYourTurn
            return await self._record_mastermind_guess(connection, game, guess)

    async def select_mastermind_color(
        self, game_id: int, user_id: int, color: str, expected_version: int
    ) -> MastermindSelection:
        if color not in MASTERMIND_COLORS:
            raise InvalidGameSetup
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            is_hider = game.phase is GamePhase.HIDING and game.hider_id == user_id
            is_guesser = game.phase is GamePhase.GUESSING and game.guesser_id == user_id
            if game.game_type is not GameType.MASTERMIND or not (is_hider or is_guesser):
                raise NotYourTurn

            selection = (*game.mastermind_draft, color)
            if len(selection) < MASTERMIND_LENGTH:
                cursor = await connection.execute(
                    "UPDATE games SET mastermind_draft_json = ?, version = version + 1, "
                    "updated_at = ? WHERE id = ? AND version = ?",
                    (
                        json.dumps(selection, separators=(",", ":")),
                        int(time.time()),
                        game.id,
                        game.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleAction
                updated = await _locked_game(connection, game.id)
                return MastermindSelection(updated, selection, False)

            if len(selection) != MASTERMIND_LENGTH:
                raise InvalidGameSetup
            normalized = normalize_mastermind_code(selection)
            if is_hider:
                cursor = await connection.execute(
                    """
                    UPDATE games SET mastermind_secret_json = ?, mastermind_attempts = 0,
                        mastermind_guesses_json = '[]', mastermind_draft_json = '[]',
                        phase = 'guessing', version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'hiding'
                    """,
                    (
                        json.dumps(normalized, separators=(",", ":")),
                        int(time.time()),
                        game.id,
                        game.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StaleAction
                updated = await _locked_game(connection, game.id)
                return MastermindSelection(updated, normalized, True)

            result = await self._record_mastermind_guess(connection, game, normalized)
            return MastermindSelection(result.game, normalized, True, result)

    async def reset_mastermind_selection(
        self, game_id: int, user_id: int, expected_version: int
    ) -> Game:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            is_hider = game.phase is GamePhase.HIDING and game.hider_id == user_id
            is_guesser = game.phase is GamePhase.GUESSING and game.guesser_id == user_id
            if game.game_type is not GameType.MASTERMIND or not (is_hider or is_guesser):
                raise NotYourTurn
            cursor = await connection.execute(
                "UPDATE games SET mastermind_draft_json = '[]', version = version + 1, "
                "updated_at = ? WHERE id = ? AND version = ?",
                (int(time.time()), game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            return await _locked_game(connection, game.id)

    async def place_mark(
        self, game_id: int, user_id: int, cell: int, expected_version: int
    ) -> MoveResult:
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.game_type is not GameType.TIC_TAC_TOE
                or game.status is not GameStatus.ACTIVE
                or game.next_player_id != user_id
            ):
                raise NotYourTurn
            move = play_tic_tac_toe(game.board, cell, "X" if user_id == game.creator_id else "O")
            board, won, round_finished = move.board, move.won, move.round_finished
            score1 = game.player1_score + int(won and user_id == game.creator_id)
            score2 = game.player2_score + int(won and user_id == game.player2_id)
            finished = (round_finished if game.is_solo else won) and game.hand_number == game.total_hands
            winner = (
                game.creator_id if score1 > score2 else game.player2_id if score2 > score1 else None
            ) if finished else None
            loser = game.opponent_of(winner) if winner is not None else None
            starter = game.round_starter_id
            next_player = game.opponent_of(user_id)
            if round_finished and not finished:
                starter = game.opponent_of(starter)
                next_player = starter
            await connection.execute(
                """
                UPDATE games SET board = ?, next_player_id = ?, round_starter_id = ?,
                    player1_score = ?, player2_score = ?, hand_number = ?,
                    status = ?, phase = ?, winner_id = ?, loser_id = ?,
                    version = version + 1, updated_at = ? WHERE id = ? AND version = ?
                """,
                (
                    "........." if round_finished and not finished else board,
                    None if finished else next_player,
                    starter,
                    score1,
                    score2,
                    game.hand_number + int((round_finished if game.is_solo else won) and not finished),
                    ("finished" if game.is_solo else "choice") if finished else "active",
                    ("finished" if game.is_solo else "choice") if finished else "guessing",
                    winner,
                    loser,
                    int(time.time()),
                    game.id,
                    game.version,
                ),
            )
            if won and not game.is_solo:
                await award_point(connection, game.id, user_id, "round", game.hand_number)
            elif finished and winner == game.creator_id:
                await award_point(connection, game.id, winner, "round", game.hand_number)
            if finished:
                if winner is None:
                    await record_draw(connection, game.creator_id, game.player2_id)
                else:
                    await record_match_result(connection, winner, loser)
            updated = await _locked_game(connection, game.id)
        return MoveResult(updated, board, round_finished, user_id if won else None)
