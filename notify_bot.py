"""
Бот уведомляет о новых подписчиках/участниках и об отписках/выходах из
канала или группы. Настройка "кто на какой чат подписан на уведомления"
делается прямо в переписке с ботом, а сами привязки хранятся в Supabase
(Postgres), чтобы не терялись при перезапуске сервиса на Render.

Сценарий использования:
    1. Пользователь пишет боту /start — бот показывает его Telegram ID.
    2. Пользователь пересылает боту любое сообщение ИЗ канала/группы
       (или присылает ссылку вида https://t.me/username,
       если чат публичный).
    3. Бот проверяет, что сам состоит в этом чате как администратор,
       и сохраняет в Supabase запись "этому пользователю — уведомления
       по этому чату".
    4. При вступлении или выходе участника бот смотрит в Supabase, кто
       подписан на уведомления именно по этому чату, и рассылает им
       сообщение.

Админские команды (доступны только ADMIN_ID):
    /broadcast — бот спросит сообщение для рассылки (кнопка "Отмена")
    /referrals — меню реферальных ссылок (создать/удалить/статистика)

Переменные окружения (задаются в настройках Render):
    BOT_TOKEN      — токен бота от @BotFather
    SUPABASE_URL   — Project URL вида https://xxxxx.supabase.co
    SUPABASE_KEY   — service_role key проекта Supabase (секретный!)
    WEBHOOK_HOST   — публичный URL сервиса на Render
    WEBHOOK_SECRET (необязательно) — случайная строка для защиты вебхука
    ADMIN_ID (необязательно) — ваш Telegram ID, только он сможет
        использовать /broadcast и /referrals

Таблицы в Supabase (создать через SQL Editor, без RLS):
    subscriptions
        channel_id  int8
        user_id     int8
    bot_users
        id bigint generated always as identity primary key,
        bot_name text not null,
        user_id bigint not null,
        referral_code text,
        created_at timestamptz not null default now(),
        unique (bot_name, user_id)
    referral_links
        id bigint generated always as identity primary key,
        bot_name text not null,
        code text not null,
        created_at timestamptz not null default now(),
        unique (bot_name, code)
"""

import os
import re
import asyncio
import logging
from html import escape as html_escape

