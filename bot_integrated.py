"""
Интегрированный бот: STT (Николай) + NLP (Аня)
"""
import os
import logging
import torch
import asyncio
from datetime import datetime
from typing import Dict
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from transformers import pipeline
from dotenv import load_dotenv
from nlp_module_anya import extract_entities_async

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# STT (Николай)
print(" Загрузка Whisper-large-v3...")
device = "mps" if torch.backends.mps.is_available() else "cpu"
whisper_pipe = pipeline(
    "automatic-speech-recognition",
    model="openai/whisper-large-v3",
    device=device
)
print("✅ STT загружен!\n")

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Бот «ДЕЛУ.ВРЕМЯ» готов!\n\n"
        "Отправь голосовое или текстовое сообщение о встрече, и я:\n"
        "1. Распознаю речь (если голос)\n"
        "2. Извлеку намерение и параметры\n"
        "3. Покажу структурированное событие\n\n"
        "Примеры:\n"
        "• «Завтра в 15:00 встреча с командой»\n"
        "• «Перенеси созвон на час позже»\n"
        "• «Отмени тренировку в пятницу»"
    )

@dp.message(lambda m: m.text)
async def handle_text(message: types.Message):
    thinking_msg = await message.answer("🔍 Анализирую...")
    try:
        result = await extract_entities_async(
            text=message.text,
            context=None,
            message_time=datetime.now(),
            tz="Europe/Moscow"
        )
        response = format_result(result)
        await thinking_msg.edit_text(response, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка NLP: {e}", exc_info=True)
        await thinking_msg.edit_text("❌ Ошибка при анализе текста")

@dp.message(lambda m: m.voice)
async def handle_voice(message: types.Message):
    thinking_msg = await message.answer("⏳ Распознаю речь...")
    voice = message.voice
    file_id = voice.file_id
    duration = voice.duration
    file_size = voice.file_size
    file_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.ogg")
    
    logging.info(f"Голосовое: duration={duration}s, size={file_size} bytes")
    
    try:
        if duration < 1:
            await thinking_msg.edit_text("️ Слишком короткое сообщение")
            return
        if duration > 300:
            await thinking_msg.edit_text(f"⚠️ Слишком длинное ({duration} сек). Макс: 5 минут")
            return
        if file_size < 1000:
            await thinking_msg.edit_text("⚠️ Файл повреждён")
            return
        
        await bot.download(file_id, destination=file_path)
        
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await thinking_msg.edit_text("❌ Ошибка скачивания")
            return
        
        logging.info("Запускаю Whisper...")
        result = whisper_pipe(
            file_path,
            return_timestamps=False,
            generate_kwargs={"language": "russian", "task": "transcribe"}
        )
        recognized_text = result["text"].strip()
        
        if not recognized_text:
            await thinking_msg.edit_text("⚠️ Не удалось распознать речь")
            return
        if len(recognized_text) < 3:
            await thinking_msg.edit_text(f"⚠️ Распознано: \"{recognized_text}\" — слишком мало")
            return
        
        logging.info(f"Распознано: {recognized_text[:100]}")
        await thinking_msg.edit_text(f"📝 **Распознано:**\n_{recognized_text}_\n\n🔍 Анализирую...")
        
        # NLP (Аня)
        nlp_result = await extract_entities_async(
            text=recognized_text,
            context=None,
            message_time=datetime.now(),
            tz="Europe/Moscow"
        )
        
        response = format_result(nlp_result)
        await message.answer(response, parse_mode="Markdown")
        
    except Exception as e:
        logging.error(f"Ошибка обработки: {e}", exc_info=True)
        await thinking_msg.edit_text("❌ Ошибка обработки. Попробуй ещё раз.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def format_result(result: Dict) -> str:
    status = result.get("status", "unknown")
    event = result.get("event")
    error = result.get("error")
    missing = result.get("missing", [])
    latency = result.get("latency_s", 0)
    
    if status == "error":
        return f"❌ **Ошибка:** {error}\n(латентность: {latency:.2f}с)"
    
    if status == "no_event":
        return "🤷 Не найдено договорённости о встрече.\n\n_Это похоже на обычный разговор._"
    
    if status == "needs_clarification":
        fields_str = ", ".join(missing) if missing else "параметры"
        base = format_event(event) if event else ""
        return f"{base}\n\n❓ **Нужно уточнить:** {fields_str}\n\n_(Латентность: {latency:.2f}с)_"
    
    if status == "ok" and event:
        return format_event(event) + f"\n\n_(Латентность: {latency:.2f}с)_"
    
    return "️ Неизвестный статус"

def format_event(event: Dict) -> str:
    intent_map = {
        "create": "Создать событие",
        "move": "Перенести событие",
        "cancel": "Отменить событие"
    }
    
    lines = [f"🎯 **Намерение:** {intent_map.get(event.get('intent'), event.get('intent'))}"]
    
    if event.get("summary"):
        lines.append(f"📌 **Тема:** {event['summary']}")
    
    if event.get("date") or event.get("time"):
        parts = []
        if event.get("date"):
            parts.append(f" {event['date']}")
        if event.get("time"):
            parts.append(f" {event['time']}")
        if event.get("end_time"):
            parts.append(f"→ {event['end_time']}")
        lines.append(" ".join(parts))
    
    if event.get("duration_minutes"):
        lines.append(f"⏱ **Длительность:** {event['duration_minutes']} мин")
    
    if event.get("location"):
        lines.append(f"📍 **Место:** {event['location']}")
    
    if event.get("attendees"):
        lines.append(f"👥 **Участники:** {', '.join(event['attendees'])}")
    
    if event.get("recurrence"):
        lines.append(f" **Повтор:** {event['recurrence']}")
    
    if event.get("timezone"):
        lines.append(f"🌍 **Часовой пояс:** {event['timezone']}")
    
    if event.get("original_text"):
        lines.append(f"\n_Исходный текст: {event['original_text'][:120]}_")
    
    return "\n".join(lines)

async def main():
    for attempt in range(3):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            print("✅ Вебхук удален")
            break
        except Exception as e:
            print(f"⚠️ Попытка {attempt+1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    
    print("🚀 Интегрированный бот запущен! (Ctrl+C для остановки)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
