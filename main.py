import os
import sqlite3
import logging
import re
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# --- Настройки из переменных окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "417850992"))
DB_PATH = os.getenv("DATABASE_PATH", "voices.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Инициализация ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Создаём таблицу голосовых, если её нет
    conn.execute('''CREATE TABLE IF NOT EXISTS voices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        file_id TEXT NOT NULL,
        play_count INTEGER DEFAULT 0
    )''')
    # Создаём таблицу для временного хранения шага добавления
    conn.execute('''CREATE TABLE IF NOT EXISTS pending (
        owner_id INTEGER PRIMARY KEY,
        stage TEXT,
        file_id TEXT
    )''')
    conn.commit()
    return conn

conn = get_db()

# --- Хелперы ---
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

def is_private(chat_type: str) -> bool:
    return chat_type == "private"

# --- Команды ---
@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я библиотека голосовых.\n\n"
        "/voices — каталог\n"
        "/randomvoice — случайное голосовое\n"
        "/1, /2, /3... — воспроизвести по номеру"
    )

@dp.message_handler(commands=['addvoice'])
async def cmd_add(message: types.Message):
    if not is_owner(message.from_user.id) or not is_private(message.chat.type):
        return await message.answer("⛔ Команда доступна только владельцу в личных сообщениях.")
    
    conn.execute("INSERT OR REPLACE INTO pending (owner_id, stage, file_id) VALUES (?, 'voice', NULL)", (OWNER_ID,))
    conn.commit()
    await message.answer("🎤 Отправь голосовое сообщение.\nДля отмены: /cancel")

@dp.message_handler(commands=['cancel'])
async def cmd_cancel(message: types.Message):
    if is_owner(message.from_user.id) and is_private(message.chat.type):
        conn.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
        conn.commit()
        await message.answer("❌ Добавление отменено.")

@dp.message_handler(content_types=['voice'])
async def handle_voice(message: types.Message):
    if not is_owner(message.from_user.id) or not is_private(message.chat.type):
        return
    
    cursor = conn.execute("SELECT stage FROM pending WHERE owner_id=?", (OWNER_ID,))
    row = cursor.fetchone()
    
    if row and row['stage'] == 'voice':
        conn.execute("UPDATE pending SET stage='title', file_id=? WHERE owner_id=?", 
                     (message.voice.file_id, OWNER_ID))
        conn.commit()
        await message.answer("✏️ Голосовое получено. Теперь отправь название (до 200 символов).")

@dp.message_handler(content_types=['text'])
async def handle_text(message: types.Message):
    text = message.text.strip()
    user_id = message.from_user.id
    chat_type = message.chat.type

    # 1. Обработка названия при добавлении
    if is_owner(user_id) and is_private(chat_type):
        cursor = conn.execute("SELECT stage, file_id FROM pending WHERE owner_id=?", (OWNER_ID,))
        row = cursor.fetchone()
        if row and row['stage'] == 'title':
            title = text[:200]
            if not title:
                return await message.answer("Название не может быть пустым.")
            
            conn.execute("INSERT INTO voices (title, file_id) VALUES (?, ?)", (title, row['file_id']))
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("DELETE FROM pending WHERE owner_id=?", (OWNER_ID,))
            conn.commit()
            return await message.answer(f"✅ Сохранено!\n\nНомер: /{new_id}\nНазвание: {title}")

    # 2. Обработка команд /1, /2, /3...
    match = re.match(r"^/(\d+)(?:@\w+)?$", text)
    if match:
        voice_id = int(match.group(1))
        cursor = conn.execute("SELECT file_id FROM voices WHERE id=?", (voice_id,))
        row = cursor.fetchone()
        if row:
            conn.execute("UPDATE voices SET play_count = play_count + 1 WHERE id=?", (voice_id,))
            conn.commit()
            return await message.answer_voice(row['file_id'])
        else:
            return await message.answer(f"❌ Голосовое №{voice_id} не найдено.")

    # 3. Команда /voices
    if text.lower() == '/voices':
        cursor = conn.execute("SELECT id, title, play_count FROM voices ORDER BY id LIMIT 25")
        rows = cursor.fetchall()
        if not rows:
            return await message.answer("📚 Библиотека пока пустая.")
        
        lines = ["📚 Библиотека голосовых:"]
        for r in rows:
            lines.append(f"/{r['id']}. {r['title']} ({r['play_count']})")
        return await message.answer("\n".join(lines))

    # 4. Команда /randomvoice
    if text.lower() == '/randomvoice':
        cursor = conn.execute("SELECT id FROM voices ORDER BY RANDOM() LIMIT 1")
        row = cursor.fetchone()
        if row:
            voice_id = row['id']
            cursor2 = conn.execute("SELECT file_id FROM voices WHERE id=?", (voice_id,))
            row2 = cursor2.fetchone()
            conn.execute("UPDATE voices SET play_count = play_count + 1 WHERE id=?", (voice_id,))
            conn.commit()
            await message.answer_voice(row2['file_id'])
        else:
            await message.answer("📚 Библиотека пока пустая.")

    # 5. Админские команды (только в ЛС)
    if is_owner(user_id) and is_private(chat_type):
        if text.startswith('/deletevoice '):
            parts = text.split()
            if len(parts) == 2 and parts[1].isdigit():
                conn.execute("DELETE FROM voices WHERE id=?", (int(parts[1]),))
                conn.commit()
                await message.answer(f"🗑 Голосовое №{parts[1]} удалено.")
            else:
                await message.answer("Использование: /deletevoice 5")
        
        elif text.startswith('/renamevoice '):
            parts = text.split(maxsplit=2)
            if len(parts) == 3 and parts[1].isdigit():
                voice_id = int(parts[1])
                title = parts[2].strip()[:200]
                conn.execute("UPDATE voices SET title=? WHERE id=?", (title, voice_id))
                conn.commit()
                await message.answer(f"✅ Голосовое №{voice_id} переименовано.")
            else:
                await message.answer("Использование: /renamevoice 5 Новое название")
        
        elif text.lower() == '/stats':
            total = conn.execute("SELECT COUNT(*) FROM voices").fetchone()[0]
            plays = conn.execute("SELECT COALESCE(SUM(play_count), 0) FROM voices").fetchone()[0]
            await message.answer(f"📊 Статистика:\nГолосовых: {total}\nВоспроизведений: {plays}")

if __name__ == "__main__":
    logger.info("Bot starting...")
    # ЭТА СТРОКА ГАРАНТИРОВАННО ПРОЙДЁТ ПРОВЕРКУ BOTHOST!
    executor.start_polling(dp, skip_updates=True)
