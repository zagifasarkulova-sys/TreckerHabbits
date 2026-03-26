"""
🎯 Трекер Привычек — Telegram Bot
Деплой: Render.com (Docker)
Токен: переменная окружения BOT_TOKEN
"""

import os
import asyncio
import logging
from datetime import datetime, date
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]  # Render → Environment → BOT_TOKEN
DB_PATH = "/data/habits.db"          # persistent disk на Render
TZ = os.getenv("TZ", "Europe/Moscow")
REMINDER_START_HOUR = 21
REMINDER_INTERVAL_MINUTES = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("habit_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler(timezone=TZ)

# Состояния пользователей (в памяти)
user_states = {}


# ═══════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                target INTEGER NOT NULL DEFAULT 1,
                initial_target INTEGER NOT NULL DEFAULT 1,
                cycle_days INTEGER NOT NULL DEFAULT 10,
                step INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS check_ins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(habit_id, check_date),
                FOREIGN KEY (habit_id) REFERENCES habits(id)
            )
        """)
        await db.commit()
    logger.info(f"✅ Database ready: {DB_PATH}")


async def get_user_habits(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM habits WHERE user_id = ? AND is_active = 1 ORDER BY id",
            (user_id,)
        )
        return await cursor.fetchall()


async def add_habit(user_id: int, name: str, target: int, cycle_days: int, step: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO habits (user_id, name, target, initial_target, cycle_days, step, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, target, target, cycle_days, step, date.today().isoformat())
        )
        await db.commit()
        return cursor.lastrowid


async def delete_habit(habit_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE habits SET is_active = 0 WHERE id = ? AND user_id = ?",
            (habit_id, user_id)
        )
        await db.commit()


async def get_today_checkins(user_id: int) -> dict:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT habit_id, status FROM check_ins WHERE user_id = ? AND check_date = ?",
            (user_id, today)
        )
        rows = await cursor.fetchall()
        return {row["habit_id"]: row["status"] for row in rows}


async def set_checkin(habit_id: int, user_id: int, status: str):
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO check_ins (habit_id, user_id, check_date, status)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(habit_id, check_date) DO UPDATE SET status = ?""",
            (habit_id, user_id, today, status, status)
        )
        await db.commit()


async def get_habit_by_id(habit_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_habit_stats(habit_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt FROM check_ins WHERE habit_id = ? GROUP BY status",
            (habit_id,)
        )
        rows = await cursor.fetchall()
        stats = {"done": 0, "not_done": 0, "skip": 0}
        for row in rows:
            stats[row["status"]] = row["cnt"]
        return stats


async def get_all_stats(user_id: int) -> list:
    habits = await get_user_habits(user_id)
    result = []
    for h in habits:
        stats = await get_habit_stats(h["id"])
        total = stats["done"] + stats["not_done"] + stats["skip"]
        streak = await get_streak(h["id"])
        result.append({
            "name": h["name"],
            "target": compute_current_target(dict(h)),
            "done": stats["done"],
            "not_done": stats["not_done"],
            "skip": stats["skip"],
            "total": total,
            "rate": round(stats["done"] / total * 100) if total > 0 else 0,
            "streak": streak,
        })
    return result


async def get_streak(habit_id: int) -> int:
    """Считает текущий streak (подряд дней 'done')"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT check_date, status FROM check_ins
               WHERE habit_id = ? ORDER BY check_date DESC""",
            (habit_id,)
        )
        rows = await cursor.fetchall()

    streak = 0
    for row in rows:
        if row[1] == "done":
            streak += 1
        else:
            break
    return streak


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def compute_current_target(habit: dict) -> int:
    if habit["step"] == 0:
        return habit["target"]
    created = date.fromisoformat(habit["created_at"])
    days_passed = (date.today() - created).days
    cycles_completed = days_passed // habit["cycle_days"]
    return habit["initial_target"] + (habit["step"] * cycles_completed)


def get_day_in_cycle(habit: dict) -> str:
    created = date.fromisoformat(habit["created_at"])
    days_passed = (date.today() - created).days
    day_in_cycle = (days_passed % habit["cycle_days"]) + 1
    return f"{day_in_cycle}/{habit['cycle_days']}"


def progress_bar(percent: int, length: int = 10) -> str:
    filled = round(percent / 100 * length)
    return "▓" * filled + "░" * (length - filled)


# ═══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════

