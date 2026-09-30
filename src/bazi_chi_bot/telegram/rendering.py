"""Persian text and native Telegram inline keyboards."""

from __future__ import annotations

from html import escape

from ..leaderboard import next_leaderboard_reset_label
from ..models import (
    FinalChoice,
    Game,
    GamePhase,
    GameStatus,
    GameType,
    LeaderboardEntry,
    MastermindGuess,
    WordGuess,
)
from .keyboards import board_text
from ..rules import MASTERMIND_COLOR_EMOJIS, MASTERMIND_MAX_ATTEMPTS
from .texts import FINAL_LABELS, TRUTH_OR_DARE_RULE

GAME_TEXT_PREVIEW_LENGTH = 1200


def game_text_preview(text: str) -> str:
    preview = escape(text[:GAME_TEXT_PREVIEW_LENGTH])
    if len(text) > GAME_TEXT_PREVIEW_LENGTH:
        preview += "\n… متن کامل در پیام جداگانهٔ همین بازی آمده است."
    return preview


def word_guess_board(guesses: tuple[WordGuess, ...]) -> str:
    icons = {"g": "🟩", "y": "🟨", "b": "⬛"}
    # A 20-letter word can be guessed 20 times; all rows plus a result banner
    # exceed Telegram's single-message limit. Keep the useful recent history.
    recent = guesses[-8:]
    earlier = f"… {len(guesses) - len(recent)} حدس قبلی\n" if len(guesses) > 8 else ""
    return earlier + "\n".join(
        " ".join(
            f"{icons.get(status, '⬛')}<b>{escape(character)}</b>"
            for character, status in zip(item.text, item.feedback, strict=True)
        )
        for item in recent
    )


def mastermind_board(guesses: tuple[MastermindGuess, ...]) -> str:
    """Render color guesses and their black/white peg feedback."""
    return "\n".join(
        " ".join(MASTERMIND_COLOR_EMOJIS.get(color, "⚪") for color in guess.colors)
        + f"  ➜  ⚫ {guess.black} | ⚪ {guess.white}"
        for guess in guesses
    )


