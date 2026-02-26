"""
Telegram-бот — Полина Евдокимова. v13
✅ Подтверждение записи = 1 кнопка, дата берётся из слов клиента автоматически
✅ Настройка напоминаний в админ-панели (вкл/выкл каждый интервал)
✅ Удаление записи после подтверждения
✅ Записи хранятся в SQLite — не теряются при перезапуске
✅ Кэш админов в памяти — кнопки мгновенные
"""

import asyncio, logging, json, re, urllib.parse, aiosqlite
from datetime import datetime
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
    def _key(k: StorageKey) -> str:
        return f"{k.bot_id}:{k.chat_id}:{k.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None):
        k  = self._key(key)
        sv = state.state if hasattr(state, "state") else (state if isinstance(state, str) else None)
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO fsm_data (key,state,data) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET state=excluded.state
                """, (k, sv, "{}"))
                await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT state FROM fsm_data WHERE key=?", (self._key(key),))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]):
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO fsm_data (key,state,data) VALUES(?,NULL,?)
                    ON CONFLICT(key) DO UPDATE SET data=excluded.data
                """, (self._key(key), json.dumps(data, ensure_ascii=False)))
                await db.commit()

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("SELECT data FROM fsm_data WHERE key=?", (self._key(key),))
            row = await cur.fetchone()
        if not row: return {}
        try: return json.loads(row[0]) or {}
        except: return {}

    async def close(self): pass


# ══════════════════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN        = "8386414173:"8386414173:AAHcbwu1krGSmu_i0hWfsVER-aqKEX5lLBw"
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

# Кэш авторизованных админов в памяти — проверка мгновенная без запроса к БД
ADMIN_CACHE: set[int] = set()

# Кэш настроек напоминаний в памяти (обновляется из БД при старте)
# Формат: {"r24": True, "r12": False, "r6": True, "r1": True}
REMINDER_SETTINGS: dict = {"r24": True, "r12": False, "r6": True, "r1": True}

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
            -- status: pending | confirmed | cancelled
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
                reminded_12  INTEGER DEFAULT 0,
                reminded_6   INTEGER DEFAULT 0,
                reminded_1   INTEGER DEFAULT 0,
                created_at   TEXT NOT NULL
            );
            -- Настройки бота (ключ-значение)
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await db.commit()

        # Загружаем кэш админов
        cur = await db.execute("SELECT user_id FROM admin_sessions")
        for row in await cur.fetchall():
            ADMIN_CACHE.add(row[0])

        # Загружаем настройки напоминаний
        cur = await db.execute("SELECT key, value FROM settings WHERE key LIKE 'reminder_%'")
        rows = await cur.fetchall()
        mapping = {"reminder_24":"r24","reminder_12":"r12","reminder_6":"r6","reminder_1":"r1"}
        for key, val in rows:
            if key in mapping:
                REMINDER_SETTINGS[mapping[key]] = (val == "1")

    log.info(f"БД готова. Админы: {ADMIN_CACHE}. Напоминания: {REMINDER_SETTINGS}")


async def db_save_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES(?,?)", (key, value))
        await db.commit()


# ── Пользователи ──────────────────────────────────────────────────────────────

async def db_save_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id,username,first_name,created_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name
        """, (user_id, username, first_name, datetime.now().isoformat()))
        await db.commit()

async def db_get_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id,username,first_name,created_at FROM users ORDER BY created_at DESC")
        rows = await cur.fetchall()
    return [{"user_id":r[0],"username":r[1],"first_name":r[2],"created_at":r[3]} for r in rows]

async def db_get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]

async def db_count_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Тексты услуг ──────────────────────────────────────────────────────────────

async def db_get_service_text(idx):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT custom_text FROM service_texts WHERE svc_index=?", (idx,))
        row = await cur.fetchone()
    return row[0] if row else DEFAULT_SERVICE_TEXTS[idx]

async def db_set_service_text(idx, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO service_texts (svc_index,custom_text) VALUES(?,?)", (idx, text))
        await db.commit()

async def db_reset_service_text(idx):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM service_texts WHERE svc_index=?", (idx,))
        await db.commit()


# ── Авторизация ───────────────────────────────────────────────────────────────

async def db_admin_add(user_id):
    ADMIN_CACHE.add(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO admin_sessions (user_id,authed_at) VALUES(?,?)",
                         (user_id, datetime.now().isoformat()))
        await db.commit()


# ── Отзывы ────────────────────────────────────────────────────────────────────

async def db_add_review(user_id, username, first_name, rating, text):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO reviews (user_id,username,first_name,rating,text,status,created_at)
            VALUES(?,?,?,?,?,'pending',?)
        """, (user_id, username, first_name, rating, text, datetime.now().isoformat()))
        await db.commit()
        return cur.lastrowid

async def db_get_approved_reviews():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,rating,text,created_at
            FROM reviews WHERE status='approved' ORDER BY created_at DESC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "rating":r[4],"text":r[5],"created_at":r[6]} for r in rows]

async def db_get_pending_reviews():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,rating,text,created_at
            FROM reviews WHERE status='pending' ORDER BY created_at ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "rating":r[4],"text":r[5],"created_at":r[6]} for r in rows]