def main_menu_kb(has_habits: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if has_habits:
        rows.append([InlineKeyboardButton(text="✍️ Отметить привычки", callback_data="checkin_start")])
        rows.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="📋 Мои привычки", callback_data="my_habits"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Новая привычка", callback_data="add_habit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkin_kb(habit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сделано", callback_data=f"done_{habit_id}"),
            InlineKeyboardButton(text="❌ Не сделано", callback_data=f"notdone_{habit_id}"),
        ],
        [InlineKeyboardButton(text="⏭ Пропуск", callback_data=f"skip_{habit_id}")],
    ])


def progression_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Да", callback_data="prog_yes"),
            InlineKeyboardButton(text="➡️ Нет", callback_data="prog_no"),
        ],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Меню", callback_data="menu")],
    ])


async def habits_list_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_user_habits(user_id)
    rows = []
    for h in habits:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить «{h['name']}»",
            callback_data=f"del_{h['id']}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ═══════════════════════════════════════════════════════════════════

async def build_main(user_id: int) -> str:
    habits = await get_user_habits(user_id)
    checkins = await get_today_checkins(user_id)
    total = len(habits)
    done_count = sum(1 for h in habits if h["id"] in checkins)
    today_str = date.today().strftime("%d.%m.%Y")

    status_map = {"done": "✅", "not_done": "❌", "skip": "⏭"}

    lines = [
        f"🎯 <b>Трекер Привычек</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"📅 {today_str}  ·  Отмечено: <b>{done_count}/{total}</b>",
        "",
    ]

    if not habits:
        lines.append("🫙 <i>Пусто! Нажми ➕ и добавь первую привычку</i>")
    else:
        for h in habits:
            ct = compute_current_target(dict(h))
            icon = status_map.get(checkins.get(h["id"], ""), "⬜")
            lines.append(f"  {icon}  {h['name']}  ·  🎯 {ct}")

    lines.append("")
    lines.append(f"{'━' * 18}")
    return "\n".join(lines)


async def build_habits_detail(user_id: int) -> str:
    habits = await get_user_habits(user_id)
    if not habits:
        return "📋 <b>Мои привычки</b>\n\n🫙 <i>Список пуст</i>"

    lines = ["📋 <b>Мои привычки</b>", ""]
    for h in habits:
        hd = dict(h)
        ct = compute_current_target(hd)
        day = get_day_in_cycle(hd)
        step = f" (+{h['step']})" if h["step"] > 0 else ""

        lines.append(f"▸ <b>{h['name']}</b>  🎯 {ct}{step}")
        lines.append(f"   📆 день {day}  ·  с {h['created_at']}")
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════════

@router.message(Command("start", "menu"))
async def cmd_start(message: Message):
    user_states.pop(message.from_user.id, None)
    habits = await get_user_habits(message.from_user.id)
    text = await build_main(message.from_user.id)
    await message.answer(text, reply_markup=main_menu_kb(bool(habits)))


# ─── Menu ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    user_states.pop(cb.from_user.id, None)
    habits = await get_user_habits(cb.from_user.id)
    text = await build_main(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=main_menu_kb(bool(habits)))
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    user_states.pop(cb.from_user.id, None)
    habits = await get_user_habits(cb.from_user.id)
    text = await build_main(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=main_menu_kb(bool(habits)))
    await cb.answer("✖️ Отменено")


# ─── Add Habit ────────────────────────────────────────────────────

@router.callback_query(F.data == "add_habit")
async def cb_add(cb: CallbackQuery):
    user_states[cb.from_user.id] = {"state": "name", "data": {}}
    await cb.message.edit_text(
        "✏️ <b>Новая привычка</b>\n\nВведи название:",
        reply_markup=cancel_kb()
    )
    await cb.answer()


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "name")
async def on_name(msg: Message):
    uid = msg.from_user.id
    name = msg.text.strip()
    if not name or len(name) > 64:
        return await msg.answer("⚠️ Название: 1–64 символа. Попробуй ещё:")

    user_states[uid]["data"]["name"] = name
    user_states[uid]["state"] = "progression"

    await msg.answer(
        f"📈 <b>Прогрессия</b>\n\n"
        f"Добавить числовую цель с автоматическим увеличением?\n"
        f"<i>Например: 20 отжиманий, +2 каждые 10 дней</i>",
        reply_markup=progression_kb()
    )


