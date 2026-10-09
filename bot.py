import asyncio
import json
import sqlite3
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
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

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot is running!".encode("utf-8"))

    def log_message(self, format, *args):
        pass

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
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
        [KeyboardButton(text="🎯 Мой главный фокус"), KeyboardButton(text="⚙️ Часовой пояс")]
    ],
    resize_keyboard=True
)

def get_timezone_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇰🇿 Казахстан (UTC+5)", callback_data="set_tz_Asia/Almaty"),
                InlineKeyboardButton(text="🇷🇺 Москва (UTC+3)", callback_data="set_tz_Europe/Moscow")
            ],
            [
                InlineKeyboardButton(text="🇺🇿 Ташкент (UTC+5)", callback_data="set_tz_Asia/Tashkent"),
                InlineKeyboardButton(text="🇰🇬 Бишкек (UTC+6)", callback_data="set_tz_Asia/Bishkek")
            ],
            [
                InlineKeyboardButton(text="🇷🇺 Екатеринбург (UTC+5)", callback_data="set_tz_Asia/Yekaterinburg"),
                InlineKeyboardButton(text="🇷🇺 Новосибирск (UTC+7)", callback_data="set_tz_Asia/Novosibirsk")
            ],
            [
                InlineKeyboardButton(text="🇪🇺 Варшава / Берлин (UTC+1)", callback_data="set_tz_Europe/Warsaw"),
                InlineKeyboardButton(text="🇺🇦 Киев (UTC+2)", callback_data="set_tz_Europe/Kyiv")
            ]
        ]
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

def format_friendly_time(dt: datetime, tz_name: str) -> str:
    user_tz = ZoneInfo(tz_name)
    now = datetime.now(user_tz)
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
            goal TEXT,
            timezone TEXT DEFAULT 'Asia/Almaty'
        )
    """)
    # Авто-добавление колонки timezone, если таблица была создана ранее
    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if "timezone" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Asia/Almaty'")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            task TEXT,
            reason TEXT,
            run_date TEXT,
            timezone TEXT DEFAULT 'Asia/Almaty'
        )
    """)
    cursor.execute("PRAGMA table_info(tasks)")
    t_columns = [row[1] for row in cursor.fetchall()]
    if "timezone" not in t_columns:
        cursor.execute("ALTER TABLE tasks ADD COLUMN timezone TEXT DEFAULT 'Asia/Almaty'")

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
    return row[0] if (row and row[0]) else "твое развитие и спокойствие"

