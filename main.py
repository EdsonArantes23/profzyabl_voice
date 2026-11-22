import os
import sqlite3
import logging
import re
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "417850992"))
DB_PATH = os.getenv("DATABASE_PATH", "voices.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS voices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        file_id TEXT NOT NULL,
        play_count INTEGER DEFAULT 0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS pending (
        owner_id INTEGER PRIMARY KEY,
        stage TEXT,
        file_id TEXT
    )''')
    conn.commit()
    return conn

conn = get_db()

def is_owner(uid): return uid == OWNER_ID
def is_priv(ctype): return ctype == "private"

# ============================================================
#  INLINE MODE — список с ▶️ при @твой_бот
# ============================================================
@dp.inline_handler()
async def inline_voice(query: types.InlineQuery):
    text = (query.query or "").strip().lower()
    if text:
        cursor = conn.execute(
            "SELECT id, title, file_id, play_count FROM voices "
            "WHERE LOWER(title) LIKE ? ORDER BY id LIMIT 50",
            (f"%{text}%",)
        )
    else:
        cursor = conn.execute(
            "SELECT id, title, file_id, play_count FROM voices ORDER BY id LIMIT 50"
        )
    rows = cursor.fetchall()

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
        vid = row["id"]
        title = row["title"]
        plays = row["play_count"] or 0
        results.append(
            types.InlineQueryResultVoice(
                type="voice",
                id=f"v{vid}",
                voice_url="",
                voice_file_id=row["file_id"],
                title=f"{title}  🎵{plays}",
                caption=f"/{vid}. {title} ({plays})",
                parse_mode="Markdown",
            )
        )

    await query.answer(
        results,
        cache_time=1,
        is_private=False,
        switch_pm_text="📚 Открыть каталог",
        switch_pm_parameter="catalog",
    )

# ============================================================
#  КОМАНДЫ
# ============================================================
@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    if is_owner(message.from_user.id):
        await message.answer(
            "👋 Привет! Я библиотека голосовых.\n\n"
            "📌 В любом чате напиши @твой_бот — откроется список ▶️\n"
            "📚 /voices — каталог\n"
            "🎲 /randomvoice — случайное\n"
            "🔢 /1, /2, /3… — воспроизвести\n\n"
            "⚙️ Управление (только для тебя):\n"
            "/addvoice — добавить голосовое\n"
            "/deletevoice 5 — удалить №5\n"
            "/renamevoice 5 Новое название — переименовать\n"
            "/stats — статистика\n"
            "/cancel — отменить добавление"
        )
    else:
        await message.answer(
            "👋 Привет! Я библиотека голосовых.\n\n"
            "📌 В любом чате напиши @твой_бот — откроется список ▶️\n"
            "📚 /voices — каталог\n"
            "🎲 /randomvoice — случайное голосовое\n"
            "🔢 /1, /2, /3… — воспроизвести по номеру"
        )

@dp.message_handler(commands=['addvoice'])
async def cmd_add(message: types.Message):
    if not is_owner(message.from_user.id) or not is_priv(message.chat.type):
        return await message.answer("⛔ Только владелец в личке.")
    conn.execute(
        "INSERT OR REPLACE INTO pending (owner_id, stage, file_id) VALUES (?, 'voice', NULL)",
        (OWNER_ID,)
    )
    conn.commit()
    await message.answer("🎤 Отправь голосовое.\n/cancel — отмена")

@dp.message_handler(commands=['cancel'])
async def cmd_cancel(message: types.Message):
    if is_owner(message.from_user.id) and is_priv(message.chat.type):
        conn.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
        conn.commit()
        await message.answer("❌ Отменено.")

@dp.message_handler(content_types=['voice'])
async def handle_voice(message: types.Message):
    if not is_owner(message.from_user.id) or not is_priv(message.chat.type):
        return
    cursor = conn.execute("SELECT stage FROM pending WHERE owner_id=?", (OWNER_ID,))
    row = cursor.fetchone()
    if row and row['stage'] == 'voice':
        conn.execute(
            "UPDATE pending SET stage='title', file_id=? WHERE owner_id=?",
            (message.voice.file_id, OWNER_ID)
        )
        conn.commit()
        await message.answer("✏️ Отправь название (до 200 символов).")

@dp.message_handler(content_types=['text'])
async def handle_text(message: types.Message):
    text = message.text.strip()
    uid = message.from_user.id
    ctype = message.chat.type

    # Название при добавлении
    if is_owner(uid) and is_priv(ctype):
        cursor = conn.execute("SELECT stage, file_id FROM pending WHERE owner_id=?", (OWNER_ID,))
        row = cursor.fetchone()
        if row and row['stage'] == 'title':
            title = text[:200]
            if not title:
                return await message.answer("Название пустое.")
            conn.execute("INSERT INTO voices (title, file_id) VALUES (?, ?)", (title, row['file_id']))
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
            conn.commit()
            return await message.answer(f"✅ Сохранено!\nНомер: /{new_id}\nНазвание: {title}")

    # /1 /2 /3...
    match = re.match(r"^/(\d+)(?:@\w+)?$", text)
    if match:
        vid = int(match.group(1))
        cursor = conn.execute("SELECT file_id FROM voices WHERE id=?", (vid,))
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE voices SET play_count = play_count + 1 WHERE id=?", (vid,))
            conn.commit()
            return await message.answer_voice(row['file_id'])
        return await message.answer(f"❌ №{vid} не найдено.")

    # /voices
    if text.lower() == '/voices':
        cursor = conn.execute("SELECT id, title, play_count FROM voices ORDER BY id LIMIT 25")
        rows = cursor.fetchall()
        if not rows:
            return await message.answer("📚 Пусто.")
        lines = ["📚 Библиотека:"]
        for r in rows:
            lines.append(f"/{r['id']}. {r['title']} ({r['play_count']})")
        return await message.answer("\n".join(lines))

    # /randomvoice
    if text.lower() == '/randomvoice':
        cursor = conn.execute("SELECT id, file_id FROM voices ORDER BY RANDOM() LIMIT 1")
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE voices SET play_count = play_count + 1 WHERE id=?", (row['id'],))
            conn.commit()
            return await message.answer_voice(row['file_id'])
        return await message.answer("📚 Пусто.")

    # Админ-команды (только владелец в личке)
    if is_owner(uid) and is_priv(ctype):
        if text.startswith('/deletevoice '):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit():
                conn.execute("DELETE FROM voices WHERE id=?", (int(parts[1]),))
                conn.commit()
                await message.answer(f"🗑 №{parts[1]} удалено.")
        elif text.startswith('/renamevoice '):
            parts = text.split(maxsplit=2)
            if len(parts) == 3 and parts[1].isdigit():
                conn.execute("UPDATE voices SET title=? WHERE id=?", (parts[2].strip()[:200], int(parts[1])))
                conn.commit()
                await message.answer(f"✅ №{parts[1]} переименовано.")
        elif text.lower() == '/stats':
            total = conn.execute("SELECT COUNT(*) FROM voices").fetchone()[0]
            plays = conn.execute("SELECT COALESCE(SUM(play_count),0) FROM voices").fetchone()[0]
            await message.answer(f"📊 Голосовых: {total}\nВоспроизведений: {plays}")


if __name__ == "__main__":
    logger.info("Bot starting...")
    executor.start_polling(dp, skip_updates=True)
