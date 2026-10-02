"""Tournament setup: choose mode, count, then distinct games in play order."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..game import GameError, GameService
from ..models import GameType
from ..services.tournaments import DUO_TYPES, SOLO_TYPES, STAGE_HANDS
from .shared import GamePresenter, safe_edit, telegram_user

CODES = {
    "g": (GameType.GOL_YA_POOCH, "🌸 گل یا پوچ"),
    "t": (GameType.TIC_TAC_TOE, "❌⭕ دوز سه‌تایی"),
    "r": (GameType.ROCK_PAPER_SCISSORS, "✊✋✌️ سنگ، کاغذ، قیچی"),
    "w": (GameType.WORD_GUESS, "🔤 حدس کلمه"),
    "m": (GameType.MASTERMIND, "🎨 فکر بکر"),
    "h": (GameType.TRUTH_OR_DARE, "🎭 جرئت یا حقیقت"),
}


def _options(mode: str) -> tuple[str, ...]:
    return tuple(
        code
        for code, (kind, _) in CODES.items()
        if kind in (SOLO_TYPES if mode == "s" else DUO_TYPES)
    )


def _selection_keyboard(mode: str, count: int, selected: tuple[str, ...]):
    builder = InlineKeyboardBuilder()
    for code in _options(mode):
        if code not in selected:
            encoded = "".join((*selected, code))
            builder.button(text=CODES[code][1], callback_data=f"tour:pick:{mode}:{count}:{encoded}")
    builder.button(text="↩️ از ابتدا", callback_data="menu:tournament")
    builder.adjust(2)
    return builder.as_markup()


def register_handlers(router: Router, service: GameService) -> None:
    presenter = GamePresenter(service)

    @router.callback_query(F.data == "menu:tournament")
    async def tournament_menu(callback: CallbackQuery) -> None:
        current = await service.current_tournament(callback.from_user.id)
        if current is not None:
            tournament, game = current
            builder = InlineKeyboardBuilder()
            builder.button(text="▶️ ادامهٔ تورنومنت", callback_data="tour:resume")
            builder.button(text="🛑 لغو تورنومنت", callback_data=f"tour:cancel:ask:{tournament.id}")
            builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
            builder.adjust(1)
            await safe_edit(
                callback,
                f"🏆 <b>تورنومنت #{tournament.id}</b>\n"
                f"بازی {tournament.current_stage} از {len(tournament.game_types)}\n"
                f"برد بازی‌ها: {tournament.player1_score} - {tournament.player2_score}\n\n"
                f"وضعیت بازی جاری: {'منتظر دوست' if game.status.value == 'waiting' else 'در حال اجرا'}",
                builder.as_markup(),
            )
            await callback.answer()
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="🤝 با دوست", callback_data="tour:mode:d")
        builder.button(text="🤖 با ربات", callback_data="tour:mode:s")
        builder.button(text="🏠 منوی اصلی", callback_data="menu:home")
        builder.adjust(2, 1)
        await safe_edit(
            callback,
            "🏆 <b>تورنومنت تازه</b>\n\nاول حریف را انتخاب کن. "
            "بعد تعداد بازی‌های متفاوت و ترتیب آن‌ها را مشخص می‌کنی. "
            f"هر بازی {STAGE_HANDS} دست یا دور دارد؛ برندهٔ هر بازی یک برد تورنومنت می‌گیرد.",
            builder.as_markup(),
        )
        await callback.answer()

    @router.callback_query(F.data == "tour:resume")
    async def tournament_resume(callback: CallbackQuery, bot: Bot) -> None:
        current = await service.current_tournament(callback.from_user.id)
        if current is None:
            await callback.answer("تورنومنت بازی برای ادامه نداری.", show_alert=True)
            return
        await presenter.edit_game_view(
            callback, bot, current[1], "🏆 <b>ادامهٔ تورنومنت</b>", fresh=True
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tour:cancel:ask:"))
    async def tournament_cancel_ask(callback: CallbackQuery) -> None:
        try:
            tournament_id = int((callback.data or "").rsplit(":", 1)[-1])
        except ValueError:
            await callback.answer("تورنومنت معتبر نیست.", show_alert=True)
            return
        current = await service.current_tournament(callback.from_user.id)
        if current is None or current[0].id != tournament_id:
            await callback.answer("تورنومنت بازی پیدا نشد.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        builder.button(text="🛑 بله، لغو شود", callback_data=f"tour:cancel:yes:{tournament_id}")
        builder.button(text="↩️ بازگشت", callback_data="menu:tournament")
        builder.adjust(1)
        await safe_edit(callback, "🏆 تورنومنت و بازی جاری آن لغو شود؟", builder.as_markup())
        await callback.answer()

    @router.callback_query(F.data.startswith("tour:cancel:yes:"))
    async def tournament_cancel_yes(callback: CallbackQuery, bot: Bot) -> None:
        try:
            tournament_id = int((callback.data or "").rsplit(":", 1)[-1])
            game = await service.cancel_tournament(tournament_id, callback.from_user.id)
        except ValueError, GameError:
            await callback.answer(
                "لغو تورنومنت انجام نشد؛ وضعیت را دوباره باز کن.", show_alert=True
            )
            return
        await safe_edit(callback, "🛑 تورنومنت لغو شد. می‌توانی تورنومنت تازه‌ای بسازی.")
        opponent = game.opponent_of(callback.from_user.id)
        if opponent is not None and opponent > 0:
            await presenter.send_game_view(bot, game, opponent, "🛑 تورنومنت لغو شد.", fresh=True)
        await callback.answer("تورنومنت لغو شد")

    @router.callback_query(F.data.startswith("tour:mode:"))
    async def tournament_mode(callback: CallbackQuery) -> None:
        mode = (callback.data or "").rsplit(":", 1)[-1]
        if mode not in ("s", "d"):
            await callback.answer("حالت تورنومنت معتبر نیست.", show_alert=True)
            return
        maximum = len(_options(mode))
        builder = InlineKeyboardBuilder()
        for count in range(2, maximum + 1):
            builder.button(text=f"{count} بازی", callback_data=f"tour:count:{mode}:{count}")
        builder.button(text="↩️ بازگشت", callback_data="menu:tournament")
        builder.adjust(2)
        await safe_edit(
            callback,
            f"🏆 <b>چند نوع بازی متفاوت؟</b>\n\nبین ۲ تا {maximum} بازی انتخاب کن.",
            builder.as_markup(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tour:count:"))
    async def tournament_count(callback: CallbackQuery) -> None:
        try:
            _, _, mode, count_text = (callback.data or "").split(":")
            count = int(count_text)
            if mode not in ("s", "d") or not 2 <= count <= len(_options(mode)):
                raise ValueError
        except ValueError:
            await callback.answer("تعداد بازی معتبر نیست.", show_alert=True)
            return
        await safe_edit(
            callback,
            f"🏆 <b>انتخاب بازی‌ها به ترتیب</b>\n\nبازی اول را انتخاب کن؛ "
            f"در مجموع {count} بازی متفاوت اجرا می‌شود.",
            _selection_keyboard(mode, count, ()),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("tour:pick:"))
    async def tournament_pick(callback: CallbackQuery, bot: Bot) -> None:
        try:
            _, _, mode, count_text, encoded = (callback.data or "").split(":", 4)
            count = int(count_text)
            selected = tuple(encoded)
            allowed = _options(mode)
            if (
                mode not in ("s", "d")
                or not 2 <= count <= len(allowed)
                or not 1 <= len(selected) <= count
                or len(set(selected)) != len(selected)
                or any(code not in allowed for code in selected)
            ):
                raise ValueError
        except ValueError:
            await callback.answer("انتخاب بازی معتبر نیست.", show_alert=True)
            return
        if len(selected) < count:
            names = " ← ".join(CODES[code][1] for code in selected)
            await safe_edit(
                callback,
                f"🏆 <b>ترتیب انتخابی</b>\n{names}\n\n"
                f"بازی {len(selected) + 1} از {count} را انتخاب کن.",
                _selection_keyboard(mode, count, selected),
            )
            await callback.answer()
            return
        try:
            await service.save_user(telegram_user(callback.from_user))
            advance = await service.create_tournament(
                callback.from_user.id,
                mode == "s",
                tuple(CODES[code][0] for code in selected),
            )
        except GameError:
            await callback.answer(
                "یک تورنومنت باز داری یا انتخابت معتبر نیست. از «ادامهٔ بازی» وارد شو.",
                show_alert=True,
            )
            return
        prefix = "🏆 <b>تورنومنت شروع شد!</b>"
        if mode == "d":
            prefix += "\nلینک دعوت را برای دوستت بفرست؛ همهٔ بازی‌ها را با همان دوست انجام می‌دهی."
        if advance.messages:
            prefix += "\n" + "\n".join(advance.messages[-4:])
        await presenter.edit_game_view(callback, bot, advance.game, prefix, fresh=True)
        await callback.answer("تورنومنت آماده است ✅")