async def db_set_review_status(review_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reviews SET status=? WHERE id=?", (status, review_id))
        await db.commit()

async def db_count_approved_reviews():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM reviews WHERE status='approved'")
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Записи ────────────────────────────────────────────────────────────────────

async def db_add_booking(user_id, username, first_name, service_name, datetime_txt):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO bookings (user_id,username,first_name,service_name,datetime_txt,status,created_at)
            VALUES(?,?,?,?,?,'pending',?)
        """, (user_id, username, first_name, service_name, datetime_txt, datetime.now().isoformat()))
        await db.commit()
        return cur.lastrowid

async def db_confirm_booking(booking_id, appt_dt):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET status='confirmed',appt_dt=? WHERE id=?", (appt_dt, booking_id))
        await db.commit()

async def db_cancel_booking(booking_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        await db.commit()

async def db_get_pending_bookings():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,service_name,datetime_txt,created_at
            FROM bookings WHERE status='pending' ORDER BY created_at ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "service_name":r[4],"datetime_txt":r[5],"created_at":r[6]} for r in rows]

async def db_get_confirmed_bookings():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,username,first_name,service_name,datetime_txt,appt_dt
            FROM bookings WHERE status='confirmed' ORDER BY
                CASE WHEN appt_dt IS NOT NULL THEN appt_dt ELSE datetime_txt END ASC
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"username":r[2],"first_name":r[3],
             "service_name":r[4],"datetime_txt":r[5],"appt_dt":r[6]} for r in rows]

async def db_get_booking(booking_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,user_id,username,first_name,service_name,datetime_txt,appt_dt,status FROM bookings WHERE id=?",
            (booking_id,)
        )
        row = await cur.fetchone()
    if not row: return None
    return {"id":row[0],"user_id":row[1],"username":row[2],"first_name":row[3],
            "service_name":row[4],"datetime_txt":row[5],"appt_dt":row[6],"status":row[7]}

async def db_get_bookings_for_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,user_id,service_name,appt_dt,reminded_24,reminded_12,reminded_6,reminded_1
            FROM bookings WHERE status='confirmed' AND appt_dt IS NOT NULL
              AND (reminded_24=0 OR reminded_12=0 OR reminded_6=0 OR reminded_1=0)
        """)
        rows = await cur.fetchall()
    return [{"id":r[0],"user_id":r[1],"service_name":r[2],"appt_dt":r[3],
             "reminded_24":r[4],"reminded_12":r[5],"reminded_6":r[6],"reminded_1":r[7]} for r in rows]

async def db_mark_reminded(booking_id, field):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE bookings SET {field}=1 WHERE id=?", (booking_id,))
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  ХЭЛПЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID or uid in ADMIN_CACHE

async def make_master_link(idx):
    text = await db_get_service_text(idx)
    return f"https://t.me/{MASTER_USERNAME}?text={urllib.parse.quote(text)}"

def stars(r): return "⭐"*r + "☆"*(5-r)

def fmt_review(r, idx, total):
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    return (f"💬 <b>Отзыв {idx} из {total}</b>\n{'─'*26}\n"
            f"{stars(r['rating'])}  <b>{name}</b>{uname}\n"
            f"<i>{r['created_at'][:10]}</i>\n\n{r['text']}")

def parse_dt_from_text(text: str) -> datetime | None:
    """
    Умно ищет дату и время в свободном тексте клиента.
    Понимает: '15.01 14:00', '15.01.2025 14:00', '15 января в 14:00' и т.д.
    """
    text = text.strip().lower()
    now  = datetime.now()

    # Формат ДД.ММ.ГГГГ ЧЧ:ММ или ДД.ММ ЧЧ:ММ
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        m = re.search(r'\d{1,2}\.\d{1,2}(?:\.\d{4})?\s+\d{1,2}:\d{2}', text)
        if m:
            try:
                dt = datetime.strptime(m.group(), fmt)
                if fmt == "%d.%m %H:%M":
                    dt = dt.replace(year=now.year)
                return dt
            except: pass

    # Формат ДД.ММ.ГГГГ ЧЧ:ММ без пробела
    m = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})', text)
    if m:
        day, mon, yr, hr, mi = m.groups()
        yr = int(yr) if yr else now.year
        try:
            return datetime(int(yr), int(mon), int(day), int(hr), int(mi))
        except: pass

    return None

def fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        m  = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"][dt.month-1]
        return f"{dt.day} {m} в {dt.strftime('%H:%M')}"
    except:
        return iso

def reminder_label() -> str:
    active = []
    if REMINDER_SETTINGS["r24"]: active.append("24ч")
    if REMINDER_SETTINGS["r12"]: active.append("12ч")
    if REMINDER_SETTINGS["r6"]:  active.append("6ч")
    if REMINDER_SETTINGS["r1"]:  active.append("1ч")
    return ", ".join(active) if active else "выключены"


# ══════════════════════════════════════════════════════════════════════════════
#  FSM
# ══════════════════════════════════════════════════════════════════════════════

class AdminFSM(StatesGroup):
    password          = State()
    broadcast_msg     = State()
    broadcast_confirm = State()
    edit_svc_text     = State()

class ReviewFSM(StatesGroup):
    rating = State()
    text   = State()

class BookingFSM(StatesGroup):
    datetime_txt = State()

class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject) -> bool:
        uid = getattr(getattr(event, "from_user", None), "id", None)
        return is_admin(uid) if uid else False


# ══════════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════════

