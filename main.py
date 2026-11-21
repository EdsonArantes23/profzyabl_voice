import asyncio
import html
import logging
import os
import re
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/voices.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "25"))

OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW.isdigit() else None

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("voice_library")


# ============================================================
# DATABASE
# ============================================================

async def table_columns(db, table_name):
    async with db.execute(
        f"PRAGMA table_info({table_name})"
    ) as cursor:
        rows = await cursor.fetchall()

    return {row["name"] for row in rows}


async def init_db():
    Path(DATABASE_PATH).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row

    await db.execute("PRAGMA busy_timeout = 5000")
    await db.execute("PRAGMA journal_mode = WAL")

    # Create the table if it does not exist.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'Без названия',
            file_id TEXT NOT NULL,
            play_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    columns = await table_columns(db, "voices")

    # Find an existing file_id column before migration.
    file_column = None
    for candidate in (
        "file_id",
        "telegram_file_id",
        "voice_file_id",
    ):
        if candidate in columns:
            file_column = candidate
            break

    if file_column is None:
        raise RuntimeError(
            "В таблице voices отсутствует file_id. "
            "Автоматическая миграция остановлена, "
            "чтобы не повредить старые данные."
        )

    # Add missing title column.
    if "title" not in columns:
        await db.execute("""
            ALTER TABLE voices
            ADD COLUMN title TEXT NOT NULL DEFAULT 'Без названия'
        """)
        log.info("Database migration: title added")

        # Copy names from an older version, if available.
        for old_name in ("name", "voice_name", "command"):
            if old_name in columns:
                await db.execute(f"""
                    UPDATE voices
                    SET title = "{old_name}"
                    WHERE "{old_name}" IS NOT NULL
                      AND TRIM(CAST("{old_name}" AS TEXT)) != ''
                """)
                break

    # Add missing play counter.
    if "play_count" not in columns:
        await db.execute("""
            ALTER TABLE voices
            ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0
        """)
        log.info("Database migration: play_count added")

        for old_counter in ("plays", "views", "count"):
            if old_counter in columns:
                await db.execute(f"""
                    UPDATE voices
                    SET play_count = COALESCE("{old_counter}", 0)
                """)
                break

    # Add missing creation date.
    if "created_at" not in columns:
        await db.execute("""
            ALTER TABLE voices
            ADD COLUMN created_at TIMESTAMP
        """)
        log.info("Database migration: created_at added")

    # Normalize an older file ID column name if necessary.
    if file_column != "file_id":
        current_columns = await table_columns(db, "voices")
        if "file_id" not in current_columns:
            await db.execute("""
                ALTER TABLE voices
                ADD COLUMN file_id TEXT
            """)
            await db.execute(f"""
                UPDATE voices
                SET file_id = "{file_column}"
            """)
            log.info("Database migration: file_id copied")

    # Pending operations are temporary. Recreate only if the
    # old table has an incompatible structure.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            owner_id INTEGER PRIMARY KEY,
            stage TEXT NOT NULL,
            file_id TEXT
        )
    """)

    pending_columns = await table_columns(db, "pending")
    if not {"owner_id", "stage", "file_id"}.issubset(pending_columns):
        log.warning("Recreating incompatible temporary pending table")
        await db.execute("DROP TABLE pending")
        await db.execute("""
            CREATE TABLE pending (
                owner_id INTEGER PRIMARY KEY,
                stage TEXT NOT NULL,
                file_id TEXT
            )
        """)

    await db.commit()

    log.info("Database initialized: %s", DATABASE_PATH)
    return db


# ============================================================
# ACCESS CHECKS
# ============================================================

def is_owner(message: Message):
    return (
        message.from_user is not None
        and message.from_user.id == OWNER_ID
    )


def is_private(message: Message):
    return message.chat.type == ChatType.PRIVATE


async def require_owner_private(message: Message):
    if not is_owner(message):
        await message.answer(
            "Эта команда доступна только администратору."
        )
        return False

    if not is_private(message):
        await message.answer(
            "Эта команда работает только в личке с ботом."
        )
        return False

    return True


# ============================================================
# DATABASE HELPERS
# ============================================================

async def get_pending(db):
    async with db.execute(
        "SELECT * FROM pending WHERE owner_id = ?",
        (OWNER_ID,),
    ) as cursor:
        return await cursor.fetchone()


async def get_voice_count(db):
    async with db.execute(
        "SELECT COUNT(*) AS total FROM voices"
    ) as cursor:
        row = await cursor.fetchone()
        return row["total"]


# ============================================================
# ADD VOICE
# ============================================================

async def add_voice_start(message: Message, db):
    if not await require_owner_private(message):
        return

    await db.execute("""
        INSERT INTO pending(owner_id, stage, file_id)
        VALUES(?, 'voice', NULL)
        ON CONFLICT(owner_id)
        DO UPDATE SET stage = 'voice', file_id = NULL
    """, (OWNER_ID,))

    await db.commit()

    await message.answer(
        "🎙 <b>Добавление голосового</b>\n\n"
        "Отправь мне голосовое сообщение.\n"
        "После этого я попрошу написать его название.\n\n"
        "Для отмены используй /cancel",
        parse_mode="HTML",
    )


async def receive_voice(message: Message, db):
    if not is_owner(message) or not is_private(message):
        return

    pending = await get_pending(db)

    if not pending or pending["stage"] != "voice":
        return

    await db.execute("""
        UPDATE pending
        SET stage = 'title', file_id = ?
        WHERE owner_id = ?
    """, (message.voice.file_id, OWNER_ID))

    await db.commit()

    await message.answer(
        "✅ Голосовое получено!\n\n"
        "Теперь напиши его название."
    )


async def receive_title(message: Message, db):
    if not is_owner(message) or not is_private(message):
        return False

    pending = await get_pending(db)

    if not pending or pending["stage"] != "title":
        return False

    title = (message.text or "").strip()

    if not title or title.startswith("/"):
        await message.answer(
            "Напиши название обычным текстом. "
            "Для отмены используй /cancel."
        )
        return True

    if len(title) > 200:
        await message.answer(
            "Название слишком длинное. Максимум 200 символов."
        )
        return True

    if not pending["file_id"]:
        await message.answer(
            "Не удалось найти файл голосового. "
            "Начни добавление заново командой /addvoice."
        )
        await db.execute(
            "DELETE FROM pending WHERE owner_id = ?",
            (OWNER_ID,),
        )
        await db.commit()
        return True

    try:
        async with db.execute("""
            INSERT INTO voices(title, file_id)
            VALUES(?, ?)
        """, (title, pending["file_id"])) as cursor:
            voice_id = cursor.lastrowid

        await db.execute(
            "DELETE FROM pending WHERE owner_id = ?",
            (OWNER_ID,),
        )
        await db.commit()

    except Exception:
        await db.rollback()
        log.exception("Failed to save voice")
        await message.answer(
            "❌ Не удалось сохранить голосовое. "
            "Проверь логи бота."
        )
        return True

    await message.answer(
        "✅ <b>Голосовое успешно сохранено!</b>\n\n"
        f"Номер: №{voice_id}\n"
        f"Название: {html.escape(title)}\n\n"
        f"Команда вызова: <code>/{voice_id}</code>",
        parse_mode="HTML",
    )

    log.info(
        "Voice added: id=%s owner=%s title=%s",
        voice_id,
        OWNER_ID,
        title,
    )

    return True


async def cancel_add(message: Message, db):
    if not await require_owner_private(message):
        return

    await db.execute(
        "DELETE FROM pending WHERE owner_id = ?",
        (OWNER_ID,),
    )
    await db.commit()

    await message.answer("Добавление голосового отменено.")


# ============================================================
# PLAY VOICE
# ============================================================

async def send_voice(message: Message, db, voice_id: int):
    async with db.execute(
        "SELECT file_id FROM voices WHERE id = ?",
        (voice_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        await message.answer(
            f"Голосовое №{voice_id} не найдено."
        )
        return

    try:
        await message.answer_voice(row["file_id"])

        await db.execute("""
            UPDATE voices
            SET play_count = COALESCE(play_count, 0) + 1
            WHERE id = ?
        """, (voice_id,))

        await db.commit()

        log.info(
            "Voice played: id=%s user=%s",
            voice_id,
            message.from_user.id if message.from_user else "unknown",
        )

    except TelegramForbiddenError:
        log.exception("Bot has no permission to send voice")
        raise

    except Exception:
        log.exception("Failed to send voice")
        await message.answer(
            "Не удалось отправить это голосовое. "
            "Возможно, Telegram больше не принимает его file_id."
        )


# ============================================================
# VOICE LIST
# ============================================================

async def build_voice_list(db, page=1):
    total = await get_voice_count(db)

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    offset = (page - 1) * PAGE_SIZE

    async with db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY id ASC
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset)) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return "📂 Библиотека пока пустая.", None

    lines = ["🎙 <b>Библиотека голосовых</b>\n"]

    for row in rows:
        title = html.escape(row["title"] or "Без названия")
        plays = row["play_count"] or 0

        lines.append(
            f"<code>/{row['id']}</code>. "
            f"{title} ({plays})"
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
                    callback_data=f"page:{page - 1}",
                )
            )

        if page < pages:
            buttons.append(
                InlineKeyboardButton(
                    text="Вперёд ➡️",
                    callback_data=f"page:{page + 1}",
                )
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[buttons]
        )

    return "\n".join(lines), keyboard


# ============================================================
# DELETE VOICE
# ============================================================

async def delete_voice(message: Message, db):
    if not await require_owner_private(message):
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Использование: /deletevoice 5"
        )
        return

    voice_id = int(parts[1])

    async with db.execute(
        "DELETE FROM voices WHERE id = ?",
        (voice_id,),
    ) as cursor:
        deleted = cursor.rowcount

    await db.commit()

    if deleted:
        await message.answer(
            f"🗑 Запись №{voice_id} удалена.\n"
            "Номера остальных записей не изменились."
        )
    else:
        await message.answer("Запись не найдена.")


# ============================================================
# RENAME VOICE
# ============================================================

async def rename_voice(message: Message, db):
    if not await require_owner_private(message):
        return

    parts = (message.text or "").split(maxsplit=2)

    if len(parts) != 3 or not parts[1].isdigit():
        await message.answer(
            "Использование:\n"
            "/renamevoice 5 Новое название"
        )
        return

    voice_id = int(parts[1])
    title = parts[2].strip()

    if not title or len(title) > 200:
        await message.answer(
            "Название должно содержать от 1 до 200 символов."
        )
        return

    async with db.execute(
        "UPDATE voices SET title = ? WHERE id = ?",
        (title, voice_id),
    ) as cursor:
        updated = cursor.rowcount

    await db.commit()

    if updated:
        await message.answer(
            f"✏️ Название записи №{voice_id} изменено."
        )
    else:
        await message.answer("Запись не найдена.")


# ============================================================
# STATISTICS
# ============================================================

async def show_statistics(message: Message, db):
    if not await require_owner_private(message):
        return

    async with db.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(play_count), 0) AS plays
        FROM voices
    """) as cursor:
        summary = await cursor.fetchone()

    async with db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY play_count DESC, id ASC
        LIMIT 5
    """) as cursor:
        top = await cursor.fetchall()

    result = (
        "📊 <b>Статистика библиотеки</b>\n\n"
        f"Голосовых: {summary['total']}\n"
        f"Прослушиваний: {summary['plays']}\n"
    )

    if top:
        result += "\n<b>Топ-5 голосовых:</b>\n"

        for index, item in enumerate(top, start=1):
            result += (
                f"{index}. №{item['id']} — "
                f"{html.escape(item['title'] or 'Без названия')} "
                f"({item['play_count'] or 0})\n"
            )

    await message.answer(result, parse_mode="HTML")


# ============================================================
# RANDOM VOICE
# ============================================================

async def random_voice(message: Message, db):
    async with db.execute(
        "SELECT id FROM voices ORDER BY RANDOM() LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        await message.answer("Библиотека пока пустая.")
        return

    await send_voice(message, db, row["id"])


# ============================================================
# BOT COMMANDS
# ============================================================

async def setup_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Информация о боте"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="voices", description="Список голосовых"),
        BotCommand(command="randomvoice", description="Случайное голосовое"),
        BotCommand(command="addvoice", description="Добавить голосовое"),
        BotCommand(command="deletevoice", description="Удалить голосовое"),
        BotCommand(command="renamevoice", description="Переименовать"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="cancel", description="Отменить добавление"),
    ])


# ============================================================
# MAIN
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")

    if OWNER_ID is None:
        raise RuntimeError(
            "OWNER_ID должен быть задан числом в переменных окружения."
        )

    if PAGE_SIZE < 1 or PAGE_SIZE > 50:
        raise RuntimeError("PAGE_SIZE должен быть от 1 до 50.")

    db = await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    me = await bot.get_me()
    await setup_commands(bot)

    log.info(
        "Bot started: @%s | owner=%s | database=%s",
        me.username,
        OWNER_ID,
        DATABASE_PATH,
    )

    # /start
    @dp.message(Command("start"))
    @dp.message(Command("help"))
    async def start_handler(message: Message):
        await message.answer(
            "👋 Привет! Я библиотека голосовых сообщений.\n\n"
            "<b>Команды для всех:</b>\n"
            "/voices — список голосовых\n"
            "/randomvoice — случайное голосовое\n"
            "/1, /2, /3 — вызвать голосовое по номеру\n\n"
            "<b>Команды администратора:</b>\n"
            "/addvoice — добавить голосовое\n"
            "/deletevoice 5 — удалить запись\n"
            "/renamevoice 5 Новое имя — переименовать\n"
            "/stats — статистика\n"
            "/cancel — отменить добавление",
            parse_mode="HTML",
        )

    # Add voice
    @dp.message(Command("addvoice"))
    async def add_handler(message: Message):
        await add_voice_start(message, db)

    # Cancel
    @dp.message(Command("cancel"))
    async def cancel_handler(message: Message):
        await cancel_add(message, db)

    # Delete
    @dp.message(Command("deletevoice"))
    async def delete_handler(message: Message):
        await delete_voice(message, db)

    # Rename
    @dp.message(Command("renamevoice"))
    async def rename_handler(message: Message):
        await rename_voice(message, db)

    # Statistics
    @dp.message(Command("stats"))
    async def stats_handler(message: Message):
        await show_statistics(message, db)

    # List
    @dp.message(Command("voices"))
    async def voices_handler(message: Message):
        parts = (message.text or "").split(maxsplit=1)
        page = 1

        if len(parts) == 2 and parts[1].isdigit():
            page = int(parts[1])

        text, keyboard = await build_voice_list(db, page)

        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    # Pagination
    @dp.callback_query(F.data.startswith("page:"))
    async def page_handler(callback: CallbackQuery):
        try:
            page = int(callback.data.split(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("Некорректная страница.")
            return

        text, keyboard = await build_voice_list(db, page)

        if callback.message:
            try:
                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    log.exception("Pagination edit failed")

        await callback.answer()

    # Random voice
    @dp.message(Command("randomvoice"))
    async def random_handler(message: Message):
        await random_voice(message, db)

    # Receive voice during adding
    @dp.message(F.voice)
    async def voice_handler(message: Message):
        await receive_voice(message, db)

    # Text handler: title or numeric command
    @dp.message(F.text)
    async def text_handler(message: Message):
        if await receive_title(message, db):
            return

        text = (message.text or "").strip()

        username_part = (
            r"(?:@" + re.escape(me.username) + r")?"
            if me.username else ""
        )

        match = re.fullmatch(
            r"/([1-9][0-9]*)" + username_part,
            text,
            re.IGNORECASE,
        )

        if match:
            await send_voice(
                message,
                db,
                int(match.group(1)),
            )

    try:
        await dp.start_polling(bot)

    finally:
        await db.close()
        await bot.session.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")
