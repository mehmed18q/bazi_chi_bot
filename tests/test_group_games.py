from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User as TelegramUser

from bazi_chi_bot.game import GameService
from bazi_chi_bot.models import GameType, User
from bazi_chi_bot.reset_database import reset_history
from bazi_chi_bot.services.group_games import GroupSessionError
from bazi_chi_bot.telegram.groups import (
    GROUP_TYPES,
    build_group_router,
    mentions_bot_name,
    session_card,
)
from bazi_chi_bot.telegram.router import build_router


async def _players(service, players):
    third = User(303, "third", "سوم", None, "سوم")
    await service.save_user(third)
    return (*players, third)


async def _session(service, players, game_type, chat_id=-10001):
    group = service.group_games
    first, *others = players
    session = await group.create(
        chat_id, 100, first.telegram_id, first.display_name, game_type
    )
    for player in others:
        session = await group.join(session.id, player.telegram_id, player.display_name)
    return await group.start(session.id, first.telegram_id)


async def test_group_rps_all_members_play_and_session_survives_restart(service, players, database):
    first, second, third = await _players(service, players)
    session = await _session(service, (first, second, third), GameType.ROCK_PAPER_SCISSORS)
    assert session.total_rounds == 3
    with pytest.raises(GroupSessionError, match="open"):
        await service.group_games.create(-10001, 101, second.telegram_id, second.display_name, GameType.ROCK_PAPER_SCISSORS)
    group = service.group_games
    for player, choice in ((first, "rock"), (second, "scissors")):
        session = await group.move(session.id, 1, player.telegram_id, choice)
    assert session.submitted_count == 2 and session.current_round == 1
    with pytest.raises(GroupSessionError, match="already"):
        await group.move(session.id, 1, first.telegram_id, "paper")
    session = await group.move(session.id, 1, third.telegram_id, "rock")
    assert session.current_round == 2
    assert [p.score for p in session.players] == [1, 0, 1]
    restarted = GameService(database).group_games
    for player, choice in ((first, "rock"), (second, "paper"), (third, "scissors")):
        session = await restarted.move(session.id, 2, player.telegram_id, choice)
    assert [p.score for p in session.players] == [1, 0, 1]
    for player, choice in ((first, "paper"), (second, "paper"), (third, "rock")):
        session = await restarted.move(session.id, 3, player.telegram_id, choice)
    assert session.status == "finished"
    assert [p.score for p in session.players] == [2, 1, 1]
    assert await restarted.active_in_chat(-10001) is None


async def test_group_gol_rotates_hider_and_hides_choice(service, players):
    first, second, third = await _players(service, players)
    group = service.group_games
    session = await _session(service, (first, second, third), GameType.GOL_YA_POOCH, -10002)
    assert session.role_id == first.telegram_id
    with pytest.raises(GroupSessionError, match="turn"):
        await group.move(session.id, 1, second.telegram_id, "2")
    session = await group.move(session.id, 1, first.telegram_id, "2")
    text, _ = session_card(session)
    assert session.secret_choice == "2" and "گل پنهان شد" in text
    assert "گل در مشت 2" not in text
    session = await group.move(session.id, 1, second.telegram_id, "2")
    session = await group.move(session.id, 1, third.telegram_id, "1")
    assert session.role_id == second.telegram_id
    session = await group.move(session.id, 2, second.telegram_id, "3")
    session = await group.move(session.id, 2, first.telegram_id, "1")
    session = await group.move(session.id, 2, third.telegram_id, "1")
    assert session.role_id == third.telegram_id
    session = await group.move(session.id, 3, third.telegram_id, "1")
    session = await group.move(session.id, 3, first.telegram_id, "1")
    session = await group.move(session.id, 3, second.telegram_id, "1")
    assert session.status == "finished"
    assert [p.score for p in session.players] == [1, 3, 0]


async def test_group_tic_tac_toe_rotates_pairs(service, players):
    first, second, third = await _players(service, players)
    group = service.group_games
    session = await _session(service, (first, second, third), GameType.TIC_TAC_TOE, -10003)
    assert (session.pair[0].user_id, session.pair[1].user_id) == (101, 202)
    for round_number, pair in ((1, (first, second)), (2, (second, third)), (3, (third, first))):
        for player, cell in ((pair[0], 0), (pair[1], 3), (pair[0], 1), (pair[1], 4), (pair[0], 2)):
            session = await group.move(session.id, round_number, player.telegram_id, str(cell))
        assert session.current_round == round_number + 1 or session.status == "finished"
    assert session.status == "finished"
    assert [p.score for p in session.players] == [1, 1, 1]


@pytest.mark.parametrize("game_type,kind,secret", [
    (GameType.WORD_GUESS, "word", "کتاب"),
    (GameType.MASTERMIND, "code", "🔵,🟡,⚫,⚪"),
])
async def test_group_secret_games_rotate_setter_and_give_private_feedback(
    service, players, game_type, kind, secret
):
    first, second, third = await _players(service, players)
    group = service.group_games
    session = await _session(service, (first, second, third), game_type, -10004)
    for round_number, setter in enumerate((first, second, third), 1):
        assert session.role_id == setter.telegram_id
        session, feedback = await group.text_input(session.id, setter.telegram_id, kind, secret)
        assert "راز ثبت شد" in feedback
        text, _ = session_card(session)
        assert secret not in text
        for guesser in (p for p in (first, second, third) if p != setter):
            session, feedback = await group.text_input(session.id, guesser.telegram_id, kind, secret)
            assert "درست بود" in feedback
    assert session.status == "finished"
    assert [p.score for p in session.players] == [2, 2, 2]


