import asyncio
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
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


def is_owner(message: Message):
    return bool(
        message.from_user
        and OWNER_ID
        and message.from_user.id == OWNER_ID
    )


def is_private(message: Message):
    return message.chat.type == "private"


async def get_columns(db, table):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row["name"] for row in rows}


async def init_db():
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
        raise RuntimeError(
            "В старой таблице voices отсутствует колонка id. "
            "Автоматическая миграция остановлена."
        )

    file_col = next(
        (
            name for name in (
                "file_id",
                "telegram_file_id",
                "voice_file_id",
            )
            if name in cols
        ),
        None,
    )
    if file_col is None:
        raise RuntimeError(
            "Не найдена колонка file_id. "
            "Проверьте структуру старой базы данных."
        )

    if "title" not in cols:
        await db.execute(
            "ALTER TABLE voices ADD COLUMN title TEXT NOT NULL DEFAULT ''"
        )
        old_title = next(
            (
                name for name in ("name", "voice_name", "command")
                if name in cols
            ),
            None,
        )
        if old_title:
            await db.execute(
                f'UPDATE voices SET title = "{old_title}" '
                "WHERE title = ''"
            )
        else:
            await db.execute(
                "UPDATE voices SET title = 'Голосовое ' || id "
                "WHERE title = ''"
            )

    cols = await get_columns(db, "voices")
    if "play_count" not in cols:
        await db.execute(
            "ALTER TABLE voices ADD COLUMN "
            "play_count INTEGER NOT NULL DEFAULT 0"
        )
        old_count = next(
            (
                name for name in ("plays", "views", "count")
                if name in cols
            ),
            None,
        )
        if old_count:
            await db.execute(
                f'UPDATE voices SET play_count = COALESCE("{old_count}", 0)'
            )

    cols = await get_columns(db, "voices")
    if "created_at" not in cols:
        await db.execute(
            "ALTER TABLE voices ADD COLUMN created_at TEXT"
        )
        await db.execute(
            "UPDATE voices SET created_at = CURRENT_TIMESTAMP "
            "WHERE created_at IS NULL"
        )

    if file_col != "file_id":
        await db.execute(
            "ALTER TABLE voices ADD COLUMN file_id TEXT"
        )
        await db.execute(
            f'UPDATE voices SET file_id = "{file_col}" '
            "WHERE file_id IS NULL"
        )

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


async def get_pending(db):
    cursor = await db.execute(
        "SELECT * FROM pending WHERE owner_id=?",
        (OWNER_ID,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def start_add(message: Message, db):
    if not is_owner(message):
        await message.answer(
            "Добавлять голосовые может только администратор."
        )
        return
    if not is_private(message):
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


async def cancel_add(message: Message, db):
    if not is_owner(message) or not is_private(message):
        await message.answer("Недостаточно прав.")
        return

    await db.execute(
        "DELETE FROM pending WHERE owner_id=?",
        (OWNER_ID,),
    )
    await db.commit()
    await message.answer("Добавление отменено.")


async def accept_voice(message: Message, db):
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

    await message.answer(
        "Теперь отправь название голосового.\n"
        "Максимум 200 символов."
    )


async def accept_title(message: Message, db):
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
        await message.answer(
            "Название слишком длинное. Максимум 200 символов."
        )
        return True

    if not pending["file_id"]:
        await db.execute(
            "DELETE FROM pending WHERE owner_id=?",
            (OWNER_ID,),
        )
        await db.commit()
        await message.answer(
            "Файл голосового не найден. Начни заново: /addvoice"
        )
        return True

    cursor = await db.execute(
        "INSERT INTO voices(title, file_id) VALUES(?, ?)",
        (title, pending["file_id"]),
    )
    voice_id = cursor.lastrowid
    await cursor.close()

    await db.execute(
        "DELETE FROM pending WHERE owner_id=?",
        (OWNER_ID,),
    )
    await db.commit()

    await message.answer(
        f"Голосовое добавлено!\n\n"
        f"Номер: /{voice_id}\n"
        f"Название: {html.escape(title)}",
        parse_mode="HTML",
    )
    return True


async def send_voice(message: Message, db, voice_id: int):
    cursor = await db.execute(
        "SELECT id, title, file_id FROM voices WHERE id=?",
        (voice_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()

    if not row:
        await message.answer(
            f"Голосовое №{voice_id} не найдено."
        )
        return

    try:
        await message.answer_voice(row["file_id"])
    except TelegramBadRequest:
        log.exception("Cannot send voice ID %s", voice_id)
        await message.answer(
            "Не удалось отправить голосовое. "
            "Возможно, файл больше недоступен."
        )
        return

    await db.execute(
        "UPDATE voices SET play_count = "
        "COALESCE(play_count, 0) + 1 WHERE id=?",
        (voice_id,),
    )
    await db.commit()


async def show_voices(db, page=1):
    cursor = await db.execute(
        "SELECT COUNT(*) AS total FROM voices"
    )
    total = (await cursor.fetchone())["total"]
    await cursor.close()

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    offset = (page - 1) * PAGE_SIZE

    cursor = await db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY id
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, offset))
    rows = await cursor.fetchall()
    await cursor.close()

    if not rows:
        text = "Библиотека пока пустая."
    else:
        lines = [
            "<b>Библиотека голосовых</b>",
            "",
        ]
        for item in rows:
            title = html.escape(item["title"] or "Без названия")
            lines.append(
                f"/{item['id']}. {title} "
                f"({item['play_count'] or 0})"
            )
        text = "\n".join(lines)

    keyboard = None
    if pages > 1:
        buttons = []
        if page > 1:
            buttons.append(
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"voices:{page - 1}",
                )
            )
        buttons.append(
            InlineKeyboardButton(
                text=f"{page}/{pages}",
                callback_data="voices:noop",
            )
        )
        if page < pages:
            buttons.append(
                InlineKeyboardButton(
                    text="Вперёд ➡️",
                    callback_data=f"voices:{page + 1}",
                )
            )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[buttons]
        )

    return text, keyboard


