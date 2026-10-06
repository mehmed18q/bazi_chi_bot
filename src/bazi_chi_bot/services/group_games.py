"""Transactional multiplayer games played by all registered group members."""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

from ..db import Database
from ..errors import InvalidWord, InvalidWordLength
from ..models import GameType
from ..rules import (
    MASTERMIND_MAX_ATTEMPTS,
    evaluate_mastermind_guess,
    evaluate_word_guess,
    normalize_mastermind_code,
    normalize_word,
)
from .word_bank import record_player_word

GROUP_TYPES = tuple(GameType)
RPS_CHOICES = ("rock", "paper", "scissors")
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
WIN_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


class GroupSessionError(ValueError):
    """An action does not fit the current group round."""


@dataclass(frozen=True)
class GroupPlayer:
    user_id: int
    display_name: str
    score: int
    joined_at: int


@dataclass(frozen=True)
class GroupSession:
    id: int
    chat_id: int
    message_id: int
    creator_id: int
    game_type: GameType
    total_rounds: int
    current_round: int
    phase: str
    board: str
    secret_choice: str | None
    prompt_text: str | None
    answer_text: str | None
    status: str
    last_result: str | None
    players: tuple[GroupPlayer, ...]
    submitted_count: int
    final_challenge_enabled: bool
    final_target_id: int | None
    final_approved: bool | None

    @property
    def role_id(self) -> int:
        return self.players[(self.current_round - 1) % len(self.players)].user_id

    @property
    def pair(self) -> tuple[GroupPlayer, GroupPlayer]:
        index = (self.current_round - 1) % len(self.players)
        return self.players[index], self.players[(index + 1) % len(self.players)]

    @property
    def turn_id(self) -> int:
        first, second = self.pair
        return first.user_id if self.board.count("X") == self.board.count("O") else second.user_id