def kb_main(admin=False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 Записаться",  callback_data="book_start"))
    b.row(InlineKeyboardButton(text="💰 Прайс-лист", callback_data="prices"),
          InlineKeyboardButton(text="🌸 Портфолио",  callback_data="portfolio"))
    b.row(InlineKeyboardButton(text="⭐ Отзывы",      callback_data="reviews_menu"))
    if admin:
        b.row(InlineKeyboardButton(text="🛠 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_adm_back() -> InlineKeyboardMarkup:
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

def kb_svc_page(svc_index: int, master_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✍️ Написать мастеру", url=master_url))
    b.row(InlineKeyboardButton(text="✅ Мастер одобрил — оформить запись",
                               callback_data=f"booking_approved:{svc_index}"))
    b.row(InlineKeyboardButton(text="🔙 Выбрать другую услугу", callback_data="book_start"))
    b.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return b.as_markup()

def kb_cancel_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu"))
    return b.as_markup()

def kb_cancel_adm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel"))
    return b.as_markup()

def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👥 Список пользователей",          callback_data="adm_users"))
    b.row(InlineKeyboardButton(text="📋 Записи клиентов",               callback_data="adm_bookings"))
    b.row(InlineKeyboardButton(text="📣 Рассылка всем клиентам",        callback_data="adm_broadcast"))
    b.row(InlineKeyboardButton(text="✏️ Тексты для записи",             callback_data="adm_svc_texts"))
    b.row(InlineKeyboardButton(text="🛡 Модерация отзывов",             callback_data="adm_reviews"))
    b.row(InlineKeyboardButton(
        text=f"🔔 Напоминания: {reminder_label()}",
        callback_data="adm_reminders"
    ))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",                  callback_data="main_menu"))
    return b.as_markup()

def kb_broadcast_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Отправить", callback_data="adm_do_broadcast"),
          InlineKeyboardButton(text="❌ Отмена",    callback_data="admin_panel"))
    return b.as_markup()

def kb_svc_list() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i, (name, _) in enumerate(SERVICES):
        b.button(text=name, callback_data=f"adm_edit_svc:{i}")
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔙 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_svc_edit(idx) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 Сбросить на стандартный", callback_data=f"adm_reset_svc:{idx}"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="adm_svc_texts"))
    return b.as_markup()

def kb_bookings_nav() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🕐 Ожидают подтверждения", callback_data="adm_book_pending"))
    b.row(InlineKeyboardButton(text="✅ Подтверждённые",         callback_data="adm_book_confirmed:0"))
    b.row(InlineKeyboardButton(text="🔙 Панель администратора",  callback_data="admin_panel"))
    return b.as_markup()

def kb_booking_moderate(booking_id, datetime_txt="") -> InlineKeyboardMarkup:
    """Кнопки для заявки: подтвердить (с автоопределением даты) и отклонить."""
    b = InlineKeyboardBuilder()
    # Если дата успешно распознана — показываем её в кнопке
    dt = parse_dt_from_text(datetime_txt) if datetime_txt else None
    if dt:
        dt_str = fmt_dt(dt.isoformat())
        b.row(InlineKeyboardButton(
            text=f"✅ Подтвердить ({dt_str})",
            callback_data=f"adm_book_ok:{booking_id}"
        ))
    else:
        b.row(InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"adm_book_ok:{booking_id}"
        ))
    b.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_book_del:{booking_id}"))
    b.row(InlineKeyboardButton(text="🔙 К заявкам", callback_data="adm_book_pending"))
    return b.as_markup()

def kb_booking_actions(booking_id) -> InlineKeyboardMarkup:
    """Кнопки для подтверждённой записи."""
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="🔔 Отправить напоминание",
        callback_data=f"adm_remind:{booking_id}"
    ))
    b.row(InlineKeyboardButton(
        text="❌ Отменить запись",
        callback_data=f"adm_book_del:{booking_id}"
    ))
    b.row(InlineKeyboardButton(text="🔙 К записям", callback_data="adm_book_confirmed:0"))
    return b.as_markup()

def kb_confirmed_nav(idx, total, booking_id) -> InlineKeyboardMarkup:
    """Навигация по подтверждённым записям + управление."""
    b = InlineKeyboardBuilder()
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"adm_book_confirmed:{idx-1}"))
    nav.append(InlineKeyboardButton(text=f"{idx+1}/{total}", callback_data="noop"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"adm_book_confirmed:{idx+1}"))
    if nav: b.row(*nav)
    b.row(InlineKeyboardButton(
        text="🔔 Отправить напоминание",
        callback_data=f"adm_remind:{booking_id}"
    ))
    b.row(InlineKeyboardButton(
        text="❌ Отменить запись",
        callback_data=f"adm_book_del:{booking_id}"
    ))
    b.row(InlineKeyboardButton(text="🔙 К записям", callback_data="adm_bookings"))
    return b.as_markup()

def kb_reminders() -> InlineKeyboardMarkup:
    """Настройка напоминаний — toggle кнопки."""
    def icon(key): return "✅" if REMINDER_SETTINGS[key] else "❌"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text=f"{icon('r24')} За 24 часа",  callback_data="adm_rem_toggle:r24"))
    b.row(InlineKeyboardButton(
        text=f"{icon('r12')} За 12 часов", callback_data="adm_rem_toggle:r12"))
    b.row(InlineKeyboardButton(
        text=f"{icon('r6')}  За 6 часов",  callback_data="adm_rem_toggle:r6"))
    b.row(InlineKeyboardButton(
        text=f"{icon('r1')}  За 1 час",    callback_data="adm_rem_toggle:r1"))
    b.row(InlineKeyboardButton(text="🔙 Панель администратора", callback_data="admin_panel"))
    return b.as_markup()

