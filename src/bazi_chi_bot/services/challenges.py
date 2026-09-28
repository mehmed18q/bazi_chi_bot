"""Transactional challenges use cases."""

from __future__ import annotations

import time

from ..db import Database
from ..errors import (
    InvalidFinalChoice,
    NotYourTurn,
    PendingFinalResponse,
    RepeatedQuestion,
    StaleAction,
)
from ..models import FinalChoice, Game, GamePhase, GameStatus, GameType
from ..persistence.games import _locked_game
from ..persistence.mappers import _game_from_row
from ..persistence.scores import award_point, record_draw, record_match_result
from ..rules import _clean_final_message, _require_player, _require_version


class ChallengeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def choose_challenge(
        self, game_id: int, user_id: int, kind: FinalChoice | str, expected_version: int
    ) -> Game:
        try:
            kind = FinalChoice(kind)
        except ValueError as error:
            raise InvalidFinalChoice from error
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.game_type is not GameType.TRUTH_OR_DARE
                or game.status is not GameStatus.ACTIVE
                or game.phase is not GamePhase.CHOICE
                or game.challenge_asker_id != user_id
            ):
                raise NotYourTurn
            question = await (
                await connection.execute(
                    """
                SELECT q.id, q.text FROM questions q
                WHERE q.kind = ? AND q.active = 1 AND NOT EXISTS (
                    SELECT 1 FROM question_answers a WHERE a.question_id = q.id
                      AND a.respondent_id = ? AND a.opponent_id = ?
                ) AND NOT EXISTS (
                    SELECT 1 FROM challenge_rounds r WHERE r.question_id = q.id
                      AND r.respondent_id = ? AND r.asker_id = ?
                ) ORDER BY RANDOM() LIMIT 1
                """,
                    (
                        kind.value,
                        game.challenge_respondent_id,
                        user_id,
                        game.challenge_respondent_id,
                        user_id,
                    ),
                )
            ).fetchone()
            if question is None:
                now = int(time.time())
                cursor = await connection.execute(
                    """UPDATE games SET challenge_kind = ?, challenge_question_id = NULL,
                    challenge_prompt_text = NULL, version = version + 1, updated_at = ?
                    WHERE id = ? AND version = ? AND phase = 'choice'
                      AND challenge_kind IS NULL""",
                    (kind.value, now, game.id, game.version),
                )
                if cursor.rowcount != 1:
                    raise StaleAction
                return await _locked_game(connection, game.id)
            now = int(time.time())
            await connection.execute(
                """INSERT INTO challenge_rounds
                (game_id, round_number, kind, question_id, asker_id, respondent_id,
                 question_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    game.id,
                    game.hand_number,
                    kind.value,
                    question["id"],
                    user_id,
                    game.challenge_respondent_id,
                    question["text"],
                    now,
                ),
            )
            await connection.execute(
                """UPDATE games SET challenge_kind = ?, challenge_question_id = ?,
                challenge_prompt_text = ?, phase = 'guessing', version = version + 1,
                updated_at = ? WHERE id = ? AND version = ?""",
                (kind.value, question["id"], question["text"], now, game.id, game.version),
            )
            return await _locked_game(connection, game.id)

    async def submit_challenge_prompt(self, game_id: int, user_id: int, text: str) -> Game:
        """Store an asker's prompt when the shared bank has no unused question."""
        text = _clean_final_message(text)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.game_type is not GameType.TRUTH_OR_DARE
                or game.status is not GameStatus.ACTIVE
                or game.phase is not GamePhase.CHOICE
                or game.challenge_asker_id != user_id
                or game.challenge_respondent_id is None
                or game.challenge_kind is None
                or game.challenge_question_id is not None
                or game.challenge_prompt_text is not None
            ):
                raise NotYourTurn

            # Manual prompts keep a stable identity so their pair-specific history
            # survives restarts and future question-bank imports.
            await connection.execute(
                "INSERT INTO questions (kind, text, active) VALUES (?, ?, 0) "
                "ON CONFLICT(kind, text) DO NOTHING",
                (game.challenge_kind.value, text),
            )
            question = await (
                await connection.execute(
                    "SELECT id FROM questions WHERE kind = ? AND text = ?",
                    (game.challenge_kind.value, text),
                )
            ).fetchone()
            previous = await (
                await connection.execute(
                    """
                    SELECT 1 FROM question_answers
                    WHERE question_id = ? AND respondent_id = ? AND opponent_id = ?
                    UNION ALL
                    SELECT 1 FROM challenge_rounds
                    WHERE question_id = ? AND respondent_id = ? AND asker_id = ?
                    LIMIT 1
                    """,
                    (
                        question["id"],
                        game.challenge_respondent_id,
                        user_id,
                        question["id"],
                        game.challenge_respondent_id,
                        user_id,
                    ),
                )
            ).fetchone()
            if previous is not None:
                raise RepeatedQuestion

            now = int(time.time())
            await connection.execute(
                """INSERT INTO challenge_rounds
                (game_id, round_number, kind, question_id, asker_id, respondent_id,
                 question_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    game.id,
                    game.hand_number,
                    game.challenge_kind.value,
                    question["id"],
                    user_id,
                    game.challenge_respondent_id,
                    text,
                    now,
                ),
            )
            cursor = await connection.execute(
                """UPDATE games SET challenge_question_id = ?, challenge_prompt_text = ?,
                phase = 'guessing', version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'choice'
                  AND challenge_kind IS NOT NULL AND challenge_prompt_text IS NULL""",
                (question["id"], text, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            return await _locked_game(connection, game.id)

    async def submit_challenge_response(self, game_id: int, user_id: int, text: str) -> Game:
        text = _clean_final_message(text)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.game_type is not GameType.TRUTH_OR_DARE
                or game.phase is not GamePhase.GUESSING
                or game.challenge_respondent_id != user_id
                or game.challenge_response_text is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            await connection.execute(
                "UPDATE challenge_rounds SET response_text = ?, answered_at = ? "
                "WHERE game_id = ? AND round_number = ? AND respondent_id = ?",
                (text, now, game.id, game.hand_number, user_id),
            )
            await connection.execute(
                "UPDATE games SET challenge_response_text = ?, phase = 'finished', "
                "version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
                (text, now, game.id, game.version),
            )
            return await _locked_game(connection, game.id)

    async def review_challenge(
        self, game_id: int, user_id: int, approved: bool, expected_version: int
    ) -> Game:
        if not isinstance(approved, bool):
            raise InvalidFinalChoice
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.game_type is not GameType.TRUTH_OR_DARE
                or game.phase is not GamePhase.FINISHED
                or game.challenge_asker_id != user_id
                or game.challenge_response_text is None
                or game.challenge_approved is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            await connection.execute(
                "UPDATE challenge_rounds SET approved = ?, reviewed_at = ? "
                "WHERE game_id = ? AND round_number = ?",
                (int(approved), now, game.id, game.hand_number),
            )
            player1_score = game.player1_score
            player2_score = game.player2_score
            if approved:
                if game.challenge_respondent_id == game.creator_id:
                    player1_score += 1
                elif game.challenge_respondent_id == game.player2_id:
                    player2_score += 1
                else:
                    raise RuntimeError("Challenge respondent is not a game player")
                await award_point(
                    connection, game.id, game.challenge_respondent_id, "challenge", game.hand_number
                )
            finished = game.hand_number >= game.total_hands
            if finished:
                if game.player2_id is None:
                    raise RuntimeError("Finished game has no second player")
                if player1_score > player2_score:
                    winner, loser = game.creator_id, game.player2_id
                elif player2_score > player1_score:
                    winner, loser = game.player2_id, game.creator_id
                else:
                    winner = loser = None
                await connection.execute(
                    "UPDATE games SET player1_score = ?, player2_score = ?, challenge_approved = ?, "
                    "status = 'finished', phase = 'finished', "
                    "winner_id = ?, loser_id = ?, version = version + 1, updated_at = ? "
                    "WHERE id = ? AND version = ?",
                    (
                        player1_score,
                        player2_score,
                        int(approved),
                        winner,
                        loser,
                        now,
                        game.id,
                        game.version,
                    ),
                )
                if winner is None or loser is None:
                    await record_draw(connection, game.creator_id, game.player2_id)
                else:
                    await record_match_result(connection, winner, loser)
            else:
                next_asker = game.challenge_respondent_id
                next_respondent = game.challenge_asker_id
                await connection.execute(
                    """UPDATE games SET hand_number = hand_number + 1,
                    player1_score = ?, player2_score = ?,
                    challenge_asker_id = ?, challenge_respondent_id = ?,
                    challenge_kind = NULL, challenge_question_id = NULL,
                    challenge_prompt_text = NULL, challenge_response_text = NULL,
                    challenge_approved = NULL, phase = 'choice', version = version + 1,
                    updated_at = ? WHERE id = ? AND version = ?""",
                    (
                        player1_score,
                        player2_score,
                        next_asker,
                        next_respondent,
                        now,
                        game.id,
                        game.version,
                    ),
                )
            return await _locked_game(connection, game.id)

    async def pending_final_prompt(self, user_id: int) -> Game | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM games
                    WHERE winner_id = ?
                      AND status = 'finished'
                      AND final_choice IS NOT NULL
                      AND final_prompt_text IS NULL
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            ).fetchone()
        return _game_from_row(row) if row else None

    async def pending_final_response(self, user_id: int) -> Game | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """
                    SELECT * FROM games
                    WHERE loser_id = ?
                      AND status = 'finished'
                      AND final_choice IS NOT NULL
                      AND final_prompt_text IS NOT NULL
                      AND final_response_text IS NULL
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            ).fetchone()
        return _game_from_row(row) if row else None

    async def add_question(self, kind: FinalChoice | str, text: str) -> int:
        """Import a plain-text question; repeated imports preserve its identity/history."""
        try:
            kind = FinalChoice(kind)
        except ValueError as error:
            raise InvalidFinalChoice from error
        text = _clean_final_message(text)
        async with self.database.transaction() as connection:
            await connection.execute(
                "INSERT INTO questions (kind, text) VALUES (?, ?) "
                "ON CONFLICT(kind, text) DO NOTHING",
                (kind.value, text),
            )
            row = await (
                await connection.execute(
                    "SELECT id FROM questions WHERE kind = ? AND text = ?",
                    (kind.value, text),
                )
            ).fetchone()
        return row["id"]

    async def choose_final(
        self,
        game_id: int,
        user_id: int,
        choice: FinalChoice | str,
        expected_version: int,
    ) -> Game:
        try:
            final_choice = FinalChoice(choice)
        except ValueError as error:
            raise InvalidFinalChoice from error
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if game.phase is not GamePhase.CHOICE or game.loser_id != user_id:
                raise NotYourTurn
            pending = await (
                await connection.execute(
                    "SELECT id FROM games WHERE loser_id = ? AND status = 'finished' "
                    "AND final_choice IS NOT NULL AND final_response_text IS NULL LIMIT 1",
                    (user_id,),
                )
            ).fetchone()
            if pending is not None:
                raise PendingFinalResponse
            question = await (
                await connection.execute(
                    """
                SELECT q.id, q.text FROM questions q
                WHERE q.kind = ? AND q.active = 1 AND NOT EXISTS (
                    SELECT 1 FROM question_answers a WHERE a.question_id = q.id
                        AND a.respondent_id = ? AND a.opponent_id = ?
                )
                ORDER BY RANDOM() LIMIT 1
                """,
                    (final_choice.value, user_id, game.winner_id),
                )
            ).fetchone()
            now = int(time.time())
            if question is not None:
                await connection.execute(
                    """
                    INSERT INTO question_answers (
                        game_id, question_id, respondent_id, opponent_id, question_text, asked_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (game.id, question["id"], user_id, game.winner_id, question["text"], now),
                )
            cursor = await connection.execute(
                """
                UPDATE games SET final_choice = ?, status = 'finished', phase = 'finished',
                    final_question_id = ?, final_prompt_text = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND phase = 'choice'
                """,
                (
                    final_choice.value,
                    question["id"] if question else None,
                    question["text"] if question else None,
                    now,
                    game.id,
                    game.version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            updated = await _locked_game(connection, game.id)
        return updated

    async def submit_final_prompt(self, game_id: int, user_id: int, text: str) -> Game:
        text = _clean_final_message(text)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.status is not GameStatus.FINISHED
                or game.winner_id != user_id
                or game.final_choice is None
                or game.final_prompt_text is not None
                or game.final_question_id is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            # Manual prompts are tracked too, but not published to the shared bank.
            await connection.execute(
                "INSERT INTO questions (kind, text, active) VALUES (?, ?, 0) "
                "ON CONFLICT(kind, text) DO NOTHING",
                (game.final_choice.value, text),
            )
            question = await (
                await connection.execute(
                    "SELECT id FROM questions WHERE kind = ? AND text = ?",
                    (game.final_choice.value, text),
                )
            ).fetchone()
            previous = await (
                await connection.execute(
                    "SELECT id FROM question_answers WHERE question_id = ? "
                    "AND respondent_id = ? AND opponent_id = ?",
                    (question["id"], game.loser_id, user_id),
                )
            ).fetchone()
            if previous is not None:
                raise RepeatedQuestion
            await connection.execute(
                "INSERT INTO question_answers (game_id, question_id, respondent_id, "
                "opponent_id, question_text, asked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (game.id, question["id"], game.loser_id, user_id, text, now),
            )
            cursor = await connection.execute(
                """
                UPDATE games SET final_prompt_text = ?, final_question_id = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                  AND winner_id = ?
                  AND status = 'finished'
                  AND final_choice IS NOT NULL
                  AND final_prompt_text IS NULL
                """,
                (text, question["id"], now, game.id, user_id),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            updated = await _locked_game(connection, game.id)
        return updated

    async def submit_final_response(self, game_id: int, user_id: int, text: str) -> Game:
        text = _clean_final_message(text)
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            if (
                game.status is not GameStatus.FINISHED
                or game.loser_id != user_id
                or game.final_choice is None
                or game.final_prompt_text is None
                or game.final_response_text is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET final_response_text = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                  AND loser_id = ?
                  AND status = 'finished'
                  AND final_choice IS NOT NULL
                  AND final_prompt_text IS NOT NULL
                  AND final_response_text IS NULL
                """,
                (text, now, game.id, user_id),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            if game.final_question_id is not None:
                await connection.execute(
                    "UPDATE question_answers SET response_text = ?, answered_at = ? "
                    "WHERE game_id = ? AND respondent_id = ? AND response_text IS NULL",
                    (text, now, game.id, user_id),
                )
            updated = await _locked_game(connection, game.id)
        return updated

    async def review_final_response(
        self, game_id: int, user_id: int, approved: bool, expected_version: int
    ) -> Game:
        if not isinstance(approved, bool):
            raise InvalidFinalChoice
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            _require_player(game, user_id)
            _require_version(game, expected_version)
            if (
                game.status is not GameStatus.FINISHED
                or game.winner_id != user_id
                or game.final_response_text is None
                or game.final_response_approved is not None
            ):
                raise NotYourTurn
            now = int(time.time())
            cursor = await connection.execute(
                """
                UPDATE games SET final_response_approved = ?, final_reviewed_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ? AND final_response_approved IS NULL
                """,
                (int(approved), now, now, game.id, game.version),
            )
            if cursor.rowcount != 1:
                raise StaleAction
            if approved:
                await award_point(connection, game.id, game.loser_id, "challenge", game.hand_number)
            updated = await _locked_game(connection, game.id)
        return updated
