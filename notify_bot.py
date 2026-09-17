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

async def register_channel_subscription(message: Message, channel_id: int, channel_title: str) -> None:
    bot_info = await bot.me()

    try:
        bot_member = await bot.get_chat_member(channel_id, bot_info.id)
    except Exception:
        await message.answer(
            f"Не удалось проверить права бота в канале «{channel_title}». "
            "Убедитесь, что бот добавлен в этот канал как администратор."
        )
        return

    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer(
            f"Бот должен быть администратором канала «{channel_title}», "
            "чтобы присылать уведомления о новых подписчиках. "
            "Добавьте бота в администраторы и попробуйте снова."
        )
        return

    user_id = message.from_user.id

    if await is_already_subscribed(channel_id, user_id):
        await message.answer(f"Вы уже подписаны на уведомления канала «{channel_title}».")
        return

    await add_subscription(channel_id, user_id)
    await message.answer(
        f"Готово! Теперь вы будете получать уведомления о новых "
        f"подписчиках канала «{channel_title}»."
    )


# ---------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я уведомляю о новых подписчиках канала.\n\n"
        f"Ваш Telegram ID: {message.from_user.id}\n\n"
        "Чтобы подключить канал:\n"
        "1. Добавьте меня в канал как администратора.\n"
        "2. Перешлите мне сюда любое сообщение из этого канала "
        "(или пришлите ссылку вида https://t.me/username, если канал публичный)."
    )


@dp.message(F.forward_from_chat)
async def on_forwarded_message(message: Message):
    chat = message.forward_from_chat
    if chat.type != "channel":
        await message.answer("Это сообщение переслано не из канала.")
        return
    await register_channel_subscription(message, chat.id, chat.title)


@dp.message(F.text.regexp(r"t\.me/([A-Za-z0-9_]+)"))
async def on_channel_link(message: Message):
    match = re.search(r"t\.me/([A-Za-z0-9_]+)", message.text)
    username = match.group(1)

    try:
        chat = await bot.get_chat(f"@{username}")
    except Exception:
        await message.answer(
            "Не удалось найти канал по этой ссылке. Проверьте, что ссылка "
            "верна и канал публичный (для приватных каналов перешлите "
            "сообщение из канала вместо ссылки)."
        )
        return

    if chat.type != "channel":
        await message.answer("Эта ссылка ведёт не на канал.")
        return

    await register_channel_subscription(message, chat.id, chat.title)


@dp.message()
async def on_other_message(message: Message):
    await message.answer(
        "Чтобы подписаться на уведомления канала, перешлите мне сюда "
        "любое сообщение из этого канала, либо пришлите ссылку на "
        "публичный канал вида https://t.me/username."
    )


# ---------------------------------------------------------------------
# Событие "новый подписчик канала"
# ---------------------------------------------------------------------

def is_new_join(update: ChatMemberUpdated) -> bool:
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status

    was_outside = old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    now_inside = new_status == ChatMemberStatus.MEMBER

    return was_outside and now_inside


@dp.chat_member()
async def on_channel_member_update(update: ChatMemberUpdated):
    if not is_new_join(update):
        return

    channel_id = update.chat.id
    recipients = await get_subscribers(channel_id)

    if not recipients:
        logging.info(
            "Для канала %s (%s) нет подписчиков на уведомления — пропускаем",
            channel_id,
            update.chat.title,
        )
        return

    user = update.new_chat_member.user
    username = f"@{user.username}" if user.username else "(нет юзернейма)"
    full_name = user.full_name or "—"

    text = (
        "🔔 Новый подписчик канала!\n\n"
        f"Имя: {full_name}\n"
        f"Username: {username}\n"
        f"ID: {user.id}\n"
        f"Канал: {update.chat.title}"
    )

    for recipient_id in recipients:
        try:
            await bot.send_message(chat_id=recipient_id, text=text)
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
    await bot.delete_webhook()
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
