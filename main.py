"""
Telegram-бот для записи к мастеру — Полина Евдокимова.
Aiogram 3 · SQLite (aiosqlite)

v11 — СИСТЕМА ЗАПИСИ:
✅ Клиент выбирает услугу → пишет мастеру → возвращается в бот → вводит дату и время
✅ Запись уходит в админку со статусом «ожидает»
✅ Ты подтверждаешь запись в админке → клиенту приходит уведомление
✅ Напоминания автоматически: за 24 ч, за 6 ч, за 1 ч до визита
✅ Список всех записей в админке (ожидают / подтверждённые / прошедшие)
✅ Отзывы с модерацией, постоянная авторизация админа, тексты услуг
"""

import asyncio
import logging
import json
import urllib.parse
import aiosqlite

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE FSM STORAGE
# ══════════════════════════════════════════════════════════════════════════════

class SQLiteFSMStorage(BaseStorage):
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock    = asyncio.Lock()

    async def init(self):
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fsm_data (
                    key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.commit()

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None):
        k  = self._key(key)
        sv = state.state if hasattr(state, "state") else (state if isinstance(state, str) else None)
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO fsm_data (key, state, data) VALUES (?, ?, '{}')
                    ON CONFLICT(key) DO UPDATE SET state=excluded.state
                """, (k, sv))
                await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT state FROM fsm_data WHERE key=?", (self._key(key),))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]):
        k = self._key(key)
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO fsm_data (key, state, data) VALUES (?, NULL, ?)
                    ON CONFLICT(key) DO UPDATE SET data=excluded.data
                """, (k, json.dumps(data, ensure_ascii=False)))
                await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT data FROM fsm_data WHERE key=?", (self._key(key),))
            row = await cur.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0]) or {}
        except Exception:
            return {}

    async def close(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN        = "ВАШ_ТОКЕН"
ADMIN_ID         = 123456789
DB_PATH          = "manicure.db"
ADMIN_PASSWORD   = "adinspalina999"
MASTER_USERNAME  = "Evdokimkaaa"
MASTER_NAME_FULL = "Полина Евдокимова"
PORTFOLIO_LINK   = "https://t.me/evdokimovapolinatg"

SERVICES = [
    ("Сложное окрашивание",             "9 000 – 14 000 ₽"),
    ("В один тон",                       "5 000 – 9 000 ₽"),
    ("Окрашивание корней",               "3 500 – 4 000 ₽"),
    ("Тонирование блонда",               "5 000 – 8 000 ₽"),
    ("Осветление корней + тонирование",  "6 000 – 9 000 ₽"),
    ("Глубокий контуринг",              "7 500 – 12 500 ₽"),
    ("Стрижка",                          "2 000 ₽"),
    ("Укладка (брашинг)",                "1 500 ₽"),
    ("Укладка локоны",                   "2 500 – 3 500 ₽"),
]

DEFAULT_SERVICE_TEXTS = [
    "Здравствуйте, я с бота по записи, хочу записаться на сложное окрашивание",
    "Здравствуйте, я с бота по записи, хочу записаться на окрашивание в один тон",
    "Здравствуйте, я с бота по записи, хочу записаться на окрашивание корней",
    "Здравствуйте, я с бота по записи, хочу записаться на тонирование блонда",
    "Здравствуйте, я с бота по записи, хочу записаться на осветление корней + тонирование",
    "Здравствуйте, я с бота по записи, хочу записаться на глубокий контуринг",
    "Здравствуйте, я с бота по записи, хочу записаться на стрижку",
    "Здравствуйте, я с бота по записи, хочу записаться на укладку (брашинг)",
    "Здравствуйте, я с бота по записи, хочу записаться на укладку локоны",
]

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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT,
                first_name TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS service_texts (
                svc_index INTEGER PRIMARY KEY, custom_text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
                user_id INTEGER PRIMARY KEY, authed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL, username TEXT, first_name TEXT,
                rating INTEGER NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
            );
            -- Записи клиентов
            -- status: pending | confirmed | cancelled | done
            CREATE TABLE IF NOT EXISTS bookings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                username     TEXT,
                first_name   TEXT,
                service_name TEXT NOT NULL,
                datetime_txt TEXT NOT NULL,
                appt_dt      TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                reminded_24  INTEGER DEFAULT 0,
                reminded_6   INTEGER DEFAULT 0,
                reminded_1   INTEGER DEFAULT 0,
                created_at   TEXT NOT NULL
            );
        """)
        await db.commit()
    log.info("БД готова.")


# ── Пользователи ──────────────────────────────────────────────────────────────

async def db_save_user(user_id: int, username: str | None, first_name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, created_at) VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
        """, (user_id, username, first_name, datetime.now().isoformat()))
        await db.commit()

async def db_get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id,username,first_name,created_at FROM users ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    return [{"user_id": r[0],"username": r[1],"first_name": r[2],"created_at": r[3]} for r in rows]

async def db_get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def db_count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Тексты услуг ──────────────────────────────────────────────────────────────

