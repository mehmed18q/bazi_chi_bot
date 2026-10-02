"""Persistent, ordered tournaments built from ordinary match stages."""

from __future__ import annotations

import json
import secrets
import time

import aiosqlite

from ..db import Database
from ..errors import InvalidGameSetup, NotYourTurn
from ..models import Game, GameStatus, GameType, Tournament
from ..persistence.games import _locked_game
from ..rules import MASTERMIND_MAX_ATTEMPTS
from .matches import BOT_USER_ID, MatchService
from .prediction import choose_rps_response

SOLO_TYPES = (
    GameType.GOL_YA_POOCH,
    GameType.TIC_TAC_TOE,
    GameType.ROCK_PAPER_SCISSORS,
    GameType.WORD_GUESS,
    GameType.MASTERMIND,
)
DUO_TYPES = (*SOLO_TYPES, GameType.TRUTH_OR_DARE)
STAGE_HANDS = 3
GOL_FISTS = 3


def _from_row(row: aiosqlite.Row) -> Tournament:
    return Tournament(
        id=row["id"],
        creator_id=row["creator_id"],
        player2_id=row["player2_id"],
        is_solo=bool(row["is_solo"]),
        game_types=tuple(GameType(item) for item in json.loads(row["game_types_json"])),
        current_stage=row["current_stage"],
        current_game_id=row["current_game_id"],
        player1_score=row["player1_score"],
        player2_score=row["player2_score"],
        status=row["status"],
    )


