import asyncio
import html
import logging
import os
import re
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultVoice,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "417850992").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/voices.db")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "25"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-library")


def is_owner(message: Message) -> bool:
    return bool(
        message.from_user
        and OWNER_ID
        and message.from_user.id == OWNER_ID
    )


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


async def get_columns(db: aiosqlite.Connection, table: str) -> set:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["name"] for row in rows}


async def init_db() -> aiosqlite.Connection:
    db_path = Path(DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA journal_mode=WAL")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            file_id TEXT NOT NULL,
            play_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cols = await get_columns(db, "voices")
    if "id" not in cols:
        raise RuntimeError("В старой таблице voices отсутствует колонка id.")

    file_col = next(
        (
            name for name in ("file_id", "telegram_file_id", "voice_file_id")
            if name in cols
        ),
        None,
    )
    if file_col is None:
        raise RuntimeError("Не найдена колонка file_id.")

    if "title" not in cols:
        await db.execute("ALTER TABLE voices ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        old_title = next((name for name in ("name", "voice_name", "command") if name in cols), None)
        if old_title:
            await db.execute(f'UPDATE voices SET title = "{old_title}" WHERE title = ""')
        else:
            await db.execute("UPDATE voices SET title = 'Голосовое ' || id WHERE title = ''")

    cols = await get_columns(db, "voices")
    if "play_count" not in cols:
        await db.execute("ALTER TABLE voices ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
        old_count = next((name for name in ("plays", "views", "count") if name in cols), None)
        if old_count:
            await db.execute(f'UPDATE voices SET play_count = COALESCE("{old_count}", 0)')

    cols = await get_columns(db, "voices")
    if "created_at" not in cols:
        await db.execute("ALTER TABLE voices ADD COLUMN created_at TEXT")
        await db.execute("UPDATE voices SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")

    if file_col != "file_id":
        await db.execute("ALTER TABLE voices ADD COLUMN file_id TEXT")
        await db.execute(f'UPDATE voices SET file_id = "{file_col}" WHERE file_id IS NULL')

    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            owner_id INTEGER PRIMARY KEY,
            stage TEXT NOT NULL,
            file_id TEXT
        )
    """)
    await db.commit()
    log.info("Database initialized: %s", db_path)
    return db


async def get_pending(db: aiosqlite.Connection):
    cursor = await db.execute("SELECT * FROM pending WHERE owner_id=?", (OWNER_ID,))
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def start_add(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message):
        await message.answer("Добавлять голосовые может только администратор.")
        return
    if not is_private(message):
        await message.answer("Добавление доступно только в личке с ботом.")
        return

    await db.execute("""
        INSERT INTO pending(owner_id, stage, file_id)
        VALUES(?, 'voice', NULL)
        ON CONFLICT(owner_id)
        DO UPDATE SET stage='voice', file_id=NULL
    """, (OWNER_ID,))
    await db.commit()
    await message.answer(
        "🎤 Отправь мне голосовое сообщение.\n\n"
        "После этого я попрошу написать его название.\n\n"
        "Для отмены: /cancel"
    )


async def cancel_add(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message) or not is_private(message):
        await message.answer("Недостаточно прав.")
        return
    await db.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
    await db.commit()
    await message.answer("❌ Добавление отменено.")


async def accept_voice(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message) or not is_private(message):
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
    await message.answer("✏️ Теперь отправь название голосового.\nМаксимум 200 символов.")


async def accept_title(message: Message, db: aiosqlite.Connection) -> bool:
    if not is_owner(message) or not is_private(message):
        return False
    pending = await get_pending(db)
    if not pending or pending["stage"] != "title":
        return False

    title = (message.text or "").strip()
    if not title:
        await message.answer("Название не должно быть пустым.")
        return True
    if len(title) > 200:
        await message.answer("Название слишком длинное. Максимум 200 символов.")
        return True
    if not pending["file_id"]:
        await db.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
        await db.commit()
        await message.answer("Файл голосового не найден. Начни заново: /addvoice")
        return True

    cursor = await db.execute(
        "INSERT INTO voices(title, file_id) VALUES(?, ?)",
        (title, pending["file_id"]),
    )
    voice_id = cursor.lastrowid
    await cursor.close()

    await db.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
    await db.commit()

    await message.answer(
        f"✅ Голосовое добавлено!\n\n"
        f"Номер: /{voice_id}\n"
        f"Название: {html.escape(title)}",
        parse_mode="HTML",
    )
    return True


async def send_voice(message: Message, db: aiosqlite.Connection, voice_id: int) -> None:
    cursor = await db.execute(
        "SELECT id, title, file_id FROM voices WHERE id=?", (voice_id,)
    )
    row = await cursor.fetchone()
    await cursor.close()

    if not row:
        await message.answer(f"❌ Голосовое №{voice_id} не найдено.")
        return

    try:
        await message.answer_voice(row["file_id"])
    except TelegramBadRequest:
        log.exception("Cannot send voice ID %s", voice_id)
        await message.answer("⚠️ Не удалось отправить голосовое. Возможно, файл больше недоступен.")
        return

    await db.execute(
        "UPDATE voices SET play_count = COALESCE(play_count, 0) + 1 WHERE id=?",
        (voice_id,),
    )
    await db.commit()


async def show_voices(db: aiosqlite.Connection, page: int = 1):
    cursor = await db.execute("SELECT COUNT(*) AS total FROM voices")
    total = (await cursor.fetchone())["total"]
    await cursor.close()

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    offset = (page - 1) * PAGE_SIZE

    cursor = await db.execute(
        "SELECT id, title, play_count FROM voices ORDER BY id LIMIT ? OFFSET ?",
        (PAGE_SIZE, offset),
    )
    rows = await cursor.fetchall()
    await cursor.close()

    if not rows:
        text = "📚 Библиотека пока пустая."
        keyboard = None
    else:
        lines = [f"<b>📚 Библиотека голосовых</b> (стр. {page}/{pages})", ""]
        for item in rows:
            title = html.escape(item["title"] or "Без названия")
            lines.append(f"/{item['id']}. {title} ({item['play_count'] or 0})")
        text = "\n".join(lines)

        keyboard = None
        if pages > 1:
            buttons = []
            if page > 1:
                buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"voices:{page - 1}"))
            buttons.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="voices:noop"))
            if page < pages:
                buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"voices:{page + 1}"))
            keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])

    return text, keyboard


async def delete_voice(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message) or not is_private(message):
        await message.answer("⛔ Недостаточно прав.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /deletevoice 5")
        return

    voice_id = int(parts[1])
    cursor = await db.execute("DELETE FROM voices WHERE id=?", (voice_id,))
    await db.commit()

    if cursor.rowcount:
        await message.answer(f"🗑 Голосовое №{voice_id} удалено.")
    else:
        await message.answer("❌ Запись не найдена.")
    await cursor.close()


async def rename_voice(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message) or not is_private(message):
        await message.answer("⛔ Недостаточно прав.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer("Использование:\n/renamevoice 5 Новое название")
        return

    voice_id = int(parts[1])
    title = parts[2].strip()
    if not title or len(title) > 200:
        await message.answer("Название должно содержать от 1 до 200 символов.")
        return

    cursor = await db.execute("UPDATE voices SET title=? WHERE id=?", (title, voice_id))
    await db.commit()

    if cursor.rowcount:
        await message.answer(f"✅ Голосовое №{voice_id} переименовано.")
    else:
        await message.answer("❌ Запись не найдена.")
    await cursor.close()


async def show_stats(message: Message, db: aiosqlite.Connection) -> None:
    if not is_owner(message) or not is_private(message):
        await message.answer("⛔ Недостаточно прав.")
        return

    cursor = await db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(play_count), 0) AS plays FROM voices")
    totals = await cursor.fetchone()
    await cursor.close()

    cursor = await db.execute("SELECT id, title, play_count FROM voices ORDER BY play_count DESC, id ASC LIMIT 5")
    top = await cursor.fetchall()
    await cursor.close()

    text = (
        "<b>📊 Статистика библиотеки</b>\n\n"
        f"Голосовых: <b>{totals['total']}</b>\n"
        f"Воспроизведений: <b>{totals['plays']}</b>"
    )
    if top:
        text += "\n\n<b>🏆 Топ-5:</b>\n"
        for item in top:
            title = html.escape(item["title"] or "Без названия")
            text += f"/{item['id']} — {title}: {item['play_count'] or 0}\n"

    await message.answer(text, parse_mode="HTML")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN.")
    if OWNER_ID is None:
        raise RuntimeError("OWNER_ID должен быть числовым Telegram ID.")
    if not 1 <= PAGE_SIZE <= 50:
        raise RuntimeError("PAGE_SIZE должен быть от 1 до 50.")

    db = await init_db()
    bot = Bot(BOT_TOKEN)
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
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "👋 Привет! Я библиотека голосовых.\n\n"
            "📌 В любом чате напиши @твой_бот — откроется список ▶️\n"
            "📚 /voices — список голосовых\n"
            "🎲 /randomvoice — случайное голосовое\n\n"
            "Для вызова записи отправь её номер, например /1."
        )

    @dp.message(Command("addvoice"))
    async def cmd_add(message: Message) -> None:
        await start_add(message, db)

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message) -> None:
        await cancel_add(message, db)

    @dp.message(Command("deletevoice"))
    async def cmd_delete(message: Message) -> None:
        await delete_voice(message, db)

    @dp.message(Command("renamevoice"))
    async def cmd_rename(message: Message) -> None:
        await rename_voice(message, db)

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        await show_stats(message, db)

    @dp.message(Command("voices"))
    async def cmd_voices(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        page = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 1
        text, keyboard = await show_voices(db, page)
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

    @dp.callback_query(F.data.startswith("voices:"))
    async def callback_voices(callback: CallbackQuery) -> None:
        value = callback.data.split(":", 1)[1]
        if value == "noop":
            await callback.answer()
            return
        text, keyboard = await show_voices(db, int(value))
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        await callback.answer()

    @dp.message(Command("randomvoice"))
    async def cmd_random(message: Message) -> None:
        cursor = await db.execute("SELECT id FROM voices ORDER BY RANDOM() LIMIT 1")
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            await message.answer("📚 Библиотека пока пустая.")
            return
        await send_voice(message, db, row["id"])

    # ============================================================
    #  📌 INLINE MODE — список с ▶️ при @твой_бот
    # ============================================================
    @dp.inline_query()
    async def inline_voice(query: InlineQuery) -> None:
        text = (query.query or "").strip().lower()

        if text:
            cursor = await db.execute(
                "SELECT id, title, file_id, play_count FROM voices "
                "WHERE LOWER(title) LIKE ? ORDER BY id LIMIT 50",
                (f"%{text}%",),
            )
        else:
            cursor = await db.execute(
                "SELECT id, title, file_id, play_count FROM voices ORDER BY id LIMIT 50"
            )
        rows = await cursor.fetchall()
        await cursor.close()

        if not rows:
            await query.answer(
                [],
                cache_time=1,
                switch_pm_text="📚 Библиотека пуста",
                switch_pm_parameter="empty",
            )
            return

        results = []
        for row in rows:
            results.append(
                InlineQueryResultVoice(
                    type="voice",
                    id=f"v{row['id']}",
                    voice_file_id=row["file_id"],
                    voice_url="",
                    title=row["title"] or "Без названия",
                    caption=f"/{row['id']}. {row['title']} ({row['play_count'] or 0})",
                )
            )

        await query.answer(
            results,
            cache_time=1,
            is_private=False,
            switch_pm_text="📚 Открыть каталог",
            switch_pm_parameter="catalog",
        )

    @dp.message(F.voice)
    async def on_voice(message: Message) -> None:
        await accept_voice(message, db)

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        if await accept_title(message, db):
            return
        match = re.fullmatch(
            r"/([1-9][0-9]*)(?:@[A-Za-z0-9_]+)?",
            (message.text or "").strip(),
        )
        if match:
            await send_voice(message, db, int(match.group(1)))

    me = await bot.get_me()
    log.info("Bot started: @%s, ID=%s", me.username, me.id)

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await db.close()
        await bot.session.close()


# ⚠️ ЗДЕСЬ БЫЛА ОПЕЧАТКА: было `if name == "main":`
if __name__ == "__main__":
    asyncio.run(main())
