
import asyncio
import logging
import os
import re
import html
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from aiogram.exceptions import TelegramBadRequest

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else None

DATABASE_PATH = os.getenv(
    "DATABASE_PATH", "/app/data/voices.db"
)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "25"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger(__name__)


async def init_db():
    Path(DATABASE_PATH).parent.mkdir(
        parents=True, exist_ok=True
    )

    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row

    await db.execute("""
        CREATE TABLE IF NOT EXISTS voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            play_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            owner_id INTEGER PRIMARY KEY,
            stage TEXT NOT NULL,
            file_id TEXT
        )
    """)

    await db.commit()
    return db


def owner(message: Message):
    return (
        message.from_user is not None
        and message.from_user.id == OWNER_ID
    )


def private(message: Message):
    return message.chat.type == "private"


async def get_pending(db):
    cur = await db.execute(
        "SELECT * FROM pending WHERE owner_id=?",
        (OWNER_ID,)
    )
    return await cur.fetchone()


async def voice_count(db):
    cur = await db.execute(
        "SELECT COUNT(*) AS total FROM voices"
    )
    return (await cur.fetchone())["total"]


async def add_voice_start(message: Message, db):
    if not owner(message):
        await message.answer(
            "Добавлять голосовые может только администратор."
        )
        return

    if not private(message):
        await message.answer(
            "Добавление доступно только в личке с ботом."
        )
        return

    await db.execute("""
        INSERT INTO pending(owner_id, stage, file_id)
        VALUES(?, 'voice', NULL)
        ON CONFLICT(owner_id)
        DO UPDATE SET stage='voice', file_id=NULL
    """, (OWNER_ID,))

    await db.commit()

    await message.answer(
        "Отправь мне голосовое сообщение.\n\n"
        "После этого я попрошу написать его название.\n\n"
        "Для отмены: /cancel"
    )


async def receive_voice(message: Message, db):
    if not owner(message) or not private(message):
        return

    pending = await get_pending(db)

    if not pending or pending["stage"] != "voice":
        return

    await db.execute("""
        UPDATE pending
        SET stage='title', file_id=?
        WHERE owner_id=?
    """, (message.voice.file_id, OWNER_ID))

    await db.commit()

    await message.answer(
        "Голосовое получено!\n\n"
        "Теперь напиши его название."
    )


async def receive_title(message: Message, db):
    if not owner(message) or not private(message):
        return False

    pending = await get_pending(db)

    if not pending or pending["stage"] != "title":
        return False

    title = (message.text or "").strip()

    if not title or title.startswith("/"):
        await message.answer(
            "Напиши название обычным текстом."
        )
        return True

    if len(title) > 200:
        await message.answer(
            "Название слишком длинное. Максимум 200 символов."
        )
        return True

    cur = await db.execute("""
        INSERT INTO voices(title, file_id)
        VALUES(?, ?)
    """, (title, pending["file_id"]))

    voice_id = cur.lastrowid

    await db.execute(
        "DELETE FROM pending WHERE owner_id=?",
        (OWNER_ID,)
    )

    await db.commit()

    await message.answer(
        f"✅ Голосовое сохранено!\n\n"
        f"Номер: №{voice_id}\n"
        f"Название: {title}\n"
        f"Команда вызова: /{voice_id}"
    )

    return True


async def cancel(message: Message, db):
    if not owner(message) or not private(message):
        await message.answer("Недостаточно прав.")
        return

    await db.execute(
        "DELETE FROM pending WHERE owner_id=?",
        (OWNER_ID,)
    )
    await db.commit()

    await message.answer("Добавление отменено.")


async def send_voice(message: Message, db, voice_id: int):
    cur = await db.execute(
        "SELECT file_id FROM voices WHERE id=?",
        (voice_id,)
    )
    row = await cur.fetchone()

    if not row:
        await message.answer(
            f"Голосовое №{voice_id} не найдено."
        )
        return

    await message.answer_voice(row["file_id"])

    await db.execute("""
        UPDATE voices
        SET play_count = play_count + 1
        WHERE id=?
    """, (voice_id,))

    await db.commit()


async def show_voices(db, page=1):
    total = await voice_count(db)

    pages = max(
        1, (total + PAGE_SIZE - 1) // PAGE_SIZE
    )

    page = max(1, min(page, pages))
    offset = (page - 1) * PAGE_SIZE

    cur = await db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY id
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset))

    rows = await cur.fetchall()

    if not rows:
        return (
            "Библиотека пока пустая.",
            None
        )

    lines = ["<b>Голосовые сообщения</b>\n"]

    for row in rows:
        title = html.escape(row["title"])
        lines.append(
            f"/{row['id']}. {title} "
            f"({row['play_count']})"
        )

    lines.append(
        f"\nСтраница {page} из {pages}"
        f" · Всего: {total}"
    )

    keyboard = None

    if pages > 1:
        buttons = []

        if page > 1:
            buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"page:{page-1}"
                )
            )

        if page < pages:
            buttons.append(
                InlineKeyboardButton(
                    text="Вперёд ➡️",
                    callback_data=f"page:{page+1}"
                )
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[buttons]
        )

    return "\n".join(lines), keyboard


