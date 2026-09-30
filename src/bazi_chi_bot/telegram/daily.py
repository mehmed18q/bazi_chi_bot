"""Daily challenge entry and opt-in recurring reminder controls."""

from __future__ import annotations

import time

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from ..daily_schedule import local_date
from ..game import GameError, GameService
from ..models import GameStatus, GameType
from ..services.countdowns import CountdownService
from .countdown_worker import CountdownScheduler
from .keyboards import daily_challenge_keyboard
from .shared import GamePresenter, game_error_text, safe_edit


def register_handlers(
    router: Router,
    service: GameService,
    countdown_service: CountdownService | None,
    countdown_scheduler: CountdownScheduler | None,
) -> None:
    presenter = GamePresenter(service)

    async def show_daily(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        current = int(time.time())
        day = local_date(current)
        status = service.daily.window_status(current)
        reminder = (
            await countdown_service.daily_reminder(user_id)
            if countdown_service is not None
            else None
        )
        if status == "before":
            text = (
                "⏳ <b>چالش امروز هنوز شروع نشده است.</b>\n"
                "از ساعت ۱۲ تا ۱۳ به وقت تهران فرصت بازی داری. "
                "اگر بخواهی، ساعت ۱۱:۵۵ هر روز یادت می‌اندازم."
            )
            can_play = False
        elif status == "ended":
            challenge = await service.daily.get(day)
            text = (
                await service.daily.notification_text(challenge.challenge_date, user_id, "end")
                if challenge is not None
                else "🏁 <b>زمان چالش امروز تمام شده است.</b>\nفردا ساعت ۱۲ دوباره بیا."
            )
            can_play = False
        else:
            challenge = await service.daily.ensure_today(now=current)
            game = await service.daily.participation(day, user_id)
            title = {
                "gol_ya_pooch": "گل یا پوچ",
                "tic_tac_toe": "دوز سه‌تایی",
                "word_guess": "حدس کلمه",
                "mastermind": "فکر بکر",
            }[challenge.game_type.value]
            unit = (
                "دست"
                if challenge.game_type in (GameType.GOL_YA_POOCH, GameType.TIC_TAC_TOE)
                else "دور"
            )
            text = (
                f"🎯 <b>چالش امروز شروع شده!</b>\nبازی: <b>{title}</b> | "
                f"{challenge.total_hands} {unit}\nتا ساعت ۱۳ فرصت داری. "
                "اگر ربات را شکست بدهی، یک امتیاز می‌گیری."
            )
            if game is not None:
                if game.status is GameStatus.FINISHED:
                    text += "\n\n✅ بازی امروزت تمام شده و تلاش دیگری نداری."
                elif game.status is GameStatus.ACTIVE:
                    text += "\n\n🔄 بازی امروزت شروع شده؛ از همان‌جا ادامه بده."
                else:
                    text += "\n\n🏁 تلاش امروزت بسته شده است."
            can_play = game is None or game.status is GameStatus.ACTIVE
        await safe_edit(
            callback,
            text,
            daily_challenge_keyboard(
                open_now=status == "open",
                reminder_enabled=reminder is not None,
                can_play=can_play,
            ),
        )

    @router.callback_query(F.data == "menu:daily_challenge")
    async def daily_menu(callback: CallbackQuery) -> None:
        await show_daily(callback)
        await callback.answer()

    @router.callback_query(F.data == "daily:play")
    async def daily_play(callback: CallbackQuery, bot: Bot) -> None:
        try:
            advance = await service.start_daily_challenge(callback.from_user.id)
        except GameError as error:
            await callback.answer(game_error_text(error), show_alert=True)
            await show_daily(callback)
            return
        prefix = "🎯 <b>چالش روزانهٔ امروز</b>"
        if advance.messages:
            prefix += "\n" + "\n".join(advance.messages[-8:])
        await presenter.edit_game_view(callback, bot, advance.game, prefix, fresh=True)
        await callback.answer("چالش آماده است ✅")

    @router.callback_query(F.data.in_({"daily:reminder:on", "daily:reminder:off"}))
    async def daily_reminder(callback: CallbackQuery) -> None:
        if countdown_service is None:
            await callback.answer("یادآور فعلاً در دسترس نیست.", show_alert=True)
            return
        if callback.data == "daily:reminder:on":
            try:
                await countdown_service.subscribe_daily_reminder(callback.from_user.id)
            except GameError as error:
                await callback.answer(game_error_text(error), show_alert=True)
                return
            note = "یادآور روزانه برای ساعت ۱۱:۵۵ فعال شد. 🔔"
        else:
            await countdown_service.cancel_daily_reminder(callback.from_user.id)
            note = "یادآور روزانه لغو شد. 🔕"
        if countdown_scheduler is not None:
            countdown_scheduler.wake()
        await show_daily(callback)
        await callback.answer(note)