async def db_get_service_text(svc_index: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT custom_text FROM service_texts WHERE svc_index=?", (svc_index,))
        row = await cur.fetchone()
    return row[0] if row else DEFAULT_SERVICE_TEXTS[svc_index]

async def db_set_service_text(svc_index: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO service_texts (svc_index,custom_text) VALUES (?,?)", (svc_index, text))
        await db.commit()

async def db_reset_service_text(svc_index: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM service_texts WHERE svc_index=?", (svc_index,))
        await db.commit()


# ── Авторизация ───────────────────────────────────────────────────────────────

async def db_admin_add(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO admin_sessions (user_id,authed_at) VALUES (?,?)",
                         (user_id, datetime.now().isoformat()))
        await db.commit()

async def db_admin_check(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admin_sessions WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None


# ── Отзывы ────────────────────────────────────────────────────────────────────

async def db_add_review(user_id, username, first_name, rating, text) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO reviews (user_id,username,first_name,rating,text,status,created_at)
            VALUES (?,?,?,?,?,'pending',?)
        """, (user_id, username, first_name, rating, text, datetime.now().isoformat()))
        await db.commit()
        return cur.lastrowid

async def db_get_approved_reviews() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,rating,text,created_at
            FROM reviews WHERE status='approved' ORDER BY created_at DESC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "rating":r[4],"text":r[5],"created_at":r[6]} for r in rows]

async def db_get_pending_reviews() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,rating,text,created_at
            FROM reviews WHERE status='pending' ORDER BY created_at ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "rating":r[4],"text":r[5],"created_at":r[6]} for r in rows]

async def db_set_review_status(review_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))
        await db.commit()

async def db_count_approved_reviews() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM reviews WHERE status='approved'")
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Записи ────────────────────────────────────────────────────────────────────

async def db_add_booking(user_id, username, first_name, service_name, datetime_txt) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO bookings (user_id,username,first_name,service_name,datetime_txt,status,created_at)
            VALUES (?,?,?,?,?,'pending',?)
        """, (user_id, username, first_name, service_name, datetime_txt, datetime.now().isoformat()))
        await db.commit()
        return cur.lastrowid

async def db_confirm_booking(booking_id: int, appt_dt: str):
    """Подтвердить запись и задать дату визита в формате ISO (для напоминаний)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET status='confirmed', appt_dt=? WHERE id=?",
            (appt_dt, booking_id)
        )
        await db.commit()

async def db_cancel_booking(booking_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        await db.commit()

async def db_get_pending_bookings() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,service_name,datetime_txt,created_at
            FROM bookings WHERE status='pending' ORDER BY created_at ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "service_name":r[4],"datetime_txt":r[5],"created_at":r[6]} for r in rows]

async def db_get_upcoming_bookings() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,service_name,datetime_txt,appt_dt,created_at
            FROM bookings WHERE status='confirmed' ORDER BY appt_dt ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "service_name":r[4],"datetime_txt":r[5],"appt_dt":r[6],"created_at":r[7]} for r in rows]

async def db_get_bookings_for_reminders() -> list[dict]:
    """Все подтверждённые записи у которых ещё есть несосланные напоминания."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,service_name,appt_dt,reminded_24,reminded_6,reminded_1
            FROM bookings
            WHERE status='confirmed' AND appt_dt IS NOT NULL
              AND (reminded_24=0 OR reminded_6=0 OR reminded_1=0)
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"service_name":r[2],"appt_dt":r[3],
             "reminded_24":r[4],"reminded_6":r[5],"reminded_1":r[6]} for r in rows]

async def db_mark_reminded(booking_id: int, field: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE bookings SET {field}=1 WHERE id=?", (booking_id,))
        await db.commit()

async def db_get_booking_by_id(booking_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,username,first_name,service_name,datetime_txt,appt_dt,status FROM bookings WHERE id=?",
            (booking_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {"id":row[0],"user_id":row[1],"username":row[2],"first_name":row[3],
            "service_name":row[4],"datetime_txt":row[5],"appt_dt":row[6],"status":row[7]}


# ══════════════════════════════════════════════════════════════════════════════
#  ХЭЛПЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

async def is_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    return await db_admin_check(user_id)

async def make_master_link(svc_index: int) -> str:
    text    = await db_get_service_text(svc_index)
    encoded = urllib.parse.quote(text)
    return f"https://t.me/{MASTER_USERNAME}?text={encoded}"

def stars(rating: int) -> str:
    return "⭐" * rating + "☆" * (5 - rating)

def format_review(r: dict, idx: int, total: int) -> str:
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    date  = r["created_at"][:10]
    return (
        f"💬 <b>Отзыв {idx} из {total}</b>\n{'─'*28}\n"
        f"{stars(r['rating'])}  <b>{name}</b>{uname}\n<i>{date}</i>\n\n{r['text']}"
    )

def parse_appt_dt(text: str) -> datetime | None:
    """Парсит дату в формате ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ"""
    text = text.strip()
    formats = ["%d.%m.%Y %H:%M", "%d.%m %H:%M"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None

def format_appt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        MONTHS = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
        return f"{dt.day} {MONTHS[dt.month-1]} в {dt.strftime('%H:%M')}"
    except Exception:
        return iso


# ══════════════════════════════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════════════════════════════

class AdminFSM(StatesGroup):
    password          = State()
    broadcast_msg     = State()
    broadcast_confirm = State()
    edit_svc_text     = State()
    confirm_booking   = State()   # ввод даты при подтверждении записи

class ReviewFSM(StatesGroup):
    rating = State()
    text   = State()

class BookingFSM(StatesGroup):
    datetime_txt = State()   # клиент вводит дату и время


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        uid = getattr(getattr(event, "from_user", None), "id", None)
        if uid is None:
            return False
        return await is_admin(uid)


# ══════════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════════

async def kb_main_menu(user_id: int = 0) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 Записаться",  callback_data="book_start"))
    b.row(
        InlineKeyboardButton(text="💰 Прайс-лист", callback_data="prices"),
        InlineKeyboardButton(text="🌸 Портфолио",  callback_data="portfolio"),
    )
    b.row(InlineKeyboardButton(text="⭐ Отзывы", callback_data="reviews_menu"))
    if await is_admin(user_id):
        b.row(InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_back_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_admin_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_portfolio() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🌸 Смотреть работы", url=PORTFOLIO_LINK))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",    callback_data="main_menu"))
    return b.as_markup()

def kb_services() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (name, price) in enumerate(SERVICES):
        b.button(text=f"{name}  —  {price}", callback_data=f"svc:{i}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()

async def kb_write_to_master(svc_index: int, service_name: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="✍️ Написать мастеру",
        url=await make_master_link(svc_index)
    ))
    # После переписки клиент возвращается и жмёт эту кнопку
    b.row(InlineKeyboardButton(
        text="✅ Мастер одобрил — оформить запись",
        callback_data=f"booking_approved:{svc_index}"
    ))
    b.row(InlineKeyboardButton(text="🔙 Выбрать другую услугу", callback_data="book_start"))
    b.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_booking_cancel_input() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()

def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👥 Список пользователей",    callback_data="adm_users"))
    b.row(InlineKeyboardButton(text="📋 Записи клиентов",         callback_data="adm_bookings"))
    b.row(InlineKeyboardButton(text="📣 Рассылка всем клиентам",  callback_data="adm_broadcast"))
    b.row(InlineKeyboardButton(text="✏️ Тексты для записи",       callback_data="adm_svc_texts"))
    b.row(InlineKeyboardButton(text="🛡 Модерация отзывов",       callback_data="adm_reviews"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",            callback_data="main_menu"))
    return b.as_markup()

def kb_broadcast_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Отправить", callback_data="adm_do_broadcast"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="admin_panel"),
    )
    return b.as_markup()

def kb_svc_texts_list() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (name, _) in enumerate(SERVICES):
        b.button(text=name, callback_data=f"adm_edit_svc:{i}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔙 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_svc_text_edit(svc_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 Сбросить на стандартный", callback_data=f"adm_reset_svc:{svc_index}"))
    b.row(InlineKeyboardButton(text="❌ Отменить редактирование", callback_data="adm_svc_texts"))
    return b.as_markup()

def kb_bookings_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🕐 Ожидают подтверждения", callback_data="adm_bookings_pending"))
    b.row(InlineKeyboardButton(text="✅ Подтверждённые записи",  callback_data="adm_bookings_confirmed"))
    b.row(InlineKeyboardButton(text="🔙 Панель администратора",  callback_data="admin_panel"))
    return b.as_markup()

def kb_moderate_booking(booking_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_book_ok:{booking_id}"),
        InlineKeyboardButton(text="❌ Отклонить",   callback_data=f"adm_book_cancel:{booking_id}"),
    )
    b.row(InlineKeyboardButton(text="🔙 К записям", callback_data="adm_bookings_pending"))
    return b.as_markup()

def kb_booking_confirmed_actions(booking_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"adm_book_cancel:{booking_id}"))
    b.row(InlineKeyboardButton(text="🔙 К записям",       callback_data="adm_bookings_confirmed"))
    return b.as_markup()

# ── Отзывы ────────────────────────────────────────────────────────────────────

def kb_reviews_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📖 Смотреть отзывы", callback_data="reviews_browse:0"))
    b.row(InlineKeyboardButton(text="✍️ Написать отзыв",  callback_data="review_write"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",    callback_data="main_menu"))
    return b.as_markup()

def kb_rating() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(1, 6):
        b.button(text=f"{'⭐'*i}", callback_data=f"rate:{i}")
    b.adjust(5)
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu"))
    return b.as_markup()

def kb_reviews_nav(idx: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    row = []
    if idx > 0:
        row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"reviews_browse:{idx-1}"))
    row.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        row.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"reviews_browse:{idx+1}"))
    b.row(*row)
    b.row(InlineKeyboardButton(text="✍️ Написать отзыв", callback_data="review_write"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",   callback_data="main_menu"))
    return b.as_markup()

def kb_review_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Отправить", callback_data="review_submit"),
        InlineKeyboardButton(text="✏️ Изменить",  callback_data="review_write"),
    )
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu"))
    return b.as_markup()

def kb_moderate_review(review_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_rev_ok:{review_id}"),
        InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"adm_rev_del:{review_id}"),
    )
    b.row(InlineKeyboardButton(text="🔙 К модерации", callback_data="adm_reviews"))
    return b.as_markup()

def kb_cancel_input() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel"))
    return b.as_markup()


# ══════════════════════════════════════════════════════════════════════════════
#  ТЕКСТЫ
# ══════════════════════════════════════════════════════════════════════════════

WELCOME = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Я бот мастера по волосам\n"
    "💇‍♀️ <b>Полины Евдокимовой</b>\n\n"
    "Выберите действие:"
)

PRICES_TEXT = (
    "💰 <b>Прайс-лист</b>\n\n"
    "<b>🎨 ОКРАШИВАНИЕ</b>\n"
    "┣ Сложное окрашивание\n"
    "┃  <i>(густота, макс. длина)</i> — <b>9 000 – 14 000 ₽</b>\n"
    "┃  <i>доп. оплата густота / макс. длина</i> — <b>1 500 – 2 000 ₽</b>\n"
    "┣ В один тон — <b>5 000 – 9 000 ₽</b>\n"
    "┣ Окрашивание корней — <b>3 500 – 4 000 ₽</b>\n"
    "┣ Тонирование блонда — <b>5 000 – 8 000 ₽</b>\n"
    "┣ Осветление корней + тонирование — <b>6 000 – 9 000 ₽</b>\n"
    "┗ Глубокий контуринг — <b>7 500 – 12 500 ₽</b>\n\n"
    "<b>✂️ СТРИЖКА</b>\n"
    "┗ Стрижка — <b>2 000 ₽</b>\n\n"
    "<b>💨 УКЛАДКА</b>\n"
    "┣ Укладка (мытьё + брашинг) — <b>1 500 ₽</b>\n"
    "┗ Укладка локоны — <b>2 500 – 3 500 ₽</b>\n\n"
    "Для записи нажмите <b>📅 Записаться</b>"
)


# ══════════════════════════════════════════════════════════════════════════════
#  РОУТЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

auth_router      = Router()
common_router    = Router()
user_router      = Router()
review_router    = Router()
booking_router   = Router()
admin_cb_router  = Router()
admin_fsm_router = Router()

admin_cb_router.callback_query.filter(IsAdmin())


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════════

@auth_router.message(Command("admin"))
async def cmd_admin_entry(message: Message, state: FSMContext):
    if await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())
        return
    await state.set_state(AdminFSM.password)
    await message.answer("🔐 <b>Введите пароль администратора:</b>")