def save_user_tz(user_id: int, tz: str):
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (user_id, timezone) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET timezone=excluded.timezone
    """, (user_id, tz))
    conn.commit()
    conn.close()

def get_user_tz(user_id: int) -> str:
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if (row and row[0]) else "Asia/Almaty"

def add_task_to_db(user_id: int, chat_id: int, task: str, reason: str, run_date: datetime, tz_name: str) -> int:
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tasks (user_id, chat_id, task, reason, run_date, timezone)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, chat_id, task, reason, run_date.strftime("%Y-%m-%d %H:%M:%S"), tz_name))
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
    cursor.execute("SELECT id, task, run_date, timezone FROM tasks WHERE user_id = ? ORDER BY run_date ASC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --- ЛОГИКА НАПОМИНАНИЙ ---
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

def restore_scheduled_jobs():
    conn = sqlite3.connect("reminders.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, chat_id, task, reason, run_date, timezone FROM tasks")
    rows = cursor.fetchall()
    for row in rows:
        t_id, u_id, c_id, task, reason, r_str, tz_name = row
        tz_name = tz_name or "Asia/Almaty"
        user_tz = ZoneInfo(tz_name)
        r_dt = datetime.strptime(r_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=user_tz)
        now = datetime.now(user_tz)
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

# --- ПАРСЕР С УЧЕТОМ ЧАСОВОГО ПОЯСА ---
async def parse_with_ai(user_message: str, tz_name: str):
    user_tz = ZoneInfo(tz_name)
    now = datetime.now(user_tz)
    text_lower = user_message.lower().strip()

    norm_text = text_lower
    norm_text = re.sub(r"\bчерез\s+секунду\b", "через 1 секунду", norm_text)
    norm_text = re.sub(r"\bчерез\s+минуту\b", "через 1 минуту", norm_text)
    norm_text = re.sub(r"\bчерез\s+(час|часик)\b", "через 1 час", norm_text)
    norm_text = re.sub(r"\bчерез\s+полчаса\b", "через 30 минут", norm_text)
    norm_text = re.sub(r"\bминуток\b", "минут", norm_text)
    norm_text = re.sub(r"\bсекундок\b", "секунд", norm_text)

    # Быстрый парсинг секунд
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

    # Регулярные выражения времени
    time_regex = r"(через\s+[\w\d\s]+?(?:мин\w*|час\w*|дн\w*|день)|завтра\s+в\s+\d{1,2}:\d{2}|в\s+\d{1,2}:\d{2})"
    match = re.search(time_regex, norm_text)
    if match:
        phrase = match.group(1)
        phrase_fixed = re.sub(r"\b(мин\w*|час\w*)\s+через\s+(\d+)", r"через \2 \1", phrase)
        naive_dt = dateparser.parse(
            phrase_fixed, 
            languages=['ru'], 
            settings={'PREFER_DATES_FROM': 'future', 'RELATIVE_BASE': now.replace(tzinfo=None)}
        )
        
        if naive_dt:
            parsed_dt = naive_dt.replace(tzinfo=user_tz)
            if parsed_dt <= now and "завтра" not in phrase_fixed:
                parsed_dt += timedelta(days=1)

            task_clean = re.sub(time_regex, "", user_message, flags=re.IGNORECASE)
            task_clean = re.sub(r"^(напомни|надо|хочу|короче|плиз|пожалуйста|пж|бы|мне)\b\s*", "", task_clean.strip(), flags=re.IGNORECASE)
            task_clean = re.sub(r"\s*\b(напомни|плиз|пожалуйста|пж)\b$", "", task_clean.strip(), flags=re.IGNORECASE).strip(" ,.-!?")

            return {
                "task": task_clean.capitalize() if clean_task else None,
                "target_datetime": parsed_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": None
            }

    # AI Fallback (Gemini)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    prompt = f"""
Ты — модуль извлечения задач и времени.
Текущее местное время пользователя: {now_str}.

Извлеки:
1. "task": суть задачи. С большой буквы. Если во фразе нет действия — верни null.
2. "target_datetime": дата и время "ГГГГ-ММ-ДД ЧЧ:ММ:СС" по местному времени пользователя.
   - "через N минут/часов" -> прибавь к текущему времени {now_str}.
   - "в 18:42" -> если время сегодня уже прошло, перенеси на завтра.
   - Если точного времени нет -> null.
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

