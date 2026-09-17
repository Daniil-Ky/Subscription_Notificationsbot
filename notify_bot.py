"""
Бот уведомляет о новых подписчиках канала. Настройка "кто на какой канал
подписан на уведомления" делается прямо в переписке с ботом, а сами
привязки хранятся в Supabase (Postgres), чтобы не терялись при
перезапуске сервиса на Render.

Сценарий использования:
    1. Пользователь пишет боту /start — бот показывает его Telegram ID.
    2. Пользователь пересылает боту любое сообщение ИЗ канала
       (или присылает ссылку вида https://t.me/channel_username,
       если канал публичный).
    3. Бот проверяет, что сам состоит в этом канале как администратор,
       и сохраняет в Supabase запись "этому пользователю — уведомления
       по этому каналу".
    4. Когда в канал вступает новый подписчик, бот смотрит в Supabase,
       кто подписан на уведомления именно по этому каналу, и рассылает
       им сообщение.

Переменные окружения (задаются в настройках Render):
    BOT_TOKEN      — токен бота от @BotFather
    SUPABASE_URL   — Project URL вида https://xxxxx.supabase.co
    SUPABASE_KEY   — service_role key проекта Supabase (секретный!)
    WEBHOOK_HOST   — публичный URL сервиса на Render
    WEBHOOK_SECRET (необязательно) — случайная строка для защиты вебхука

Таблица в Supabase (создать через Table Editor, без RLS):
    subscriptions
        channel_id  int8
        user_id     int8
"""

import os
import re
import asyncio
import logging
from html import escape as html_escape

from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import ChatMemberUpdated, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"].strip()
WEBHOOK_HOST = os.environ["WEBHOOK_HOST"].strip().rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET or 'hook'}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

if not WEBHOOK_HOST.startswith("https://"):
    raise RuntimeError(f"WEBHOOK_HOST должен начинаться с https:// , сейчас: {WEBHOOK_HOST!r}")

PORT = int(os.environ.get("PORT", 10000))  # Render сам передаёт PORT

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

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
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b> Я уведомляю о новых подписчиках канала.\n\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "<b>Чтобы подключить канал или группу:</b>\n"
        "1. Добавьте меня в канал или группу как администратора.\n"
        "2. Перешлите мне сюда любое сообщение из этого канала или группы "
        "(или пришлите ссылку вида <code>https://t.me/username</code>, "
        "если канал публичный).",
        parse_mode="HTML",
    )


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
