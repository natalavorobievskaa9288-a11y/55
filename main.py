"""
Telegram-бот для записи к мастеру — Полина Евдокимова.
Aiogram 3 · SQLite (aiosqlite)

v10:
✅ Система отзывов: написать отзыв (оценка ⭐ + текст) → модерация у админа → публикация
✅ Листалка отзывов (1 отзыв на экране, кнопки ◀ ▶)
✅ Счётчик пользователей не считает самого администратора
✅ Авторизация админа постоянная (в БД)
✅ Тексты услуг редактируются в админке и применяются для всех
"""

import asyncio
import logging
import json
import urllib.parse
import aiosqlite

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
                    key   TEXT PRIMARY KEY,
                    state TEXT,
                    data  TEXT NOT NULL DEFAULT '{}'
                )
            """)
            await db.commit()

    @staticmethod
    def _key(key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}"

    async def set_state(self, key: StorageKey, state: StateType = None):
        k  = self._key(key)
        sv = state.state if hasattr(state, "state") else (
             state if isinstance(state, str) else None)
        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO fsm_data (key, state, data) VALUES (?, ?, '{}')
                    ON CONFLICT(key) DO UPDATE SET state=excluded.state
                """, (k, sv))
                await db.commit()

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT state FROM fsm_data WHERE key=?", (self._key(key),)
            )
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
            cur = await db.execute(
                "SELECT data FROM fsm_data WHERE key=?", (self._key(key),)
            )
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

