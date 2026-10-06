"""Translate persisted rows into immutable domain snapshots."""

import json

import aiosqlite

from ..models import (
    FinalChoice,
    Game,
    GamePhase,
    GameStatus,
    GameType,
    MastermindGuess,
    User,
    WordGuess,
)


def _game_from_row(row: aiosqlite.Row) -> Game:
    choice = FinalChoice(row["final_choice"]) if row["final_choice"] else None
    return Game(
        id=row["id"],
        invite_token=row["invite_token"],
        creator_id=row["creator_id"],
        player2_id=row["player2_id"],
        fists=row["fists"],
        total_hands=row["total_hands"],
        hand_number=row["hand_number"],
        first_hider_id=row["first_hider_id"],
        hider_id=row["hider_id"],
        guesser_id=row["guesser_id"],
        hidden_fist=row["hidden_fist"],
        player1_score=row["player1_score"],
        player2_score=row["player2_score"],
        status=GameStatus(row["status"]),
        phase=GamePhase(row["phase"]),
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        final_choice=choice,
        final_prompt_text=row["final_prompt_text"],
        final_response_text=row["final_response_text"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        game_type=GameType(row["game_type"]),
        board=row["board"],
        next_player_id=row["next_player_id"],
        round_starter_id=row["round_starter_id"],
        final_question_id=row["final_question_id"],
        final_response_approved=(
            bool(row["final_response_approved"])
            if row["final_response_approved"] is not None
            else None
        ),
        final_reviewed_at=row["final_reviewed_at"],
        challenge_kind=FinalChoice(row["challenge_kind"]) if row["challenge_kind"] else None,
        challenge_question_id=row["challenge_question_id"],
        challenge_prompt_text=row["challenge_prompt_text"],
        challenge_response_text=row["challenge_response_text"],
        challenge_approved=(
            bool(row["challenge_approved"]) if row["challenge_approved"] is not None else None
        ),
        challenge_asker_id=row["challenge_asker_id"],
        challenge_respondent_id=row["challenge_respondent_id"],
        word_secret=row["word_secret"],
        word_attempts=row["word_attempts"],
        word_guesses=tuple(
            WordGuess(item["text"], item["feedback"])
            for item in json.loads(row["word_guesses_json"])
        ),
        mastermind_secret=(
            tuple(json.loads(row["mastermind_secret_json"]))
            if row["mastermind_secret_json"]
            else None
        ),
        mastermind_attempts=row["mastermind_attempts"],
        mastermind_guesses=tuple(
            MastermindGuess(tuple(item["colors"]), item["black"], item["white"])
            for item in json.loads(row["mastermind_guesses_json"])
        ),
        mastermind_draft=tuple(json.loads(row["mastermind_draft_json"])),
        is_solo=bool(row["is_solo"]),
        daily_challenge_date=row["daily_challenge_date"],
        rps_creator_move=row["rps_creator_move"],
        rps_player2_move=row["rps_player2_move"],
        tournament_id=row["tournament_id"],
        tournament_stage=row["tournament_stage"],
        final_challenge_enabled=bool(row["final_challenge_enabled"]),
    )


def _user_from_row(row: aiosqlite.Row) -> User:
    nickname = row["nickname"] or row["display_name"]
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        display_name=nickname,
        profile_photo_file_id=row["profile_photo_file_id"],
        id=row["id"],
        is_activated=bool(row["is_activated"]),
        activation_approved_at=row["activation_approved_at"],
        activation_approved_by=row["activation_approved_by"],
        pending_invite_token=row["pending_invite_token"],
        nickname=nickname,
        nickname_is_custom=bool(row["nickname_is_custom"]),
        telegram_display_name=row["display_name"],
    )
