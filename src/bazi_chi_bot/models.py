"""Domain models shared by persistence, game logic, and Telegram handlers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GameType(StrEnum):
    GOL_YA_POOCH = "gol_ya_pooch"
    TIC_TAC_TOE = "tic_tac_toe"
    TRUTH_OR_DARE = "truth_or_dare"
    WORD_GUESS = "word_guess"
    MASTERMIND = "mastermind"
    ROCK_PAPER_SCISSORS = "rock_paper_scissors"


class GameStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    CHOICE = "choice"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class GamePhase(StrEnum):
    WAITING = "waiting"
    HIDING = "hiding"
    GUESSING = "guessing"
    CHOICE = "choice"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class FinalChoice(StrEnum):
    TRUTH = "truth"
    DARE = "dare"


class CountdownStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PaymentReceiptStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class User:
    telegram_id: int
    username: str | None
    first_name: str
    last_name: str | None
    display_name: str
    profile_photo_file_id: str | None = None
    id: int | None = None
    is_activated: bool = False
    activation_approved_at: int | None = None
    activation_approved_by: int | None = None
    pending_invite_token: str | None = None
    nickname: str | None = None
    nickname_is_custom: bool = False
    telegram_display_name: str | None = None


@dataclass(frozen=True, slots=True)
class PaymentSettings:
    base_amount_toman: int
    card_number: str
    card_holder: str
    updated_at: int
    updated_by: int | None


@dataclass(frozen=True, slots=True)
class PaymentReceipt:
    id: int
    user_telegram_id: int
    user_internal_id: int
    expected_amount_toman: int
    receipt_type: str
    receipt_text: str | None
    telegram_file_id: str | None
    status: PaymentReceiptStatus
    submitted_at: int
    reviewed_at: int | None
    reviewed_by: int | None


@dataclass(frozen=True, slots=True)
class Stats:
    telegram_id: int
    games_played: int
    wins: int
    losses: int
    correct_guesses: int
    wrong_guesses: int
    points_won: int


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    """A player's position in the persistent points leaderboard."""

    rank: int
    telegram_id: int
    display_name: str
    points_won: int
    wins: int
    games_played: int


@dataclass(frozen=True, slots=True)
class WordGuess:
    text: str
    feedback: str


@dataclass(frozen=True, slots=True)
class MastermindGuess:
    colors: tuple[str, ...]
    black: int
    white: int


@dataclass(frozen=True, slots=True)
class Game:
    id: int
    invite_token: str
    creator_id: int
    player2_id: int | None
    fists: int
    total_hands: int
    hand_number: int
    first_hider_id: int | None
    hider_id: int | None
    guesser_id: int | None
    hidden_fist: int | None
    player1_score: int
    player2_score: int
    status: GameStatus
    phase: GamePhase
    winner_id: int | None
    loser_id: int | None
    final_choice: FinalChoice | None
    final_prompt_text: str | None
    final_response_text: str | None
    version: int
    created_at: int
    updated_at: int

    game_type: GameType = GameType.GOL_YA_POOCH
    board: str = "........."
    next_player_id: int | None = None
    round_starter_id: int | None = None
    final_question_id: int | None = None
    final_response_approved: bool | None = None
    final_reviewed_at: int | None = None
    challenge_kind: FinalChoice | None = None
    challenge_question_id: int | None = None
    challenge_prompt_text: str | None = None
    challenge_response_text: str | None = None
    challenge_approved: bool | None = None
    challenge_asker_id: int | None = None
    challenge_respondent_id: int | None = None
    word_secret: str | None = None
    word_attempts: int = 0
    word_guesses: tuple[WordGuess, ...] = ()
    mastermind_secret: tuple[str, ...] | None = None
    mastermind_attempts: int = 0
    mastermind_guesses: tuple[MastermindGuess, ...] = ()
    mastermind_draft: tuple[str, ...] = ()
    is_solo: bool = False
    daily_challenge_date: str | None = None
    rps_creator_move: str | None = None
    rps_player2_move: str | None = None

    def score_for(self, user_id: int) -> int:
        if user_id == self.creator_id:
            return self.player1_score
        if user_id == self.player2_id:
            return self.player2_score
        raise ValueError("User is not a player in this game")

    def opponent_of(self, user_id: int) -> int | None:
        if user_id == self.creator_id:
            return self.player2_id
        if user_id == self.player2_id:
            return self.creator_id
        raise ValueError("User is not a player in this game")

    def has_player(self, user_id: int) -> bool:
        return user_id in (self.creator_id, self.player2_id)


@dataclass(frozen=True, slots=True)
class TurnResult:
    game: Game
    hidden_fist: int
    guessed_fist: int
    correct: bool
    point_winner_id: int
    match_finished: bool


@dataclass(frozen=True, slots=True)
class MoveResult:
    game: Game
    board: str
    round_finished: bool
    point_winner_id: int | None


@dataclass(frozen=True, slots=True)
class RpsResult:
    game: Game
    creator_move: str | None = None
    player2_move: str | None = None
    point_winner_id: int | None = None
    round_finished: bool = False


@dataclass(frozen=True, slots=True)
class WordGuessResult:
    game: Game
    guess: str
    feedback: str
    secret: str
    guesses: tuple[WordGuess, ...]
    guessed_correctly: bool
    round_finished: bool
    match_finished: bool
    point_winner_id: int | None


@dataclass(frozen=True, slots=True)
class MastermindGuessResult:
    game: Game
    guess: tuple[str, ...]
    black: int
    white: int
    guesses: tuple[MastermindGuess, ...]
    guessed_correctly: bool
    round_finished: bool
    match_finished: bool
    point_winner_id: int | None
    secret: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MastermindSelection:
    game: Game
    selection: tuple[str, ...]
    complete: bool
    result: MastermindGuessResult | None = None


@dataclass(frozen=True, slots=True)
class Countdown:
    id: int
    creator_id: int
    target_user_id: int
    target_at: int
    next_run_at: int
    status: CountdownStatus
    created_at: int
    last_sent_at: int | None
    completed_at: int | None
    kind: str = "standard"


@dataclass(frozen=True, slots=True)
class DailyChallenge:
    challenge_date: str
    starts_at: int
    ends_at: int
    game_type: GameType
    fists: int
    total_hands: int
    bot_starts: bool
    secrets: tuple[object, ...]