BOT_TOKEN      = "8386414173:AAEy5JnqOpqKvT72RQi8NeoMx7tk9xxEJyk"
ADMIN_ID       = 123456789
DB_PATH        = "manicure.db"
ADMIN_PASSWORD = "adinspalina999"

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
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS service_texts (
                svc_index   INTEGER PRIMARY KEY,
                custom_text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_sessions (
                user_id   INTEGER PRIMARY KEY,
                authed_at TEXT NOT NULL
            );

            -- Отзывы: status = 'pending' | 'approved' | 'rejected'
            CREATE TABLE IF NOT EXISTS reviews (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                first_name TEXT,
                rating     INTEGER NOT NULL,
                text       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
        """)
        await db.commit()
    log.info("БД готова.")


# ── Пользователи ──────────────────────────────────────────────────────────────

async def db_save_user(user_id: int, username: str | None, first_name: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username, first_name, datetime.now().isoformat()))
        await db.commit()


async def db_get_all_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at DESC"
        )
        rows = await cur.fetchall()
    return [{"user_id": r[0], "username": r[1], "first_name": r[2], "created_at": r[3]}
            for r in rows]


async def db_get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        return [r[0] for r in await cur.fetchall()]


async def db_count_users(exclude_id: int | None = None) -> int:
    """Считает всех пользователей, опционально исключая одного (например, администратора)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if exclude_id:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE user_id != ?", (exclude_id,)
            )
        else:
            cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
    return row[0] if row else 0


# ── Тексты услуг ──────────────────────────────────────────────────────────────

async def db_get_service_text(svc_index: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT custom_text FROM service_texts WHERE svc_index=?", (svc_index,)
        )
        row = await cur.fetchone()
    return row[0] if row else DEFAULT_SERVICE_TEXTS[svc_index]


async def db_set_service_text(svc_index: int, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO service_texts (svc_index, custom_text) VALUES (?,?)",
            (svc_index, text)
        )
        await db.commit()


async def db_reset_service_text(svc_index: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM service_texts WHERE svc_index=?", (svc_index,))
        await db.commit()


# ── Авторизация админов ───────────────────────────────────────────────────────

async def db_admin_add(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admin_sessions (user_id, authed_at) VALUES (?,?)",
            (user_id, datetime.now().isoformat())
        )
        await db.commit()


async def db_admin_check(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM admin_sessions WHERE user_id=?", (user_id,)
        )
        return await cur.fetchone() is not None


# ── Отзывы ────────────────────────────────────────────────────────────────────

async def db_add_review(user_id: int, username: str | None, first_name: str | None,
                        rating: int, text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO reviews (user_id, username, first_name, rating, text, status, created_at)
            VALUES (?,?,?,?,?,'pending',?)
        """, (user_id, username, first_name, rating, text, datetime.now().isoformat()))
        await db.commit()
        return cur.lastrowid


async def db_get_approved_reviews() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, first_name, rating, text, created_at
            FROM reviews WHERE status='approved'
            ORDER BY created_at DESC
        """)
        rows = await cur.fetchall()
    return [{"id": r[0], "user_id": r[1], "username": r[2], "first_name": r[3],
             "rating": r[4], "text": r[5], "created_at": r[6]} for r in rows]


async def db_get_pending_reviews() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, user_id, username, first_name, rating, text, created_at
            FROM reviews WHERE status='pending'
            ORDER BY created_at ASC
        """)
        rows = await cur.fetchall()
    return [{"id": r[0], "user_id": r[1], "username": r[2], "first_name": r[3],
             "rating": r[4], "text": r[5], "created_at": r[6]} for r in rows]


async def db_set_review_status(review_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reviews SET status=? WHERE id=?", (status, review_id)
        )
        await db.commit()


async def db_count_approved_reviews() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM reviews WHERE status='approved'")
        row = await cur.fetchone()
    return row[0] if row else 0


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
        f"💬 <b>Отзыв {idx} из {total}</b>\n"
        f"{'─'*28}\n"
        f"{stars(r['rating'])}  <b>{name}</b>{uname}\n"
        f"<i>{date}</i>\n\n"
        f"{r['text']}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  FSM-СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════════════════════════

class AdminFSM(StatesGroup):
    password          = State()
    broadcast_msg     = State()
    broadcast_confirm = State()
    edit_svc_text     = State()


class ReviewFSM(StatesGroup):
    rating = State()
    text   = State()


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


async def kb_write_to_master(svc_index: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="✍️ Написать мастеру",
        url=await make_master_link(svc_index)
    ))
    b.row(InlineKeyboardButton(text="🔙 Выбрать другую услугу", callback_data="book_start"))
    b.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return b.as_markup()


def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👥 Список пользователей",    callback_data="adm_users"))
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
    b.row(InlineKeyboardButton(
        text="🔄 Сбросить на стандартный",
        callback_data=f"adm_reset_svc:{svc_index}"
    ))
    b.row(InlineKeyboardButton(
        text="❌ Отменить редактирование",
        callback_data="adm_svc_texts"
    ))
    return b.as_markup()


# ── Отзывы (пользователь) ─────────────────────────────────────────────────────

def kb_reviews_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📖 Смотреть отзывы",   callback_data="reviews_browse:0"))
    b.row(InlineKeyboardButton(text="✍️ Написать отзыв",    callback_data="review_write"))
    b.row(InlineKeyboardButton(text="🔙 Главное меню",      callback_data="main_menu"))
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


# ── Модерация отзывов (админ) ─────────────────────────────────────────────────

def kb_moderate_review(review_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ Одобрить",  callback_data=f"adm_rev_ok:{review_id}"),
        InlineKeyboardButton(text="🗑 Удалить",   callback_data=f"adm_rev_del:{review_id}"),
    )
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
            "<i>Вы навсегда добавлены как администратор — пароль больше вводить не нужно.</i>\n\n"
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
#  ЗАПИСЬ
# ══════════════════════════════════════════════════════════════════════════════

@user_router.callback_query(F.data == "book_start")
async def cb_book_start(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "💇‍♀️ <b>Выберите услугу для записи:</b>\n\n"
        "После выбора вы перейдёте в чат с мастером — "
        "там уже будет готовый текст сообщения!",
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
        f"✅ Вы выбрали:\n"
        f"<b>{service_name}</b>  —  {service_price}\n\n"
        f"Нажмите кнопку ниже — откроется чат с мастером.\n"
        f"Сообщение уже будет заполнено, просто нажмите <b>Отправить</b>! 👇",
        reply_markup=await kb_write_to_master(idx)
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ОТЗЫВЫ — ПОЛЬЗОВАТЕЛЬ
# ══════════════════════════════════════════════════════════════════════════════

@review_router.callback_query(F.data == "reviews_menu")
async def cb_reviews_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    total = await db_count_approved_reviews()
    await cb.message.edit_text(
        f"⭐ <b>Отзывы клиентов</b>\n\n"
        f"Всего отзывов: <b>{total}</b>\n\n"
        f"Здесь вы можете почитать отзывы других клиентов или оставить свой:",
        reply_markup=kb_reviews_menu()
    )


@review_router.callback_query(F.data.startswith("reviews_browse:"))
async def cb_reviews_browse(cb: CallbackQuery):
    await cb.answer()
    idx      = int(cb.data.split(":")[1])
    reviews  = await db_get_approved_reviews()
    total    = len(reviews)

    if total == 0:
        await cb.message.edit_text(
            "💬 <b>Отзывов пока нет.</b>\n\nБудьте первым — оставьте отзыв!",
            reply_markup=kb_reviews_menu()
        )
        return

    # Защита от выхода за пределы
    idx = max(0, min(idx, total - 1))
    r   = reviews[idx]

    await cb.message.edit_text(
        format_review(r, idx + 1, total),
        reply_markup=kb_reviews_nav(idx, total)
    )


@review_router.callback_query(F.data == "review_write")
async def cb_review_write(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ReviewFSM.rating)
    await cb.message.edit_text(
        "✍️ <b>Оставить отзыв</b>\n\n"
        "Шаг 1 из 2: Выберите вашу оценку 👇",
        reply_markup=kb_rating()
    )


@review_router.callback_query(ReviewFSM.rating, F.data.startswith("rate:"))
async def cb_review_rating(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    rating = int(cb.data.split(":")[1])
    await state.update_data(rating=rating)
    await state.set_state(ReviewFSM.text)
    await cb.message.edit_text(
        f"✍️ <b>Оставить отзыв</b>\n\n"
        f"Ваша оценка: {stars(rating)}\n\n"
        f"Шаг 2 из 2: Напишите ваш отзыв текстом 👇\n"
        f"<i>(минимум 10 символов)</i>",
        reply_markup=InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="❌ Отмена", callback_data="reviews_menu")
        ).as_markup()
    )


@review_router.message(ReviewFSM.text)
async def fsm_review_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 10:
        await message.answer("⚠️ Отзыв слишком короткий. Напишите хотя бы 10 символов:")
        return

    data   = await state.get_data()
    rating = data.get("rating", 5)
    await state.update_data(review_text=text)
    await state.set_state(ReviewFSM.text)  # остаёмся, ждём confirm

    await message.answer(
        f"👀 <b>Предпросмотр вашего отзыва:</b>\n\n"
        f"{stars(rating)}\n\n"
        f"{text}\n\n"
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
        await cb.message.edit_text(
            "Что-то пошло не так. Попробуйте снова.",
            reply_markup=kb_reviews_menu()
        )
        await state.clear()
        return

    user = cb.from_user
    review_id = await db_add_review(user.id, user.username, user.first_name, rating, text)
    await state.clear()

    await cb.message.edit_text(
        "✅ <b>Спасибо за отзыв!</b>\n\n"
        "Он отправлен на проверку мастеру и скоро появится в общем списке.",
        reply_markup=kb_reviews_menu()
    )

    # Уведомляем администратора
    name  = user.first_name or "Аноним"
    uname = f" (@{user.username})" if user.username else ""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Новый отзыв на проверку!</b>\n\n"
            f"От: <b>{name}</b>{uname}\n"
            f"Оценка: {stars(rating)}\n\n"
            f"{text}\n\n"
            f"<i>ID отзыва: {review_id}</i>",
            reply_markup=kb_moderate_review(review_id)
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — CALLBACK КНОПКИ
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
        await cb.message.edit_text(
            "👥 <b>Пользователей пока нет.</b>",
            reply_markup=kb_admin_back()
        )
        return

    lines = [f"👥 <b>Пользователей в боте: {total} чел.</b>\n"]
    for u in users[:50]:
        uname = f"@{u['username']}" if u["username"] else f"ID {u['user_id']}"
        name  = u["first_name"] or "—"
        lines.append(f"• {name} — {uname}")
    if total > 50:
        lines.append(f"\n<i>...и ещё {total - 50} пользователей</i>")

    await cb.message.edit_text("\n".join(lines), reply_markup=kb_admin_back())


@admin_cb_router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    total = await db_count_users()
    await state.set_state(AdminFSM.broadcast_msg)
    await cb.message.edit_text(
        f"📣 <b>Рассылка</b>\n\n"
        f"Получателей: <b>{total} чел.</b>\n\n"
        f"Введите текст рассылки.\n"
        f"Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>",
        reply_markup=kb_admin_back()
    )


@admin_cb_router.callback_query(F.data == "adm_do_broadcast")
async def cb_adm_do_broadcast(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    cur_state = await state.get_state()
    if cur_state != AdminFSM.broadcast_confirm:
        await cb.answer("Сначала введите текст рассылки.", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    # Рассылаем всем включая администратора
    user_ids = await db_get_all_user_ids()
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
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"✔ Отправлено: <b>{sent}</b>\n"
        f"✖ Ошибок:    <b>{failed}</b>",
        reply_markup=kb_admin_main()
    )


# ── Редактор текстов услуг ────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_svc_texts")
async def cb_adm_svc_texts(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        "✏️ <b>Редактирование авто-текстов</b>\n\n"
        "Выберите услугу — изменения применяются сразу для <b>всех</b> пользователей:",
        reply_markup=kb_svc_texts_list()
    )


@admin_cb_router.callback_query(F.data.startswith("adm_edit_svc:"))
async def cb_adm_edit_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx       = int(cb.data.split(":")[1])
    svc_name  = SERVICES[idx][0]
    current   = await db_get_service_text(idx)
    is_custom = (current != DEFAULT_SERVICE_TEXTS[idx])
    status    = "🟡 изменён вами" if is_custom else "🟢 стандартный"

    await state.set_state(AdminFSM.edit_svc_text)
    await state.update_data(editing_svc_index=idx)

    await cb.message.edit_text(
        f"✏️ <b>Редактирование: «{svc_name}»</b>\n\n"
        f"Статус: <i>{status}</i>\n\n"
        f"<b>Текущий текст:</b>\n"
        f"<code>{current}</code>\n\n"
        f"Напишите новый текст — он появится у всех клиентов при записи на эту услугу.",
        reply_markup=kb_svc_text_edit(idx)
    )


@admin_cb_router.callback_query(F.data.startswith("adm_reset_svc:"))
async def cb_adm_reset_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx      = int(cb.data.split(":")[1])
    svc_name = SERVICES[idx][0]
    await db_reset_service_text(idx)
    await state.clear()
    await cb.message.edit_text(
        f"✅ <b>Текст для «{svc_name}» сброшен на стандартный:</b>\n\n"
        f"<code>{DEFAULT_SERVICE_TEXTS[idx]}</code>",
        reply_markup=kb_svc_texts_list()
    )


# ── Модерация отзывов ─────────────────────────────────────────────────────────

@admin_cb_router.callback_query(F.data == "adm_reviews")
async def cb_adm_reviews(cb: CallbackQuery):
    await cb.answer()
    pending = await db_get_pending_reviews()

    if not pending:
        await cb.message.edit_text(
            "🛡 <b>Модерация отзывов</b>\n\n"
            "✅ Нет отзывов на проверке. Всё чисто!",
            reply_markup=kb_admin_back()
        )
        return

    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    date  = r["created_at"][:10]

    await cb.message.edit_text(
        f"🛡 <b>Модерация отзывов</b>\n"
        f"Ожидают проверки: <b>{len(pending)}</b>\n\n"
        f"{'─'*28}\n"
        f"От: <b>{name}</b>{uname} | {date}\n"
        f"Оценка: {stars(r['rating'])}\n\n"
        f"{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )


@admin_cb_router.callback_query(F.data.startswith("adm_rev_ok:"))
async def cb_adm_rev_approve(cb: CallbackQuery):
    await cb.answer("✅ Отзыв одобрен!")
    review_id = int(cb.data.split(":")[1])
    await db_set_review_status(review_id, "approved")

    # Показываем следующий на модерации
    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text(
            "🛡 <b>Модерация отзывов</b>\n\n"
            "✅ Все отзывы проверены!",
            reply_markup=kb_admin_back()
        )
        return

    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    date  = r["created_at"][:10]
    await cb.message.edit_text(
        f"🛡 <b>Модерация отзывов</b>\n"
        f"Ожидают проверки: <b>{len(pending)}</b>\n\n"
        f"{'─'*28}\n"
        f"От: <b>{name}</b>{uname} | {date}\n"
        f"Оценка: {stars(r['rating'])}\n\n"
        f"{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )


@admin_cb_router.callback_query(F.data.startswith("adm_rev_del:"))
async def cb_adm_rev_delete(cb: CallbackQuery):
    await cb.answer("🗑 Отзыв удалён.")
    review_id = int(cb.data.split(":")[1])
    await db_set_review_status(review_id, "rejected")

    pending = await db_get_pending_reviews()
    if not pending:
        await cb.message.edit_text(
            "🛡 <b>Модерация отзывов</b>\n\n"
            "✅ Все отзывы проверены!",
            reply_markup=kb_admin_back()
        )
        return

    r     = pending[0]
    name  = r["first_name"] or "Аноним"
    uname = f" (@{r['username']})" if r["username"] else ""
    date  = r["created_at"][:10]
    await cb.message.edit_text(
        f"🛡 <b>Модерация отзывов</b>\n"
        f"Ожидают проверки: <b>{len(pending)}</b>\n\n"
        f"{'─'*28}\n"
        f"От: <b>{name}</b>{uname} | {date}\n"
        f"Оценка: {stars(r['rating'])}\n\n"
        f"{r['text']}",
        reply_markup=kb_moderate_review(r["id"])
    )


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
        f"📣 <b>Предпросмотр рассылки:</b>\n\n"
        f"{'─'*28}\n{text}\n{'─'*28}\n\n"
        f"Отправить <b>{total}</b> пользователям?",
        reply_markup=kb_broadcast_confirm()
    )


@admin_fsm_router.message(AdminFSM.edit_svc_text)
async def fsm_edit_svc_text(message: Message, state: FSMContext):
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("⚠️ Текст не может быть пустым. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    idx  = data.get("editing_svc_index")
    if idx is None:
        await state.clear()
        await message.answer("Ошибка. Попробуйте снова.", reply_markup=kb_admin_main())
        return

    await db_set_service_text(idx, new_text)
    await state.clear()
    svc_name = SERVICES[idx][0]
    await message.answer(
        f"✅ <b>Текст для «{svc_name}» обновлён для всех пользователей!</b>\n\n"
        f"<b>Новый текст:</b>\n"
        f"<code>{new_text}</code>",
        reply_markup=kb_svc_texts_list()
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    log.info("Запуск бота...")
    await init_db()

    fsm_storage = SQLiteFSMStorage(DB_PATH)
    await fsm_storage.init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=fsm_storage)

    dp.include_router(auth_router)
    dp.include_router(common_router)
    dp.include_router(user_router)
    dp.include_router(review_router)
    dp.include_router(admin_cb_router)
    dp.include_router(admin_fsm_router)

    try:
        log.info("Бот запущен. Жду сообщений...")
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