async def delete_voice(message: Message, db):
    if not owner(message) or not private(message):
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Используй: /deletevoice 5"
        )
        return

    voice_id = int(parts[1])

    cur = await db.execute(
        "DELETE FROM voices WHERE id=?",
        (voice_id,)
    )

    await db.commit()

    if cur.rowcount:
        await message.answer(
            f"Запись №{voice_id} удалена.\n"
            "Остальные номера не изменились."
        )
    else:
        await message.answer("Запись не найдена.")


async def rename_voice(message: Message, db):
    if not owner(message) or not private(message):
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split(maxsplit=2)

    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Используй: /renamevoice 5 Новое название"
        )
        return

    voice_id = int(parts[1])
    title = parts[2].strip()

    if not title or len(title) > 200:
        await message.answer(
            "Название должно содержать от 1 до 200 символов."
        )
        return

    cur = await db.execute(
        "UPDATE voices SET title=? WHERE id=?",
        (title, voice_id)
    )

    await db.commit()

    if cur.rowcount:
        await message.answer(
            f"Название записи №{voice_id} изменено."
        )
    else:
        await message.answer("Запись не найдена.")


async def statistics(message: Message, db):
    if not owner(message) or not private(message):
        await message.answer("Недостаточно прав.")
        return

    cur = await db.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(play_count), 0) AS plays
        FROM voices
    """)

    row = await cur.fetchone()

    cur = await db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY play_count DESC
        LIMIT 5
    """)

    top = await cur.fetchall()

    text = (
        "<b>Статистика</b>\n\n"
        f"Голосовых: {row['total']}\n"
        f"Прослушиваний: {row['plays']}\n"
    )

    if top:
        text += "\n<b>Топ-5:</b>\n"

        for item in top:
            text += (
                f"№{item['id']} — "
                f"{html.escape(item['title'])}: "
                f"{item['play_count']}\n"
            )

    await message.answer(text, parse_mode="HTML")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN.")

    if OWNER_ID is None:
        raise RuntimeError("Не задан OWNER_ID.")

    if PAGE_SIZE < 1 or PAGE_SIZE > 50:
        raise RuntimeError("PAGE_SIZE должен быть от 1 до 50.")

    db = await init_db()
    bot = Bot(BOT_TOKEN)
    me = await bot.get_me()

    dp = Dispatcher()

    await bot.set_my_commands([
        BotCommand(command="start", description="Информация"),
        BotCommand(command="addvoice", description="Добавить голосовое"),
        BotCommand(command="voices", description="Список голосовых"),
        BotCommand(command="randomvoice", description="Случайное голосовое"),
        BotCommand(command="deletevoice", description="Удалить голосовое"),
        BotCommand(command="renamevoice", description="Переименовать"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="cancel", description="Отменить добавление"),
    ])

    @dp.message(Command("start"))
    async def start(message: Message):
        await message.answer(
            "Привет! Я библиотека голосовых.\n\n"
            "/voices — список голосовых\n"
            "/randomvoice — случайное голосовое\n\n"
            "Чтобы вызвать запись, отправь её номер, "
            "например /1."
        )

    @dp.message(Command("addvoice"))
    async def add(message: Message):
        await add_voice_start(message, db)

    @dp.message(Command("cancel"))
    async def cancel_handler(message: Message):
        await cancel(message, db)

    @dp.message(Command("deletevoice"))
    async def delete_handler(message: Message):
        await delete_voice(message, db)

    @dp.message(Command("renamevoice"))
    async def rename_handler(message: Message):
        await rename_voice(message, db)

    @dp.message(Command("stats"))
    async def stats_handler(message: Message):
        await statistics(message, db)

    @dp.message(Command("voices"))
    async def voices_handler(message: Message):
        parts = (message.text or "").split(maxsplit=1)
        page = 1

        if len(parts) == 2 and parts[1].isdigit():
            page = int(parts[1])

        text, keyboard = await show_voices(db, page)

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

    @dp.callback_query(F.data.startswith("page:"))
    async def page_handler(callback: CallbackQuery):
        page = int(callback.data.split(":")[1])

        text, keyboard = await show_voices(db, page)

        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

        await callback.answer()

    @dp.message(Command("randomvoice"))
    async def random_handler(message: Message):
        cur = await db.execute(
            "SELECT id FROM voices ORDER BY RANDOM() LIMIT 1"
        )
        row = await cur.fetchone()

        if not row:
            await message.answer("Библиотека пока пустая.")
            return

        await send_voice(message, db, row["id"])

    @dp.message(F.voice)
    async def voice_handler(message: Message):
        await receive_voice(message, db)

    @dp.message(F.text)
    async def text_handler(message: Message):
        # Сначала проверяем, не ожидается ли название записи.
        if await receive_title(message, db):
            return

        # Вызов голосового командой /1, /2, /3...
        match = re.fullmatch(
            r"/([1-9][0-9]*)(?:@" +
            re.escape(me.username or "") + r")?",
            (message.text or "").strip(),
            re.IGNORECASE
        )

        if match:
            await send_voice(
                message,
                db,
                int(match.group(1))
            )

    log.info("Bot started as @%s", me.username)

    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
