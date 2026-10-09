import asyncio
import json
import sqlite3
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
import dateparser
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, 
    CallbackQuery, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardMarkup, 
    KeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai

# --- ЗАГРУЗКА .ENV ---
BASE_DIR = Path(__file__).resolve().parent
if (BASE_DIR / ".env").exists():
    load_dotenv(BASE_DIR / ".env")
elif (BASE_DIR / ".env.txt").exists():
    load_dotenv(BASE_DIR / ".env.txt")
else:
    load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

pending_confirmations = {}

# --- СОСТОЯНИЯ (FSM) ---
class ReminderFlow(StatesGroup):
    waiting_for_custom_time = State()

class GoalFlow(StatesGroup):
    waiting_for_goal = State()

# --- КЛАВИАТУРЫ ---
main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои напоминания")],
        [KeyboardButton(text="🎯 Мой главный фокус")]
    ],
    resize_keyboard=True
)

def get_time_selection_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="15 минут", callback_data="ai_time_15m"),
                InlineKeyboardButton(text="1 час", callback_data="ai_time_1h"),
                InlineKeyboardButton(text="3 часа", callback_data="ai_time_3h")
            ],
            [
                InlineKeyboardButton(text="Завтра в 09:00", callback_data="ai_time_tomorrow"),
                InlineKeyboardButton(text="1 минута (тест)", callback_data="ai_time_1m")
            ]
        ]
    )

# --- ЧЕЛОВЕЧЕСКИЙ ФОРМАТ ДАТЫ ---
def format_friendly_time(dt: datetime) -> str:
    now = datetime.now()
    today = now.date()
    target_date = dt.date()
    time_str = dt.strftime("%H:%M")
    
    diff_sec = (dt - now).total_seconds()
    if 0 <= diff_sec < 90:
        return f"через {int(max(diff_sec, 1))} сек."

    if target_date == today:
        return f"сегодня в {time_str}"
    elif target_date == today + timedelta(days=1):
        return f"завтра в {time_str}"
    elif target_date == today + timedelta(days=2):
        return f"послезавтра в {time_str}"
    else:
        return dt.strftime("%d.%m в %H:%M")