async def send_confirmation_prompt(message: Message, user_id: int, task: str, run_date: datetime, tz_name: str, reason: str = None):
    friendly_str = format_friendly_time(run_date, tz_name)
    
    pending_confirmations[user_id] = {
        "chat_id": message.chat.id,
        "task": task,
        "run_date": run_date,
        "timezone": tz_name,
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

# --- ОБРАБОТЧИКИ ХЕНДЛЕРОВ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    tz = get_user_tz(message.from_user.id)
    await message.answer(
        f"👋 Салам! Я твой умный AI-бот-напоминалка.\n\n"
        f"🌍 Твой часовой пояс: `{tz}`\n\n"
        "Пиши задачи свободным текстом:\n"
        "• _Покушать через 40 минут_\n"
        "• _Завтра в 11:30 созвон_\n"
        "• _Тренировка_ (время выберешь кнопкой)",
        reply_markup=main_menu_kb,
        parse_mode="Markdown"
    )

@dp.message(F.text == "⚙️ Часовой пояс")
async def choose_timezone_cmd(message: Message):
    current_tz = get_user_tz(message.from_user.id)
    await message.answer(
        f"🌍 Текущий часовой пояс: **{current_tz}**\n\n"
        "Выбери свой регион из списка ниже:",
        reply_markup=get_timezone_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("set_tz_"))
async def process_tz_selection(callback: CallbackQuery):
    tz_val = callback.data.replace("set_tz_", "")
    save_user_tz(callback.from_user.id, tz_val)
    now_in_tz = datetime.now(ZoneInfo(tz_val)).strftime("%H:%M")
    await callback.message.edit_text(
        f"✅ Часовой пояс изменён на **{tz_val}**!\n"
        f"Твоё текущее время: **{now_in_tz}**",
        parse_mode="Markdown"
    )
    await callback.answer()

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

@dp.message(Command("tasks"))
@dp.message(Command("my"))
@dp.message(F.text == "📋 Мои напоминания")
async def show_my_tasks(message: Message):
    tasks = get_user_tasks(message.from_user.id)
    if not tasks:
        await message.answer("У тебя пока нет активных напоминаний 📭")
        return

    await message.answer("📋 **Твои запланированные дела:**", parse_mode="Markdown")
    for t_id, task, r_str, t_tz in tasks:
        t_tz = t_tz or "Asia/Almaty"
        user_tz = ZoneInfo(t_tz)
        r_dt = datetime.strptime(r_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=user_tz)
        friendly = format_friendly_time(r_dt, t_tz)
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

@dp.message(F.text)
async def handle_ai_message(message: Message, state: FSMContext):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    user_id = message.from_user.id
    tz_name = get_user_tz(user_id)
    user_tz = ZoneInfo(tz_name)
    current_state = await state.get_state()
    
    if user_id in pending_confirmations or current_state == ReminderFlow.waiting_for_custom_time:
        prev_task = pending_confirmations.get(user_id, {}).get("task")
        if not prev_task:
            data = await state.get_data()
            prev_task = data.get("task", "Дело")
            
        parsed = await parse_with_ai(f"{prev_task} {message.text}", tz_name)
        if parsed and parsed.get("target_datetime"):
            dt_format = "%Y-%m-%d %H:%M:%S" if len(parsed["target_datetime"]) > 16 else "%Y-%m-%d %H:%M"
            naive_dt = datetime.strptime(parsed["target_datetime"], dt_format)
            run_date = naive_dt.replace(tzinfo=user_tz)
            await state.clear()
            await send_confirmation_prompt(message, user_id, prev_task, run_date, tz_name, parsed.get("reason"))
            return

    parsed = await parse_with_ai(message.text, tz_name)
    
    if not parsed or (not parsed.get("task") and not parsed.get("target_datetime")):
        await message.answer("Не уловил задачу 🤔 Напиши, что сделать (например: *«покушать через часик»* или *«проверить отчет в 18:00»*).", parse_mode="Markdown")
        return
        
    task = parsed.get("task")
    target_dt_str = parsed.get("target_datetime")
    reason = parsed.get("reason")
    
    if target_dt_str and task:
        dt_format = "%Y-%m-%d %H:%M:%S" if len(target_dt_str) > 16 else "%Y-%m-%d %H:%M"
        naive_dt = datetime.strptime(target_dt_str, dt_format)
        run_date = naive_dt.replace(tzinfo=user_tz)
        await send_confirmation_prompt(message, user_id, task, run_date, tz_name, reason)
        return

    if task:
        await state.update_data(task=task)
        await state.set_state(ReminderFlow.waiting_for_custom_time)
        await message.answer(
            f"📌 Понял задачу: **«{task}»**.\n\n"
            "Когда напомнить? Нажми кнопку или напиши время (например: `сегодня в 18:42` или `через 10 минут`):",
            reply_markup=get_time_selection_kb(),
            parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("ai_time_"))
async def handle_quick_time_buttons(callback: CallbackQuery, state: FSMContext):
    time_key = callback.data.replace("ai_time_", "")
    tz_name = get_user_tz(callback.from_user.id)
    user_tz = ZoneInfo(tz_name)
    now = datetime.now(user_tz)
    
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
    await send_confirmation_prompt(callback.message, callback.from_user.id, task, run_date, tz_name)
    await callback.answer()

@dp.callback_query(F.data.in_(["ai_conf_yes", "ai_conf_no"]))
async def handle_ai_confirmation(callback: CallbackQuery):
    user_id = callback.from_user.id
    item = pending_confirmations.get(user_id)
    
    if not item:
        await callback.message.edit_text("⚠️ Время подтверждения вышло, напиши задачу заново.")
        await callback.answer()
        return

    if callback.data == "ai_conf_yes":
        task_id = add_task_to_db(
            user_id, item["chat_id"], item["task"], item["reason"], item["run_date"], item["timezone"]
        )
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

async def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    init_db()
    restore_scheduled_jobs()
    scheduler.start()
    print("AI-бот запущен со списком задач и поддержкой часовых поясов!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())