@router.callback_query(F.data == "prog_no")
async def cb_prog_no(cb: CallbackQuery):
    uid = cb.from_user.id
    st = user_states.get(uid, {})
    if st.get("state") != "progression":
        return await cb.answer("⚠️ Нет активного действия")

    name = st["data"]["name"]
    await add_habit(uid, name, target=1, cycle_days=10, step=0)
    user_states.pop(uid, None)

    habits = await get_user_habits(uid)
    await cb.message.edit_text(
        f"✅ Привычка «<b>{name}</b>» добавлена!\n\n"
        f"Без прогрессии — просто отмечай каждый день 💪",
        reply_markup=main_menu_kb(bool(habits))
    )
    await cb.answer("✅ Добавлено!")


@router.callback_query(F.data == "prog_yes")
async def cb_prog_yes(cb: CallbackQuery):
    uid = cb.from_user.id
    st = user_states.get(uid, {})
    if st.get("state") != "progression":
        return await cb.answer("⚠️ Нет активного действия")

    user_states[uid]["state"] = "target"
    await cb.message.edit_text(
        "🔢 <b>Начальная цель</b>\n\n"
        "Введи начальное количество (число):\n"
        "<i>Например: 20</i>",
        reply_markup=cancel_kb()
    )
    await cb.answer()


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "target")
async def on_target(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val > 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    user_states[uid]["data"]["target"] = val
    user_states[uid]["state"] = "cycle"
    await msg.answer(
        "🗓 <b>Длина цикла</b>\n\n"
        "Через сколько дней увеличивать цель?\n"
        "<i>Например: 10</i>",
        reply_markup=cancel_kb()
    )


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "cycle")
async def on_cycle(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val > 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    user_states[uid]["data"]["cycle_days"] = val
    user_states[uid]["state"] = "step"
    await msg.answer(
        "📈 <b>Шаг прогрессии</b>\n\n"
        "На сколько увеличивать цель каждый цикл?\n"
        "<i>Например: 2</i>",
        reply_markup=cancel_kb()
    )


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "step")
async def on_step(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val >= 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    d = user_states[uid]["data"]
    await add_habit(uid, d["name"], d["target"], d["cycle_days"], val)
    user_states.pop(uid, None)

    habits = await get_user_habits(uid)
    await msg.answer(
        f"✅ Привычка «<b>{d['name']}</b>» добавлена!\n\n"
        f"🎯 Цель: {d['target']}\n"
        f"🗓 Цикл: {d['cycle_days']} дней\n"
        f"📈 Шаг: +{val} каждый цикл",
        reply_markup=main_menu_kb(bool(habits))
    )


# ─── Check-in ────────────────────────────────────────────────────

@router.callback_query(F.data == "checkin_start")
async def cb_checkin(cb: CallbackQuery):
    await _show_next_checkin(cb)


@router.callback_query(F.data.startswith("done_"))
async def cb_done(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "done")
    await cb.answer("✅ Сделано!")
    await _show_next_checkin(cb)


@router.callback_query(F.data.startswith("notdone_"))
async def cb_notdone(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "not_done")
    await cb.answer("❌ Не сделано")
    await _show_next_checkin(cb)


@router.callback_query(F.data.startswith("skip_"))
async def cb_skip(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "skip")
    await cb.answer("⏭ Пропущено")
    await _show_next_checkin(cb)


async def _show_next_checkin(cb: CallbackQuery):
    uid = cb.from_user.id
    habits = await get_user_habits(uid)
    checkins = await get_today_checkins(uid)
    unchecked = [h for h in habits if h["id"] not in checkins]

    if not unchecked:
        done_count = len(habits)
        await cb.message.edit_text(
            f"🎉 <b>Все {done_count} привычек отмечены!</b>\n\n"
            f"Ты огонь! 🔥🔥🔥 Так держать!",
            reply_markup=main_menu_kb(True)
        )
        return

    h = unchecked[0]
    hd = dict(h)
    ct = compute_current_target(hd)
    day = get_day_in_cycle(hd)
    left = len(unchecked)

    await cb.message.edit_text(
        f"📋 <b>Отметка привычек</b>  ({left} осталось)\n"
        f"{'━' * 20}\n\n"
        f"❓ <b>{h['name']}</b>\n"
        f"🎯 Цель: {ct}  ·  📆 День {day}\n\n"
        f"Выполнена сегодня?",
        reply_markup=checkin_kb(h["id"])
    )


# ─── My Habits ────────────────────────────────────────────────────

@router.callback_query(F.data == "my_habits")
async def cb_my_habits(cb: CallbackQuery):
    text = await build_habits_detail(cb.from_user.id)
    kb = await habits_list_kb(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("del_"))
async def cb_del(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    habit = await get_habit_by_id(hid)
    if habit and habit["user_id"] == cb.from_user.id:
        await delete_habit(hid, cb.from_user.id)
        await cb.answer(f"🗑 «{habit['name']}» удалена")
    else:
        await cb.answer("⚠️ Не найдена")

    text = await build_habits_detail(cb.from_user.id)
    kb = await habits_list_kb(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)


# ─── Statistics ──────────────────────────────────────────────────

@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    stats = await get_all_stats(cb.from_user.id)

    if not stats:
        text = "📊 <b>Статистика</b>\n\n🫙 <i>Нет данных</i>"
    else:
        lines = ["📊 <b>Статистика</b>", ""]
        for s in stats:
            bar = progress_bar(s["rate"])
            fire = "🔥" if s["streak"] >= 3 else ""
            lines.append(f"▸ <b>{s['name']}</b>  🎯 {s['target']}")
            lines.append(f"   {bar}  {s['rate']}%")
            lines.append(f"   ✅ {s['done']}  ❌ {s['not_done']}  ⏭ {s['skip']}  🔗 {s['streak']}д {fire}")
            lines.append("")
        text = "\n".join(lines)

    await cb.message.edit_text(text, reply_markup=back_menu_kb())
    await cb.answer()


# ═══════════════════════════════════════════════════════════════════
#  SPAM REMINDERS  21:00 → 00:00 каждые 2 мин
# ═══════════════════════════════════════════════════════════════════

REMINDER_MESSAGES = [
    "🔔 Эй! У тебя есть неотмеченные привычки! Не ленись 💪",
    "⏰ Тик-так! Привычки ждут отметки! Давай! 🚀",
    "😤 Ну ты чего?! Отметь привычки уже! 🔥",
    "🫵 ДА, ТЫ! Привычки сами себя не отметят!",
    "💀 Полночь близко… Отметь привычки пока не поздно!",
    "🚨🚨🚨 ТРЕВОГА! Неотмеченные привычки! 🚨🚨🚨",
    "😈 Я буду спамить пока не отметишь. Ты меня знаешь.",
    "🐌 Даже улитка быстрее отмечает привычки…",
    "⚡️ Осталось немного до полуночи. ДЕЙСТВУЙ!",
    "🫠 Каждые 2 минуты. Пока. Не. Отметишь.",
]

_reminder_counter = {}


async def send_reminders():
    now = datetime.now()
    if now.hour < REMINDER_START_HOUR:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT DISTINCT user_id FROM habits WHERE is_active = 1"
        )
        users = await cursor.fetchall()

    for (user_id,) in users:
        try:
            habits = await get_user_habits(user_id)
            checkins = await get_today_checkins(user_id)
            unchecked = [h for h in habits if h["id"] not in checkins]

            if not unchecked:
                _reminder_counter.pop(user_id, None)
                continue

            idx = _reminder_counter.get(user_id, 0) % len(REMINDER_MESSAGES)
            _reminder_counter[user_id] = idx + 1

            names = "\n".join(f"  ▸ {h['name']}" for h in unchecked)
            mins_left = (24 * 60 - now.hour * 60 - now.minute)

            text = (
                f"{REMINDER_MESSAGES[idx]}\n\n"
                f"📌 <b>Не отмечено ({len(unchecked)}):</b>\n{names}\n\n"
                f"⏳ До полуночи: ~{mins_left} мин"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Отметить сейчас!", callback_data="checkin_start")],
            ])
            await bot.send_message(user_id, text, reply_markup=kb)

        except Exception as e:
            logger.warning(f"Reminder failed for {user_id}: {e}")


# ═══════════════════════════════════════════════════════════════════
#  FALLBACK
# ═══════════════════════════════════════════════════════════════════

@router.message()
async def fallback(message: Message):
    if message.from_user.id in user_states:
        return
    habits = await get_user_habits(message.from_user.id)
    await message.answer(
        "🤔 Используй кнопки или /start",
        reply_markup=main_menu_kb(bool(habits))
    )


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

async def health_server():
    """Мини HTTP-сервер для Render (health check)"""
    from aiohttp import web

    async def handle(request):
        return web.Response(text="🎯 Habit Bot is running!")

    app = web.Application()
    app.router.add_get("/", handle)

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Health server on port {port}")


async def main():
    await init_db()

    scheduler.add_job(
        send_reminders,
        "interval",
        minutes=REMINDER_INTERVAL_MINUTES,
        id="spam",
        replace_existing=True,
    )
    scheduler.start()

    # HTTP-сервер для Render health check
    await health_server()

    logger.info("🚀 Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