class GroupGameService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def _load(self, connection: aiosqlite.Connection, session_id: int) -> GroupSession:
        row = await (
            await connection.execute("SELECT * FROM group_sessions WHERE id = ?", (session_id,))
        ).fetchone()
        if row is None:
            raise GroupSessionError("missing")
        player_rows = await (
            await connection.execute(
                """SELECT user_id, display_name, score, joined_at FROM group_players
                   WHERE session_id = ? ORDER BY joined_at, rowid""",
                (session_id,),
            )
        ).fetchall()
        count = await (
            await connection.execute(
                """SELECT count(*) FROM group_round_moves
                   WHERE session_id = ? AND round_number = ? AND phase = ?""",
                (session_id, row["current_round"], row["phase"]),
            )
        ).fetchone()
        return GroupSession(
            id=row["id"], chat_id=row["chat_id"], message_id=row["message_id"],
            creator_id=row["creator_id"], game_type=GameType(row["game_type"]),
            total_rounds=row["total_rounds"], current_round=row["current_round"],
            phase=row["phase"], board=row["board"], secret_choice=row["secret_choice"],
            prompt_text=row["prompt_text"], answer_text=row["answer_text"],
            status=row["status"], last_result=row["last_result"],
            players=tuple(GroupPlayer(**dict(item)) for item in player_rows),
            submitted_count=count[0],
            final_challenge_enabled=bool(row["final_challenge_enabled"]),
            final_target_id=row["final_target_id"],
            final_approved=(
                bool(row["final_approved"]) if row["final_approved"] is not None else None
            ),
        )

    async def get(self, session_id: int) -> GroupSession:
        async with self.database.connect() as connection:
            return await self._load(connection, session_id)

    async def active_in_chat(self, chat_id: int) -> GroupSession | None:
        async with self.database.connect() as connection:
            row = await (
                await connection.execute(
                    """SELECT id FROM group_sessions WHERE chat_id = ?
                       AND status IN ('waiting', 'active') LIMIT 1""", (chat_id,)
                )
            ).fetchone()
            return await self._load(connection, row["id"]) if row else None

    async def create(
        self, chat_id: int, message_id: int, creator_id: int,
        display_name: str, game_type: GameType, *, final_challenge_enabled: bool = False,
    ) -> GroupSession:
        if game_type not in GROUP_TYPES:
            raise GroupSessionError("setup")
        if game_type is GameType.TRUTH_OR_DARE and final_challenge_enabled:
            raise GroupSessionError("setup")
        now = int(time.time())
        async with self.database.transaction() as connection:
            try:
                cursor = await connection.execute(
                    """INSERT INTO group_sessions
                       (chat_id, message_id, creator_id, game_type, created_at,
                        final_challenge_enabled)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat_id, message_id, creator_id, game_type.value, now,
                     int(final_challenge_enabled)),
                )
            except aiosqlite.IntegrityError as error:
                raise GroupSessionError("open") from error
            await connection.execute(
                """INSERT INTO group_players
                   (session_id, user_id, display_name, joined_at) VALUES (?, ?, ?, ?)""",
                (cursor.lastrowid, creator_id, display_name, now),
            )
            return await self._load(connection, cursor.lastrowid)

    async def join(self, session_id: int, user_id: int, display_name: str) -> GroupSession:
        async with self.database.transaction() as connection:
            session = await self._load(connection, session_id)
            if session.status != "waiting":
                raise GroupSessionError("started")
            if any(player.user_id == user_id for player in session.players):
                raise GroupSessionError("joined")
            await connection.execute(
                """INSERT INTO group_players
                   (session_id, user_id, display_name, joined_at) VALUES (?, ?, ?, ?)""",
                (session_id, user_id, display_name, int(time.time())),
            )
            return await self._load(connection, session_id)

    async def start(self, session_id: int, user_id: int) -> GroupSession:
        async with self.database.transaction() as connection:
            session = await self._load(connection, session_id)
            if session.creator_id != user_id:
                raise GroupSessionError("owner")
            if session.status != "waiting":
                raise GroupSessionError("started")
            size = len(session.players)
            if size < 2:
                raise GroupSessionError("players")
            rounds = (
                max(3, size) if session.game_type is GameType.ROCK_PAPER_SCISSORS
                else 1 if session.game_type is GameType.TIC_TAC_TOE and size == 2
                else size
            )
            await connection.execute(
                """UPDATE group_sessions SET status = 'active', total_rounds = ?, phase = ?
                   WHERE id = ?""",
                (rounds, self._initial_phase(session.game_type), session_id),
            )
            return await self._load(connection, session_id)

    @staticmethod
    def _initial_phase(game_type: GameType) -> str:
        return {
            GameType.ROCK_PAPER_SCISSORS: "move",
            GameType.GOL_YA_POOCH: "hide",
            GameType.TIC_TAC_TOE: "board",
            GameType.WORD_GUESS: "secret",
            GameType.MASTERMIND: "secret",
            GameType.TRUTH_OR_DARE: "vote",
        }[game_type]

    async def _award(
        self, connection: aiosqlite.Connection, session_id: int, winners: list[int]
    ) -> None:
        for user_id in winners:
            await connection.execute(
                "UPDATE group_players SET score = score + 1 WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )

    async def _complete(
        self, connection: aiosqlite.Connection, session: GroupSession, result: str
    ) -> GroupSession:
        finished = session.current_round >= session.total_rounds
        final_target_id = None
        if finished and session.final_challenge_enabled:
            # The last row uses the same score and join-order ranking as the group card.
            players = await (
                await connection.execute(
                    """SELECT user_id FROM group_players WHERE session_id = ?
                       ORDER BY score DESC, joined_at ASC, user_id ASC""",
                    (session.id,),
                )
            ).fetchall()
            final_target_id = players[-1]["user_id"]
        final_challenge = final_target_id is not None
        await connection.execute(
            """UPDATE group_sessions SET current_round = ?, status = ?, phase = ?,
               board = '.........', secret_choice = NULL, prompt_text = NULL,
               answer_text = NULL, last_result = ?, final_target_id = ? WHERE id = ?""",
            (
                session.current_round if finished else session.current_round + 1,
                "active" if final_challenge or not finished else "finished",
                "final_choice" if final_challenge else
                "finished" if finished else self._initial_phase(session.game_type),
                result,
                final_target_id,
                session.id,
            ),
        )
        return await self._load(connection, session.id)

    async def _record_move(
        self, connection: aiosqlite.Connection, session: GroupSession,
        user_id: int, choice: str,
    ) -> None:
        try:
            await connection.execute(
                """INSERT INTO group_round_moves
                   (session_id, round_number, phase, user_id, choice)
                   VALUES (?, ?, ?, ?, ?)""",
                (session.id, session.current_round, session.phase, user_id, choice),
            )
        except aiosqlite.IntegrityError as error:
            raise GroupSessionError("already") from error

    async def move(
        self, session_id: int, round_number: int, user_id: int, choice: str
    ) -> GroupSession:
        async with self.database.transaction() as connection:
            session = await self._load(connection, session_id)
            if session.status != "active" or session.current_round != round_number:
                raise GroupSessionError("stale")
            if all(player.user_id != user_id for player in session.players):
                raise GroupSessionError("member")

            if session.phase == "final_choice":
                if user_id != session.final_target_id or choice not in ("truth", "dare"):
                    raise GroupSessionError("turn")
                question = await (
                    await connection.execute(
                        """SELECT text FROM questions WHERE active = 1 AND kind = ?
                           AND length(text) <= 1500 ORDER BY RANDOM() LIMIT 1""",
                        (choice,),
                    )
                ).fetchone()
                prompt = question["text"] if question else (
                    "یک حقیقت دربارهٔ خودت بگو." if choice == "truth"
                    else "یک کار خلاقانه برای گروه انجام بده."
                )
                await connection.execute(
                    """UPDATE group_sessions SET phase = 'final_response',
                       secret_choice = ?, prompt_text = ? WHERE id = ?""",
                    (choice, prompt, session_id),
                )
                return await self._load(connection, session_id)

            if session.phase == "final_review":
                if user_id == session.final_target_id or choice not in ("yes", "no"):
                    raise GroupSessionError("turn")
                await self._record_move(connection, session, user_id, choice)
                session = await self._load(connection, session_id)
                if session.submitted_count < len(session.players) - 1:
                    return session
                rows = await self._round_moves(connection, session)
                approved = sum(row["choice"] == "yes" for row in rows) > len(rows) / 2
                if approved:
                    await self._award(connection, session_id, [session.final_target_id])
                await connection.execute(
                    """UPDATE group_sessions SET status = 'finished', phase = 'finished',
                       final_approved = ? WHERE id = ?""",
                    (int(approved), session_id),
                )
                return await self._load(connection, session_id)

            if session.game_type is GameType.TIC_TAC_TOE:
                if session.phase != "board" or user_id != session.turn_id:
                    raise GroupSessionError("turn")
                try:
                    cell = int(choice)
                except ValueError as error:
                    raise GroupSessionError("choice") from error
                if cell not in range(9) or session.board[cell] != ".":
                    raise GroupSessionError("choice")
                mark = "X" if session.board.count("X") == session.board.count("O") else "O"
                board = session.board[:cell] + mark + session.board[cell + 1:]
                await connection.execute(
                    "UPDATE group_sessions SET board = ? WHERE id = ?", (board, session_id)
                )
                if any(all(board[index] == mark for index in line) for line in WIN_LINES):
                    await self._award(connection, session_id, [user_id])
                    return await self._complete(
                        connection, session, f"❌⭕ بازی {round_number}: یک امتیاز برای {self._name(session, user_id)}."
                    )
                if "." not in board:
                    return await self._complete(connection, session, f"❌⭕ بازی {round_number} مساوی شد.")
                return await self._load(connection, session_id)

            if session.game_type is GameType.GOL_YA_POOCH:
                if choice not in ("1", "2", "3"):
                    raise GroupSessionError("choice")
                if session.phase == "hide":
                    if user_id != session.role_id:
                        raise GroupSessionError("turn")
                    await connection.execute(
                        "UPDATE group_sessions SET secret_choice = ?, phase = 'guess' WHERE id = ?",
                        (choice, session_id),
                    )
                    return await self._load(connection, session_id)
                if session.phase != "guess" or user_id == session.role_id:
                    raise GroupSessionError("turn")
                await self._record_move(connection, session, user_id, choice)
                session = await self._load(connection, session_id)
                if session.submitted_count < len(session.players) - 1:
                    return session
                rows = await self._round_moves(connection, session)
                winners = [row["user_id"] for row in rows if row["choice"] == session.secret_choice]
                if not winners:
                    winners = [session.role_id]
                await self._award(connection, session_id, winners)
                return await self._complete(
                    connection, session,
                    f"🌸 دست {round_number}: گل در مشت {session.secret_choice} بود؛ "
                    f"{len(winners)} نفر امتیاز گرفتند.",
                )

            if session.game_type is GameType.ROCK_PAPER_SCISSORS:
                if session.phase != "move" or choice not in RPS_CHOICES:
                    raise GroupSessionError("choice")
                await self._record_move(connection, session, user_id, choice)
                session = await self._load(connection, session_id)
                if session.submitted_count < len(session.players):
                    return session
                rows = await self._round_moves(connection, session)
                choices = {row["choice"] for row in rows}
                winners = (
                    [row["user_id"] for row in rows if RPS_BEATS[row["choice"]] in choices]
                    if len(choices) == 2 else []
                )
                await self._award(connection, session_id, winners)
                result = (
                    f"✊✋✌️ دست {round_number}: {len(winners)} نفر امتیاز گرفتند."
                    if winners else f"✊✋✌️ دست {round_number} مساوی شد."
                )
                return await self._complete(connection, session, result)

            if session.game_type is GameType.TRUTH_OR_DARE:
                if user_id == session.role_id:
                    raise GroupSessionError("turn")
                if session.phase == "vote" and choice in ("truth", "dare"):
                    await self._record_move(connection, session, user_id, choice)
                    session = await self._load(connection, session_id)
                    if session.submitted_count < len(session.players) - 1:
                        return session
                    rows = await self._round_moves(connection, session)
                    truths = sum(row["choice"] == "truth" for row in rows)
                    kind = "truth" if truths >= len(rows) - truths else "dare"
                    question = await (
                        await connection.execute(
                            """SELECT text FROM questions
                               WHERE active = 1 AND kind = ? AND length(text) <= 1500
                               ORDER BY RANDOM() LIMIT 1""",
                            (kind,),
                        )
                    ).fetchone()
                    prompt = question["text"] if question else (
                        "یک حقیقت دربارهٔ خودت بگو." if kind == "truth" else "یک کار خلاقانه برای گروه انجام بده."
                    )
                    await connection.execute(
                        """UPDATE group_sessions SET phase = 'response',
                           secret_choice = ?, prompt_text = ? WHERE id = ?""",
                        (kind, prompt, session_id),
                    )
                    return await self._load(connection, session_id)
                if session.phase == "review" and choice in ("yes", "no"):
                    await self._record_move(connection, session, user_id, choice)
                    session = await self._load(connection, session_id)
                    if session.submitted_count < len(session.players) - 1:
                        return session
                    rows = await self._round_moves(connection, session)
                    approved = sum(row["choice"] == "yes" for row in rows) > len(rows) / 2
                    if approved:
                        await self._award(connection, session_id, [session.role_id])
                    return await self._complete(
                        connection, session,
                        f"🎭 دور {round_number}: پاسخ {self._name(session, session.role_id)} "
                        + ("تأیید شد و یک امتیاز گرفت." if approved else "تأیید نشد."),
                    )
                raise GroupSessionError("turn")

            raise GroupSessionError("text")

    @staticmethod
    def _name(session: GroupSession, user_id: int) -> str:
        return next(player.display_name for player in session.players if player.user_id == user_id)

    @staticmethod
    async def _round_moves(connection: aiosqlite.Connection, session: GroupSession):
        return await (
            await connection.execute(
                """SELECT user_id, choice FROM group_round_moves
                   WHERE session_id = ? AND round_number = ? AND phase = ?""",
                (session.id, session.current_round, session.phase),
            )
        ).fetchall()

    async def text_input(
        self, session_id: int, user_id: int, kind: str, text: str
    ) -> tuple[GroupSession, str]:
        async with self.database.transaction() as connection:
            session = await self._load(connection, session_id)
            if session.status != "active" or all(p.user_id != user_id for p in session.players):
                raise GroupSessionError("member")
            if kind == "answer":
                if session.phase == "final_response":
                    if user_id != session.final_target_id:
                        raise GroupSessionError("turn")
                    answer = text.strip()
                    if not 1 <= len(answer) <= 1000:
                        raise GroupSessionError("answer")
                    await connection.execute(
                        """UPDATE group_sessions SET answer_text = ?, phase = 'final_review'
                           WHERE id = ?""",
                        (answer, session_id),
                    )
                    return (
                        await self._load(connection, session_id),
                        "پاسخ چالش پایانی ثبت شد؛ منتظر رأی گروه باش.",
                    )
                if (
                    session.game_type is not GameType.TRUTH_OR_DARE
                    or session.phase != "response" or user_id != session.role_id
                ):
                    raise GroupSessionError("turn")
                answer = text.strip()
                if not 1 <= len(answer) <= 1000:
                    raise GroupSessionError("answer")
                await connection.execute(
                    "UPDATE group_sessions SET answer_text = ?, phase = 'review' WHERE id = ?",
                    (answer, session_id),
                )
                return await self._load(connection, session_id), "پاسخ ثبت شد؛ منتظر رأی گروه باش."

            expected = (
                GameType.WORD_GUESS if kind == "word"
                else GameType.MASTERMIND if kind == "code" else None
            )
            if session.game_type is not expected:
                raise GroupSessionError("turn")
            try:
                value = (
                    normalize_word(text, expected_length=len(session.secret_choice) if session.phase == "guess" else None)
                    if kind == "word" else ",".join(normalize_mastermind_code(text))
                )
            except InvalidWordLength as error:
                raise GroupSessionError("length") from error
            except InvalidWord as error:
                raise GroupSessionError("word" if kind == "word" else "code") from error
            if session.phase == "secret":
                if user_id != session.role_id:
                    raise GroupSessionError("turn")
                await connection.execute(
                    "UPDATE group_sessions SET secret_choice = ?, phase = 'guess' WHERE id = ?",
                    (value, session_id),
                )
                if kind == "word":
                    await record_player_word(connection, user_id, value)
                return await self._load(connection, session_id), "راز ثبت شد؛ بقیه می‌توانند حدس بزنند."
            if session.phase != "guess" or user_id == session.role_id:
                raise GroupSessionError("turn")
            done = await (
                await connection.execute(
                    """SELECT 1 FROM group_round_moves WHERE session_id = ?
                       AND round_number = ? AND phase = 'guess' AND user_id = ?""",
                    (session_id, session.current_round, user_id),
                )
            ).fetchone()
            if done:
                raise GroupSessionError("already")
            used = await (
                await connection.execute(
                    """SELECT count(*) FROM group_attempts WHERE session_id = ?
                       AND round_number = ? AND user_id = ?""",
                    (session_id, session.current_round, user_id),
                )
            ).fetchone()
            attempt = used[0] + 1
            if kind == "word":
                feedback = evaluate_word_guess(session.secret_choice, value)
                correct = value == session.secret_choice
                limit = len(session.secret_choice)
                marks = "".join({"g": "🟩", "y": "🟨", "b": "⬛"}[mark] for mark in feedback)
                detail = f"{marks} | فرصت {attempt} از {limit}"
            else:
                black, white = evaluate_mastermind_guess(session.secret_choice, value)
                correct = black == 4
                limit = MASTERMIND_MAX_ATTEMPTS
                feedback = f"{black},{white}"
                detail = f"⚫ {black} | ⚪ {white} | تلاش {attempt} از {limit}"
            await connection.execute(
                """INSERT INTO group_attempts
                   (session_id, round_number, user_id, attempt_number, guess, feedback)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, session.current_round, user_id, attempt, value, feedback),
            )
            if correct or attempt >= limit:
                await connection.execute(
                    """INSERT INTO group_round_moves
                       (session_id, round_number, phase, user_id, choice)
                       VALUES (?, ?, 'guess', ?, ?)""",
                    (session_id, session.current_round, user_id, "success" if correct else "failed"),
                )
            session = await self._load(connection, session_id)
            if session.submitted_count == len(session.players) - 1:
                rows = await self._round_moves(connection, session)
                winners = [row["user_id"] for row in rows if row["choice"] == "success"]
                if not winners:
                    winners = [session.role_id]
                await self._award(connection, session_id, winners)
                label = "🔤 کلمه" if kind == "word" else "🎨 کد"
                result = (
                    f"{label} دور {session.current_round} پایان یافت؛ "
                    f"{len(winners)} نفر امتیاز گرفتند."
                )
                session = await self._complete(connection, session, result)
            return session, detail + (" | درست بود ✅" if correct else " | فرصت‌ها تمام شد." if attempt >= limit else "")

    async def cancel(self, session_id: int, user_id: int) -> GroupSession:
        async with self.database.transaction() as connection:
            session = await self._load(connection, session_id)
            if session.creator_id != user_id:
                raise GroupSessionError("owner")
            if session.status not in ("waiting", "active"):
                raise GroupSessionError("stale")
            await connection.execute(
                "UPDATE group_sessions SET status = 'cancelled', phase = 'finished' WHERE id = ?",
                (session_id,),
            )
            return await self._load(connection, session_id)
