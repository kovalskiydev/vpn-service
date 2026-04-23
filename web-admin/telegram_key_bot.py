#!/usr/bin/env python3
import asyncio
import html
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHANNEL = os.getenv("TG_CHANNEL", "").strip()  # @channel_username or -100...
TG_CHANNEL_URL = os.getenv("TG_CHANNEL_URL", "").strip()
PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "http://127.0.0.1:18081").rstrip("/")
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def channel_url() -> str:
    if TG_CHANNEL_URL:
        return TG_CHANNEL_URL
    ch = TG_CHANNEL.strip()
    if ch.startswith("@") and len(ch) > 1:
        return "https://t.me/" + ch[1:]
    return ""


def menu_markup() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub")],
        [InlineKeyboardButton(text="Получить ключ на 7 дней", callback_data="get_key")],
        [
            InlineKeyboardButton(text="Гайд по Happ", callback_data="guide_happ"),
            InlineKeyboardButton(text="О боте", callback_data="about_bot"),
        ],
    ]
    ch_url = channel_url()
    if ch_url:
        rows.append([InlineKeyboardButton(text="Перейти в канал", url=ch_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_wait(seconds: int) -> str:
    seconds = max(1, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    return " ".join(parts) if parts else "<1м"


def text_welcome() -> str:
    ch = html.escape(TG_CHANNEL)
    return (
        "<b>BURMALDAA VPN BOT</b>\n\n"
        "Этот бот выдает VPN-ключ после подписки на канал.\n\n"
        "Условия:\n"
        "1. Нужно быть подписанным на канал.\n"
        "2. Ключ выдается на 7 дней.\n"
        "3. Новый ключ можно получить раз в 7 дней.\n\n"
        f"Канал для подписки: <b>{ch}</b>\n"
        "Нажмите кнопку ниже."
    )


def text_about() -> str:
    return (
        "<b>О боте</b>\n\n"
        "Бот автоматически проверяет подписку на канал и выдает персональную VPN-подписку.\n"
        "Срок действия каждого ключа: 7 дней.\n"
        "Повторная выдача: через 7 дней.\n\n"
        "Формат ссылки подходит для Happ и других клиентов с подписками."
    )


def text_happ_guide() -> str:
    return (
        "<b>Гайд: как подключить в Happ</b>\n\n"
        "1. Нажмите «Получить ключ на 7 дней».\n"
        "2. Скопируйте ссылку из ответа бота.\n"
        "3. Откройте Happ -> Добавить подписку.\n"
        "4. Вставьте ссылку и сохраните.\n"
        "5. Обновите подписку и подключитесь к любой локации.\n\n"
        "Если не подключается:\n"
        "- Проверьте интернет.\n"
        "- Обновите подписку вручную в Happ.\n"
        "- Убедитесь, что срок ключа еще не истек."
    )


async def check_channel_member(bot: Bot, user_id: int) -> bool:
    member = await bot.get_chat_member(chat_id=TG_CHANNEL, user_id=user_id)
    status = str(member.status).lower()
    return status in {"member", "administrator", "creator"}


async def request_issue_key(user: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "telegram_user_id": int(user.get("id") or 0),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "base_url": PUBLIC_BASE_URL or None,
    }
    url = PANEL_BASE_URL + "/api/bot/issue-key"
    headers = {
        "Content-Type": "application/json",
        "X-Bot-Token": BOT_API_TOKEN,
    }
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            raw = await resp.text()
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"detail": raw}
            if resp.status >= 400:
                detail = data.get("detail") or data.get("error") or raw or f"HTTP {resp.status}"
                raise RuntimeError(f"Backend error: {detail}")
            return data


async def process_key_issue(bot: Bot, user: dict[str, Any]) -> tuple[bool, str]:
    uid = int(user.get("id") or 0)
    if uid <= 0:
        return False, "Не удалось определить ваш Telegram ID."

    try:
        is_member = await check_channel_member(bot, uid)
    except Exception:
        return False, "Не удалось проверить подписку на канал. Попробуйте позже."

    if not is_member:
        return False, (
            "Вы еще не подписаны на канал.\n"
            f"Подпишитесь на <b>{html.escape(TG_CHANNEL)}</b> и нажмите «Проверить подписку»."
        )

    try:
        issued = await request_issue_key(user)
    except Exception as exc:
        return False, f"Ошибка выдачи ключа: {html.escape(str(exc))}"

    if not issued.get("ok"):
        if issued.get("reason") == "cooldown":
            wait = format_wait(int(issued.get("retry_after_seconds") or 0))
            return False, f"Новый ключ можно получить через <b>{wait}</b>."
        return False, "Сейчас не удалось выдать ключ. Попробуйте позже."

    link = str(issued.get("happ_url") or issued.get("subscription_url") or "").strip()
    expires_at = html.escape(str(issued.get("expires_at") or ""))
    if not link:
        return False, "Ключ создан, но ссылка не получена. Обратитесь в поддержку."

    return True, (
        "<b>Ключ успешно создан</b>\n\n"
        f"Действует до: <b>{expires_at}</b>\n\n"
        "Ссылка подписки:\n"
        f"{html.escape(link)}\n\n"
        "Откройте Happ и добавьте эту ссылку как подписку."
    )


async def render_screen(
    bot: Bot,
    chat_id: int,
    user: dict[str, Any],
    screen: str,
    message_id: int | None = None,
) -> None:
    if screen == "about_bot":
        text = text_about()
    elif screen == "guide_happ":
        text = text_happ_guide()
    elif screen == "check_sub":
        uid = int(user.get("id") or 0)
        try:
            ok = await check_channel_member(bot, uid)
            if ok:
                text = "Подписка на канал подтверждена.\nМожно получать ключ."
            else:
                text = f"Подписка не найдена.\nПодпишитесь на <b>{html.escape(TG_CHANNEL)}</b> и повторите проверку."
        except Exception:
            text = "Не удалось проверить подписку на канал. Попробуйте позже."
    elif screen == "get_key":
        _, text = await process_key_issue(bot, user)
    else:
        text = text_welcome()

    kb = menu_markup()
    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, disable_web_page_preview=True)


def user_to_dict(user: Any) -> dict[str, Any]:
    return {
        "id": int(getattr(user, "id", 0) or 0),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
    }


async def main() -> None:
    if not TG_BOT_TOKEN:
        raise SystemExit("TG_BOT_TOKEN is required")
    if not TG_CHANNEL:
        raise SystemExit("TG_CHANNEL is required")
    if not BOT_API_TOKEN:
        raise SystemExit("BOT_API_TOKEN is required")

    print(f"[{now_iso()}] aiogram bot started")

    bot = Bot(token=TG_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    @dp.message(CommandStart())
    @dp.message(Command("help"))
    @dp.message(Command("menu"))
    async def cmd_start(message: Message) -> None:
        await render_screen(bot, message.chat.id, user_to_dict(message.from_user), "welcome")

    @dp.message(Command("key"))
    async def cmd_key(message: Message) -> None:
        await render_screen(bot, message.chat.id, user_to_dict(message.from_user), "get_key")

    @dp.message(Command("guide"))
    async def cmd_guide(message: Message) -> None:
        await render_screen(bot, message.chat.id, user_to_dict(message.from_user), "guide_happ")

    @dp.message(Command("about"))
    async def cmd_about(message: Message) -> None:
        await render_screen(bot, message.chat.id, user_to_dict(message.from_user), "about_bot")

    @dp.callback_query(F.data.in_({"welcome", "check_sub", "get_key", "guide_happ", "about_bot"}))
    async def cb_actions(query: CallbackQuery) -> None:
        try:
            await query.answer()
        except Exception:
            pass
        msg = query.message
        if msg is None:
            return
        user = user_to_dict(query.from_user)
        await render_screen(
            bot,
            msg.chat.id,
            user,
            str(query.data or "welcome"),
            message_id=msg.message_id,
        )

    @dp.message()
    async def fallback(message: Message) -> None:
        await render_screen(bot, message.chat.id, user_to_dict(message.from_user), "welcome")

    await dp.start_polling(bot)


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[{now_iso()}] polling error: {exc}")
            time.sleep(max(1, int(os.getenv("TG_POLL_SLEEP_ON_ERROR", "3"))))
