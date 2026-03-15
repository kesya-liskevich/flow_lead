# -*- coding: utf-8 -*-
"""Standalone TransRussia lead bot with lightweight manager ticket relay."""

import asyncio
import logging
import os
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("lead-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")
MANAGER_GROUP_ID = int(os.environ.get("MANAGER_GROUP_ID", "0"))

WEBSITE_URL = os.environ.get("WEBSITE_URL", "https://aeza-logistics.ru")
QUICK_CALC_URL = os.environ.get("QUICK_CALC_URL", "https://t.me/aezalogisticbot")
DEFAULT_SOURCE = os.environ.get("LEAD_SOURCE", "transrussia_qr")

START_TEXT = (
    "Привет, вы в Потоке!\n"
    "Оставьте контакт одной кнопкой, чтобы получить спецпредложение для участников выставки TransRussia."
)

THANK_YOU_TEXT = "Спасибо! Контакт сохранён, мы свяжемся с вами.\nЧто хотите делать дальше?"

_pending_campaign_by_user: dict[int, str] = {}
_awaiting_question_from_user: set[int] = set()
_open_ticket_users: set[int] = set()
_manager_msg_to_user: dict[int, int] = {}

bot = Bot(TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Оставить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def post_contact_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Быстрый просчёт", url=QUICK_CALC_URL)],
            [InlineKeyboardButton(text="Перейти на сайт", url=WEBSITE_URL)],
            [InlineKeyboardButton(text="Задать вопрос", callback_data="ask_question")],
        ]
    )


def _sanitize_campaign_tag(tag: str | None) -> str:
    raw = (tag or "").strip()
    if not raw:
        return "transrussia"
    return raw[:64]


async def save_lead(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{API_BASE_URL}/v1/leads"
    headers = {"Content-Type": "application/json"}
    if API_SECRET:
        headers["x-api-key"] = API_SECRET

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, timeout=8) as response:
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"Lead save failed [{response.status}]: {text}")
            return await response.json()


async def notify_managers(lead_id: str, lead: dict[str, Any], user: Message) -> None:
    if not MANAGER_GROUP_ID:
        log.warning("MANAGER_GROUP_ID not set, manager notification skipped")
        return

    name = (lead.get("name") or "").strip() or "Без имени"
    phone = (lead.get("phone") or "").strip() or "—"
    campaign_tag = lead.get("campaign_tag") or "transrussia"
    username = f"@{user.from_user.username}" if user.from_user and user.from_user.username else "—"

    text = (
        "📥 <b>Новый лид (TransRussia)</b>\n"
        f"ID: <code>{lead_id}</code>\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Telegram: {username}\n"
        f"TG ID: <code>{user.from_user.id if user.from_user else '—'}</code>\n"
        f"Источник: <code>{lead.get('source', DEFAULT_SOURCE)}</code>\n"
        f"Кампания: <code>{campaign_tag}</code>"
    )
    await bot.send_message(chat_id=MANAGER_GROUP_ID, text=text)


async def send_question_to_managers(user_message: Message) -> None:
    if not MANAGER_GROUP_ID:
        await user_message.answer("Вопрос принят, но чат менеджеров не настроен. Попробуйте позже.")
        return

    user = user_message.from_user
    if not user:
        return

    username = f"@{user.username}" if user.username else "—"
    text = (
        "❓ <b>Новый вопрос от лида</b>\n"
        f"Клиент: {user.full_name}\n"
        f"TG ID: <code>{user.id}</code>\n"
        f"Username: {username}\n\n"
        f"<b>Вопрос:</b>\n{user_message.text or ''}\n\n"
        "Ответьте реплаем на это сообщение — ответ уйдёт клиенту в бот."
    )
    sent = await bot.send_message(chat_id=MANAGER_GROUP_ID, text=text)
    _manager_msg_to_user[sent.message_id] = user.id

    await user_message.answer("Вопрос отправлен менеджеру. Напишите следующее сообщение, чтобы продолжить диалог.")
    _open_ticket_users.add(user.id)


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject) -> None:
    campaign_tag = _sanitize_campaign_tag(command.args)
    if message.from_user:
        _pending_campaign_by_user[message.from_user.id] = campaign_tag

    await message.answer(START_TEXT, reply_markup=contact_keyboard())


@router.message(F.contact)
async def handle_contact(message: Message) -> None:
    contact = message.contact
    user = message.from_user

    if not contact or not user:
        await message.answer("Не удалось получить контакт. Попробуйте ещё раз через кнопку.")
        return

    campaign_tag = _pending_campaign_by_user.pop(user.id, "transrussia")

    lead_payload = {
        "tg_id": str(user.id),
        "name": contact.first_name or user.full_name or "",
        "phone": contact.phone_number or "",
        "username": user.username,
        "source": DEFAULT_SOURCE,
        "campaign_tag": campaign_tag,
        "status": "new",
        "meta": {
            "chat_id": message.chat.id,
            "shared_contact_user_id": contact.user_id,
        },
    }

    try:
        saved = await save_lead(lead_payload)
        lead_id = saved.get("id", "unknown")
        await notify_managers(lead_id=lead_id, lead=lead_payload, user=message)

        await message.answer(THANK_YOU_TEXT, reply_markup=ReplyKeyboardRemove())
        await message.answer("Выберите действие:", reply_markup=post_contact_keyboard())
    except Exception as exc:
        log.exception("Contact processing failed: %s", exc)
        await message.answer(
            "Не удалось сохранить контакт. Попробуйте ещё раз чуть позже.",
            reply_markup=ReplyKeyboardRemove(),
        )


@router.callback_query(F.data == "ask_question")
async def ask_question_callback(callback) -> None:
    user = callback.from_user
    if user:
        _awaiting_question_from_user.add(user.id)
    await callback.message.answer("Напишите ваш вопрос одним сообщением — передам менеджеру.")
    await callback.answer()


@router.message(F.chat.id == MANAGER_GROUP_ID, F.reply_to_message)
async def manager_reply_to_ticket(message: Message) -> None:
    if not message.reply_to_message:
        return
    client_id = _manager_msg_to_user.get(message.reply_to_message.message_id)
    if not client_id:
        return
    if not message.text:
        return

    await bot.send_message(chat_id=client_id, text=f"Ответ менеджера:\n{message.text}")
    mirror = await bot.send_message(
        chat_id=MANAGER_GROUP_ID,
        text=f"↩️ Клиенту <code>{client_id}</code> отправлен ответ.",
        reply_to_message_id=message.reply_to_message.message_id,
    )
    _manager_msg_to_user[mirror.message_id] = client_id


@router.message(F.text)
async def text_router(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    if message.chat.id == MANAGER_GROUP_ID:
        return

    if user.id in _awaiting_question_from_user:
        _awaiting_question_from_user.discard(user.id)
        await send_question_to_managers(message)
        return

    if user.id in _open_ticket_users:
        await send_question_to_managers(message)
        return


async def main() -> None:
    log.info("Standalone TransRussia lead bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
