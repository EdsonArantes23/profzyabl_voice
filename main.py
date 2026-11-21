import asyncio
import logging
import os
import random
import re
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else None
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/voices.db")

# Telegram bot commands: 1-32 chars, Latin letters/digits/underscore.
COMMAND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED = {
    "start", "help", "addvoice", "deletevoice", "voices",
    "randomvoice", "stats", "cancel",
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("voice-library")


async def db_connect():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE IF NOT EXISTS voices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL UNIQUE,
            file_id TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_uploads (
            owner_id INTEGER PRIMARY KEY,
            command TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.commit()
    return db


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


async def reply_private_only(message: Message):
    await message.answer("Эту административную команду нужно отправлять боту в личные сообщения.")


async def get_pending(db):
    cur = await db.execute(
        "SELECT command FROM pending_uploads WHERE owner_id = ?", (OWNER_ID,)
    )
    return await cur.fetchone()


async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот-библиотека голосовых сообщений.\n\n"
        "Посмотреть записи: /voices\n"
        "Получить случайное голосовое: /randomvoice\n\n"
        "Чтобы вызвать конкретное голосовое, используй его команду, "
        "например /svetlana_vecher."
    )


async def cmd_addvoice(message: Message, db):
    if not is_owner(message):
        await message.answer("Добавлять голосовые может только администратор.")
        return
    if not is_private(message):
        await reply_private_only(message)
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Укажи команду для нового голосового.\n"
            "Пример: /addvoice svetlana_vecher\n\n"
            "Разрешены латинские буквы, цифры и _; первая буква — латинская."
        )
        return

    command = parts[1].strip().lower().lstrip("/")
    if not COMMAND_RE.fullmatch(command):
        await message.answer(
            "Некорректная команда. Используй 1–32 символа: "
            "латинские буквы, цифры и _. Начинай с буквы."
        )
        return
    if command in RESERVED:
        await message.answer("Это имя занято системной командой бота. Выбери другое.")
        return

    cur = await db.execute("SELECT id FROM voices WHERE command = ?", (command,))
    exists = await cur.fetchone()
    if exists:
        await message.answer(
            f"Команда /{command} уже существует. Удали её через "
            f"/deletevoice {command} или выбери другое имя."
        )
        return

    await db.execute(
        """INSERT INTO pending_uploads(owner_id, command)
           VALUES(?, ?)
           ON CONFLICT(owner_id) DO UPDATE SET
             command=excluded.command, created_at=CURRENT_TIMESTAMP""",
        (OWNER_ID, command),
    )
    await db.commit()
    await message.answer(
        f"Жду голосовое для команды /{command}.\n"
        "Отправь именно голосовое сообщение (не аудиофайл).\n"
        "Для отмены отправь /cancel."
    )


async def cmd_cancel(message: Message, db):
    if not is_owner(message):
        await message.answer("Отменять загрузку может только администратор.")
        return
    if not is_private(message):
        await reply_private_only(message)
        return
    await db.execute("DELETE FROM pending_uploads WHERE owner_id = ?", (OWNER_ID,))
    await db.commit()
    await message.answer("Ожидание голосового отменено.")


async def handle_voice(message: Message, db):
    if not (is_owner(message) and is_private(message)):
        return
    pending = await get_pending(db)
    if not pending:
        return
    command = pending["command"]
    await db.execute(
        "INSERT INTO voices(command, file_id) VALUES(?, ?)",
        (command, message.voice.file_id),
    )
    await db.execute("DELETE FROM pending_uploads WHERE owner_id = ?", (OWNER_ID,))
    await db.commit()
    await message.answer(
        f"Готово! Голосовое сохранено.\n"
        f"Команда вызова: /{command}\n"
        f"Добавить ещё одно: /addvoice другая_команда"
    )