# --- БАЗА ДАННЫХ (SQLite) ---
def init_db():
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            goal TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            task TEXT,
            reason TEXT,
            run_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_user_goal(user_id: int, goal: str):
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, goal) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal
    """, (user_id, goal))
    conn.commit()
    conn.close()

def get_user_goal(user_id: int) -> str:
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT goal FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "твое развитие и спокойствие"

def add_task_to_db(user_id: int, chat_id: int, task: str, reason: str, run_date: datetime) -> int:
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tasks (user_id, chat_id, task, reason, run_date)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, chat_id, task, reason, run_date.strftime("%Y-%m-%d %H:%M:%S")))
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return task_id

def delete_task_from_db(task_id: int):
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()

def get_user_tasks(user_id: int):
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, task, run_date FROM tasks WHERE user_id = ? ORDER BY run_date ASC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- ОТПРАВКА НАПОМИНАНИЯ ---
async def send_reminder_job(task_id: int, chat_id: int, user_id: int, task: str, reason: str):
    delete_task_from_db(task_id)
    goal = get_user_goal(user_id)
    text = (
        f"🔔 **Напоминание!**\n\n"
        f"📌 Пора сделать: **{task}**\n"
    )
    if reason:
        text += f"💬 *Причина:* {reason}\n"
        
    text += f"\n🎯 Твой ориентир: _{goal}_\nСделай это прямо сейчас и будь свободен!"
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

# Восстановление задач при перезапуске бота
def restore_scheduled_jobs():
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, chat_id, task, reason, run_date FROM tasks")
    rows = cursor.fetchall()
    now = datetime.now()
    for row in rows:
        t_id, u_id, c_id, task, reason, r_str = row
        r_dt = datetime.strptime(r_str, "%Y-%m-%d %H:%M:%S")
        if r_dt > now:
            scheduler.add_job(
                send_reminder_job,
                trigger="date",
                run_date=r_dt,
                args=[t_id, c_id, u_id, task, reason],
                id=f"task_{t_id}"
            )
        else:
            cursor.execute("DELETE FROM tasks WHERE id = ?", (t_id,))
    conn.commit()
    conn.close()

# --- ГИБРИДНЫЙ ПАРСЕР СООБЩЕНИЙ ---
async def parse_with_ai(user_message: str):
    text_lower = user_message.lower().strip()
    now = datetime.now()

    # 1. Нормализация слов
    norm_text = text_lower
    norm_text = re.sub(r"\bчерез\s+секунду\b", "через 1 секунду", norm_text)
    norm_text = re.sub(r"\bчерез\s+минуту\b", "через 1 минуту", norm_text)
    norm_text = re.sub(r"\bчерез\s+(час|часик)\b", "через 1 час", norm_text)
    norm_text = re.sub(r"\bчерез\s+полчаса\b", "через 30 минут", norm_text)
    norm_text = re.sub(r"\bминуток\b", "минут", norm_text)
    norm_text = re.sub(r"\bсекундок\b", "секунд", norm_text)

    # 2. Секунды (локально, 0.001 сек)
    sec_match = re.search(r"(?:через\s+(\d+)\s*сек\w*|через\s+сек\w*\s+(\d+)|\bсек\w*\s+через\s+(\d+))", norm_text)
    if sec_match:
        secs = int(sec_match.group(1) or sec_match.group(2) or sec_match.group(3))
        target_dt = now + timedelta(seconds=secs)
        clean_task = re.sub(r"(?:через\s+\d+\s*сек\w*|через\s+сек\w*\s+\d+|\bсек\w*\s+через\s+\d+|через\s+секунду)", "", user_message, flags=re.IGNORECASE)
        clean_task = re.sub(r"^(напомни|надо|хочу|короче|плиз|пожалуйста|пж|бы|мне)\b\s*", "", clean_task.strip(), flags=re.IGNORECASE)
        clean_task = re.sub(r"\s*\b(напомни|плиз|пожалуйста|пж)\b$", "", clean_task.strip(), flags=re.IGNORECASE).strip(" ,.-!?")
        return {
            "task": clean_task.capitalize() if clean_task else None,
            "target_datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": None
        }

    # 3. Минуты / часы (локально через dateparser)
    time_regex = r"(через\s+[\w\d\s]+?(?:мин\w*|час\w*|дн\w*|день)|завтра\s+в\s+\d{1,2}:\d{2}|в\s+\d{1,2}:\d{2})"
    match = re.search(time_regex, norm_text)
    if match:
        phrase = match.group(1)
        phrase_fixed = re.sub(r"\b(мин\w*|час\w*)\s+через\s+(\d+)", r"через \2 \1", phrase)
        parsed_dt = dateparser.parse(phrase_fixed, languages=['ru'], settings={'PREFER_DATES_FROM': 'future'})
        
        if parsed_dt:
            if parsed_dt <= now and "завтра" not in phrase_fixed:
                parsed_dt += timedelta(days=1)

            task_clean = re.sub(time_regex, "", user_message, flags=re.IGNORECASE)
            task_clean = re.sub(r"^(напомни|надо|хочу|короче|плиз|пожалуйста|пж|бы|мне)\b\s*", "", task_clean.strip(), flags=re.IGNORECASE)
            task_clean = re.sub(r"\s*\b(напомни|плиз|пожалуйста|пж)\b$", "", task_clean.strip(), flags=re.IGNORECASE).strip(" ,.-!?")

            return {
                "task": task_clean.capitalize() if task_clean else None,
                "target_datetime": parsed_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": None
            }

    # 4. Сложные формулировки через Gemini
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""
Ты — модуль извлечения задач и времени.
Время сервера сейчас: {now_str}.

Извлеки:
1. "task": суть задачи (без мата, сленга, 'напомни'). С большой буквы. Если во фразе нет действия — верни null.
2. "target_datetime": дата и время "ГГГГ-ММ-ДД ЧЧ:ММ:СС".
   - "через N минут/часов" -> прибавь к текущему времени.
   - "в 18:42" -> если время прошло, перенеси на завтра.
   - Если времени нет -> null.
3. "reason": причина если есть, иначе null.

Сообщение: "{user_message}"

Ответь СТРОГО JSON:
{{"task": "Название", "target_datetime": "2026-10-09 16:30:00", "reason": null}}
"""
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                ai_client.models.generate_content,
                model="gemini-3.8-flash",
                contents=prompt
            ),
            timeout=4.0
        )
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`").replace("json\n", "", 1).strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"Ошибка Gemini API: {e}")
        return None

# --- ПОДТВЕРЖДЕНИЕ ЗАДАЧИ ---
async def send_confirmation_prompt(message: Message, user_id: int, task: str, run_date: datetime, reason: str = None):
    friendly_str = format_friendly_time(run_date)
    
    pending_confirmations[user_id] = {
        "chat_id": message.chat.id,
        "task": task,
        "run_date": run_date,
        "reason": reason,
        "friendly_str": friendly_str
    }
    
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, всё верно ✅", callback_data="ai_conf_yes"),
                InlineKeyboardButton(text="Отмена ❌", callback_data="ai_conf_no")
            ]
        ]
    )
    
    reply_text = f"🎯 Задача: **{task}**\n⏰ Напомнить: **{friendly_str}**"
    if reason:
        reply_text += f"\n💡 Причина: _{reason}_"
    reply_text += "\n\nСтавим напоминание?"
    
    await message.answer(reply_text, reply_markup=confirm_kb, parse_mode="Markdown")

# --- ХЭНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Салам! Я твой умный AI-бот-напоминалка.\n\n"
        "Пиши задачи как обычному человеку:\n"
        "• _Покушать через 40 минут_\n"
        "• _Завтра в 11:30 сдать отчет_\n"
        "• Или просто напиши дело: _План тренировка_ — время выберешь кнопкой.",
        reply_markup=main_menu_kb,
        parse_mode="Markdown"
    )

# 1. Смена фокуса / ориентира
@dp.message(F.text == "🎯 Мой главный фокус")
@dp.message(F.text.lower().contains("фокус") | F.text.lower().contains("ориентир") | F.text.lower().contains("цель"))
async def handle_goal_menu(message: Message, state: FSMContext):
    goal = get_user_goal(message.from_user.id)
    await message.answer(
        f"🎯 Твой текущий фокус:\n«{goal}»\n\n"
        "Напиши новый фокус одним сообщением, если хочешь изменить:",
        parse_mode="Markdown"
    )
    await state.set_state(GoalFlow.waiting_for_goal)

@dp.message(GoalFlow.waiting_for_goal)
async def save_new_goal(message: Message, state: FSMContext):
    save_user_goal(message.from_user.id, message.text.strip())
    await state.clear()
    await message.answer("✅ Фокус сохранен!", reply_markup=main_menu_kb)

# 2. Просмотр списка задач
@dp.message(Command("tasks"))
@dp.message(Command("my"))
@dp.message(F.text == "📋 Мои напоминания")
async def show_my_tasks(message: Message):
    tasks = get_user_tasks(message.from_user.id)
    if not tasks:
        await message.answer("У тебя пока нет активных напоминаний 📭")
        return

    await message.answer("📋 **Твои запланированные дела:**", parse_mode="Markdown")
    for t_id, task, r_str in tasks:
        r_dt = datetime.strptime(r_str, "%Y-%m-%d %H:%M:%S")
        friendly = format_friendly_time(r_dt)
        del_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Удалить 🗑", callback_data=f"del_task_{t_id}")]]
        )
        await message.answer(f"📌 **{task}**\n⏰ {friendly}", reply_markup=del_kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_task_"))
async def handle_delete_task(callback: CallbackQuery):
    task_id = int(callback.data.replace("del_task_", ""))
    delete_task_from_db(task_id)
    try:
        scheduler.remove_job(f"task_{task_id}")
    except Exception:
        pass
    await callback.message.edit_text("❌ Напоминание удалено.")
    await callback.answer()

# 3. Основной роутер сообщений
@dp.message(F.text)
async def handle_ai_message(message: Message, state: FSMContext):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    user_id = message.from_user.id
    current_state = await state.get_state()
    
    # Если висит задача и прислали только время
    if user_id in pending_confirmations or current_state == ReminderFlow.waiting_for_custom_time:
        prev_task = pending_confirmations.get(user_id, {}).get("task")
        if not prev_task:
            data = await state.get_data()
            prev_task = data.get("task", "Дело")
            
        parsed = await parse_with_ai(f"{prev_task} {message.text}")
        if parsed and parsed.get("target_datetime"):
            dt_format = "%Y-%m-%d %H:%M:%S" if len(parsed["target_datetime"]) > 16 else "%Y-%m-%d %H:%M"
            run_date = datetime.strptime(parsed["target_datetime"], dt_format)
            await state.clear()
            await send_confirmation_prompt(message, user_id, prev_task, run_date, parsed.get("reason"))
            return

    parsed = await parse_with_ai(message.text)
    
    if not parsed or (not parsed.get("task") and not parsed.get("target_datetime")):
        await message.answer("Не уловил задачу 🤔 Напиши, что сделать (например: *«покушать через часик»* или *«проверить отчет»*).", parse_mode="Markdown")
        return
        
    task = parsed.get("task")
    target_dt_str = parsed.get("target_datetime")
    reason = parsed.get("reason")
    
    if target_dt_str and task:
        dt_format = "%Y-%m-%d %H:%M:%S" if len(target_dt_str) > 16 else "%Y-%m-%d %H:%M"
        run_date = datetime.strptime(target_dt_str, dt_format)
        await send_confirmation_prompt(message, user_id, task, run_date, reason)
        return

    if task:
        await state.update_data(task=task)
        await state.set_state(ReminderFlow.waiting_for_custom_time)
        await message.answer(
            f"📌 Понял задачу: **«{task}»**.\n\n"
            "Когда напомнить? Нажми кнопку или напиши в чат (например: `сегодня в 18:42` или `через 10 минут`):",
            reply_markup=get_time_selection_kb(),
            parse_mode="Markdown"
        )

# Быстрые кнопки времени
@dp.callback_query(F.data.startswith("ai_time_"))
async def handle_quick_time_buttons(callback: CallbackQuery, state: FSMContext):
    time_key = callback.data.replace("ai_time_", "")
    now = datetime.now()
    
    if time_key == "1m":
        run_date = now + timedelta(minutes=1)
    elif time_key == "15m":
        run_date = now + timedelta(minutes=15)
    elif time_key == "1h":
        run_date = now + timedelta(hours=1)
    elif time_key == "3h":
        run_date = now + timedelta(hours=3)
    elif time_key == "tomorrow":
        tomorrow = now + timedelta(days=1)
        run_date = tomorrow.replace(hour=9, minute=0, second=0)
    else:
        run_date = now + timedelta(hours=1)

    data = await state.get_data()
    task = data.get("task", "Дело")
    await state.clear()
    
    await callback.message.delete()
    await send_confirmation_prompt(callback.message, callback.from_user.id, task, run_date)
    await callback.answer()

# Подтверждение
@dp.callback_query(F.data.in_(["ai_conf_yes", "ai_conf_no"]))
async def handle_ai_confirmation(callback: CallbackQuery):
    user_id = callback.from_user.id
    item = pending_confirmations.get(user_id)
    
    if not item:
        await callback.message.edit_text("⚠️ Время подтверждения вышло, напиши задачу заново.")
        await callback.answer()
        return

    if callback.data == "ai_conf_yes":
        task_id = add_task_to_db(user_id, item["chat_id"], item["task"], item["reason"], item["run_date"])
        scheduler.add_job(
            send_reminder_job,
            trigger="date",
            run_date=item["run_date"],
            args=[task_id, item["chat_id"], user_id, item["task"], item["reason"]],
            id=f"task_{task_id}"
        )
        await callback.message.edit_text(
            f"✅ Записано! **{item['friendly_str']}** я напомню про: **«{item['task']}»**.",
            parse_mode="Markdown"
        )
    else:
        await callback.message.edit_text("❌ Напоминание отменено.")

    del pending_confirmations[user_id]
    await callback.answer()

# --- СТАРТ ---
async def main():
    init_db()
    restore_scheduled_jobs()
    scheduler.start()
    print("AI-бот запущен со списком задач и сохранением в БД!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())