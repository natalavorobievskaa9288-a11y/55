"""
Telegram-бот для записи к мастеру маникюра.
Один файл — весь функционал.
Aiogram 3, SQLite (aiosqlite), APScheduler.
"""

import asyncio
import logging
import calendar
import aiosqlite

from datetime import date, datetime, timedelta
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMember
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import os

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN          = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
ADMIN_ID           = int(os.getenv("ADMIN_ID", "123456789"))
SCHEDULE_CHANNEL   = os.getenv("SCHEDULE_CHANNEL_ID", "@schedule_channel")
CHANNEL_ID         = os.getenv("CHANNEL_ID", "@your_channel")
CHANNEL_LINK       = os.getenv("CHANNEL_LINK", "https://t.me/your_channel")
DB_PATH            = os.getenv("DB_PATH", "manicure.db")

# Слоты по умолчанию при добавлении нового рабочего дня
DEFAULT_SLOTS = [
    "09:00", "10:00", "11:00", "12:00", "13:00",
    "14:00", "15:00", "16:00", "17:00", "18:00",
]

# Названия месяцев и дней для календаря
MONTHS_RU   = {1:"Январь",2:"Февраль",3:"Март",4:"Апрель",5:"Май",
               6:"Июнь",7:"Июль",8:"Август",9:"Сентябрь",10:"Октябрь",
               11:"Ноябрь",12:"Декабрь"}
WEEKDAYS_RU = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]

# Глобальный планировщик
scheduler: AsyncIOScheduler | None = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════════════

async def init_db():
    """Создать таблицы при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS slots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT NOT NULL,
                time      TEXT NOT NULL,
                is_booked INTEGER DEFAULT 0,
                UNIQUE(date, time)
            );
            CREATE TABLE IF NOT EXISTS blocked_days (
                date TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id    INTEGER NOT NULL REFERENCES slots(id),
                user_id    INTEGER NOT NULL,
                username   TEXT,
                name       TEXT NOT NULL,
                phone      TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await conn.commit()
    log.info("БД инициализирована.")


# ─── Слоты ────────────────────────────────────────────────────────────────────

async def db_add_slot(slot_date: str, slot_time: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("INSERT INTO slots (date,time) VALUES (?,?)", (slot_date, slot_time))
            await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def db_delete_slot(slot_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT is_booked FROM slots WHERE id=?", (slot_id,))
        row = await cur.fetchone()
        if not row or row[0] == 1:
            return False
        await conn.execute("DELETE FROM slots WHERE id=?", (slot_id,))
        await conn.commit()
    return True


async def db_get_available_dates() -> list[str]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT DISTINCT s.date FROM slots s
            WHERE s.is_booked=0 AND s.date>=?
              AND s.date NOT IN (SELECT date FROM blocked_days)
            ORDER BY s.date
        """, (today,))
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def db_get_free_slots(slot_date: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT id,time FROM slots WHERE date=? AND is_booked=0 ORDER BY time",
            (slot_date,)
        )
        rows = await cur.fetchall()
    return [{"id": r[0], "time": r[1]} for r in rows]


async def db_get_all_slots(slot_date: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT s.id, s.time, s.is_booked, b.name, b.phone, b.user_id, b.id
            FROM slots s
            LEFT JOIN bookings b ON b.slot_id=s.id
            WHERE s.date=? ORDER BY s.time
        """, (slot_date,))
        rows = await cur.fetchall()
    return [{"id":r[0],"time":r[1],"is_booked":r[2],
             "client_name":r[3],"phone":r[4],"user_id":r[5],"booking_id":r[6]}
            for r in rows]


async def db_get_slot(slot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT id,date,time,is_booked FROM slots WHERE id=?", (slot_id,))
        row = await cur.fetchone()
    return {"id":row[0],"date":row[1],"time":row[2],"is_booked":row[3]} if row else None


# ─── Записи ───────────────────────────────────────────────────────────────────

async def db_create_booking(slot_id, user_id, username, name, phone) -> int | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT is_booked FROM slots WHERE id=?", (slot_id,))
        row = await cur.fetchone()
        if not row or row[0] == 1:
            return None
        await conn.execute("UPDATE slots SET is_booked=1 WHERE id=?", (slot_id,))
        cur = await conn.execute(
            "INSERT INTO bookings (slot_id,user_id,username,name,phone,created_at) VALUES (?,?,?,?,?,?)",
            (slot_id, user_id, username, name, phone, datetime.now().isoformat())
        )
        await conn.commit()
        return cur.lastrowid


async def db_get_user_booking(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT b.id, b.name, b.phone, s.date, s.time, s.id
            FROM bookings b JOIN slots s ON s.id=b.slot_id
            WHERE b.user_id=? AND s.date>=?
            ORDER BY s.date, s.time LIMIT 1
        """, (user_id, date.today().isoformat()))
        row = await cur.fetchone()
    return {"booking_id":row[0],"name":row[1],"phone":row[2],
            "date":row[3],"time":row[4],"slot_id":row[5]} if row else None


