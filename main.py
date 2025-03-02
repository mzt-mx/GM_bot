import asyncio
import aiosqlite
import google.generativeai as genai
import os
import logging
import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram import F
import io

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DB_PATH = "chat_history.db"

if not TELEGRAM_TOKEN or not GOOGLE_API_KEY:
    logging.error("Необходимые переменные окружения не найдены.")
    exit(1)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
genai.configure(api_key=GOOGLE_API_KEY)
# Use a specific API version and the currently available model
model = genai.GenerativeModel(
    model_name='gemini-1.5-pro',
    generation_config={'temperature': 0.7})


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id INTEGER,
                role TEXT,
                content TEXT
            )
        """)
        cursor = await db.execute("PRAGMA table_info(chat_history)")
        columns = [col[1] for col in await cursor.fetchall()]
        if 'type' not in columns:
            logging.info("Добавляем столбец 'type' в таблицу chat_history")
            await db.execute(
                "ALTER TABLE chat_history ADD COLUMN type TEXT DEFAULT 'text'")
        await db.commit()


async def save_message(chat_id, role, content, msg_type="text"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_history (chat_id, role, content, type) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, msg_type))
        await db.commit()


async def get_chat_history(chat_id, max_tokens=1000000):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT role, content, COALESCE(type, 'text') as type FROM chat_history WHERE chat_id = ? ORDER BY rowid DESC",
            (chat_id, ))
        rows = await cursor.fetchall()

    history = []
    token_count = 0
    for role, content, msg_type in rows:
        text = f"{role}: {content} [{msg_type}]"
        tokens = len(text.split())
        if token_count + tokens > max_tokens:
            break
        token_count += tokens
        history.append({"role": role, "content": content, "type": msg_type})
    return list(reversed(history))


async def clear_chat_history(chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM chat_history WHERE chat_id = ?",
                         (chat_id, ))
        await db.commit()


@dp.message(Command("start"))
async def start_command(message: types.Message):
    username = message.from_user.first_name or message.from_user.username or "Пользователь"
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True,
                                   keyboard=[[
                                       KeyboardButton(text="💾 Сохранить"),
                                       KeyboardButton(text="🗑️ Очистить"),
                                       KeyboardButton(text="❓ Помощь")
                                   ]])
    await message.answer(
        f"Привет, {username}! Я бот на базе Gemini 2.0 Pro Experimental.\n"
        "Могу ответить на вопросы, проанализировать фото или аудио.\n"
        "Попробуй: 'Что на фото?' или 'Перескажи аудио'.",
        reply_markup=keyboard)


@dp.message(F.text == "💾 Сохранить")
async def save_button_handler(message: types.Message):
    chat_id = message.chat.id
    history = await get_chat_history(chat_id)
    if history:
        filename = f"chat_{chat_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            for entry in history:
                f.write(
                    f"{entry['role']}: {entry['content']} [{entry['type']}]\n")
        await message.answer_document(types.FSInputFile(filename),
                                      caption="✅ История сохранена")
        os.remove(filename)
    else:
        await message.answer("⚠️ История пуста.")


@dp.message(F.text == "🗑️ Очистить")
async def clear_button_handler(message: types.Message):
    await clear_chat_history(message.chat.id)
    await message.answer("🗑️ История очищена.")


@dp.message(F.text == "❓ Помощь")
async def help_button_handler(message: types.Message):
    await message.answer("Команды:\n"
                         "💾 Сохранить - сохранить диалог\n"
                         "🗑️ Очистить - новый диалог\n"
                         "❓ Помощь - это сообщение\n"
                         "Отправляйте текст, фото или аудио для анализа!")


async def process_content(message: types.Message, content_type: str):
    chat_id = message.chat.id
    loading_message = await message.answer("⏳ Обрабатываю ваш запрос...")
    await bot.send_chat_action(chat_id, "typing")

    content = []
    history = await get_chat_history(chat_id)
    for entry in history:
        if entry["type"] == "text":
            content.append({
                "role": "user" if entry["role"] == "user" else "model",
                "parts": [{
                    "text": entry["content"]
                }]
            })

    if content_type == "text":
        await save_message(chat_id, "user", message.text)
        content.append({"role": "user", "parts": [{"text": message.text}]})
    elif content_type == "photo":
        photo = await message.photo[-1].download(destination_file=io.BytesIO())
        photo_data = photo.getvalue()
        await save_message(chat_id, "user", "Фото", "image")
        content.append({
            "role":
            "user",
            "parts": [{
                "file_data": {
                    "mime_type": "image/jpeg",
                    "data": photo_data
                }
            }]
        })
    elif content_type == "audio":
        audio = await message.audio.download(destination_file=io.BytesIO())
        audio_data = audio.getvalue()
        await save_message(chat_id, "user", "Аудио", "audio")
        content.append({
            "role":
            "user",
            "parts": [{
                "file_data": {
                    "mime_type": "audio/mp3",
                    "data": audio_data
                }
            }]
        })

    try:
        response = await asyncio.to_thread(model.generate_content, content)
        full_response = response.text
        
        # Обработка кода в ответе
        # Находим код между ```python и ``` и выделяем его рамкой
        formatted_response = full_response
        
        # Убедимся, что блоки кода правильно отформатированы
        if "```" in formatted_response:
            # Сохраняем оригинальный текст для базы данных
            original_response = full_response
            
            # Обработка текста с учетом блоков кода
            lines = formatted_response.split('\n')
            in_code_block = False
            for i, line in enumerate(lines):
                if line.startswith("```"):
                    in_code_block = not in_code_block
                    # Добавляем язык, если не указан
                    if line == "```" and not in_code_block:
                        lines[i] = "```"
                    elif line == "```" and in_code_block:
                        lines[i] = "```python"
                elif not in_code_block:
                    # Находим и форматируем инлайн-код с символом ` 
                    # но не экранируем сам инлайн-код
                    new_line = ""
                    j = 0
                    while j < len(line):
                        if j < len(line) - 1 and line[j] == '`' and line[j+1] != '`':
                            # Начало инлайн-кода
                            backtick_end = line.find('`', j + 1)
                            if backtick_end != -1:
                                # Сохраняем инлайн-код без изменений
                                inline_code = line[j:backtick_end + 1]
                                new_line += inline_code
                                j = backtick_end + 1
                            else:
                                # Если нет закрывающей кавычки, добавляем как обычный текст
                                new_line += line[j]
                                j += 1
                        else:
                            # Обычный текст - экранируем Markdown символы кроме звездочек
                            if line[j] == '_':
                                new_line += "\\_"
                            elif line[j] == '[':
                                new_line += "\\["
                            else:
                                new_line += line[j]
                            j += 1
                    
                    lines[i] = new_line
            
            formatted_response = '\n'.join(lines)
        else:
            # Более интеллектуальная обработка текста без кодовых блоков
            # Находим и сохраняем инлайн-код с символом `
            formatted_response = ""
            i = 0
            while i < len(full_response):
                if i < len(full_response) - 1 and full_response[i] == '`' and full_response[i+1] != '`':
                    # Начало инлайн-кода
                    backtick_end = full_response.find('`', i + 1)
                    if backtick_end != -1:
                        # Сохраняем инлайн-код без изменений
                        formatted_response += full_response[i:backtick_end + 1]
                        i = backtick_end + 1
                    else:
                        # Если нет закрывающей кавычки, экранируем
                        formatted_response += "\\`"
                        i += 1
                else:
                    # Обычный текст - экранируем Markdown символы кроме звездочек
                    if full_response[i] == '_':
                        formatted_response += "\\_"
                    elif full_response[i] == '[':
                        formatted_response += "\\["
                    else:
                        formatted_response += full_response[i]
                    i += 1
            original_response = full_response
        
        await bot.edit_message_text(text=formatted_response,
                                   chat_id=str(chat_id),
                                   message_id=loading_message.message_id,
                                   parse_mode="Markdown")
        await save_message(chat_id, "bot", original_response)
    except Exception as e:
        logging.error(f"Ошибка при обработке: {e}")
        await bot.edit_message_text(
            text="⚠️ Произошла ошибка, попробуйте снова.",
            chat_id=str(chat_id),
            message_id=loading_message.message_id)


@dp.message(F.text & ~(F.text.in_({"💾 Сохранить", "🗑️ Очистить", "❓ Помощь"})))
async def handle_text(message: types.Message):
    await process_content(message, "text")


@dp.message(F.photo)
async def handle_photo(message: types.Message):
    await process_content(message, "photo")


@dp.message(F.audio)
async def handle_audio(message: types.Message):
    await process_content(message, "audio")


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