def render_game(
    game: Game,
    viewer_id: int,
    names: dict[int, str],
    invite_url: str | None = None,
) -> str:
    me = escape(names.get(viewer_id, "بازیکن"))
    opponent_id = game.opponent_of(viewer_id)
    opponent = escape(names.get(opponent_id, "منتظر بازیکن")) if opponent_id else "منتظر بازیکن"

    score = ""
    if game.player2_id is not None:
        score = (
            "\n\n<b>📊 جدول امتیاز</b>\n"
            f"• {me}: <b>{game.score_for(viewer_id)}</b>\n"
            f"• {opponent}: <b>{game.score_for(game.player2_id if viewer_id == game.creator_id else game.creator_id)}</b>"
        )

    header = f"🌸 <b>گل یا پوچ #{game.id}</b>\n✊ {game.fists} مشت | 🧮 {game.total_hands} دست"
    if game.game_type is GameType.TIC_TAC_TOE:
        unit = "دست" if game.is_solo else "دستِ امتیازدار"
        header = f"❌⭕ <b>دوز سه‌تایی #{game.id}</b>\n🧮 {game.total_hands} {unit}"
    elif game.game_type is GameType.TRUTH_OR_DARE:
        header = f"🎭 <b>جرئت یا حقیقت #{game.id}</b>\n🧮 {game.total_hands} دور"
    elif game.game_type is GameType.WORD_GUESS:
        header = f"🔤 <b>حدس کلمه #{game.id}</b>\n🧮 {game.total_hands} دور"
    elif game.game_type is GameType.MASTERMIND:
        header = (
            f"🎨 <b>فکر بکر #{game.id}</b>\n"
            f"🧮 {game.total_hands} دور | 🎯 {MASTERMIND_MAX_ATTEMPTS} تلاش در هر دور"
        )

    if game.status is GameStatus.WAITING:
        link = (
            f'\n\n🔗 <a href="{escape(invite_url, quote=True)}">لینک ورود به بازی</a>'
            if invite_url
            else ""
        )
        return f"{header}\n\n⏳ <b>منتظر هم‌بازی</b>\nلینک دعوت را برای دوستت بفرست.{link}"

    round_name = (
        "دور"
        if game.game_type in {GameType.TRUTH_OR_DARE, GameType.WORD_GUESS, GameType.MASTERMIND}
        else "دست"
    )
    hand = f"\n\n🎲 {round_name} <b>{game.hand_number}</b> از <b>{game.total_hands}</b>"
    if game.is_solo and game.status is GameStatus.FINISHED:
        if game.winner_id == viewer_id:
            state = "🏆 <b>ربات را بردی!</b> یک امتیاز به مجموع امتیازهایت اضافه شد."
        elif game.winner_id is None:
            state = "🤝 <b>بازی با ربات مساوی شد.</b> امتیازی ثبت نشد."
        else:
            state = "🏁 <b>این بار ربات برنده شد.</b> امتیازی ثبت نشد."
        if game.game_type is GameType.TIC_TAC_TOE:
            state += f"\n\n{board_text(game.board)}"
    elif game.game_type is GameType.TIC_TAC_TOE and game.status is GameStatus.ACTIVE:
        symbol = "❌" if viewer_id == game.creator_id else "⭕"
        state = (
            f"🎯 <b>نوبت توست</b>\nبا {symbol} یک خانهٔ خالی را انتخاب کن."
            if game.next_player_id == viewer_id
            else f"⏳ <b>نوبت {opponent}</b> است؛ منتظر حرکتش باش."
        )
        state += f"\n\nعلامت تو: <b>{symbol}</b>\n\n{board_text(game.board)}"
    elif game.game_type is GameType.WORD_GUESS and game.phase is GamePhase.HIDING:
        state = (
            "🔐 <b>نوبت توست کلمهٔ مخفی را انتخاب کنی.</b>\n"
            "یک کلمهٔ ۲ تا ۲۰ حرفی و بدون فاصله همینجا بفرست."
            if game.hider_id == viewer_id
            else f"⏳ {opponent} در حال انتخاب کلمهٔ مخفی است..."
        )
    elif game.game_type is GameType.WORD_GUESS and game.phase is GamePhase.GUESSING:
        if game.word_secret is None:
            state = "⏳ کلمهٔ مخفی در حال آماده‌شدن است."
        else:
            board = word_guess_board(game.word_guesses)
            history = f"\n\n{board}" if board else ""
            remaining = len(game.word_secret) - game.word_attempts
            legend = "\n\n🟩 جای درست  |  🟨 جای نادرست  |  ⬛ ناموجود"
            if game.guesser_id == viewer_id:
                state = (
                    f"🔎 <b>یک کلمهٔ {len(game.word_secret)} حرفی را حدس بزن.</b>\n"
                    f"فرصت باقی‌مانده: <b>{remaining}</b>\n"
                    "حدست را همینجا برای ربات بفرست."
                    f"{history}{legend}"
                )
            else:
                state = (
                    f"🔐 کلمهٔ تو: <b>{escape(game.word_secret)}</b>\n"
                    f"حدس‌های باقی‌ماندهٔ {opponent}: <b>{remaining}</b>\n"
                    "منتظر حدس هم‌بازی‌ات باش."
                    f"{history}{legend}"
                )
    elif game.game_type is GameType.MASTERMIND and game.phase is GamePhase.HIDING:
        chosen = " ".join(
            MASTERMIND_COLOR_EMOJIS.get(color, "⚪") for color in game.mastermind_draft
        )
        progress = f"\nانتخاب فعلی: {chosen}" if chosen else ""
        state = (
            "🎨 <b>کد چهاررنگ را بساز.</b>\n"
            "رنگ‌ها: 🔵 آبی، 🟡 زرد، ⚫ مشکی، ⚪ سفید، 🔴 قرمز، 🟢 سبز\n"
            "چهار مهره انتخاب کن؛ تکرار رنگ هم مجاز است."
            f"{progress}"
            if game.hider_id == viewer_id
            else f"⏳ {opponent} در حال ساختن کد مخفی است..."
        )
    elif game.game_type is GameType.MASTERMIND and game.phase is GamePhase.GUESSING:
        board = mastermind_board(game.mastermind_guesses)
        history = f"\n\n{board}" if board else ""
        remaining = MASTERMIND_MAX_ATTEMPTS - game.mastermind_attempts
        progress = (
            f"\n🎯 تلاش باقی‌مانده: <b>{remaining}</b> | "
            f"📋 ردیف‌های انجام‌شده: <b>{len(game.mastermind_guesses)}</b> از "
            f"<b>{MASTERMIND_MAX_ATTEMPTS}</b>"
        )
        draft = " ".join(MASTERMIND_COLOR_EMOJIS.get(color, "⚪") for color in game.mastermind_draft)
        draft_text = f"\nانتخاب فعلی: {draft}" if draft else ""
        legend = "\n\n⚫ سیاه: رنگ و جای درست  |  ⚪ سفید: رنگ درست، جای اشتباه"
        if game.guesser_id == viewer_id:
            state = (
                "🎨 <b>کد چهاررنگ را حدس بزن.</b>\n"
                f"{progress}\n"
                "رنگ‌ها: 🔵 آبی، 🟡 زرد، ⚫ مشکی، ⚪ سفید، 🔴 قرمز، 🟢 سبز\n"
                "چهار رنگ را به‌ترتیب انتخاب کن."
                f"{draft_text}{history}{legend}"
            )
        else:
            secret = " ".join(
                MASTERMIND_COLOR_EMOJIS.get(color, "⚪")
                for color in (game.mastermind_secret or ())
            )
            state = (
                f"🔐 کد تو: <b>{secret}</b>\n"
                f"🎯 تلاش‌های باقی‌ماندهٔ {opponent}: <b>{remaining}</b> | "
                f"📋 ردیف‌های انجام‌شده: <b>{len(game.mastermind_guesses)}</b> از "
                f"<b>{MASTERMIND_MAX_ATTEMPTS}</b>\n"
                "منتظر حدس هم‌بازی‌ات باش."
                f"{history}{legend}"
            )
    elif game.game_type is GameType.TRUTH_OR_DARE and game.phase is GamePhase.CHOICE:
        if game.challenge_kind is not None:
            label = FINAL_LABELS[game.challenge_kind]
            state = (
                f"🎭 <b>{label}</b> انتخاب شد.\n"
                "سؤال تازه‌ای در بانک نمانده؛ متن سؤال یا چالش را همینجا بفرست."
                if game.challenge_asker_id == viewer_id
                else f"⏳ هم‌بازی‌ات <b>{label}</b> را انتخاب کرده و در حال نوشتن متن است."
            )
        else:
            state = (
                "🎭 <b>نوبت انتخاب توست</b>\nبرای هم‌بازی‌ات «حقیقت» یا «جرئت» را انتخاب کن."
                if game.challenge_asker_id == viewer_id
                else "⏳ منتظر انتخاب «حقیقت» یا «جرئت» از طرف هم‌بازی‌ات باش."
            )
    elif game.game_type is GameType.TRUTH_OR_DARE and game.phase is GamePhase.GUESSING:
        prompt = game_text_preview(game.challenge_prompt_text or "")
        challenge_label = FINAL_LABELS.get(game.challenge_kind, "جرئت یا حقیقت")
        state = (
            f"🎭 <b>{challenge_label}</b>\n\n{prompt}\n\n✍️ پاسخت را همینجا بفرست."
            if game.challenge_respondent_id == viewer_id
            else f"🎭 <b>سؤال یا چالش:</b>\n{prompt}\n\n⏳ منتظر پاسخ هم‌بازی‌ات باش."
        )
    elif game.game_type is GameType.TRUTH_OR_DARE and game.phase is GamePhase.FINISHED:
        state = (
            f"🎭 <b>سؤال یا چالش:</b>\n{game_text_preview(game.challenge_prompt_text or '')}"
            f"\n\n📩 <b>پاسخ:</b>\n{game_text_preview(game.challenge_response_text or '')}"
        )
        if game.challenge_approved is True:
            if game.challenge_respondent_id == viewer_id:
                state += "\n\n✅ تأیید شد؛ یک امتیاز گرفتی."
            else:
                respondent = escape(names.get(game.challenge_respondent_id, "پاسخ‌دهنده"))
                state += f"\n\n✅ تأیید شد؛ {respondent} یک امتیاز گرفت."
        elif game.challenge_approved is False:
            state += "\n\n❌ تأیید نشد؛ امتیازی ثبت نشد."
        elif game.challenge_asker_id == viewer_id:
            state += "\n\nپاسخ درست انجام شده؟ آن را ارزیابی کن."
        else:
            state += "\n\n⏳ منتظر ارزیابی هم‌بازی‌ات باش."
        if game.status is GameStatus.FINISHED and game.challenge_approved is not None:
            if game.winner_id == viewer_id:
                state += "\n\n🏆 <b>بازی تمام شد؛ برنده شدی!</b>"
            elif game.loser_id == viewer_id:
                state += "\n\n🏁 <b>بازی تمام شد؛ این بار باختی.</b>"
            else:
                state += "\n\n🤝 <b>بازی با نتیجهٔ مساوی تمام شد.</b>"
    elif game.phase is GamePhase.HIDING:
        if game.hider_id == viewer_id:
            state = "🌺 <b>گل را پنهان کن</b>\nگل را در کدام مشت می‌گذاری؟"
        else:
            state = f"⏳ {opponent} در حال پنهان‌کردن گل است..."
    elif game.phase is GamePhase.GUESSING:
        if game.guesser_id == viewer_id:
            state = "🔎 <b>نوبت حدس توست</b>\nگل در کدام مشت است؟ فقط یک فرصت داری."
        else:
            state = f"✅ انتخابت ثبت شد.\nمنتظر حدس {opponent} باش."
    elif game.phase is GamePhase.CHOICE:
        if game.loser_id == viewer_id:
            state = f"🎭 <b>بازی تمام شد</b>\nاین بار باختی؛ انتخاب آخر با توست.\n\n{TRUTH_OR_DARE_RULE}"
        else:
            state = (
                f"🏆 <b>برنده شدی!</b>\nمنتظر انتخاب «حقیقت» یا «جرئت» توسط {opponent} باش.\n\n"
                f"{TRUTH_OR_DARE_RULE}"
            )
    elif game.phase is GamePhase.FINISHED:
        choice = game.final_choice
        label = FINAL_LABELS.get(choice, "انتخاب نامشخص")
        if game.final_question_id is not None:
            prompt = game_text_preview(game.final_prompt_text or "")
            state = f"🎭 <b>{label}</b>\n\n<b>متن سؤال یا چالش:</b>\n{prompt}"
            if game.final_response_text is not None:
                state += f"\n\n📩 <b>پاسخ:</b>\n{game_text_preview(game.final_response_text)}"
            elif game.loser_id == viewer_id:
                state += "\n\n✍️ جوابت را همینجا بفرست تا برای برنده ارسال شود."
            else:
                state += "\n\n🏆 برنده شدی؛ منتظر پاسخ هم‌بازی‌ات باش."
        elif game.final_response_text is not None:
            if game.winner_id == viewer_id:
                state = (
                    f"🏆 <b>تو برنده شدی!</b>\n"
                    f"🎭 انتخاب بازنده: <b>{label}</b>\n"
                    "✅ پاسخ بازنده دریافت شد."
                )
            else:
                state = f"🎭 انتخابت: <b>{label}</b>\n✅ پاسخت برای برنده ارسال شد."
        elif game.final_prompt_text is not None:
            if game.loser_id == viewer_id:
                state = (
                    f"🎭 انتخابت: <b>{label}</b>\n\n"
                    "📨 سؤال یا چالش برنده رسید. جوابت را همینجا برای ربات بفرست."
                )
            else:
                state = (
                    f"🏆 <b>تو برنده شدی!</b>\n"
                    f"🎭 انتخاب بازنده: <b>{label}</b>\n"
                    "⏳ سؤال یا چالشت ارسال شد؛ منتظر پاسخ بازنده باش."
                )
        elif game.winner_id == viewer_id:
            instruction = (
                "از بازنده یک سؤال بپرس؛ او باید صادقانه جواب بدهد. 🗣"
                if choice is FinalChoice.TRUTH
                else "یک چالش امن و محترمانه برای بازنده تعیین کن. 🔥"
            )
            state = (
                f"🏆 <b>تو برنده شدی!</b>\n"
                f"🎭 انتخاب بازنده: <b>{label}</b>\n"
                f"👉 {instruction}\n\n"
                "متن سؤال یا چالش را همینجا برای ربات بفرست."
            )
        else:
            state = (
                f"🎭 انتخابت ثبت شد: <b>{label}</b>\n"
                "حالا منتظر سؤال یا چالش برنده باش و جوانمردانه انجامش بده! 🤝"
            )
    elif game.phase is GamePhase.CANCELLED:
        state = (
            "🏁 زمان چالش روزانه به پایان رسید؛ این بازی دیگر امتیاز ندارد."
            if game.daily_challenge_date is not None
            else "❌ این بازی لغو شده است."
        )
    else:
        state = "⏳ وضعیت بازی در حال به‌روزرسانی است."

    if game.status is GameStatus.FINISHED and game.final_response_text is not None:
        if game.final_response_approved is True:
            state += (
                "\n\n✅ انجام جرئت یا حقیقت تأیید شد؛ ۱ امتیاز به مجموع امتیاز پاسخ‌دهنده اضافه شد."
            )
        elif game.final_response_approved is False:
            state += "\n\n❌ انجام جرئت یا حقیقت تأیید نشد؛ امتیازی اضافه نشد."
        elif game.winner_id == viewer_id:
            state += "\n\nآیا جرئت یا حقیقت درست انجام شده؟ تأیید تو ۱ امتیاز به پاسخ‌دهنده می‌دهد."
        else:
            state += "\n\n⏳ منتظر تأیید هم‌بازی‌ات هستیم؛ با تأیید او ۱ امتیاز می‌گیری."

    return f"{header}{hand}{score}\n\n{state}"