@auth_router.message(AdminFSM.password)
async def fsm_admin_password(message: Message, state: FSMContext):
    if message.text and message.text.strip() == ADMIN_PASSWORD:
        await db_admin_add(message.from_user.id)
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ <b>Доступ разрешён!</b>\n"
            "<i>Вы навсегда добавлены как администратор.</i>\n\n"
            "🛠 <b>Панель администратора</b>",
            reply_markup=kb_admin_main()
        )
    else:
        await message.answer("❌ Неверный пароль. Попробуйте ещё раз:")


# ══════════════════════════════════════════════════════════════════════════════
#  ОБЩИЕ ХЭНДЛЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

@common_router.message(CommandStart())
@common_router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    await db_save_user(user.id, user.username, user.first_name)
    await message.answer(WELCOME, reply_markup=await kb_main_menu(user.id))

@common_router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(WELCOME, reply_markup=await kb_main_menu(cb.from_user.id))

@common_router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

@common_router.callback_query(F.data == "prices")
async def cb_prices(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(PRICES_TEXT, reply_markup=kb_back_menu())

@common_router.callback_query(F.data == "portfolio")
async def cb_portfolio(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "🌸 <b>Портфолио</b>\n\nСмотрите работы мастера в Telegram-канале:",
        reply_markup=kb_portfolio()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ — ВЫБОР УСЛУГИ
# ══════════════════════════════════════════════════════════════════════════════

@user_router.callback_query(F.data == "book_start")
async def cb_book_start(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "💇‍♀️ <b>Выберите услугу для записи:</b>",
        reply_markup=kb_services()
    )

@user_router.callback_query(F.data.startswith("svc:"))
async def cb_book_service(cb: CallbackQuery):
    await cb.answer()
    idx = int(cb.data.split(":")[1])
    if idx >= len(SERVICES):
        return
    service_name, service_price = SERVICES[idx]
    await cb.message.edit_text(
        f"✅ Вы выбрали: <b>{service_name}</b>  —  {service_price}\n\n"
        f"<b>Шаг 1:</b> Нажмите «Написать мастеру» — напишите ей, договоритесь о дате и времени.\n\n"
        f"<b>Шаг 2:</b> Вернитесь сюда и нажмите «Мастер одобрил — оформить запись» 👇",
        reply_markup=await kb_write_to_master(idx, service_name)
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ — ОФОРМЛЕНИЕ ПОСЛЕ ОДОБРЕНИЯ МАСТЕРОМ
# ══════════════════════════════════════════════════════════════════════════════

@booking_router.callback_query(F.data.startswith("booking_approved:"))
async def cb_booking_approved(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx          = int(cb.data.split(":")[1])
    service_name = SERVICES[idx][0]
    await state.set_state(BookingFSM.datetime_txt)
    await state.update_data(booking_service=service_name)
    await cb.message.edit_text(
        f"📅 <b>Оформление записи</b>\n\n"
        f"Услуга: <b>{service_name}</b>\n\n"
        f"Введите дату и время визита, которые вы согласовали с мастером:\n\n"
        f"<i>Например: <code>15.01 14:00</code> или <code>15.01.2025 14:00</code></i>",
        reply_markup=kb_booking_cancel_input()
    )

@booking_router.message(BookingFSM.datetime_txt)
async def fsm_booking_datetime(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите дату и время.")
        return

    data         = await state.get_data()
    service_name = data.get("booking_service", "—")
    user         = message.from_user

    booking_id = await db_add_booking(
        user.id, user.username, user.first_name, service_name, text
    )
    await state.clear()

    await message.answer(
        f"✅ <b>Заявка на запись отправлена!</b>\n\n"
        f"Услуга: <b>{service_name}</b>\n"
        f"Дата/время: <b>{text}</b>\n\n"
        f"⏳ Ожидайте подтверждения от мастера — вам придёт уведомление.",
        reply_markup=await kb_main_menu(user.id)
    )

    # Уведомление администратору
    name  = user.first_name or "Аноним"
    uname = f" (@{user.username})" if user.username else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📋 <b>Новая заявка на запись!</b>\n\n"
            f"Клиент: <b>{name}</b>{uname}\n"
            f"Услуга: <b>{service_name}</b>\n"
            f"Дата/время (слова клиента): <b>{text}</b>\n\n"
            f"<i>ID заявки: {booking_id}</i>\n\n"
            f"Подтвердите запись — введите точную дату для напоминаний.",
            reply_markup=kb_moderate_booking(booking_id)
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  ОТЗЫВЫ — ПОЛЬЗОВАТЕЛЬ
# ══════════════════════════════════════════════════════════════════════════════

@review_router.callback_query(F.data == "reviews_menu")
async def cb_reviews_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    total = await db_count_approved_reviews()
    await cb.message.edit_text(
        f"⭐ <b>Отзывы клиентов</b>\n\nВсего отзывов: <b>{total}</b>\n\n"
        f"Почитайте отзывы или оставьте свой:",
        reply_markup=kb_reviews_menu()
    )

@review_router.callback_query(F.data.startswith("reviews_browse:"))
async def cb_reviews_browse(cb: CallbackQuery):
    await cb.answer()
    idx     = int(cb.data.split(":")[1])
    reviews = await db_get_approved_reviews()
    total   = len(reviews)
    if total == 0:
        await cb.message.edit_text(
            "💬 <b>Отзывов пока нет.</b>\n\nБудьте первым!",
            reply_markup=kb_reviews_menu()
        )
        return
    idx = max(0, min(idx, total - 1))
    await cb.message.edit_text(
        format_review(reviews[idx], idx + 1, total),
        reply_markup=kb_reviews_nav(idx, total)
    )

@review_router.callback_query(F.data == "review_write")
async def cb_review_write(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ReviewFSM.rating)
    await cb.message.edit_text(
        "✍️ <b>Оставить отзыв</b>\n\nШаг 1 из 2: Выберите оценку 👇",
        reply_markup=kb_rating()
    )

@review_router.callback_query(ReviewFSM.rating, F.data.startswith("rate:"))
async def cb_review_rating(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    rating = int(cb.data.split(":")[1])
    await state.update_data(rating=rating)
    await state.set_state(ReviewFSM.text)
    await cb.message.edit_text(
        f"✍️ <b>Оставить отзыв</b>\n\nВаша оценка: {stars(rating)}\n\n"
        f"Шаг 2 из 2: Напишите ваш отзыв 👇\n<i>(минимум 10 символов)</i>",
        reply_markup=InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu")
        ).as_markup()
    )

@review_router.message(ReviewFSM.text)
async def fsm_review_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer("⚠️ Слишком короткий отзыв. Напишите хотя бы 10 символов:")
        return
    data   = await state.get_data()
    rating = data.get("rating", 5)
    await state.update_data(review_text=text)
    await message.answer(
        f"👀 <b>Предпросмотр:</b>\n\n{stars(rating)}\n\n{text}\n\n"
        f"Всё верно? После отправки отзыв уйдёт на проверку мастеру.",
        reply_markup=kb_review_confirm()
    )

@review_router.callback_query(F.data == "review_submit")
async def cb_review_submit(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    data   = await state.get_data()
    rating = data.get("rating")
    text   = data.get("review_text")
    if not rating or not text:
        await cb.message.edit_text("Что-то пошло не так. Попробуйте снова.", reply_markup=kb_reviews_menu())
        await state.clear()
        return
    user      = cb.from_user
    review_id = await db_add_review(user.id, user.username, user.first_name, rating, text)
    await state.clear()
    await cb.message.edit_text(
        "✅ <b>Спасибо за отзыв!</b>\n\nОн отправлен на проверку и скоро появится в списке.",
        reply_markup=kb_reviews_menu()
    )
    name  = user.first_name or "Аноним"
    uname = f" (@{user.username})" if user.username else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Новый отзыв на проверку!</b>\n\nОт: <b>{name}</b>{uname}\n"
            f"Оценка: {stars(rating)}\n\n{text}\n\n<i>ID отзыва: {review_id}</i>",
            reply_markup=kb_moderate_review(review_id)
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

@admin_cb_router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())

@admin_cb_router.callback_query(F.data == "adm_users")
async def cb_adm_users(cb: CallbackQuery):
    await cb.answer()
    users = await db_get_all_users()
    total = len(users)
    if not users:
        await cb.message.edit_text("👥 <b>Пользователей пока нет.</b>", reply_markup=kb_admin_back())
        return
    lines = [f"👥 <b>Всего пользователей: {total} чел.</b>\n"]
    for u in users[:50]:
        uname = f"@{u['username']}" if u["username"] else f"ID {u['user_id']}"
        lines.append(f"• {u['first_name'] or '—'} — {uname}")
    if total > 50:
        lines.append(f"\n<i>...и ещё {total - 50}</i>")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_back())

@admin_cb_router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    total = await db_count_users()
    await state.set_state(AdminFSM.broadcast_msg)
    await cb.message.edit_text(
        f"📣 <b>Рассылка</b>\n\nПолучателей: <b>{total} чел.</b>\n\n"
        f"Введите текст рассылки.\nПоддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>",
        reply_markup=kb_admin_back()
    )

@admin_cb_router.callback_query(F.data == "adm_do_broadcast")
async def cb_adm_do_broadcast(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    if await state.get_state() != AdminFSM.broadcast_confirm:
        await cb.answer("Сначала введите текст рассылки.", show_alert=True)
        return
    data     = await state.get_data()
    text     = data.get("broadcast_text", "")
    user_ids = await db_get_all_user_ids()
    await state.clear()
    sent = failed = 0
    await cb.message.edit_text(f"📣 Отправляю... ({len(user_ids)} получателей)")
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await cb.message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n✔ Отправлено: <b>{sent}</b>\n✖ Ошибок: <b>{failed}</b>",
        reply_markup=kb_admin_main()
    )

@admin_cb_router.callback_query(F.data == "adm_svc_texts")
async def cb_adm_svc_texts(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        "✏️ <b>Редактирование авто-текстов</b>\n\n"
        "Выберите услугу — изменения применяются сразу для <b>всех</b>:",
        reply_markup=kb_svc_texts_list()
    )

@admin_cb_router.callback_query(F.data.startswith("adm_edit_svc:"))
async def cb_adm_edit_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx       = int(cb.data.split(":")[1])
    current   = await db_get_service_text(idx)
    is_custom = (current != DEFAULT_SERVICE_TEXTS[idx])
    await state.set_state(AdminFSM.edit_svc_text)
    await state.update_data(editing_svc_index=idx)
    await cb.message.edit_text(
        f"✏️ <b>Редактирование: «{SERVICES[idx][0]}»</b>\n\n"
        f"Статус: <i>{'🟡 изменён' if is_custom else '🟢 стандартный'}</i>\n\n"
        f"<b>Текущий текст:</b>\n<code>{current}</code>\n\n"
        f"Напишите новый текст:",
        reply_markup=kb_svc_text_edit(idx)
    )

@admin_cb_router.callback_query(F.data.startswith("adm_reset_svc:"))
async def cb_adm_reset_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx = int(cb.data.split(":")[1])
    await db_reset_service_text(idx)
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Текст для «{SERVICES[idx][0]}» сброшен:</b>\n\n<code>{DEFAULT_SERVICE_TEXTS[idx]}</code>",
        reply_markup=kb_svc_texts_list()
    )


# ── Записи клиентов ───────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_bookings")
async def cb_adm_bookings(cb: CallbackQuery):
    await cb.answer()
    pending   = await db_get_pending_bookings()
    confirmed = await db_get_upcoming_bookings()
    await cb.message.edit_text(
        f"📋 <b>Записи клиентов</b>\n\n"
        f"🕐 Ожидают подтверждения: <b>{len(pending)}</b>\n"
        f"✅ Предстоящие записи: <b>{len(confirmed)}</b>",
        reply_markup=kb_bookings_menu()
    )

@admin_cb_router.callback_query(F.data == "adm_bookings_pending")
async def cb_adm_bookings_pending(cb: CallbackQuery):
    await cb.answer()
    bookings = await db_get_pending_bookings()
    if not bookings:
        await cb.message.edit_text(
            "🕐 <b>Нет заявок на подтверждение.</b>",
            reply_markup=kb_bookings_menu()
        )
        return
    # Показываем первую
    b     = bookings[0]
    name  = b["first_name"] or "Аноним"
    uname = f" (@{b['username']})" if b["username"] else ""
    await cb.message.edit_text(
        f"🕐 <b>Заявки на подтверждение: {len(bookings)} шт.</b>\n\n"
        f"{'─'*28}\n"
        f"Клиент: <b>{name}</b>{uname}\n"
        f"Услуга: <b>{b['service_name']}</b>\n"
        f"Дата/время: <b>{b['datetime_txt']}</b>\n\n"
        f"<i>Нажмите «Подтвердить» — введёте точную дату для напоминаний.</i>",
        reply_markup=kb_moderate_booking(b["id"])
    )

@admin_cb_router.callback_query(F.data == "adm_bookings_confirmed")
async def cb_adm_bookings_confirmed(cb: CallbackQuery):
    await cb.answer()
    bookings = await db_get_upcoming_bookings()
    if not bookings:
        await cb.message.edit_text("✅ <b>Предстоящих записей нет.</b>", reply_markup=kb_bookings_menu())
        return
    lines = [f"✅ <b>Предстоящие записи: {len(bookings)} шт.</b>\n"]
    for b in bookings:
        name  = b["first_name"] or "Аноним"
        uname = f" @{b['username']}" if b["username"] else ""
        dt    = format_appt_dt(b["appt_dt"]) if b["appt_dt"] else b["datetime_txt"]
        lines.append(f"• <b>{name}</b>{uname}\n  {b['service_name']} — {dt}")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_bookings_menu())

@admin_cb_router.callback_query(F.data.startswith("adm_book_ok:"))
async def cb_adm_book_ok(cb: CallbackQuery, state: FSMContext):
    """Нажали «Подтвердить» — просим ввести точную дату для напоминаний."""
    await cb.answer()
    booking_id = int(cb.data.split(":")[1])
    booking    = await db_get_booking_by_id(booking_id)
    if not booking:
        await cb.answer("Запись не найдена.", show_alert=True)
        return
    await state.set_state(AdminFSM.confirm_booking)
    await state.update_data(confirming_booking_id=booking_id,
                             confirming_user_id=booking["user_id"],
                             confirming_service=booking["service_name"])
    await cb.message.edit_text(
        f"✅ <b>Подтверждение записи</b>\n\n"
        f"Клиент написал: <b>{booking['datetime_txt']}</b>\n\n"
        f"Введите точную дату и время для автоматических напоминаний:\n"
        f"<i>Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\nНапример: <code>15.01.2025 14:00</code></i>",
        reply_markup=kb_cancel_input()
    )

@admin_cb_router.callback_query(F.data.startswith("adm_book_cancel:"))
async def cb_adm_book_cancel(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    booking_id = int(cb.data.split(":")[1])
    booking    = await db_get_booking_by_id(booking_id)
    await db_cancel_booking(booking_id)
    # Уведомляем клиента
    if booking:
        try:
            await bot.send_message(
                booking["user_id"],
                f"😔 <b>Ваша запись отменена.</b>\n\n"
                f"Услуга: <b>{booking['service_name']}</b>\n\n"
                f"Если хотите перенести — свяжитесь с мастером.",
                reply_markup=await kb_main_menu(booking["user_id"])
            )
        except Exception:
            pass
    await cb.message.edit_text("❌ Запись отменена, клиент уведомлён.", reply_markup=kb_bookings_menu())


# ── Модерация отзывов ─────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_reviews")
async def cb_adm_reviews(cb: CallbackQuery):
    await cb.answer()
    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text("🛡 <b>Нет отзывов на проверке. Всё чисто!</b>", reply_markup=kb_admin_back())
        return
    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    await cb.message.edit_text(
        f"🛡 <b>Модерация: {len(pending)} шт.</b>\n\n{'─'*28}\n"
        f"От: <b>{name}</b>{uname} | {r['created_at'][:10]}\n"
        f"Оценка: {stars(r['rating'])}\n\n{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )

async def _show_next_review(cb: CallbackQuery):
    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text("🛡 <b>Все отзывы проверены!</b>", reply_markup=kb_admin_back())
        return
    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    await cb.message.edit_text(
        f"🛡 <b>Модерация: {len(pending)} шт.</b>\n\n{'─'*28}\n"
        f"От: <b>{name}</b>{uname} | {r['created_at'][:10]}\n"
        f"Оценка: {stars(r['rating'])}\n\n{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )

@admin_cb_router.callback_query(F.data.startswith("adm_rev_ok:"))
async def cb_adm_rev_approve(cb: CallbackQuery):
    await cb.answer("✅ Одобрен!")
    await db_set_review_status(int(cb.data.split(":")[1]), "approved")
    await _show_next_review(cb)

@admin_cb_router.callback_query(F.data.startswith("adm_rev_del:"))
async def cb_adm_rev_delete(cb: CallbackQuery):
    await cb.answer("🗑 Удалён.")
    await db_set_review_status(int(cb.data.split(":")[1]), "rejected")
    await _show_next_review(cb)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — FSM ТЕКСТОВЫЙ ВВОД
# ══════════════════════════════════════════════════════════════════════════════

@admin_fsm_router.message(AdminFSM.broadcast_msg)
async def fsm_broadcast_msg(message: Message, state: FSMContext):
    text = message.text or ""
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminFSM.broadcast_confirm)
    total = await db_count_users()
    await message.answer(
        f"📣 <b>Предпросмотр:</b>\n\n{'─'*28}\n{text}\n{'─'*28}\n\n"
        f"Отправить <b>{total}</b> пользователям?",
        reply_markup=kb_broadcast_confirm()
    )

@admin_fsm_router.message(AdminFSM.edit_svc_text)
async def fsm_edit_svc_text(message: Message, state: FSMContext):
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("⚠️ Текст не может быть пустым:")
        return
    data = await state.get_data()
    idx  = data.get("editing_svc_index")
    if idx is None:
        await state.clear()
        await message.answer("Ошибка.", reply_markup=kb_admin_main())
        return
    await db_set_service_text(idx, new_text)
    await state.clear()
    await message.answer(
        f"✅ <b>Текст для «{SERVICES[idx][0]}» обновлён для всех!</b>\n\n<code>{new_text}</code>",
        reply_markup=kb_svc_texts_list()
    )

@admin_fsm_router.message(AdminFSM.confirm_booking)
async def fsm_confirm_booking(message: Message, state: FSMContext, bot: Bot):
    """Админ ввёл дату подтверждения записи."""
    text = (message.text or "").strip()
    dt   = parse_appt_dt(text)
    if not dt:
        await message.answer(
            "⚠️ Не удалось распознать дату.\n"
            "Введите в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            "Например: <code>15.01.2025 14:00</code>"
        )
        return

    data       = await state.get_data()
    booking_id = data.get("confirming_booking_id")
    user_id    = data.get("confirming_user_id")
    service    = data.get("confirming_service")
    await state.clear()

    await db_confirm_booking(booking_id, dt.isoformat())

    dt_fmt = format_appt_dt(dt.isoformat())

    # ── Уведомление клиенту ──────────────────────────────────────
    # Как оно выглядит для клиента:
    confirmation_text = (
        f"🎉 <b>Ваша запись подтверждена!</b>\n\n"
        f"💇‍♀️ Услуга: <b>{service}</b>\n"
        f"📅 Дата и время: <b>{dt_fmt}</b>\n"
        f"✂️ Мастер: <b>{MASTER_NAME_FULL}</b>\n\n"
        f"Вам придут напоминания:\n"
        f"  • за 24 часа до визита\n"
        f"  • за 6 часов\n"
        f"  • за 1 час\n\n"
        f"<i>Если нужно перенести — напишите мастеру.</i>"
    )
    try:
        await bot.send_message(user_id, confirmation_text)
    except Exception:
        pass

    await message.answer(
        f"✅ <b>Запись подтверждена!</b>\n\n"
        f"Услуга: <b>{service}</b>\n"
        f"Дата: <b>{dt_fmt}</b>\n\n"
        f"Клиент уведомлён. Напоминания будут отправлены автоматически за 24ч, 6ч и 1ч.",
        reply_markup=kb_admin_main()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ФОНОВАЯ ЗАДАЧА — НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def reminder_worker(bot: Bot):
    """Проверяет каждую минуту, нужно ли отправить напоминания."""
    while True:
        try:
            now      = datetime.now()
            bookings = await db_get_bookings_for_reminders()

            for b in bookings:
                try:
                    appt = datetime.fromisoformat(b["appt_dt"])
                except Exception:
                    continue

                diff_hours = (appt - now).total_seconds() / 3600
                service    = b["service_name"]
                uid        = b["user_id"]
                bid        = b["id"]
                dt_fmt     = format_appt_dt(b["appt_dt"])

                # За 24 часа (окно: от 24.5 до 23.5 ч)
                if not b["reminded_24"] and 23.5 <= diff_hours <= 24.5:
                    try:
                        await bot.send_message(
                            uid,
                            f"🔔 <b>Напоминание о записи!</b>\n\n"
                            f"Завтра у вас запись:\n"
                            f"💇‍♀️ <b>{service}</b>\n"
                            f"📅 <b>{dt_fmt}</b>\n\n"
                            f"Мастер: <b>{MASTER_NAME_FULL}</b>\n"
                            f"<i>Ждём вас! 🌸</i>"
                        )
                        await db_mark_reminded(bid, "reminded_24")
                    except Exception:
                        pass

                # За 6 часов (окно: от 6.5 до 5.5 ч)
                elif not b["reminded_6"] and 5.5 <= diff_hours <= 6.5:
                    try:
                        await bot.send_message(
                            uid,
                            f"⏰ <b>Напоминание!</b>\n\n"
                            f"Через 6 часов у вас запись:\n"
                            f"💇‍♀️ <b>{service}</b>\n"
                            f"📅 <b>{dt_fmt}</b>\n\n"
                            f"<i>Не забудьте! 💫</i>"
                        )
                        await db_mark_reminded(bid, "reminded_6")
                    except Exception:
                        pass

                # За 1 час (окно: от 1.25 до 0.75 ч)
                elif not b["reminded_1"] and 0.75 <= diff_hours <= 1.25:
                    try:
                        await bot.send_message(
                            uid,
                            f"⚡ <b>Напоминание!</b>\n\n"
                            f"Через час у вас запись:\n"
                            f"💇‍♀️ <b>{service}</b>\n"
                            f"📅 <b>{dt_fmt}</b>\n\n"
                            f"<i>Выезжайте вовремя! 🚀</i>"
                        )
                        await db_mark_reminded(bid, "reminded_1")
                    except Exception:
                        pass

        except Exception as e:
            log.error(f"Ошибка в reminder_worker: {e}")

        await asyncio.sleep(60)  # проверяем раз в минуту


# ══════════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("Запуск бота...")
    await init_db()

    fsm_storage = SQLiteFSMStorage(DB_PATH)
    await fsm_storage.init()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=fsm_storage)

    dp.include_router(auth_router)
    dp.include_router(common_router)
    dp.include_router(user_router)
    dp.include_router(review_router)
    dp.include_router(booking_router)
    dp.include_router(admin_cb_router)
    dp.include_router(admin_fsm_router)

    # Запускаем напоминания параллельно
    asyncio.create_task(reminder_worker(bot))

    try:
        log.info("Бот запущен!")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True
        )
    finally:
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
