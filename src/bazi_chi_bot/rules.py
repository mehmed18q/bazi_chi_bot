"""Shared domain validation; no I/O or framework dependencies."""

import unicodedata
from dataclasses import dataclass

from .errors import (
    InvalidCell,
    InvalidFinalMessage,
    InvalidWord,
    InvalidWordLength,
    NotAPlayer,
    StaleAction,
)
from .models import Game

ALLOWED_FISTS = frozenset(range(2, 7))
ALLOWED_HAND_COUNTS = frozenset((3, 5, 7, 9))
MAX_FINAL_MESSAGE_LENGTH = 3000
MIN_WORD_LENGTH = 2
MAX_WORD_LENGTH = 20

WINNING_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


@dataclass(frozen=True, slots=True)
class BoardMove:
    board: str
    won: bool

    @property
    def round_finished(self) -> bool:
        return self.won or "." not in self.board


def play_tic_tac_toe(board: str, cell: int, mark: str) -> BoardMove:
    """Resolve one move without accessing the database or Telegram."""
    if len(board) != 9 or set(board) - {".", "X", "O"} or mark not in ("X", "O"):
        raise ValueError("Invalid board state or mark")
    if not 0 <= cell < 9 or board[cell] != ".":
        raise InvalidCell
    updated = board[:cell] + mark + board[cell + 1 :]
    won = any(all(updated[index] == mark for index in line) for line in WINNING_LINES)
    return BoardMove(updated, won)


def normalize_word(text: str, *, expected_length: int | None = None) -> str:
    """Normalize Arabic/Persian variants and accept one letter-only word."""
    normalized = unicodedata.normalize("NFKC", text.strip()).translate(
        str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})
    )
    normalized = "".join(
        character for character in normalized if not unicodedata.category(character).startswith("M")
    ).casefold()
    if not MIN_WORD_LENGTH <= len(normalized) <= MAX_WORD_LENGTH or not all(
        unicodedata.category(character).startswith("L") for character in normalized
    ):
        raise InvalidWord
    if expected_length is not None and len(normalized) != expected_length:
        raise InvalidWordLength
    return normalized


def evaluate_word_guess(secret: str, guess: str) -> str:
    """Return Wordle-style green/yellow/black feedback with duplicate accounting."""
    if len(secret) != len(guess):
        raise InvalidWordLength
    feedback = ["b"] * len(secret)
    remaining: dict[str, int] = {}
    for index, target in enumerate(secret):
        if guess[index] == target:
            feedback[index] = "g"
        else:
            remaining[target] = remaining.get(target, 0) + 1
    for index, character in enumerate(guess):
        if feedback[index] == "g":
            continue
        if remaining.get(character, 0):
            feedback[index] = "y"
            remaining[character] -= 1
    return "".join(feedback)


def _require_player(game: Game, user_id: int) -> None:
    if not game.has_player(user_id):
        raise NotAPlayer


def _require_version(game: Game, expected_version: int) -> None:
    if game.version != expected_version:
        raise StaleAction


def _clean_final_message(text: str) -> str:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > MAX_FINAL_MESSAGE_LENGTH:
        raise InvalidFinalMessage
    return cleaned