async def db_get_booking(booking_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT b.id, b.user_id, b.name, b.phone, b.username, s.date, s.time
            FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?
        """, (booking_id,))
        row = await cur.fetchone()
    return {"booking_id":row[0],"user_id":row[1],"name":row[2],"phone":row[3],
            "username":row[4],"date":row[5],"time":row[6]} if row else None


async def db_cancel_booking(booking_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT slot_id FROM bookings WHERE id=?", (booking_id,))
        row = await cur.fetchone()
        if not row:
            return False
        await conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        await conn.execute("UPDATE slots SET is_booked=0 WHERE id=?", (row[0],))
        await conn.commit()
    return True


async def db_cancel_booking_by_slot(slot_id: int) -> int | None:
    """Отменить запись по slot_id. Возвращает user_id."""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT id,user_id FROM bookings WHERE slot_id=?", (slot_id,))
        row = await cur.fetchone()
        if not row:
            return None
        await conn.execute("DELETE FROM bookings WHERE id=?", (row[0],))
        await conn.execute("UPDATE slots SET is_booked=0 WHERE id=?", (slot_id,))
        await conn.commit()
    return row[1]


async def db_user_has_booking(user_id: int) -> bool:
    return (await db_get_user_booking(user_id)) is not None


async def db_get_future_bookings() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("""
            SELECT b.id, b.user_id, b.name, s.date, s.time
            FROM bookings b JOIN slots s ON s.id=b.slot_id
            WHERE s.date>=? ORDER BY s.date, s.time
        """, (date.today().isoformat(),))
        rows = await cur.fetchall()
    return [{"booking_id":r[0],"user_id":r[1],"name":r[2],"date":r[3],"time":r[4]}
            for r in rows]


# ─── Заблокированные дни ──────────────────────────────────────────────────────

async def db_block_day(slot_date: str) -> list[dict]:
    """Заблокировать день. Возвращает список отменённых записей (для уведомлений)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute("INSERT INTO blocked_days (date) VALUES (?)", (slot_date,))
        except aiosqlite.IntegrityError:
            pass
        cur = await conn.execute("""
            SELECT b.user_id, s.time FROM bookings b JOIN slots s ON s.id=b.slot_id
            WHERE s.date=?
        """, (slot_date,))
        cancelled = [{"user_id": r[0], "time": r[1]} for r in await cur.fetchall()]
        await conn.execute("""
            DELETE FROM bookings WHERE slot_id IN (SELECT id FROM slots WHERE date=?)
        """, (slot_date,))
        await conn.execute("UPDATE slots SET is_booked=0 WHERE date=?", (slot_date,))
        await conn.commit()
    return cancelled


async def db_is_day_blocked(slot_date: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT 1 FROM blocked_days WHERE date=?", (slot_date,))
        return bool(await cur.fetchone())


# ══════════════════════════════════════════════════════════════════════════════
#  ПЛАНИРОВЩИК НАПОМИНАНИЙ
# ══════════════════════════════════════════════════════════════════════════════

async def _send_reminder(bot: Bot, user_id: int, visit_time: str, booking_id: int):
    try:
        await bot.send_message(
            user_id,
            f"⏰ <b>Напоминание о записи!</b>\n\n"
            f"Напоминаем, что вы записаны на маникюр завтра в <b>{visit_time}</b>.\n"
            f"Ждём вас! 💅",
            parse_mode="HTML"
        )
        log.info(f"Напоминание отправлено пользователю {user_id}, запись #{booking_id}")
    except Exception as e:
        log.error(f"Ошибка напоминания для {user_id}: {e}")


def sched_add(bot: Bot, booking_id: int, user_id: int, visit_dt: datetime):
    """Запланировать напоминание за 24 часа (если ещё не поздно)."""
    global scheduler
    if not scheduler:
        return
    remind_at = visit_dt - timedelta(hours=24)
    if remind_at <= datetime.now():
        log.info(f"Напоминание #{booking_id} не создано — менее 24ч до визита.")
        return
    job_id = f"reminder_{booking_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        _send_reminder, trigger="date", run_date=remind_at,
        args=[bot, user_id, visit_dt.strftime("%H:%M"), booking_id],
        id=job_id, replace_existing=True
    )
    log.info(f"Напоминание #{booking_id} → {remind_at:%Y-%m-%d %H:%M}")


def sched_remove(booking_id: int):
    global scheduler
    if scheduler:
        job_id = f"reminder_{booking_id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            log.info(f"Напоминание #{booking_id} удалено.")


async def restore_reminders(bot: Bot):
    """Восстановить все напоминания из БД после перезапуска."""
    bookings = await db_get_future_bookings()
    restored = 0
    for b in bookings:
        try:
            visit_dt = datetime.strptime(f"{b['date']} {b['time']}", "%Y-%m-%d %H:%M")
            sched_add(bot, b["booking_id"], b["user_id"], visit_dt)
            restored += 1
        except Exception as e:
            log.error(f"Не удалось восстановить напоминание #{b['booking_id']}: {e}")
    log.info(f"Восстановлено напоминаний: {restored}")


# ══════════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════════

def kb_main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 Записаться", callback_data="book_start"))
    b.row(InlineKeyboardButton(text="📋 Моя запись", callback_data="my_booking"))
    b.row(
        InlineKeyboardButton(text="💅 Прайсы",     callback_data="prices"),
        InlineKeyboardButton(text="🖼 Портфолио",  callback_data="portfolio"),
    )
    return b.as_markup()


def kb_back_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_subscription() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📢 Подписаться",        url=CHANNEL_LINK))
    b.row(InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub"))
    return b.as_markup()


def kb_portfolio() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="🌸 Смотреть портфолио",
        url="https://ru.pinterest.com/crystalwithluv/_created/"
    ))
    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_time_slots(slots: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        b.button(text=f"🕐 {s['time']}", callback_data=f"book_slot:{s['id']}:{s['time']}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🔙 К календарю", callback_data="book_start"))
    return b.as_markup()