from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import (
    ChatMemberUpdated,
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_NAME = "notify"  # метка этого бота в общей таблице bot_users

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"].strip()
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"].strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET or 'hook'}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip() or "0")

if not WEBHOOK_HOST.startswith("https://"):
    raise RuntimeError(f"WEBHOOK_HOST должен начинаться с https:// , сейчас: {WEBHOOK_HOST!r}")

PORT = int(os.environ.get("PORT", 10000))  # Render сам передаёт PORT

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# HTTP-сессия для запросов к Supabase; создаётся при старте приложения
http_session: ClientSession | None = None


# ---------------------------------------------------------------------
# Работа с Supabase
# ---------------------------------------------------------------------

async def is_already_subscribed(channel_id: int, user_id: int) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    params = {
        "channel_id": f"eq.{channel_id}",
        "user_id": f"eq.{user_id}",
        "select": "id",
    }
    async with http_session.get(url, headers=SUPABASE_HEADERS, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return len(data) > 0


async def add_subscription(channel_id: int, user_id: int) -> None:
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    payload = {"channel_id": channel_id, "user_id": user_id}
    async with http_session.post(url, headers=SUPABASE_HEADERS, json=payload) as resp:
        resp.raise_for_status()


async def get_subscribers(channel_id: int) -> list[int]:
    url = f"{SUPABASE_URL}/rest/v1/subscriptions"
    params = {"channel_id": f"eq.{channel_id}", "select": "user_id"}
    async with http_session.get(url, headers=SUPABASE_HEADERS, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return [row["user_id"] for row in data]


# ---------------------------------------------------------------------
# Общая таблица bot_users и referral_links — список пользователей и
# реферальная статистика (общая на оба бота, различаются полем bot_name)
# ---------------------------------------------------------------------

async def record_bot_user(user_id: int, referral_code: str | None = None) -> None:
    url = f"{SUPABASE_URL}/rest/v1/bot_users"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    params = {"on_conflict": "bot_name,user_id"}
    payload = {"bot_name": BOT_NAME, "user_id": user_id}
    if referral_code:
        payload["referral_code"] = referral_code
    try:
        async with http_session.post(url, headers=headers, params=params, json=payload) as resp:
            if resp.status >= 400:
                logging.warning("record_bot_user: %s", await resp.text())
    except Exception as e:
        logging.warning("record_bot_user exception: %s", e)


async def get_all_bot_users() -> list[int]:
    url = f"{SUPABASE_URL}/rest/v1/bot_users"
    params = {"bot_name": f"eq.{BOT_NAME}", "select": "user_id"}
    async with http_session.get(url, headers=SUPABASE_HEADERS, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return [row["user_id"] for row in data]


async def get_referral_stats() -> dict[str, int]:
    url = f"{SUPABASE_URL}/rest/v1/bot_users"
    params = {
        "bot_name": f"eq.{BOT_NAME}",
        "select": "referral_code",
        "referral_code": "not.is.null",
    }
    async with http_session.get(url, headers=SUPABASE_HEADERS, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    stats: dict[str, int] = {}
    for row in data:
        code = row["referral_code"]
        if code:
            stats[code] = stats.get(code, 0) + 1
    return stats


async def get_all_referral_links() -> list[str]:
    url = f"{SUPABASE_URL}/rest/v1/referral_links"
    params = {"bot_name": f"eq.{BOT_NAME}", "select": "code"}
    async with http_session.get(url, headers=SUPABASE_HEADERS, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return [row["code"] for row in data]


async def create_referral_link(code: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/referral_links"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    params = {"on_conflict": "bot_name,code"}
    payload = {"bot_name": BOT_NAME, "code": code}
    async with http_session.post(url, headers=headers, params=params, json=payload) as resp:
        return resp.status < 400


async def delete_referral_link(code: str) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/referral_links"
    params = {"bot_name": f"eq.{BOT_NAME}", "code": f"eq.{code}"}
    async with http_session.delete(url, headers=SUPABASE_HEADERS, params=params) as resp:
        return resp.status < 400


def extract_ref_code(text: str) -> str:
    text = text.strip()
    if "ref_" in text:
        return text.split("ref_", 1)[1].split("&")[0].strip()
    return text


# user_id администратора -> какого текстового ввода бот сейчас ждёт
# ("broadcast", "ref_create", "ref_delete", "ref_stat_one")
admin_state: dict[int, str] = {}


def build_referral_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать реферальную ссылку", callback_data="ref_create")
    kb.button(text="🗑 Удалить реферальную ссылку", callback_data="ref_delete")
    kb.button(text="📈 Статистика по реферальной ссылке", callback_data="ref_stat_one")
    kb.button(text="📊 Статистика по всем ссылкам", callback_data="ref_stat_all")
    kb.button(text="🚪 Выход", callback_data="ref_exit")
    kb.adjust(1)
    return kb.as_markup()


def build_back_exit_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад в меню", callback_data="ref_menu")
    kb.button(text="🚪 Выход", callback_data="ref_exit")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------------
# Регистрация подписки на канал (общая логика для forward и для ссылки)
# ---------------------------------------------------------------------

async def register_chat_subscription(
    message: Message,
    chat_id: int,
    chat_title: str,
    chat_type: str,
) -> None:
    bot_info = await bot.me()
    safe_title = html_escape(chat_title)
    chat_kind = "канале" if chat_type == "channel" else "группе"

    try:
        bot_member = await bot.get_chat_member(chat_id, bot_info.id)
    except Exception:
        await message.answer(
            f"⚠️ Не удалось проверить бота в {chat_kind} «<b>{safe_title}</b>».\n\n"
            "Похоже, бота там вообще нет. Что нужно сделать:\n"
            "1. Откройте настройки чата → <b>Администраторы</b>\n"
            "2. Нажмите <b>Добавить администратора</b>\n"
            f"3. Найдите бота (@{bot_info.username}) и добавьте его как администратора\n"
            "4. Права можно оставить любые (галочки по умолчанию) — "
            "боту достаточно самого статуса администратора\n\n"
            "После этого пришлите ссылку или пересланное сообщение ещё раз.",
            parse_mode="HTML",
        )
        return

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(
            f"⚠️ Бот состоит в {chat_kind} «<b>{safe_title}</b>», но не как "
            "администратор.\n\n"
            "Что нужно сделать:\n"
            "1. Откройте настройки чата → <b>Администраторы</b>\n"
            "2. Найдите бота в списке участников и повысьте до администратора\n"
            "(конкретные права роли значения не имеют — важен сам статус "
            "администратора, без него Telegram не присылает боту события "
            "о новых подписчиках)\n\n"
            "После этого попробуйте снова.",
            parse_mode="HTML",
        )
        return

    user_id = message.from_user.id

    if await is_already_subscribed(chat_id, user_id):
        await message.answer(
            f"Вы уже подписаны на уведомления в {chat_kind} «<b>{safe_title}</b>».",
            parse_mode="HTML",
        )
        return

    await add_subscription(chat_id, user_id)
    await message.answer(
        f"✅ Готово! Теперь вы будете получать уведомления о новых "
        f"участниках в {chat_kind} «<b>{safe_title}</b>».",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    referral_code = None
    if command.args and command.args.startswith("ref_"):
        referral_code = command.args[len("ref_"):]
    await record_bot_user(message.from_user.id, referral_code)

    await message.answer(
        "👋 <b>Привет!</b> Я уведомляю о новых подписчиках канала.\n\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "<b>Чтобы подключить канал:</b>\n"
        "1. Добавьте меня в канал как администратора.\n"
        "2. Перешлите мне сюда любое сообщение из этого канала (или "
        "пришлите ссылку вида <code>https://t.me/username</code>, если "
        "канал публичный).\n\n"
        "<b>Чтобы подключить группу:</b>\n"
        "1. Добавьте меня в группу как администратора.\n"
        "2. Прямо в этой группе (не в личке!) напишите команду /connect — "
        "она подпишет вас на уведомления именно по этой группе. Пересылка "
        "сообщений для групп не работает (Telegram не передаёт по ним "
        "данные о самой группе), а ссылка нужна только для публичных "
        "групп — поэтому для приватных групп это единственный способ.",
        parse_mode="HTML",
    )


@dp.message(Command("connect"))
async def cmd_connect_group(message: Message):
    chat = message.chat

    if chat.type not in ("group", "supergroup"):
        await message.answer(
            "Эту команду нужно вводить <b>прямо в группе</b>, которую вы "
            "хотите подключить, а не в личке со мной.",
            parse_mode="HTML",
        )
        return

    await register_chat_subscription(message, chat.id, chat.title or "Без названия", chat.type)


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        return

    admin_state[message.from_user.id] = "broadcast"
    kb = InlineKeyboardBuilder()
    kb.button(text="Отмена", callback_data="admin_cancel")
    await message.answer(
        "Введите сообщение, которое хотели бы отправить всем, кто есть в "
        "базе.\nМожно использовать текст, картинки, видео и голосовые.",
        reply_markup=kb.as_markup(),
    )


@dp.message(Command("referrals"))
async def cmd_referrals(message: Message):
    if not is_admin(message.from_user.id):
        return
    admin_state.pop(message.from_user.id, None)
    await message.answer("Меню системы реферальных ссылок:", reply_markup=build_referral_menu())


@dp.callback_query(F.data.startswith("ref_") | F.data == "admin_cancel")
async def on_admin_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer()
        return

    await callback.answer()
    data = callback.data

    if data == "ref_menu":
        admin_state.pop(user_id, None)
        await callback.message.edit_text("Меню системы реферальных ссылок:", reply_markup=build_referral_menu())

    elif data == "ref_exit":
        admin_state.pop(user_id, None)
        await callback.message.edit_text("Выход.")

    elif data == "ref_create":
        admin_state[user_id] = "ref_create"
        await callback.message.edit_text("Введите название ссылки:", reply_markup=build_back_exit_kb())

    elif data == "ref_delete":
        admin_state[user_id] = "ref_delete"
        await callback.message.edit_text("Введите ссылку или её название:", reply_markup=build_back_exit_kb())

    elif data == "ref_stat_one":
        admin_state[user_id] = "ref_stat_one"
        await callback.message.edit_text("Введите ссылку или её название:", reply_markup=build_back_exit_kb())

    elif data == "ref_stat_all":
        admin_state.pop(user_id, None)
        links = await get_all_referral_links()
        stats = await get_referral_stats()
        if not links:
            text = "Реферальных ссылок пока нет."
        else:
            lines = ["Статистика по всем ссылкам:"]
            for code in links:
                lines.append(f"{html_escape(code)}: {stats.get(code, 0)} чел.")
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=build_back_exit_kb())

    elif data == "admin_cancel":
        admin_state.pop(user_id, None)
        await callback.message.edit_text("Отменено.")


@dp.message(lambda message: message.from_user.id in admin_state)
async def on_admin_text_input(message: Message):
    user_id = message.from_user.id
    state = admin_state.get(user_id)

    if state == "broadcast":
        admin_state.pop(user_id, None)
        users = await get_all_bot_users()
        sent = failed = 0
        for uid in users:
            try:
                await message.copy_to(chat_id=uid)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(f"Готово. Отправлено: {sent}, не удалось: {failed}.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужно отправить текстом. Попробуйте снова через /referrals.")
        return

    if state == "ref_create":
        admin_state.pop(user_id, None)
        if not text or " " in text:
            await message.answer("Название не должно содержать пробелов. Откройте /referrals ещё раз.")
            return
        await create_referral_link(text)
        bot_info = await bot.me()
        link = f"https://t.me/{bot_info.username}?start=ref_{text}"
        await message.answer(f"Ссылка создана:\n{link}", reply_markup=build_back_exit_kb())

    elif state == "ref_delete":
        admin_state.pop(user_id, None)
        code = extract_ref_code(text)
        ok = await delete_referral_link(code)
        await message.answer(
            f"Ссылка «{html_escape(code)}» удалена." if ok else "Не удалось удалить (возможно, такой ссылки нет).",
            reply_markup=build_back_exit_kb(),
        )

    elif state == "ref_stat_one":
        admin_state.pop(user_id, None)
        code = extract_ref_code(text)
        stats = await get_referral_stats()
        count = stats.get(code, 0)
        await message.answer(f"«{html_escape(code)}»: {count} чел.", reply_markup=build_back_exit_kb())


@dp.message(F.forward_from_chat)
async def on_forwarded_message(message: Message):
    chat = message.forward_from_chat

    if chat.type not in ("channel", "group", "supergroup"):
        await message.answer("Это сообщение переслано не из канала или группы.")
        return

    await register_chat_subscription(
        message,
        chat.id,
        chat.title or "Без названия",
        chat.type,
    )


@dp.message(F.text.contains("t.me/"))
async def on_channel_link(message: Message):
    match = re.search(r"t\.me/([A-Za-z0-9_]+)", message.text)
    if not match:
        await message.answer(
            "Не разобрал имя канала в этой ссылке. Проверьте, что ссылка "
            "имеет вид <code>https://t.me/имя_канала</code>.",
            parse_mode="HTML",
        )
        return
    username = match.group(1)

    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        await message.answer(
            "❌ Не удалось найти канал по этой ссылке. Проверьте, что "
            "ссылка верна и канал публичный (для приватных каналов "
            "перешлите сообщение из канала вместо ссылки)."
        )
        return

    if chat.type not in ("channel", "group", "supergroup"):
        await message.answer("Эта ссылка ведёт не на канал или группу.")
        return

    await register_chat_subscription(
        message,
        chat.id,
        chat.title or "Без названия",
        chat.type,
    )


@dp.message()
async def on_other_message(message: Message):
    await message.answer(
        "Чтобы подписаться на уведомления, перешлите мне сюда "
        "любое сообщение из канала или группы, либо пришлите ссылку на "
        "публичный канал или группу вида <code>https://t.me/username</code>.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------
# Событие "новый подписчик канала"
# ---------------------------------------------------------------------

def get_member_event(update: ChatMemberUpdated) -> str | None:
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status

    was_outside = old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    now_inside = new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    )

    # Вступление: участник был вне чата и стал участником/администратором.
    if was_outside and now_inside:
        return "join"

    # Выход/отписка: участник был внутри и стал LEFT.
    if new_status == ChatMemberStatus.LEFT and old_status not in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    ):
        return "leave"

    # Исключение/бан — тоже считаем покиданием для уведомления.
    if new_status == ChatMemberStatus.KICKED and old_status not in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    ):
        return "leave"

    return None


@dp.chat_member()
async def on_channel_member_update(update: ChatMemberUpdated):
    event = get_member_event(update)
    if event is None:
        return

    chat_id = update.chat.id
    recipients = await get_subscribers(chat_id)

    if not recipients:
        logging.info(
            "Для чата %s (%s) нет подписчиков на уведомления — пропускаем",
            chat_id,
            update.chat.title,
        )
        return

    user = update.new_chat_member.user
    username = f"@{user.username}" if user.username else "(нет юзернейма)"
    full_name = html_escape(user.full_name or "—")

    chat_type = update.chat.type

    if chat_type == "channel":
        chat_label = "Канал"
        if event == "join":
            title = "🔔 <b>Новый подписчик канала!</b>"
            action = "подписался на канал"
        else:
            title = "🔕 <b>Подписчик отписался от канала!</b>"
            action = "отписался от канала"
    else:
        chat_label = "Группа"
        if event == "join":
            title = "🔔 <b>Новый участник группы!</b>"
            action = "вступил в группу"
        else:
            title = "🔕 <b>Участник покинул группу!</b>"
            action = "покинул группу"

    text = (
        f"{title}\n\n"
        f"Имя: {full_name}\n"
        f"Username: {username}\n"
        f"ID: <code>{user.id}</code>\n"
        f"{chat_label}: {html_escape(update.chat.title or 'Без названия')}\n"
        f"Действие: {action}"
    )

    for recipient_id in recipients:
        try:
            await bot.send_message(chat_id=recipient_id, text=text, parse_mode="HTML")
        except Exception as e:
            logging.warning("Не удалось отправить сообщение %s: %s", recipient_id, e)


# ---------------------------------------------------------------------
# Веб-сервер и вебхук
# ---------------------------------------------------------------------

async def register_bot_commands():
    private_commands = [
        BotCommand(command="start", description="Начало работы, показать мой ID"),
    ]
    group_commands = [
        BotCommand(command="connect", description="Подключить эту группу к уведомлениям"),
        BotCommand(command="start", description="Показать мой Telegram ID"),
    ]
    try:
        await bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group_commands, scope=BotCommandScopeAllGroupChats())
        logging.info("Списки команд для личных чатов и групп зарегистрированы")
    except Exception as e:
        logging.warning("Не удалось зарегистрировать списки команд: %s", e)


async def on_startup(app: web.Application):
    global http_session
    http_session = ClientSession()

    for attempt in range(1, 6):
        try:
            await bot.set_webhook(
                url=WEBHOOK_URL,
                secret_token=WEBHOOK_SECRET or None,
                allowed_updates=["message", "chat_member"],
            )
            logging.info("Webhook установлен: %s", WEBHOOK_URL)
            await register_bot_commands()
            return
        except TelegramRetryAfter as e:
            wait = e.retry_after + 2
            logging.warning("Флуд-контроль Telegram, жду %s сек. (попытка %s/5)", wait, attempt)
            await asyncio.sleep(wait)

    logging.error("Не удалось установить webhook после 5 попыток")


async def on_shutdown(app: web.Application):
    # Не удаляем webhook при остановке Render.
    # Telegram продолжит хранить webhook после остановки сервиса.
    if http_session is not None:
        await http_session.close()


def create_app() -> web.Application:
    app = web.Application()

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    async def health(request):
        return web.Response(text="ok")

    app.router.add_get("/", health)

    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
