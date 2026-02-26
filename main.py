"""
Telegram-бот для записи к мастеру — Полина Евдокимова.
Aiogram 3 · SQLite (aiosqlite)

v9:
✅ Авторизация админа сохраняется в БД — не слетает при перезапуске
✅ Пользователи не теряются из базы никогда
✅ Тексты услуг меняются сразу для всех клиентов
✅ Исправлен подсчёт пользователей
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
#  КОНФИГУРАЦИЯ  ←  ЗАПОЛНИТЕ ПЕРЕД ЗАПУСКОМ
# ══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN      = "ВАШ_ТОКЕН"
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
    "Здравствуйте, я с бота по записи, хочу записаться в один тон",
    "Здравствуйте, я с бота по записи, хочу записаться на окрашивание корней",
    "Здравствуйте, я с бота по записи, хочу записаться на тонирование блонда",
    "Здравствуйте, я с бота по записи, хочу записаться на осветление корней + тонирование",
    "Здравствуйте, я с бота по записи, хочу записаться на глубокий контуринг",
    "Здравствуйте, я с бота по записи, хочу записаться на стрижку",
    "Здравствуйте, я с бота по записи, хочу записаться на укладку (брашинг)",
    "Здравствуйте, я с бота по записи, хочу записаться на укладку локонами",
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
        """)
        await db.commit()
    log.info("БД готова.")


# ── Пользователи ──────────────────────────────────────────────────────────────

async def db_save_user(user_id: int, username: str | None, first_name: str | None):
    """Сохраняет нового пользователя. Уже существующих не трогает (дата сохраняется)."""
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


async def db_count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
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


# ── Постоянная авторизация админов ───────────────────────────────────────────

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


# ══════════════════════════════════════════════════════════════════════════════
#  FSM-СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════════════════════════

class AdminFSM(StatesGroup):
    password          = State()
    broadcast_msg     = State()
    broadcast_confirm = State()
    edit_svc_text     = State()


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
    "┃  <i>надбавка за густоту</i> — <b>1 500 – 2 000 ₽</b>\n"
    "┣ В один тон — <b>5 000 – 9 000 ₽</b>\n"
    "┣ Окрашивание корней — <b>3 500 – 4 000 ₽</b>\n"
    "┣ Тонирование блонда — <b>5 000 – 8 000 ₽</b>\n"
    "┣ Осветление корней + тонирование — <b>6 000 – 9 000 ₽</b>\n"
    "┗ Глубокий контуринг — <b>7 500 – 12 500 ₽</b>\n\n"
    "<b>✂️ СТРИЖКА И УКЛАДКА</b>\n"
    "┣ Стрижка — <b>2 000 ₽</b>\n"
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
        # ✅ Записываем в БД — навсегда, не слетает при перезапуске бота
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
#  ЗАПИСЬ — ВЫБОР УСЛУГИ → РЕДИРЕКТ К МАСТЕРУ
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
        await cb.message.edit_text("👥 <b>Пользователей пока нет.</b>", reply_markup=kb_admin_back())
        return

    lines = [f"👥 <b>Всего пользователей: {total} чел.</b>\n"]
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
        "Выберите услугу — изменения сразу применяются для <b>всех</b> пользователей:",
        reply_markup=kb_svc_texts_list()
    )


@admin_cb_router.callback_query(F.data.startswith("adm_edit_svc:"))
async def cb_adm_edit_svc(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    idx         = int(cb.data.split(":")[1])
    svc_name    = SERVICES[idx][0]
    current     = await db_get_service_text(idx)
    is_custom   = (current != DEFAULT_SERVICE_TEXTS[idx])
    status      = "🟡 изменён вами" if is_custom else "🟢 стандартный"

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
        f"✅ <b>Готово! Текст для «{svc_name}» обновлён для всех пользователей.</b>\n\n"
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