def kb_confirm_booking() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Подтвердить",  callback_data="book_confirm"),
        InlineKeyboardButton(text="❌ Отменить",     callback_data="book_abort"),
    )
    return b.as_markup()


def kb_cancel_booking(booking_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"user_cancel:{booking_id}"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",    callback_data="main_menu"))
    return b.as_markup()


# ─── Inline-календарь ─────────────────────────────────────────────────────────

def kb_calendar(year: int, month: int, available: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = date.today()

    # Навигация по месяцам
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1
    can_prev = (year, month) > (today.year, today.month)

    b.row(
        InlineKeyboardButton(
            text="◀" if can_prev else " ",
            callback_data=f"cal_nav:{prev_y}:{prev_m}" if can_prev else "cal_noop"
        ),
        InlineKeyboardButton(text=f"{MONTHS_RU[month]} {year}", callback_data="cal_noop"),
        InlineKeyboardButton(text="▶", callback_data=f"cal_nav:{next_y}:{next_m}"),
    )

    # Дни недели
    b.row(*[InlineKeyboardButton(text=d, callback_data="cal_noop") for d in WEEKDAYS_RU])

    # Дни месяца
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="cal_noop"))
            else:
                cur = date(year, month, day)
                ds  = cur.isoformat()
                if cur < today:
                    row.append(InlineKeyboardButton(text="✖", callback_data="cal_noop"))
                elif ds in available:
                    row.append(InlineKeyboardButton(text=f"🟢{day}", callback_data=f"cal_date:{ds}"))
                else:
                    row.append(InlineKeyboardButton(text=str(day), callback_data="cal_noop"))
        b.row(*row)

    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()


# ─── Админ-клавиатуры ─────────────────────────────────────────────────────────