class TournamentService:
    def __init__(self, database: Database, matches: MatchService) -> None:
        self.database = database
        self.matches = matches

    async def _create_stage(
        self,
        connection: aiosqlite.Connection,
        tournament_id: int,
        stage: int,
        game_type: GameType,
        creator_id: int,
        player2_id: int | None,
        is_solo: bool,
    ) -> Game:
        now = int(time.time())
        if is_solo:
            await connection.execute(
                """INSERT OR IGNORE INTO users
                   (id, telegram_id, username, first_name, display_name,
                    nickname, nickname_is_custom, created_at, updated_at)
                   VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM users),
                           -1, NULL, 'ربات', '🤖 ربات', '🤖 ربات', 1, ?, ?)""",
                (now, now),
            )
            await connection.execute(
                "INSERT OR IGNORE INTO user_stats (telegram_id) VALUES (?)", (BOT_USER_ID,)
            )
            player2_id = BOT_USER_ID
        waiting = player2_id is None
        first = None if waiting else self.matches.choose_first_hider((creator_id, player2_id))
        if first is not None and first not in (creator_id, player2_id):
            raise RuntimeError("First-player selector returned a non-player")
        active_phase = (
            "choice"
            if game_type is GameType.TRUTH_OR_DARE
            else "guessing"
            if game_type in (GameType.TIC_TAC_TOE, GameType.ROCK_PAPER_SCISSORS)
            else "hiding"
        )
        fists = (
            MASTERMIND_MAX_ATTEMPTS
            if game_type is GameType.MASTERMIND
            else GOL_FISTS
            if game_type is GameType.GOL_YA_POOCH
            else 2
        )
        turn = (
            (creator_id if is_solo and game_type is GameType.ROCK_PAPER_SCISSORS else first)
            if game_type in (GameType.TIC_TAC_TOE, GameType.ROCK_PAPER_SCISSORS)
            else None
        )
        cursor = await connection.execute(
            """INSERT INTO games (
                 invite_token, creator_id, player2_id, fists, total_hands,
                 first_hider_id, hider_id, guesser_id, status, phase,
                 next_player_id, round_starter_id, challenge_asker_id,
                 challenge_respondent_id, is_solo, game_type,
                 tournament_id, tournament_stage, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_urlsafe(8),
                creator_id,
                player2_id,
                fists,
                STAGE_HANDS,
                first,
                first,
                None if waiting else player2_id if first == creator_id else creator_id,
                "waiting" if waiting else "active",
                "waiting" if waiting else active_phase,
                turn,
                first
                if game_type in (GameType.TIC_TAC_TOE, GameType.ROCK_PAPER_SCISSORS)
                else None,
                creator_id if game_type is GameType.TRUTH_OR_DARE and not waiting else None,
                player2_id if game_type is GameType.TRUTH_OR_DARE and not waiting else None,
                int(is_solo),
                game_type.value,
                tournament_id,
                stage,
                now,
                now,
            ),
        )
        if is_solo and game_type is GameType.ROCK_PAPER_SCISSORS:
            bot_move = await choose_rps_response(connection, creator_id, 1, STAGE_HANDS)
            await connection.execute(
                "UPDATE games SET rps_player2_move = ? WHERE id = ?",
                (bot_move, cursor.lastrowid),
            )
        return await _locked_game(connection, cursor.lastrowid)

    async def start(self, creator_id: int, is_solo: bool, game_types: tuple[GameType, ...]) -> Game:
        allowed = SOLO_TYPES if is_solo else DUO_TYPES
        try:
            selected = tuple(GameType(item) for item in game_types)
        except ValueError as error:
            raise InvalidGameSetup from error
        if (
            not 2 <= len(selected) <= len(allowed)
            or len(set(selected)) != len(selected)
            or any(item not in allowed for item in selected)
        ):
            raise InvalidGameSetup
        now = int(time.time())
        async with self.database.transaction() as connection:
            existing = await (
                await connection.execute(
                    """SELECT id FROM tournaments
                       WHERE creator_id = ? AND status IN ('waiting', 'active') LIMIT 1""",
                    (creator_id,),
                )
            ).fetchone()
            if existing:
                raise InvalidGameSetup
            if is_solo:
                await connection.execute(
                    """INSERT OR IGNORE INTO users
                       (id, telegram_id, username, first_name, display_name,
                        nickname, nickname_is_custom, created_at, updated_at)
                       VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM users),
                               -1, NULL, 'ربات', '🤖 ربات', '🤖 ربات', 1, ?, ?)""",
                    (now, now),
                )
                await connection.execute(
                    "INSERT OR IGNORE INTO user_stats (telegram_id) VALUES (?)", (BOT_USER_ID,)
                )
            cursor = await connection.execute(
                """INSERT INTO tournaments
                   (creator_id, player2_id, is_solo, game_types_json,
                    current_stage, current_game_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, NULL, ?, ?, ?)""",
                (
                    creator_id,
                    BOT_USER_ID if is_solo else None,
                    int(is_solo),
                    json.dumps([item.value for item in selected]),
                    "active" if is_solo else "waiting",
                    now,
                    now,
                ),
            )
            game = await self._create_stage(
                connection,
                cursor.lastrowid,
                1,
                selected[0],
                creator_id,
                BOT_USER_ID if is_solo else None,
                is_solo,
            )
            await connection.execute(
                "UPDATE tournaments SET current_game_id = ? WHERE id = ?",
                (game.id, cursor.lastrowid),
            )
            return game

    async def for_game(self, game_id: int) -> Tournament | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """SELECT t.* FROM tournaments t
                       JOIN games g ON g.tournament_id = t.id WHERE g.id = ?""",
                    (game_id,),
                )
            ).fetchone()
        return _from_row(row) if row else None

    async def current_for_user(self, user_id: int) -> tuple[Tournament, Game] | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """SELECT * FROM tournaments
                       WHERE (creator_id = ? OR player2_id = ?)
                         AND status IN ('waiting', 'active')
                       ORDER BY id DESC LIMIT 1""",
                    (user_id, user_id),
                )
            ).fetchone()
            if row is None:
                return None
            tournament = _from_row(row)
            game = await _locked_game(connection, tournament.current_game_id)
        return tournament, game

    async def sync_game(self, game_id: int) -> Game:
        """Count a completed stage once and atomically create its successor."""
        async with self.database.transaction() as connection:
            game = await _locked_game(connection, game_id)
            if game.tournament_id is None:
                return game
            row = await (
                await connection.execute(
                    "SELECT * FROM tournaments WHERE id = ?", (game.tournament_id,)
                )
            ).fetchone()
            tournament = _from_row(row)
            if tournament.current_game_id != game.id:
                return await _locked_game(connection, tournament.current_game_id)
            if (
                tournament.status not in ("active", "waiting")
                or game.status is not GameStatus.FINISHED
            ):
                return game
            score1 = tournament.player1_score + int(game.winner_id == tournament.creator_id)
            score2 = tournament.player2_score + int(game.winner_id == tournament.player2_id)
            now = int(time.time())
            if tournament.current_stage == len(tournament.game_types):
                await connection.execute(
                    """UPDATE tournaments SET player1_score = ?, player2_score = ?,
                       status = 'finished', updated_at = ? WHERE id = ?""",
                    (score1, score2, now, tournament.id),
                )
                return game
            next_stage = tournament.current_stage + 1
            next_game = await self._create_stage(
                connection,
                tournament.id,
                next_stage,
                tournament.game_types[next_stage - 1],
                tournament.creator_id,
                tournament.player2_id,
                tournament.is_solo,
            )
            await connection.execute(
                """UPDATE tournaments SET current_stage = ?, current_game_id = ?,
                   player1_score = ?, player2_score = ?, updated_at = ? WHERE id = ?""",
                (next_stage, next_game.id, score1, score2, now, tournament.id),
            )
            return next_game

    async def sync_user(self, user_id: int) -> None:
        current = await self.current_for_user(user_id)
        if current is not None:
            await self.sync_game(current[1].id)

    async def cancel(self, tournament_id: int, user_id: int) -> Game:
        async with self.database.transaction() as connection:
            row = await (
                await connection.execute("SELECT * FROM tournaments WHERE id = ?", (tournament_id,))
            ).fetchone()
            if row is None:
                raise InvalidGameSetup
            tournament = _from_row(row)
            if user_id not in (
                tournament.creator_id,
                tournament.player2_id,
            ) or tournament.status not in ("waiting", "active"):
                raise NotYourTurn
            now = int(time.time())
            await connection.execute(
                """UPDATE tournaments SET status = 'cancelled', updated_at = ?
                   WHERE id = ? AND status IN ('waiting', 'active')""",
                (now, tournament_id),
            )
            await connection.execute(
                """UPDATE games SET status = 'cancelled', phase = 'cancelled',
                   version = version + 1, updated_at = ?
                   WHERE id = ? AND status IN ('waiting', 'active')""",
                (now, tournament.current_game_id),
            )
            return await _locked_game(connection, tournament.current_game_id)