def kb_reviews_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📖 Смотреть отзывы", callback_data="reviews_browse:0"))
    b.row(InlineKeyboardButton(text="✍️ Написать отзыв",  callback_data="review_write"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",    callback_data="main_menu"))
    return b.as_markup()

def kb_rating() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(1, 6):
        b.button(text="⭐"*i, callback_data=f"rate:{i}")
    b.adjust(5)
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu"))
    return b.as_markup()

def kb_reviews_nav(idx, total) -> InlineKeyboardMarkup:
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
    b.row(InlineKeyboardButton(text="✅ Отправить", callback_data="review_submit"),
          InlineKeyboardButton(text="✏️ Изменить",  callback_data="review_write"))
    b.row(InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu"))
    return b.as_markup()

def kb_moderate_review(review_id) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_rev_ok:{review_id}"),
          InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"adm_rev_del:{review_id}"))
    b.row(InlineKeyboardButton(text="🔙 К модерации", callback_data="adm_reviews"))
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
async def cmd_admin(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await state.clear()
        await message.answer("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())
        return
    await state.set_state(AdminFSM.password)
    await message.answer("🔐 <b>Введите пароль администратора:</b>")

@auth_router.message(AdminFSM.password)
async def fsm_password(message: Message, state: FSMContext):
    if message.text and message.text.strip() == ADMIN_PASSWORD:
        await db_admin_add(message.from_user.id)
        await state.clear()
        try: await message.delete()
        except: pass
        await message.answer(
            "✅ <b>Доступ разрешён! Вы добавлены навсегда.</b>\n\n🛠 <b>Панель администратора</b>",
            reply_markup=kb_admin_main()
        )
    else:
        await message.answer("❌ Неверный пароль:")


# ══════════════════════════════════════════════════════════════════════════════
#  ОБЩИЕ
# ══════════════════════════════════════════════════════════════════════════════

@common_router.message(CommandStart())
@common_router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db_save_user(u.id, u.username, u.first_name)
    await message.answer(WELCOME, reply_markup=kb_main(is_admin(u.id)))

@common_router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(WELCOME, reply_markup=kb_main(is_admin(cb.from_user.id)))

@common_router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery): await cb.answer()

@common_router.callback_query(F.data == "prices")
async def cb_prices(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(PRICES_TEXT, reply_markup=kb_back())

@common_router.callback_query(F.data == "portfolio")
async def cb_portfolio(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "🌸 <b>Портфолио</b>\n\nСмотрите работы мастера в Telegram-канале:",
        reply_markup=kb_portfolio()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАПИСЬ (клиент)
# ══════════════════════════════════════════════════════════════════════════════

@user_router.callback_query(F.data == "book_start")
async def cb_book_start(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("💇‍♀️ <b>Выберите услугу для записи:</b>", reply_markup=kb_services())

@user_router.callback_query(F.data.startswith("svc:"))
async def cb_svc(cb: CallbackQuery):
    await cb.answer()
    idx = int(cb.data.split(":")[1])
    if idx >= len(SERVICES): return
    name, price = SERVICES[idx]
    url = await make_master_link(idx)
    await cb.message.edit_text(
        f"✅ Вы выбрали: <b>{name}</b>  —  {price}\n\n"
        f"<b>Шаг 1:</b> Нажмите «Написать мастеру», договоритесь о дате и времени.\n\n"
        f"<b>Шаг 2:</b> Вернитесь и нажмите «Мастер одобрил» 👇",
        reply_markup=kb_svc_page(idx, url)
    )

@booking_router.callback_query(F.data.startswith("booking_approved:"))
async def cb_booking_approved(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx  = int(cb.data.split(":")[1])
    name = SERVICES[idx][0]
    await state.set_state(BookingFSM.datetime_txt)
    await state.update_data(booking_service=name)
    await cb.message.edit_text(
        f"📅 <b>Оформление записи</b>\n\nУслуга: <b>{name}</b>\n\n"
        f"Введите дату и время, которые вы согласовали с мастером:\n"
        f"<i>Пример: <code>15.01 14:00</code> или <code>15.01.2025 14:00</code></i>",
        reply_markup=kb_cancel_main()
    )

@booking_router.message(BookingFSM.datetime_txt)
async def fsm_booking_dt(message: Message, state: FSMContext, bot: Bot):
    text    = (message.text or "").strip()
    data    = await state.get_data()
    service = data.get("booking_service", "—")
    u       = message.from_user

    bid = await db_add_booking(u.id, u.username, u.first_name, service, text)
    await state.clear()

    await message.answer(
        f"✅ <b>Заявка отправлена!</b>\n\nУслуга: <b>{service}</b>\nДата: <b>{text}</b>\n\n"
        f"⏳ Ожидайте подтверждения — придёт уведомление в бот.",
        reply_markup=kb_main(is_admin(u.id))
    )

    name  = u.first_name or "Аноним"
    uname = f" (@{u.username})" if u.username else ""

    # Пробуем автоматически распознать дату
    dt_auto = parse_dt_from_text(text)
    dt_hint = f"\n\n🤖 Автоопределённая дата: <b>{fmt_dt(dt_auto.isoformat())}</b>" if dt_auto else \
              "\n\n⚠️ Дату не удалось распознать автоматически."

    try:
        await bot.send_message(
            ADMIN_ID,
            f"📋 <b>Новая заявка #{bid}!</b>\n\n"
            f"👤 <b>{name}</b>{uname}\n"
            f"💇‍♀️ <b>{service}</b>\n"
            f"📅 Клиент написал: <b>{text}</b>"
            f"{dt_hint}",
            reply_markup=kb_booking_moderate(bid, text)
        )
    except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  ОТЗЫВЫ
# ══════════════════════════════════════════════════════════════════════════════

@review_router.callback_query(F.data == "reviews_menu")
async def cb_reviews_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    total = await db_count_approved_reviews()
    await cb.message.edit_text(
        f"⭐ <b>Отзывы клиентов</b>\n\nВсего: <b>{total}</b>\n\nПочитайте или оставьте свой:",
        reply_markup=kb_reviews_menu()
    )

@review_router.callback_query(F.data.startswith("reviews_browse:"))
async def cb_reviews_browse(cb: CallbackQuery):
    await cb.answer()
    idx     = int(cb.data.split(":")[1])
    reviews = await db_get_approved_reviews()
    total   = len(reviews)
    if total == 0:
        await cb.message.edit_text("💬 <b>Отзывов пока нет.</b>\n\nБудьте первым!", reply_markup=kb_reviews_menu())
        return
    idx = max(0, min(idx, total-1))
    await cb.message.edit_text(fmt_review(reviews[idx], idx+1, total), reply_markup=kb_reviews_nav(idx, total))

@review_router.callback_query(F.data == "review_write")
async def cb_review_write(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ReviewFSM.rating)
    await cb.message.edit_text("✍️ <b>Оставить отзыв</b>\n\nШаг 1 из 2: Выберите оценку 👇", reply_markup=kb_rating())

@review_router.callback_query(ReviewFSM.rating, F.data.startswith("rate:"))
async def cb_rate(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    r = int(cb.data.split(":")[1])
    await state.update_data(rating=r)
    await state.set_state(ReviewFSM.text)
    await cb.message.edit_text(
        f"✍️ <b>Оставить отзыв</b>\n\nОценка: {stars(r)}\n\n"
        f"Шаг 2 из 2: Напишите ваш отзыв 👇\n<i>(минимум 10 символов)</i>",
        reply_markup=InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu")
        ).as_markup()
    )

@review_router.message(ReviewFSM.text)
async def fsm_review_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer("⚠️ Минимум 10 символов:")
        return
    data = await state.get_data()
    await state.update_data(review_text=text)
    await message.answer(
        f"👀 <b>Предпросмотр:</b>\n\n{stars(data.get('rating',5))}\n\n{text}\n\n"
        f"Всё верно? Отзыв уйдёт на проверку мастеру.",
        reply_markup=kb_review_confirm()
    )

@review_router.callback_query(F.data == "review_submit")
async def cb_review_submit(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    data   = await state.get_data()
    rating = data.get("rating")
    text   = data.get("review_text")
    if not rating or not text:
        await cb.message.edit_text("Ошибка. Попробуйте снова.", reply_markup=kb_reviews_menu())
        await state.clear()
        return
    u   = cb.from_user
    rid = await db_add_review(u.id, u.username, u.first_name, rating, text)
    await state.clear()
    await cb.message.edit_text(
        "✅ <b>Спасибо за отзыв!</b>\n\nОтправлен на проверку мастеру.",
        reply_markup=kb_reviews_menu()
    )
    name  = u.first_name or "Аноним"
    uname = f" (@{u.username})" if u.username else ""
    try:
        await bot.send_message(ADMIN_ID,
            f"🔔 <b>Новый отзыв!</b>\n\nОт: <b>{name}</b>{uname}\nОценка: {stars(rating)}\n\n{text}",
            reply_markup=kb_moderate_review(rid))
    except: pass


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

@admin_cb_router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("🛠 <b>Панель администратора</b>", reply_markup=kb_admin_main())

# ── Пользователи ──────────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_users")
async def cb_adm_users(cb: CallbackQuery):
    await cb.answer()
    users = await db_get_all_users()
    total = len(users)
    if not users:
        await cb.message.edit_text("👥 <b>Пользователей пока нет.</b>", reply_markup=kb_adm_back())
        return
    lines = [f"👥 <b>Всего пользователей: {total} чел.</b>\n"]
    for u in users[:50]:
        uname = f"@{u['username']}" if u["username"] else f"ID {u['user_id']}"
        lines.append(f"• {u['first_name'] or '—'} — {uname}")
    if total > 50: lines.append(f"\n<i>...и ещё {total-50}</i>")
    await cb.message.edit_text("\n".join(lines), reply_markup=kb_adm_back())

# ── Записи клиентов ───────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_bookings")
async def cb_adm_bookings(cb: CallbackQuery):
    await cb.answer()
    p = await db_get_pending_bookings()
    c = await db_get_confirmed_bookings()
    await cb.message.edit_text(
        f"📋 <b>Записи клиентов</b>\n\n"
        f"🕐 Ожидают подтверждения: <b>{len(p)}</b>\n"
        f"✅ Подтверждённые: <b>{len(c)}</b>",
        reply_markup=kb_bookings_nav()
    )

@admin_cb_router.callback_query(F.data == "adm_book_pending")
async def cb_adm_book_pending(cb: CallbackQuery):
    await cb.answer()
    bookings = await db_get_pending_bookings()
    if not bookings:
        await cb.message.edit_text("🕐 <b>Заявок на подтверждение нет.</b>", reply_markup=kb_bookings_nav())
        return
    b     = bookings[0]
    name  = b["first_name"] or "Аноним"
    uname = f" (@{b['username']})" if b["username"] else ""

    # Автоопределение даты из текста клиента
    dt_auto = parse_dt_from_text(b["datetime_txt"])
    dt_line = f"\n🤖 Авто: <b>{fmt_dt(dt_auto.isoformat())}</b>" if dt_auto else \
              "\n⚠️ Дату не удалось определить автоматически"

    await cb.message.edit_text(
        f"🕐 <b>Заявок: {len(bookings)} шт.</b>\n\n{'─'*26}\n"
        f"👤 <b>{name}</b>{uname}\n"
        f"💇‍♀️ <b>{b['service_name']}</b>\n"
        f"📅 Написал: <b>{b['datetime_txt']}</b>"
        f"{dt_line}\n"
        f"🕒 Заявка: {b['created_at'][:16].replace('T',' ')}",
        reply_markup=kb_booking_moderate(b["id"], b["datetime_txt"])
    )

@admin_cb_router.callback_query(F.data.startswith("adm_book_confirmed:"))
async def cb_adm_book_confirmed(cb: CallbackQuery):
    await cb.answer()
    idx      = int(cb.data.split(":")[1])
    bookings = await db_get_confirmed_bookings()
    if not bookings:
        await cb.message.edit_text("✅ <b>Подтверждённых записей нет.</b>", reply_markup=kb_bookings_nav())
        return
    idx = max(0, min(idx, len(bookings)-1))
    b     = bookings[idx]
    name  = b["first_name"] or "Аноним"
    uname = f" (@{b['username']})" if b["username"] else ""
    dt    = fmt_dt(b["appt_dt"]) if b["appt_dt"] else b["datetime_txt"]
    await cb.message.edit_text(
        f"✅ <b>Подтверждённые записи: {len(bookings)} шт.</b>\n\n{'─'*26}\n"
        f"👤 <b>{name}</b>{uname}\n"
        f"💇‍♀️ <b>{b['service_name']}</b>\n"
        f"📅 <b>{dt}</b>",
        reply_markup=kb_confirmed_nav(idx, len(bookings), b["id"])
    )

@admin_cb_router.callback_query(F.data.startswith("adm_book_ok:"))
async def cb_adm_book_ok(cb: CallbackQuery, bot: Bot):
    """
    Подтверждение записи одной кнопкой.
    Дата берётся автоматически из текста клиента.
    """
    await cb.answer()
    bid     = int(cb.data.split(":")[1])
    booking = await db_get_booking(bid)
    if not booking:
        await cb.answer("Запись не найдена.", show_alert=True)
        return

    dt = parse_dt_from_text(booking["datetime_txt"])

    if dt:
        # Дата распознана — подтверждаем сразу
        await db_confirm_booking(bid, dt.isoformat())
        dt_fmt = fmt_dt(dt.isoformat())
        active = reminder_label()

        try:
            await bot.send_message(
                booking["user_id"],
                f"🎉 <b>Ваша запись подтверждена!</b>\n\n"
                f"💇‍♀️ Услуга: <b>{booking['service_name']}</b>\n"
                f"📅 Дата и время: <b>{dt_fmt}</b>\n"
                f"✂️ Мастер: <b>{MASTER_NAME_FULL}</b>\n\n"
                f"Напоминания придут автоматически ({active}).\n"
                f"<i>Если нужно перенести — напишите мастеру.</i>"
            )
        except: pass

        await cb.message.edit_text(
            f"✅ <b>Запись #{bid} подтверждена!</b>\n\n"
            f"💇‍♀️ {booking['service_name']}\n"
            f"📅 {dt_fmt}\n\n"
            f"Клиент уведомлён. Напоминания: {active}.",
            reply_markup=kb_adm_back()
        )
    else:
        # Дату не удалось распознать — просим ввести вручную
        await cb.message.edit_text(
            f"⚠️ <b>Не удалось определить дату из текста клиента.</b>\n\n"
            f"Клиент написал: <b>{booking['datetime_txt']}</b>\n\n"
            f"Введите дату вручную:\n"
            f"<i>Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\nПример: <code>15.01.2025 14:00</code></i>",
            reply_markup=kb_cancel_adm()
        )
        # Сохраняем в FSM для обработки ввода
        # (используем временный state через message, но у нас callback — сохраняем в БД)
        # Запишем booking_id в settings как временное значение
        await db_save_setting("pending_confirm_bid", str(bid))

@admin_cb_router.callback_query(F.data.startswith("adm_book_del:"))
async def cb_adm_book_del(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    bid     = int(cb.data.split(":")[1])
    booking = await db_get_booking(bid)
    await db_cancel_booking(bid)
    if booking:
        try:
            await bot.send_message(
                booking["user_id"],
                f"😔 <b>Ваша запись отменена.</b>\n\n"
                f"Услуга: <b>{booking['service_name']}</b>\n\n"
                f"Если хотите перенести — напишите мастеру.",
                reply_markup=kb_main(False)
            )
        except: pass
    await cb.message.edit_text(
        f"❌ Запись #{bid} отменена, клиент уведомлён.",
        reply_markup=kb_bookings_nav()
    )

@admin_cb_router.callback_query(F.data.startswith("adm_remind:"))
async def cb_adm_remind(cb: CallbackQuery, bot: Bot):
    await cb.answer("🔔 Напоминание отправлено!")
    bid     = int(cb.data.split(":")[1])
    booking = await db_get_booking(bid)
    if not booking:
        await cb.answer("Запись не найдена.", show_alert=True)
        return
    dt = fmt_dt(booking["appt_dt"]) if booking["appt_dt"] else booking["datetime_txt"]
    try:
        await bot.send_message(
            booking["user_id"],
            f"🔔 <b>Напоминание о вашей записи!</b>\n\n"
            f"💇‍♀️ Услуга: <b>{booking['service_name']}</b>\n"
            f"📅 Дата и время: <b>{dt}</b>\n"
            f"✂️ Мастер: <b>{MASTER_NAME_FULL}</b>\n\n"
            f"<i>Ждём вас! 🌸</i>"
        )
    except: pass

# ── Настройка напоминаний ─────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_reminders")
async def cb_adm_reminders(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        f"🔔 <b>Настройка автонапоминаний</b>\n\n"
        f"Выберите когда отправлять напоминания клиентам.\n"
        f"✅ = включено  |  ❌ = выключено\n\n"
        f"<i>Изменения применяются сразу для всех будущих напоминаний.</i>",
        reply_markup=kb_reminders()
    )

@admin_cb_router.callback_query(F.data.startswith("adm_rem_toggle:"))
async def cb_adm_rem_toggle(cb: CallbackQuery):
    await cb.answer()
    key = cb.data.split(":")[1]  # r24 / r12 / r6 / r1
    if key not in REMINDER_SETTINGS:
        return
    # Переключаем
    REMINDER_SETTINGS[key] = not REMINDER_SETTINGS[key]
    # Сохраняем в БД
    db_key = key.replace("r", "reminder_")
    await db_save_setting(db_key, "1" if REMINDER_SETTINGS[key] else "0")
    # Обновляем клавиатуру
    await cb.message.edit_reply_markup(reply_markup=kb_reminders())

# ── Тексты услуг ──────────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_svc_texts")
async def cb_adm_svc_texts(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        "✏️ <b>Редактирование авто-текстов</b>\n\nВыберите услугу:",
        reply_markup=kb_svc_list()
    )

@admin_cb_router.callback_query(F.data.startswith("adm_edit_svc:"))
async def cb_adm_edit_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx     = int(cb.data.split(":")[1])
    current = await db_get_service_text(idx)
    custom  = current != DEFAULT_SERVICE_TEXTS[idx]
    await state.set_state(AdminFSM.edit_svc_text)
    await state.update_data(editing_svc=idx)
    await cb.message.edit_text(
        f"✏️ <b>«{SERVICES[idx][0]}»</b>\n\n"
        f"Статус: <i>{'🟡 изменён' if custom else '🟢 стандартный'}</i>\n\n"
        f"<b>Текущий текст:</b>\n<code>{current}</code>\n\nНапишите новый текст:",
        reply_markup=kb_svc_edit(idx)
    )

@admin_cb_router.callback_query(F.data.startswith("adm_reset_svc:"))
async def cb_adm_reset_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx = int(cb.data.split(":")[1])
    await db_reset_service_text(idx)
    await state.clear()
    await cb.message.edit_text(
        f"✅ Сброшен стандартный текст для «{SERVICES[idx][0]}»:\n\n<code>{DEFAULT_SERVICE_TEXTS[idx]}</code>",
        reply_markup=kb_svc_list()
    )

# ── Рассылка ──────────────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    total = await db_count_users()
    await state.set_state(AdminFSM.broadcast_msg)
    await cb.message.edit_text(
        f"📣 <b>Рассылка</b>\n\nПолучателей: <b>{total} чел.</b>\n\nВведите текст:",
        reply_markup=kb_adm_back()
    )

@admin_cb_router.callback_query(F.data == "adm_do_broadcast")
async def cb_adm_do_broadcast(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    if await state.get_state() != AdminFSM.broadcast_confirm:
        await cb.answer("Сначала введите текст.", show_alert=True)
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
        except: failed += 1
    await cb.message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n✔ Отправлено: <b>{sent}</b>\n✖ Ошибок: <b>{failed}</b>",
        reply_markup=kb_admin_main()
    )

# ── Модерация отзывов ─────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_reviews")
async def cb_adm_reviews(cb: CallbackQuery):
    await cb.answer()
    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text("🛡 <b>Нет отзывов на проверке.</b>", reply_markup=kb_adm_back())
        return
    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    await cb.message.edit_text(
        f"🛡 <b>Модерация: {len(pending)} шт.</b>\n\n{'─'*26}\n"
        f"От: <b>{name}</b>{uname} | {r['created_at'][:10]}\n"
        f"Оценка: {stars(r['rating'])}\n\n{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )

async def _next_review(cb):
    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text("🛡 <b>Все отзывы проверены!</b>", reply_markup=kb_adm_back())
        return
    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    await cb.message.edit_text(
        f"🛡 <b>Модерация: {len(pending)} шт.</b>\n\n{'─'*26}\n"
        f"От: <b>{name}</b>{uname} | {r['created_at'][:10]}\n"
        f"Оценка: {stars(r['rating'])}\n\n{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )

@admin_cb_router.callback_query(F.data.startswith("adm_rev_ok:"))
async def cb_adm_rev_ok(cb: CallbackQuery):
    await cb.answer("✅ Одобрен!")
    await db_set_review_status(int(cb.data.split(":")[1]), "approved")
    await _next_review(cb)

@admin_cb_router.callback_query(F.data.startswith("adm_rev_del:"))
async def cb_adm_rev_del(cb: CallbackQuery):
    await cb.answer("🗑 Удалён.")
    await db_set_review_status(int(cb.data.split(":")[1]), "rejected")
    await _next_review(cb)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — FSM ВВОД
# ══════════════════════════════════════════════════════════════════════════════

@admin_fsm_router.message(AdminFSM.broadcast_msg)
async def fsm_broadcast(message: Message, state: FSMContext):
    text  = message.text or ""
    total = await db_count_users()
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminFSM.broadcast_confirm)
    await message.answer(
        f"📣 <b>Предпросмотр:</b>\n\n{'─'*26}\n{text}\n{'─'*26}\n\n"
        f"Отправить <b>{total}</b> пользователям?",
        reply_markup=kb_broadcast_confirm()
    )

@admin_fsm_router.message(AdminFSM.edit_svc_text)
async def fsm_svc_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Текст не может быть пустым:")
        return
    data = await state.get_data()
    idx  = data.get("editing_svc")
    if idx is None:
        await state.clear()
        return
    await db_set_service_text(idx, text)
    await state.clear()
    await message.answer(
        f"✅ <b>Текст для «{SERVICES[idx][0]}» обновлён!</b>\n\n<code>{text}</code>",
        reply_markup=kb_svc_list()
    )

@admin_fsm_router.message(F.text)
async def fsm_manual_date_input(message: Message, bot: Bot):
    """
    Ловим ввод даты вручную если автоопределение не сработало.
    Используется только если в settings есть pending_confirm_bid.
    """
    if not is_admin(message.from_user.id):
        return
    # Проверяем есть ли ожидающее подтверждение
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='pending_confirm_bid'")
        row = await cur.fetchone()
    if not row:
        return

    bid  = int(row[0])
    text = (message.text or "").strip()

    # Пробуем распознать дату
    dt = parse_dt_from_text(text)
    if not dt:
        # Попробуем строго
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                if fmt == "%d.%m %H:%M":
                    dt = dt.replace(year=datetime.now().year)
                break
            except: pass

    if not dt:
        await message.answer(
            "⚠️ Не удалось распознать дату.\n"
            "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\nПример: <code>15.01.2025 14:00</code>"
        )
        return

    booking = await db_get_booking(bid)
    if not booking:
        await message.answer("Запись не найдена.")
        return

    await db_confirm_booking(bid, dt.isoformat())
    # Очищаем временный флаг
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE key='pending_confirm_bid'")
        await db.commit()

    dt_fmt = fmt_dt(dt.isoformat())
    active = reminder_label()

    try:
        await bot.send_message(
            booking["user_id"],
            f"🎉 <b>Ваша запись подтверждена!</b>\n\n"
            f"💇‍♀️ Услуга: <b>{booking['service_name']}</b>\n"
            f"📅 Дата и время: <b>{dt_fmt}</b>\n"
            f"✂️ Мастер: <b>{MASTER_NAME_FULL}</b>\n\n"
            f"Напоминания придут автоматически ({active}).\n"
            f"<i>Если нужно перенести — напишите мастеру.</i>"
        )
    except: pass

    await message.answer(
        f"✅ <b>Запись #{bid} подтверждена!</b>\n\n"
        f"💇‍♀️ {booking['service_name']}\n📅 {dt_fmt}\n\n"
        f"Клиент уведомлён. Напоминания: {active}.",
        reply_markup=kb_admin_main()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  НАПОМИНАНИЯ (фоновая задача)
# ══════════════════════════════════════════════════════════════════════════════

async def reminder_worker(bot: Bot):
    while True:
        try:
            now = datetime.now()
            for b in await db_get_bookings_for_reminders():
                try: appt = datetime.fromisoformat(b["appt_dt"])
                except: continue
                diff = (appt - now).total_seconds() / 3600
                uid  = b["user_id"]
                svc  = b["service_name"]
                bid  = b["id"]
                dtf  = fmt_dt(b["appt_dt"])

                # За 24 часа
                if REMINDER_SETTINGS["r24"] and not b["reminded_24"] and 23.5 <= diff <= 24.5:
                    try:
                        await bot.send_message(uid,
                            f"🔔 <b>Напоминание!</b>\n\nЗавтра у вас запись:\n"
                            f"💇‍♀️ <b>{svc}</b>\n📅 <b>{dtf}</b>\n\n"
                            f"<i>Ждём вас! 🌸</i>")
                        await db_mark_reminded(bid, "reminded_24")
                    except: pass

                # За 12 часов
                elif REMINDER_SETTINGS["r12"] and not b["reminded_12"] and 11.5 <= diff <= 12.5:
                    try:
                        await bot.send_message(uid,
                            f"⏰ <b>Напоминание!</b>\n\nЧерез 12 часов:\n"
                            f"💇‍♀️ <b>{svc}</b>\n📅 <b>{dtf}</b>\n\n"
                            f"<i>Не забудьте! 💫</i>")
                        await db_mark_reminded(bid, "reminded_12")
                    except: pass

                # За 6 часов
                elif REMINDER_SETTINGS["r6"] and not b["reminded_6"] and 5.5 <= diff <= 6.5:
                    try:
                        await bot.send_message(uid,
                            f"⏰ <b>Напоминание!</b>\n\nЧерез 6 часов:\n"
                            f"💇‍♀️ <b>{svc}</b>\n📅 <b>{dtf}</b>\n\n"
                            f"<i>Скоро увидимся! ✨</i>")
                        await db_mark_reminded(bid, "reminded_6")
                    except: pass

                # За 1 час
                elif REMINDER_SETTINGS["r1"] and not b["reminded_1"] and 0.75 <= diff <= 1.25:
                    try:
                        await bot.send_message(uid,
                            f"⚡ <b>Напоминание!</b>\n\nЧерез 1 час:\n"
                            f"💇‍♀️ <b>{svc}</b>\n📅 <b>{dtf}</b>\n\n"
                            f"<i>Выезжайте! 🚀</i>")
                        await db_mark_reminded(bid, "reminded_1")
                    except: pass

        except Exception as e:
            log.error(f"reminder_worker: {e}")
        await asyncio.sleep(60)


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

    asyncio.create_task(reminder_worker(bot))

    try:
        log.info("Бот запущен!")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=True)
    finally:
        await bot.session.close()
        log.info("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