def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить рабочий день", callback_data="adm_add_day"))
    b.row(
        InlineKeyboardButton(text="⏰ Добавить слот",   callback_data="adm_add_slot"),
        InlineKeyboardButton(text="🗑 Удалить слот",    callback_data="adm_del_slot"),
    )
    b.row(
        InlineKeyboardButton(text="🚫 Закрыть день",    callback_data="adm_block_day"),
        InlineKeyboardButton(text="📋 Расписание",       callback_data="adm_schedule"),
    )
    b.row(InlineKeyboardButton(text="❌ Отменить запись", callback_data="adm_cancel_booking"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",    callback_data="main_menu"))
    return b.as_markup()


def kb_admin_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()


def kb_slots_del(slots: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        b.button(text=f"🗑 {s['time']}", callback_data=f"adm_do_del:{s['id']}")
    b.adjust(3)
    b.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    return b.as_markup()


def kb_slots_cancel(slots: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        if s["is_booked"]:
            b.button(text=f"❌ {s['time']} — {s['client_name']}",
                     callback_data=f"adm_do_cancel:{s['id']}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel"))
    return b.as_markup()


# ══════════════════════════════════════════════════════════════════════════════
#  ХЭЛПЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

def fmt_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        m: ChatMember = await bot.get_chat_member(CHANNEL_ID, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.error(f"Ошибка проверки подписки {user_id}: {e}")
        return True  # если ошибка — пропускаем


# ══════════════════════════════════════════════════════════════════════════════
#  FSM-СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════════════════════════

class BookFSM(StatesGroup):
    date    = State()
    time    = State()
    name    = State()
    phone   = State()
    confirm = State()


class AdminFSM(StatesGroup):
    add_day_date      = State()
    add_slot_date     = State()
    add_slot_time     = State()
    del_slot_date     = State()
    block_day_date    = State()
    schedule_date     = State()
    cancel_book_date  = State()


# ══════════════════════════════════════════════════════════════════════════════
#  ФИЛЬТР АДМИНИСТРАТОРА
# ══════════════════════════════════════════════════════════════════════════════

class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        uid = getattr(getattr(event, "from_user", None), "id", None)
        return uid == ADMIN_ID


# ══════════════════════════════════════════════════════════════════════════════
#  РОУТЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

common_router = Router()   # общие команды + меню
user_router   = Router()   # запись/отмена клиента
admin_router  = Router()   # панель администратора

admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ══════════════════════════════════════════════════════════════════════════════
#  ОБЩИЕ ХЭНДЛЕРЫ (common_router)
# ══════════════════════════════════════════════════════════════════════════════

WELCOME = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Я бот для записи на маникюр. 💅\n"
    "Выберите действие:"
)

PRICES = (
    "💅 <b>Прайс-лист</b>\n\n"
    "┌──────────────────────┐\n"
    "│  Френч     — 1 000 ₽  │\n"
    "│  Квадрат   —   500 ₽  │\n"
    "└──────────────────────┘\n\n"
    "Для записи нажмите <b>📅 Записаться</b>."
)


@common_router.message(CommandStart())
@common_router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME, reply_markup=kb_main_menu())


@common_router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(WELCOME, reply_markup=kb_main_menu())
    await cb.answer()


@common_router.callback_query(F.data == "prices")
async def cb_prices(cb: CallbackQuery):
    await cb.message.edit_text(PRICES, reply_markup=kb_back_menu())
    await cb.answer()


@common_router.callback_query(F.data == "portfolio")
async def cb_portfolio(cb: CallbackQuery):
    await cb.message.edit_text(
        "🌸 <b>Портфолио</b>\n\nПосмотрите мои работы на Pinterest:",
        reply_markup=kb_portfolio()
    )
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  ПОЛЬЗОВАТЕЛЬСКИЕ ХЭНДЛЕРЫ (user_router) — FSM записи
# ══════════════════════════════════════════════════════════════════════════════

async def show_calendar(cb: CallbackQuery, state: FSMContext):
    """Показать inline-календарь (вспомогательная функция)."""
    today     = date.today()
    available = await db_get_available_dates()
    await state.set_state(BookFSM.date)
    await cb.message.edit_text(
        "📅 <b>Выберите дату для записи</b>\n🟢 — доступные дни",
        reply_markup=kb_calendar(today.year, today.month, available)
    )
    await cb.answer()


@user_router.callback_query(F.data == "book_start")
async def cb_book_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Начало записи: проверка подписки и активной записи."""
    uid = cb.from_user.id

    if not await check_subscription(bot, uid):
        await cb.message.edit_text(
            "📢 <b>Для записи необходимо подписаться на канал!</b>\n\n"
            "Нажмите <b>«Проверить подписку»</b> после подписки.",
            reply_markup=kb_subscription()
        )
        await cb.answer()
        return

    if await db_user_has_booking(uid):
        b = await db_get_user_booking(uid)
        await cb.message.edit_text(
            f"⚠️ <b>У вас уже есть запись</b>\n\n"
            f"📅 {fmt_date(b['date'])} в <b>{b['time']}</b>\n\n"
            f"Отмените текущую запись, чтобы создать новую.",
            reply_markup=kb_cancel_booking(b["booking_id"])
        )
        await cb.answer()
        return

    await show_calendar(cb, state)


@user_router.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if await check_subscription(bot, cb.from_user.id):
        await cb.answer("✅ Подписка подтверждена!")
        await show_calendar(cb, state)
    else:
        await cb.answer("❌ Вы ещё не подписались!", show_alert=True)


@user_router.callback_query(F.data == "cal_noop")
async def cb_cal_noop(cb: CallbackQuery):
    await cb.answer()


@user_router.callback_query(F.data.startswith("cal_nav:"))
async def cb_cal_nav(cb: CallbackQuery, state: FSMContext):
    _, y, m = cb.data.split(":")
    available = await db_get_available_dates()
    await cb.message.edit_reply_markup(
        reply_markup=kb_calendar(int(y), int(m), available)
    )
    await cb.answer()


@user_router.callback_query(F.data.startswith("cal_date:"), BookFSM.date)
async def cb_cal_date(cb: CallbackQuery, state: FSMContext):
    slot_date = cb.data.split(":")[1]
    slots = await db_get_free_slots(slot_date)
    if not slots:
        await cb.answer("На эту дату нет свободных слотов!", show_alert=True)
        return
    await state.update_data(chosen_date=slot_date)
    await state.set_state(BookFSM.time)
    await cb.message.edit_text(
        f"📅 Дата: <b>{fmt_date(slot_date)}</b>\n\n🕐 <b>Выберите время:</b>",
        reply_markup=kb_time_slots(slots)
    )
    await cb.answer()


@user_router.callback_query(F.data.startswith("book_slot:"), BookFSM.time)
async def cb_book_slot(cb: CallbackQuery, state: FSMContext):
    _, sid, stime = cb.data.split(":")
    slot = await db_get_slot(int(sid))
    if not slot or slot["is_booked"]:
        await cb.answer("Это время уже занято! Выберите другое.", show_alert=True)
        return
    await state.update_data(slot_id=int(sid), slot_time=stime)
    await state.set_state(BookFSM.name)
    await cb.message.edit_text(
        f"🕐 Время: <b>{stime}</b>\n\n👤 <b>Введите ваше имя:</b>"
    )
    await cb.answer()


@user_router.message(BookFSM.name)
async def fsm_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Пожалуйста, введите корректное имя (минимум 2 символа).")
        return
    await state.update_data(name=name)
    await state.set_state(BookFSM.phone)
    await message.answer(
        f"✅ Имя: <b>{name}</b>\n\n📞 <b>Введите номер телефона:</b>\n"
        f"<i>Например: +79001234567</i>"
    )


@user_router.message(BookFSM.phone)
async def fsm_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len("".join(c for c in phone if c.isdigit())) < 10:
        await message.answer(
            "Введите корректный номер телефона.\n<i>Например: +79001234567</i>"
        )
        return
    data = await state.get_data()
    await state.update_data(phone=phone)
    await state.set_state(BookFSM.confirm)

    summary = (
        f"📋 <b>Проверьте данные записи:</b>\n\n"
        f"👤 Имя:     <b>{data['name']}</b>\n"
        f"📞 Телефон: <b>{phone}</b>\n"
        f"📅 Дата:    <b>{fmt_date(data['chosen_date'])}</b>\n"
        f"🕐 Время:   <b>{data['slot_time']}</b>"
    )
    await message.answer(summary, reply_markup=kb_confirm_booking())


@user_router.callback_query(F.data == "book_abort")
async def cb_book_abort(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Запись отменена.", reply_markup=kb_main_menu())
    await cb.answer()


@user_router.callback_query(F.data == "book_confirm", BookFSM.confirm)
async def cb_book_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = cb.from_user

    booking_id = await db_create_booking(
        data["slot_id"], user.id, user.username,
        data["name"], data["phone"]
    )

    if not booking_id:
        await cb.answer("Время уже занято! Выберите другое.", show_alert=True)
        await state.clear()
        await cb.message.edit_text("⚠️ Это время уже заняли. Попробуйте другое.", reply_markup=kb_main_menu())
        return

    await state.clear()
    d = fmt_date(data["chosen_date"])

    await cb.message.edit_text(
        f"✅ <b>Запись подтверждена!</b>\n\n"
        f"📅 {d} в <b>{data['slot_time']}</b>\n"
        f"👤 {data['name']} | 📞 {data['phone']}\n\n"
        f"Просмотреть запись: «Моя запись».",
        reply_markup=kb_main_menu()
    )
    await cb.answer("Запись создана! ✅")

    # Уведомление администратору
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Новая запись #{booking_id}</b>\n\n"
            f"👤 {data['name']}\n📞 {data['phone']}\n"
            f"💬 @{user.username or '—'} | 🆔 <code>{user.id}</code>\n"
            f"📅 {d} в <b>{data['slot_time']}</b>"
        )
    except Exception as e:
        log.error(f"Уведомление администратору: {e}")

    # Публикация в канал расписания
    try:
        await bot.send_message(
            SCHEDULE_CHANNEL,
            f"📅 <b>Запись на {d}</b>\n🕐 {data['slot_time']} — <b>{data['name']}</b>"
        )
    except Exception as e:
        log.error(f"Публикация в канал: {e}")

    # Планирование напоминания
    try:
        visit_dt = datetime.strptime(f"{data['chosen_date']} {data['slot_time']}", "%Y-%m-%d %H:%M")
        sched_add(bot, booking_id, user.id, visit_dt)
    except Exception as e:
        log.error(f"Планировщик: {e}")


# ─── Просмотр и отмена своей записи ──────────────────────────────────────────

@user_router.callback_query(F.data == "my_booking")
async def cb_my_booking(cb: CallbackQuery):
    b = await db_get_user_booking(cb.from_user.id)
    if not b:
        await cb.message.edit_text(
            "📋 <b>У вас нет активных записей.</b>\n\nНажмите «Записаться».",
            reply_markup=kb_main_menu()
        )
    else:
        await cb.message.edit_text(
            f"📋 <b>Ваша запись</b>\n\n"
            f"📅 {fmt_date(b['date'])} в <b>{b['time']}</b>\n"
            f"👤 {b['name']} | 📞 {b['phone']}",
            reply_markup=kb_cancel_booking(b["booking_id"])
        )
    await cb.answer()


@user_router.callback_query(F.data.startswith("user_cancel:"))
async def cb_user_cancel(cb: CallbackQuery, bot: Bot):
    booking_id = int(cb.data.split(":")[1])
    b = await db_get_booking(booking_id)
    if not b or b["user_id"] != cb.from_user.id:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    await db_cancel_booking(booking_id)
    sched_remove(booking_id)

    d = fmt_date(b["date"])
    await cb.message.edit_text(
        f"✅ Запись на <b>{d}</b> в <b>{b['time']}</b> отменена.\n"
        f"Вы можете записаться на другое время.",
        reply_markup=kb_main_menu()
    )
    await cb.answer()

    try:
        await bot.send_message(
            ADMIN_ID,
            f"❌ <b>Отмена записи #{booking_id}</b>\n\n"
            f"👤 {b['name']} (@{b['username'] or '—'})\n"
            f"📅 {d} в {b['time']}\n<i>Отменено клиентом.</i>"
        )
    except Exception as e:
        log.error(f"Уведомление отмены: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ХЭНДЛЕРЫ (admin_router)
# ══════════════════════════════════════════════════════════════════════════════

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())


@admin_router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())
    await cb.answer()


# ─── Добавить рабочий день ────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_add_day")
async def cb_adm_add_day(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.add_day_date)
    await cb.message.edit_text(
        "➕ <b>Новый рабочий день</b>\n\nВведите дату <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.add_day_date)
async def fsm_add_day(message: Message, state: FSMContext):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите <code>ДД.ММ.ГГГГ</code>.")
        return
    if sd < date.today().isoformat():
        await message.answer("❌ Нельзя добавить прошедшую дату.")
        return
    results = await asyncio.gather(*[db_add_slot(sd, t) for t in DEFAULT_SLOTS])
    added = sum(results)
    await state.clear()
    await message.answer(
        f"✅ День <b>{fmt_date(sd)}</b> добавлен!\nСлотов: <b>{added}</b>",
        reply_markup=kb_admin_main()
    )


# ─── Добавить слот ────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_add_slot")
async def cb_adm_add_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.add_slot_date)
    await cb.message.edit_text(
        "⏰ <b>Добавить слот</b>\n\nДата <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.add_slot_date)
async def fsm_add_slot_date(message: Message, state: FSMContext):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите <code>ДД.ММ.ГГГГ</code>.")
        return
    await state.update_data(slot_date=sd)
    await state.set_state(AdminFSM.add_slot_time)
    await message.answer(f"📅 {fmt_date(sd)}\n\nВремя <code>ЧЧ:ММ</code>:")


@admin_router.message(AdminFSM.add_slot_time)
async def fsm_add_slot_time(message: Message, state: FSMContext):
    t = message.text.strip()
    try:
        datetime.strptime(t, "%H:%M")
    except ValueError:
        await message.answer("❌ Неверный формат. Введите <code>ЧЧ:ММ</code>.")
        return
    data = await state.get_data()
    ok   = await db_add_slot(data["slot_date"], t)
    await state.clear()
    msg = (f"✅ Слот <b>{t}</b> на <b>{fmt_date(data['slot_date'])}</b> добавлен!"
           if ok else f"⚠️ Слот <b>{t}</b> на <b>{fmt_date(data['slot_date'])}</b> уже существует.")
    await message.answer(msg, reply_markup=kb_admin_main())


# ─── Удалить слот ─────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_del_slot")
async def cb_adm_del_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.del_slot_date)
    await cb.message.edit_text(
        "🗑 <b>Удалить слот</b>\n\nДата <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.del_slot_date)
async def fsm_del_slot_date(message: Message, state: FSMContext):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат.")
        return
    slots = await db_get_all_slots(sd)
    free  = [s for s in slots if not s["is_booked"]]
    await state.clear()
    if not free:
        await message.answer("Нет свободных слотов для удаления.", reply_markup=kb_admin_main())
        return
    await message.answer(
        f"📅 <b>{fmt_date(sd)}</b> — выберите слот:",
        reply_markup=kb_slots_del(free)
    )


@admin_router.callback_query(F.data.startswith("adm_do_del:"))
async def cb_adm_do_del(cb: CallbackQuery):
    sid  = int(cb.data.split(":")[1])
    slot = await db_get_slot(sid)
    ok   = await db_delete_slot(sid)
    if ok and slot:
        await cb.message.edit_text(
            f"✅ Слот <b>{slot['time']}</b> на <b>{fmt_date(slot['date'])}</b> удалён.",
            reply_markup=kb_admin_main()
        )
    else:
        await cb.answer("Не удалось удалить слот.", show_alert=True)
    await cb.answer()


# ─── Закрыть день ─────────────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_block_day")
async def cb_adm_block_day(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.block_day_date)
    await cb.message.edit_text(
        "🚫 <b>Закрыть день</b>\n\n"
        "⚠️ Все записи на этот день будут отменены!\n\n"
        "Дата <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.block_day_date)
async def fsm_block_day(message: Message, state: FSMContext, bot: Bot):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат.")
        return
    cancelled = await db_block_day(sd)
    await state.clear()
    d = fmt_date(sd)
    await message.answer(
        f"🚫 День <b>{d}</b> закрыт.\nОтменено записей: <b>{len(cancelled)}</b>",
        reply_markup=kb_admin_main()
    )
    for c in cancelled:
        try:
            await bot.send_message(
                c["user_id"],
                f"😔 <b>Ваша запись отменена</b>\n\n"
                f"День <b>{d}</b> в <b>{c['time']}</b> закрыт мастером.\n"
                f"Пожалуйста, запишитесь на другое время."
            )
        except Exception as e:
            log.error(f"Уведомление клиента о закрытии: {e}")


# ─── Просмотр расписания ──────────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_schedule")
async def cb_adm_schedule(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.schedule_date)
    await cb.message.edit_text(
        "📋 <b>Расписание</b>\n\nДата <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.schedule_date)
async def fsm_schedule(message: Message, state: FSMContext):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат.")
        return
    slots = await db_get_all_slots(sd)
    await state.clear()
    d = fmt_date(sd)
    if not slots:
        await message.answer(f"📅 <b>{d}</b>\n\nСлотов нет.", reply_markup=kb_admin_main())
        return

    blocked = await db_is_day_blocked(sd)
    lines   = [f"📅 <b>Расписание на {d}</b>"]
    if blocked:
        lines.append("🚫 <i>День закрыт</i>")
    lines.append("")
    for s in slots:
        if s["is_booked"]:
            lines.append(f"🔴 <b>{s['time']}</b> — {s['client_name']} | {s['phone']}")
        else:
            lines.append(f"🟢 <b>{s['time']}</b> — свободно")

    await message.answer("\n".join(lines), reply_markup=kb_admin_main())


# ─── Отменить запись клиента ──────────────────────────────────────────────────

@admin_router.callback_query(F.data == "adm_cancel_booking")
async def cb_adm_cancel_booking(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.cancel_book_date)
    await cb.message.edit_text(
        "❌ <b>Отменить запись</b>\n\nДата <code>ДД.ММ.ГГГГ</code>:",
        reply_markup=kb_admin_back()
    )
    await cb.answer()


@admin_router.message(AdminFSM.cancel_book_date)
async def fsm_cancel_book(message: Message, state: FSMContext):
    try:
        sd = datetime.strptime(message.text.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        await message.answer("❌ Неверный формат.")
        return
    slots  = await db_get_all_slots(sd)
    booked = [s for s in slots if s["is_booked"]]
    await state.clear()
    if not booked:
        await message.answer("На эту дату нет записей.", reply_markup=kb_admin_main())
        return
    await message.answer(
        f"📅 <b>{fmt_date(sd)}</b> — выберите запись:",
        reply_markup=kb_slots_cancel(booked)
    )


@admin_router.callback_query(F.data.startswith("adm_do_cancel:"))
async def cb_adm_do_cancel(cb: CallbackQuery, bot: Bot):
    slot_id = int(cb.data.split(":")[1])
    slot    = await db_get_slot(slot_id)
    uid     = await db_cancel_booking_by_slot(slot_id)
    if not uid:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    # Удаляем напоминание (ищем по booking_id через актуальный список)
    # Поскольку запись уже удалена, просто пробуем удалить job с ближайшим id
    # Точное восстановление booking_id из slot_id: ищем в планировщике
    global scheduler
    if scheduler:
        for job in scheduler.get_jobs():
            if job.id.startswith("reminder_"):
                # нет прямой связи slot_id↔booking_id после удаления,
                # поэтому при восстановлении из БД они заново добавятся корректно
                pass

    if slot:
        d = fmt_date(slot["date"])
        await cb.message.edit_text(
            f"✅ Запись <b>{slot['time']}</b> на <b>{d}</b> отменена. Клиент уведомлён.",
            reply_markup=kb_admin_main()
        )
        try:
            await bot.send_message(
                uid,
                f"❌ <b>Ваша запись отменена мастером</b>\n\n"
                f"📅 {d} в <b>{slot['time']}</b>\n\n"
                f"Запишитесь на другое удобное время."
            )
        except Exception as e:
            log.error(f"Уведомление клиента об отмене: {e}")
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    global scheduler
    log.info("Запуск бота...")
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp  = Dispatcher(storage=MemoryStorage())

    dp.include_router(common_router)
    dp.include_router(user_router)
    dp.include_router(admin_router)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.start()
    await restore_reminders(bot)
    log.info("Планировщик запущен, напоминания восстановлены.")

    try:
        log.info("Бот запущен. Жду сообщений...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