async def delete_voice(message: Message, db):
    if not is_owner(message) or not is_private(message):
        await message.answer("Недостаточно прав.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Использование: /deletevoice 5"
        )
        return

    voice_id = int(parts[1])
    cursor = await db.execute(
        "DELETE FROM voices WHERE id=?",
        (voice_id,),
    )
    await db.commit()

    if cursor.rowcount:
        await message.answer(
            f"Голосовое №{voice_id} удалено.\n"
            "Остальные номера не изменились."
        )
    else:
        await message.answer("Запись не найдена.")
    await cursor.close()


async def rename_voice(message: Message, db):
    if not is_owner(message) or not is_private(message):
        await message.answer("Недостаточно прав.")
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

    cursor = await db.execute(
        "UPDATE voices SET title=? WHERE id=?",
        (title, voice_id),
    )
    await db.commit()

    if cursor.rowcount:
        await message.answer(
            f"Название голосового №{voice_id} изменено."
        )
    else:
        await message.answer("Запись не найдена.")
    await cursor.close()


async def show_stats(message: Message, db):
    if not is_owner(message) or not is_private(message):
        await message.answer("Недостаточно прав.")
        return

    cursor = await db.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(play_count), 0) AS plays
        FROM voices
    """)
    totals = await cursor.fetchone()
    await cursor.close()

    cursor = await db.execute("""
        SELECT id, title, play_count
        FROM voices
        ORDER BY play_count DESC, id ASC
        LIMIT 5
    """)
    top = await cursor.fetchall()
    await cursor.close()

    text = (
        "<b>Статистика библиотеки</b>\n\n"
        f"Голосовых: <b>{totals['total']}</b>\n"
        f"Воспроизведений: <b>{totals['plays']}</b>"
    )
    if top:
        text += "\n\n<b>Топ-5:</b>\n"
        for item in top:
            title = html.escape(item["title"] or "Без названия")
            text += (
                f"/{item['id']} — {title}: "
                f"{item['play_count'] or 0}\n"
            )

    await message.answer(text, parse_mode="HTML")


# ============================================================
# ЭКСПОРТ / ИМПОРТ
# ============================================================
async def export_library(message: Message, db, bot: Bot):
    if not is_owner(message) or not is_private(message):
        await message.answer("⛔ Только владелец в личке.")
        return

    cursor = await db.execute(
        "SELECT id, title, file_id, play_count, created_at "
        "FROM voices ORDER BY id"
    )
    rows = await cursor.fetchall()
    await cursor.close()

    payload = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "voices": [
            {
                "id": r["id"],
                "title": r["title"],
                "file_id": r["file_id"],
                "play_count": r["play_count"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    file = io.BytesIO(data)
    date_str = payload["exported_at"][:10]
    file.name = f"voices_backup_{date_str}.json"

    await message.answer_document(
        document=file,
        caption=(
            f"📦 Экспорт библиотеки\n"
            f"Записей: {len(rows)}\n\n"
            f"Сохрани этот файл. При поломке пришли его обратно командой /import."
        ),
    )


async def start_import(message: Message, db):
    if not is_owner(message) or not is_private(message):
        await message.answer("⛔ Только владелец в личке.")
        return

    await db.execute("""
        INSERT INTO pending(owner_id, stage, file_id)
        VALUES(?, 'import', NULL)
        ON CONFLICT(owner_id)
        DO UPDATE SET stage='import', file_id=NULL
    """, (OWNER_ID,))
    await db.commit()

    await message.answer(
        "📥 Отправь мне JSON-файл с бэкапом "
        "(который ты получил командой /export).\n\n"
        "Записи с совпадающими ID будут перезаписаны, "
        "новые — добавлены.\n\n"
        "Для отмены: /cancel"
    )


async def handle_import_file(message: Message, db, bot: Bot):
    pending = await get_pending(db)
    if not pending or pending["stage"] != "import":
        return False
    if not is_owner(message) or not is_private(message):
        return False

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await message.answer("❌ Ожидался .json файл.")
        await db.execute(
            "DELETE FROM pending WHERE owner_id=?",
            (OWNER_ID,),
        )
        await db.commit()
        return True

    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)

    try:
        payload = json.loads(buf.read().decode("utf-8"))
    except Exception as e:
        await message.answer(f"❌ Не удалось прочитать JSON: {e}")
        await db.execute(
            "DELETE FROM pending WHERE owner_id=?",
            (OWNER_ID,),
        )
        await db.commit()
        return True

    voices = payload.get("voices", [])
    if not isinstance(voices, list):
        await message.answer("❌ В файле нет поля 'voices'.")
        await db.execute(
            "DELETE FROM pending WHERE owner_id=?",
            (OWNER_ID,),
        )
        await db.commit()
        return True

    imported = 0
    skipped = 0
    for v in voices:
        try:
            vid = int(v["id"])
            title = str(v.get("title", "")).strip() or f"Голосовое {vid}"
            file_id = str(v.get("file_id", "")).strip()
            play_count = int(v.get("play_count", 0) or 0)

            if not file_id:
                skipped += 1
                continue

            await db.execute(
                """
                INSERT INTO voices(id, title, file_id, play_count)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    file_id=excluded.file_id,
                    play_count=excluded.play_count
                """,
                (vid, title[:200], file_id, play_count),
            )
            imported += 1
        except Exception as e:
            log.warning("Skip voice row %s: %s", v, e)
            skipped += 1

    await db.commit()
    await db.execute(
        "DELETE FROM pending WHERE owner_id=?",
        (OWNER_ID,),
    )
    await db.commit()

    await message.answer(
        f"✅ Импорт завершён\n\n"
        f"Восстановлено: {imported}\n"
        f"Пропущено (битые строки): {skipped}\n\n"
        f"Номера /1, /2, /3… сохранены."
    )
    return True


# ============================================================
# MAIN
# ============================================================
async def main():
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
        BotCommand(command="voices", description="Список голосовых"),
        BotCommand(command="randomvoice", description="Случайное голосовое"),
        BotCommand(command="addvoice", description="Добавить голосовое"),
        BotCommand(command="deletevoice", description="Удалить голосовое"),
        BotCommand(command="renamevoice", description="Переименовать"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="export", description="Экспорт библиотеки в JSON"),
        BotCommand(command="import", description="Импорт библиотеки из JSON"),
        BotCommand(command="cancel", description="Отменить добавление"),
    ])

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        await message.answer(
            "Привет! Я библиотека голосовых.\n\n"
            "/voices — список голосовых\n"
            "/randomvoice — случайное голосовое\n\n"
            "Для вызова записи отправь её номер, например /1."
        )

    @dp.message(Command("addvoice"))
    async def cmd_add(message: Message):
        await start_add(message, db)

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        await cancel_add(message, db)

    @dp.message(Command("deletevoice"))
    async def cmd_delete(message: Message):
        await delete_voice(message, db)

    @dp.message(Command("renamevoice"))
    async def cmd_rename(message: Message):
        await rename_voice(message, db)

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        await show_stats(message, db)

    @dp.message(Command("export"))
    async def cmd_export(message: Message):
        await export_library(message, db, bot)

    @dp.message(Command("import"))
    async def cmd_import(message: Message):
        await start_import(message, db)

    @dp.message(Command("voices"))
    async def cmd_voices(message: Message):
        parts = (message.text or "").split(maxsplit=1)
        page = (
            int(parts[1])
            if len(parts) == 2 and parts[1].isdigit()
            else 1
        )
        text, keyboard = await show_voices(db, page)
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    @dp.callback_query(F.data.startswith("voices:"))
    async def callback_voices(callback: CallbackQuery):
        value = callback.data.split(":", 1)[1]
        if value == "noop":
            await callback.answer()
            return
        text, keyboard = await show_voices(db, int(value))
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
        await callback.answer()

    @dp.message(Command("randomvoice"))
    async def cmd_random(message: Message):
        cursor = await db.execute(
            "SELECT id FROM voices ORDER BY RANDOM() LIMIT 1"
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            await message.answer("Библиотека пока пустая.")
            return
        await send_voice(message, db, row["id"])

    @dp.message(F.document)
    async def on_document(message: Message):
        if await handle_import_file(message, db, bot):
            return

    @dp.message(F.voice)
    async def on_voice(message: Message):
        await accept_voice(message, db)

    @dp.message(F.text)
    async def on_text(message: Message):
        if await accept_title(message, db):
            return
        match = re.fullmatch(
            r"/([1-9][0-9]*)(?:@[A-Za-z0-9_]+)?",
            (message.text or "").strip(),
        )
        if match:
            await send_voice(
                message,
                db,
                int(match.group(1)),
            )

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


# ⚠️ ВАЖНО: именно __name__ == "__main__" (с двойными подчёркиваниями)
if __name__ == "__main__":
    asyncio.run(main())