async def test_group_truth_or_dare_rotates_respondent_and_uses_group_vote(service, players):
    first, second, third = await _players(service, players)
    group = service.group_games
    session = await _session(service, (first, second, third), GameType.TRUTH_OR_DARE, -10005)
    for round_number, respondent in enumerate((first, second, third), 1):
        voters = [p for p in (first, second, third) if p != respondent]
        for voter in voters:
            session = await group.move(session.id, round_number, voter.telegram_id, "truth")
        assert session.phase == "response" and session.prompt_text
        session, _ = await group.text_input(session.id, respondent.telegram_id, "answer", "پاسخ من")
        text, _ = session_card(session)
        assert "پاسخ من" in text
        for voter in voters:
            session = await group.move(session.id, round_number, voter.telegram_id, "yes")
    assert session.status == "finished"
    assert [p.score for p in session.players] == [1, 1, 1]


async def test_group_private_command_records_secret_and_updates_group_card(service, players):
    first = players[0]
    session = await _session(service, players, GameType.WORD_GUESS, -10008)
    handler = next(
        item.callback for item in build_router(service).message.handlers
        if item.callback.__name__ == "group_private_input"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(id=first.telegram_id, is_bot=False, first_name=first.first_name),
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="bazi_chi_bot")),
        edit_message_text=AsyncMock(),
    )
    command = SimpleNamespace(command="gword", args=f"{session.id} کتاب")
    await handler(message, command, bot)
    assert (await service.group_games.get(session.id)).phase == "guess"
    args = bot.edit_message_text.call_args
    assert "کلمهٔ 4 حرفی" in args.args[0]
    assert "کتاب" not in args.args[0]
    assert args.kwargs["chat_id"] == session.chat_id


async def test_reset_removes_group_session_and_moves(service, players, database):
    session = await _session(service, players, GameType.ROCK_PAPER_SCISSORS, -10009)
    await service.group_games.move(session.id, 1, players[0].telegram_id, "rock")
    counts = await reset_history(database)
    assert counts["group_sessions"] == 1
    assert counts["group_players"] == 2
    assert counts["group_round_moves"] == 1
    assert await service.group_games.active_in_chat(-10009) is None


async def test_reset_removes_private_group_attempts(service, players, database):
    session = await _session(service, players, GameType.WORD_GUESS, -10010)
    await service.group_games.text_input(session.id, players[0].telegram_id, "word", "کتاب")
    await service.group_games.text_input(session.id, players[1].telegram_id, "word", "خانه")
    counts = await reset_history(database)
    assert counts["group_attempts"] == 1
    assert counts["group_sessions"] == 1


async def test_group_callback_flow_checks_actor_and_chat(service, players):
    first, second = players
    for player in players:
        await service.set_user_activation(player.telegram_id, 999, True)
    router = build_group_router(service)
    handlers = {item.callback.__name__: item.callback for item in router.callback_query.handlers}
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bazi_chi_bot")))

    def callback(user, data, chat_id=-10006):
        return SimpleNamespace(
            from_user=TelegramUser(id=user.telegram_id, is_bot=False, first_name=user.first_name),
            data=data,
            message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=300, edit_text=AsyncMock()),
            answer=AsyncMock(),
        )

    first_callback = callback(first, "grp:new")
    await handlers["group_new"](first_callback, bot)
    first_callback.data = f"grp:type:{first.telegram_id}:word"
    await handlers["group_type"](first_callback, bot)
    session = await service.group_games.active_in_chat(-10006)
    assert session is not None and len(session.players) == 1
    wrong_chat = callback(second, f"grp:join:{session.id}", -10007)
    await handlers["group_join"](wrong_chat, bot)
    assert wrong_chat.answer.call_args.kwargs["show_alert"] is True
    join = callback(second, f"grp:join:{session.id}")
    await handlers["group_join"](join, bot)
    start = callback(first, f"grp:start:{session.id}")
    await handlers["group_start"](start, bot)
    assert (await service.group_games.get(session.id)).status == "active"
    outsider_user = User(404, "outsider", "غریبه", None, "غریبه")
    await service.save_user(outsider_user)
    await service.set_user_activation(outsider_user.telegram_id, 999, True)
    outsider = callback(outsider_user, f"grp:move:{session.id}:1:rock")
    await handlers["group_move"](outsider, bot)
    assert outsider.answer.call_args.kwargs["show_alert"] is True


def test_group_menu_offers_all_six_games():
    assert {game_type for game_type, _ in GROUP_TYPES.values()} == set(GameType)


async def test_group_message_mention_invites_members_to_start(service, players):
    handler = next(
        item.callback for item in build_group_router(service).message.handlers
        if item.callback.__name__ == "group_name"
    )
    message = SimpleNamespace(
        from_user=TelegramUser(id=players[0].telegram_id, is_bot=False, first_name="صادق"),
        text="بازی چی، کجایی؟",
        caption=None,
        chat=SimpleNamespace(id=-10011),
        answer=AsyncMock(),
    )
    await handler(message)
    assert "من اینجام" in message.answer.call_args.args[0]
    keyboard = message.answer.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "grp:new"


@pytest.mark.parametrize("text,expected", [
    ("بازی چی، کجایی؟", True),
    ("بازی‌چی بیا", True),
    ("بازي چي", True),
    ("بازیچی", True),
    ("چه بازی کنیم؟", False),
    ("بازیچیز", False),
])
def test_group_name_detection(text, expected):
    assert mentions_bot_name(text) is expected
