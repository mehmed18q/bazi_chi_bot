"""Compatibility facade composing the focused application services."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence

from .db import Database
from .errors import CannotJoinOwnGame as CannotJoinOwnGame
from .errors import DailyChallengeClosed as DailyChallengeClosed
from .errors import DailyChallengeRequiresActivation as DailyChallengeRequiresActivation
from .errors import GameError as GameError
from .errors import GameNotFound as GameNotFound
from .errors import InvalidCell as InvalidCell
from .errors import InvalidFinalChoice as InvalidFinalChoice
from .errors import InvalidFinalMessage as InvalidFinalMessage
from .errors import InvalidFist as InvalidFist
from .errors import InvalidGameSetup as InvalidGameSetup
from .errors import InvalidWord as InvalidWord
from .errors import InvalidWordLength as InvalidWordLength
from .errors import InviteUnavailable as InviteUnavailable
from .errors import NotAPlayer as NotAPlayer
from .errors import NotYourTurn as NotYourTurn
from .errors import PendingFinalResponse as PendingFinalResponse
from .errors import RepeatedQuestion as RepeatedQuestion
from .errors import StaleAction as StaleAction
from .models import (
    FinalChoice,
    Game,
    GameType,
    LeaderboardEntry,
    MoveResult,
    MastermindGuessResult,
    MastermindSelection,
    PaymentReceipt,
    PaymentSettings,
    Stats,
    TurnResult,
    User,
    WordGuessResult,
)
from .persistence.games import GameRepository
from .persistence.sponsors import Sponsor, SponsorRepository
from .services.challenges import ChallengeService
from .services.daily_challenges import DailyChallengeService
from .services.matches import MatchService
from .services.solo import BotAdvance, SoloOpponent
from .services.payments import PaymentService
from .services.users import UserService


class GameService:
    """Stable API; services share one database transaction boundary per operation."""

    def __init__(
        self,
        database: Database,
        choose_first_hider: Callable[[Sequence[int]], int] = secrets.choice,
        *,
        activation_exempt_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.database = database
        self.users = UserService(database)
        self.matches = MatchService(
            database, choose_first_hider, activation_exempt_ids=activation_exempt_ids
        )
        self.payments = PaymentService(database)
        self.challenges = ChallengeService(database)
        self.queries = GameRepository(database)
        self.solo = SoloOpponent(self.matches, self.queries)
        self.daily = DailyChallengeService(database, self.matches)
        self.sponsors = SponsorRepository(database)

    async def active_sponsors(self) -> list[Sponsor]:
        return await self.sponsors.active()

    async def all_sponsors(self) -> list[Sponsor]:
        return await self.sponsors.all()

    async def add_sponsor(
        self,
        kind: str,
        title: str,
        url: str,
        *,
        chat_id: str | None = None,
        username: str | None = None,
    ) -> int:
        return await self.sponsors.add(kind, title, url, chat_id=chat_id, username=username)

    async def set_sponsor_active(self, sponsor_id: int, active: bool) -> None:
        await self.sponsors.set_active(sponsor_id, active)

    async def delete_sponsor(self, sponsor_id: int) -> None:
        await self.sponsors.delete(sponsor_id)

    async def save_user(self, user: User) -> None:
        return await self.users.save_user(user)

    async def register_user_entry(self, user: User, *, activation_exempt: bool = False) -> None:
        await self.users.register_entry(user, activation_exempt=activation_exempt)

    async def set_pending_invite(self, telegram_id: int, token: str | None) -> None:
        await self.users.set_pending_invite(telegram_id, token)

    async def set_user_nickname(self, telegram_id: int, nickname: str | None) -> User | None:
        return await self.users.set_nickname(telegram_id, nickname)

    async def get_user(self, telegram_id: int) -> User | None:
        return await self.users.get_user(telegram_id)

    async def all_users(self) -> list[User]:
        return await self.users.all_users()

    async def set_user_profile_photo(self, telegram_id: int, file_id: str | None) -> None:
        await self.users.set_profile_photo(telegram_id, file_id)

    async def payment_settings(self) -> PaymentSettings:
        return await self.payments.settings()

    async def update_payment_settings(
        self,
        admin_id: int,
        *,
        base_amount_toman: int | None = None,
        card_number: str | None = None,
        card_holder: str | None = None,
    ) -> PaymentSettings:
        return await self.payments.update_settings(
            admin_id,
            base_amount_toman=base_amount_toman,
            card_number=card_number,
            card_holder=card_holder,
        )

    async def expected_payment_amount(self, user: User) -> int:
        return await self.payments.expected_amount(user)

    async def set_user_activation(self, user_id: int, admin_id: int, active: bool) -> User | None:
        return await self.payments.set_user_activation(user_id, admin_id, active)

    async def submit_payment_receipt(
        self,
        user_id: int,
        *,
        receipt_text: str | None = None,
        telegram_file_id: str | None = None,
    ) -> PaymentReceipt:
        return await self.payments.submit_receipt(
            user_id,
            receipt_text=receipt_text,
            telegram_file_id=telegram_file_id,
        )

    async def payment_receipt(self, receipt_id: int) -> PaymentReceipt | None:
        return await self.payments.receipt(receipt_id)

    async def latest_pending_payment_receipt(self, user_id: int) -> PaymentReceipt | None:
        return await self.payments.latest_pending_receipt(user_id)

    async def review_payment_receipt(
        self, receipt_id: int, admin_id: int, approved: bool
    ) -> tuple[PaymentReceipt | None, bool]:
        return await self.payments.review_receipt(receipt_id, admin_id, approved)

    async def get_stats(self, telegram_id: int) -> Stats:
        return await self.users.get_stats(telegram_id)

    async def leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.users.leaderboard(limit)

    async def get_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.leaderboard(limit)

    async def leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.users.leaderboard_position(telegram_id)

    async def get_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.leaderboard_position(telegram_id)

    async def all_time_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.users.all_time_leaderboard(limit)

    async def all_time_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.users.all_time_leaderboard_position(telegram_id)

    async def get_all_time_leaderboard(self, limit: int = 3) -> list[LeaderboardEntry]:
        return await self.all_time_leaderboard(limit)

    async def get_all_time_leaderboard_position(self, telegram_id: int) -> LeaderboardEntry | None:
        return await self.all_time_leaderboard_position(telegram_id)

    async def create_game(
        self,
        creator_id: int,
        fists: int,
        total_hands: int,
        game_type: GameType = GameType.GOL_YA_POOCH,
    ) -> Game:
        return await self.matches.create_game(creator_id, fists, total_hands, game_type)

    async def join_game(self, invite_token: str, player_id: int) -> Game:
        return await self.matches.join_game(invite_token, player_id)

    async def create_solo_game(
        self, creator_id: int, fists: int, total_hands: int, game_type: GameType
    ) -> BotAdvance:
        game = await self.matches.create_solo_game(creator_id, fists, total_hands, game_type)
        return await self.advance_bot(game.id)

    async def advance_bot(self, game_id: int) -> BotAdvance:
        for _ in range(3):
            try:
                return await self.solo.advance(game_id)
            except (StaleAction, NotYourTurn):
                # Another callback advanced the same virtual turn; reload it.
                continue
        return await self.solo.advance(game_id)

    async def start_daily_challenge(self, user_id: int) -> BotAdvance:
        game = await self.daily.start(user_id)
        return await self.advance_bot(game.id)

    async def cancel_waiting(self, game_id: int, user_id: int, expected_version: int) -> Game:
        return await self.matches.cancel_waiting(game_id, user_id, expected_version)

    async def hide_fist(self, game_id: int, user_id: int, fist: int, expected_version: int) -> Game:
        return await self.matches.hide_fist(game_id, user_id, fist, expected_version)

    async def guess_fist(
        self, game_id: int, user_id: int, fist: int, expected_version: int
    ) -> TurnResult:
        return await self.matches.guess_fist(game_id, user_id, fist, expected_version)

    async def place_mark(
        self, game_id: int, user_id: int, cell: int, expected_version: int
    ) -> MoveResult:
        return await self.matches.place_mark(game_id, user_id, cell, expected_version)

    async def choose_word(self, game_id: int, user_id: int, text: str) -> Game:
        return await self.matches.choose_word(game_id, user_id, text)

    async def guess_word(self, game_id: int, user_id: int, text: str) -> WordGuessResult:
        return await self.matches.guess_word(game_id, user_id, text)

    async def choose_mastermind_code(
        self, game_id: int, user_id: int, colors: Sequence[str] | str
    ) -> Game:
        return await self.matches.choose_mastermind_code(game_id, user_id, colors)

    async def guess_mastermind(
        self,
        game_id: int,
        user_id: int,
        colors: Sequence[str] | str,
        expected_version: int | None = None,
    ) -> MastermindGuessResult:
        return await self.matches.guess_mastermind(game_id, user_id, colors, expected_version)

    async def select_mastermind_color(
        self, game_id: int, user_id: int, color: str, expected_version: int
    ) -> MastermindSelection:
        return await self.matches.select_mastermind_color(game_id, user_id, color, expected_version)

    async def reset_mastermind_selection(
        self, game_id: int, user_id: int, expected_version: int
    ) -> Game:
        return await self.matches.reset_mastermind_selection(game_id, user_id, expected_version)

    async def pending_final_prompt(self, user_id: int) -> Game | None:
        return await self.challenges.pending_final_prompt(user_id)

    async def pending_final_response(self, user_id: int) -> Game | None:
        return await self.challenges.pending_final_response(user_id)

    async def add_question(self, kind: FinalChoice | str, text: str) -> int:
        return await self.challenges.add_question(kind, text)

    async def choose_final(
        self,
        game_id: int,
        user_id: int,
        choice: FinalChoice | str,
        expected_version: int,
    ) -> Game:
        return await self.challenges.choose_final(game_id, user_id, choice, expected_version)

    async def submit_final_prompt(self, game_id: int, user_id: int, text: str) -> Game:
        return await self.challenges.submit_final_prompt(game_id, user_id, text)

    async def submit_final_response(self, game_id: int, user_id: int, text: str) -> Game:
        return await self.challenges.submit_final_response(game_id, user_id, text)

    async def review_final_response(
        self, game_id: int, user_id: int, approved: bool, expected_version: int
    ) -> Game:
        return await self.challenges.review_final_response(
            game_id, user_id, approved, expected_version
        )

    async def choose_challenge(
        self, game_id: int, user_id: int, kind: FinalChoice | str, expected_version: int
    ) -> Game:
        return await self.challenges.choose_challenge(game_id, user_id, kind, expected_version)

    async def submit_challenge_response(self, game_id: int, user_id: int, text: str) -> Game:
        return await self.challenges.submit_challenge_response(game_id, user_id, text)

    async def submit_challenge_prompt(self, game_id: int, user_id: int, text: str) -> Game:
        return await self.challenges.submit_challenge_prompt(game_id, user_id, text)

    async def review_challenge(
        self, game_id: int, user_id: int, approved: bool, expected_version: int
    ) -> Game:
        return await self.challenges.review_challenge(game_id, user_id, approved, expected_version)

    async def get_game(self, game_id: int) -> Game:
        return await self.queries.get_game(game_id)

    async def get_game_by_token(self, invite_token: str) -> Game:
        return await self.queries.get_game_by_token(invite_token)

    async def active_games(self, user_id: int) -> list[Game]:
        return await self.queries.active_games(user_id)

    async def latest_finished_game(self, user_id: int) -> Game | None:
        return await self.queries.latest_finished_game(user_id)

    async def game_message(self, game_id: int, user_id: int) -> tuple[int, int] | None:
        return await self.queries.game_message(game_id, user_id)

    async def save_game_message(
        self, game_id: int, user_id: int, chat_id: int, message_id: int
    ) -> None:
        await self.queries.save_game_message(game_id, user_id, chat_id, message_id)