def stats_text(
    display_name: str, games: int, wins: int, losses: int, correct: int, wrong: int, points: int
) -> str:
    total_guesses = correct + wrong
    accuracy = round(correct * 100 / total_guesses) if total_guesses else 0
    draws = max(games - wins - losses, 0)
    return f"""📊 <b>آمار {escape(display_name)}</b>

🎮 بازی‌های تمام‌شده: <b>{games}</b>
🏆 برد: <b>{wins}</b>   |   🤝 مساوی: <b>{draws}</b>   |   🌧 باخت: <b>{losses}</b>

⭐ مجموع امتیاز ذخیره‌شده: <b>{points}</b>

🌸 آمار گل یا پوچ
🎯 حدس درست: <b>{correct}</b>   |   🌀 حدس اشتباه: <b>{wrong}</b>
📈 دقت حدس: <b>{accuracy}٪</b>"""


def leaderboard_text(
    top_players: list[LeaderboardEntry],
    current_player: LeaderboardEntry | None,
    *,
    all_time: bool = False,
) -> str:
    """Render the podium and the requesting user's position."""
    if all_time:
        lines = [
            "🏅 <b>برترین بازیکن‌ها در همهٔ دوره‌ها</b>",
            "📚 این جدول مجموع امتیازهای تمام دوره‌ها را نشان می‌دهد.",
            "🎁 جایزهٔ نفر اول هر ماه بر اساس جدول ماهانه محاسبه می‌شود.",
            f"⏳ ریست بعدی جدول ماهانه (به وقت تهران): <b>{next_leaderboard_reset_label()}</b>",
            "",
            "سه نفر اول:",
        ]
    else:
        lines = [
            "🏆 <b>لیست برترین بازیکن‌ها</b>",
            "🎁 هر کاربر فعال با ورود ماهانه ۱ امتیاز جایزه می‌گیرد؛ هر ماه به نفر اول جایزه داده می‌شود.",
            "🔄 امتیازهای ماهانه در زمان ریست صفر می‌شوند؛ تاریخچهٔ امتیازها حفظ می‌شود.",
            f"⏳ ریست بعدی (به وقت تهران): <b>{next_leaderboard_reset_label()}</b>",
            "",
            "سه نفر اول:",
        ]
    if top_players:
        medals = ("🥇", "🥈", "🥉")
        for player in top_players:
            medal = medals[player.rank - 1] if 1 <= player.rank <= len(medals) else "🏅"
            lines.append(
                f"{medal} <b>{player.rank}. {escape(player.display_name)}</b> — "
                f"⭐ {player.points_won} امتیاز"
            )
    else:
        lines.append("هنوز بازیکنی امتیازی ثبت نکرده است.")

    if current_player is not None:
        lines.extend(
            [
                "",
                f"📍 جایگاه تو ({escape(current_player.display_name)}): "
                f"<b>رتبهٔ {current_player.rank}</b> — "
                f"⭐ {current_player.points_won} امتیاز",
            ]
        )
    return "\n".join(lines)