async def cmd_deletevoice(message: Message, db):
    if not is_owner(message):
        await message.answer("Удалять голосовые может только администратор.")
        return
    if not is_private(message):
        await reply_private_only(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Укажи команду: /deletevoice svetlana_vecher")
        return
    command = parts[1].strip().lower().lstrip("/")
    cur = await db.execute("DELETE FROM voices WHERE command = ?", (command,))
    await db.commit()
    if cur.rowcount:
        await message.answer(f"Запись /{command} удалена.")
    else:
        await message.answer(f"Команда /{command} не найдена.")


async def cmd_voices(message: Message, db):
    cur = await db.execute("SELECT command FROM voices ORDER BY command COLLATE NOCASE")
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Библиотека пока пустая.")
        return
    lines = ["Голосовые в библиотеке:"]
    lines.extend(f"/{row['command']}" for row in rows)
    text = "\n".join(lines)
    # Telegram message limit is 4096 chars.
    for start in range(0, len(text), 3900):
        await message.answer(text[start:start + 3900])


async def cmd_randomvoice(message: Message, db):
    cur = await db.execute("SELECT file_id FROM voices")
    rows = await cur.fetchall()
    if not rows:
        await message.answer("В библиотеке пока нет голосовых.")
        return
    selected = random.choice(rows)
    await message.answer_voice(selected["file_id"])


async def cmd_stats(message: Message, db):
    if not is_owner(message):
        await message.answer("Статистика доступна только администратору.")
        return
    if not is_private(message):
        await reply_private_only(message)
        return
    cur = await db.execute("SELECT COUNT(*) AS total FROM voices")
    total = (await cur.fetchone())["total"]
    cur = await db.execute("SELECT COUNT(*) AS total FROM pending_uploads")
    pending = (await cur.fetchone())["total"]
    await message.answer(
        f"Статистика библиотеки:\n"
        f"Всего голосовых: {total}\n"
        f"Ожидают загрузки: {pending}"
    )


async def handle_dynamic_command(message: Message, db, bot_username: str):
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return
    first = text.split(maxsplit=1)[0]
    raw = first[1:]
    if "@" in raw:
        command, addressed_to = raw.split("@", 1)
        if addressed_to.lower() != bot_username.lower():
            return
    else:
        command = raw
    command = command.lower()
    if not COMMAND_RE.fullmatch(command) or command in RESERVED:
        return
    cur = await db.execute("SELECT file_id FROM voices WHERE command = ?", (command,))
    row = await cur.fetchone()
    if row:
        await message.answer_voice(row["file_id"])


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Заполни переменные окружения.")
    if OWNER_ID is None:
        raise RuntimeError("Не задан OWNER_ID. Укажи Telegram ID администратора в переменных окружения.")
    db = await db_connect()
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    bot_username = me.username or ""

    await bot.set_my_commands([
        BotCommand(command="start", description="Как пользоваться ботом"),
        BotCommand(command="voices", description="Список голосовых"),
        BotCommand(command="randomvoice", description="Случайное голосовое"),
        BotCommand(command="addvoice", description="Добавить голосовое (админ)"),
        BotCommand(command="deletevoice", description="Удалить голосовое (админ)"),
        BotCommand(command="stats", description="Статистика (админ)"),
        BotCommand(command="cancel", description="Отменить добавление (админ)"),
    ])

    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start_handler(message: Message):
        await cmd_start(message)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/addvoice")
    async def add_handler(message: Message):
        await cmd_addvoice(message, db)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/cancel")
    async def cancel_handler(message: Message):
        await cmd_cancel(message, db)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/deletevoice")
    async def delete_handler(message: Message):
        await cmd_deletevoice(message, db)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/voices")
    async def voices_handler(message: Message):
        await cmd_voices(message, db)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/randomvoice")
    async def random_handler(message: Message):
        await cmd_randomvoice(message, db)

    @dp.message(lambda m: m.text and m.text.split(maxsplit=1)[0].split("@")[0] == "/stats")
    async def stats_handler(message: Message):
        await cmd_stats(message, db)

    @dp.message(lambda m: m.voice is not None)
    async def voice_handler(message: Message):
        await handle_voice(message, db)

    @dp.message(lambda m: m.text and m.text.startswith("/"))
    async def dynamic_handler(message: Message):
        await handle_dynamic_command(message, db, bot_username)

    log.info("Bot started as @%s", bot_username)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
