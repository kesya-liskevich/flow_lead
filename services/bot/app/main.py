# -*- coding: utf-8 -*-
"""Standalone TransRussia lead bot with manager ticket threads."""

import asyncio
import logging
import os
from typing import Any, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest


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
_manager_msg_to_user: dict[int, int] = {}  # non-forum fallback mapping
_user_ticket_thread: dict[int, int] = {}
_thread_ticket_user: dict[int, int] = {}
_user_label: dict[int, str] = {}

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


def manager_lead_card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✍️ Написать клиенту", callback_data=f"open_ticket:{user_id}")]]
    )


def _sanitize_campaign_tag(tag: str | None) -> str:
    raw = (tag or "").strip()
    return raw[:64] if raw else "transrussia"


def _user_mention(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Клиент"
    return f"{user.full_name} ({user.id})"


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


async def ensure_ticket_thread(user_id: int, user_name: str) -> Optional[int]:
    if user_id in _user_ticket_thread:
        return _user_ticket_thread[user_id]
    if not MANAGER_GROUP_ID:
        return None

    topic_name = f"Ticket — {user_name}"[:120]
    try:
        topic = await bot.create_forum_topic(chat_id=MANAGER_GROUP_ID, name=topic_name)
        thread_id = topic.message_thread_id
        _user_ticket_thread[user_id] = thread_id
        _thread_ticket_user[thread_id] = user_id
        return thread_id
    except TelegramBadRequest as exc:
        # Chat is not a forum supergroup or no rights
        log.warning("Forum topic create failed, fallback to reply mode: %s", exc)
        return None
    except Exception as exc:
        log.warning("Forum topic create failed: %s", exc)
        return None


async def notify_managers(lead_id: str, lead: dict[str, Any], user_message: Message) -> None:
    if not MANAGER_GROUP_ID:
        log.warning("MANAGER_GROUP_ID not set, manager notification skipped")
        return

    user = user_message.from_user
    name = (lead.get("name") or "").strip() or "Без имени"
    phone = (lead.get("phone") or "").strip() or "—"
    campaign_tag = lead.get("campaign_tag") or "transrussia"
    username = f"@{user.username}" if user and user.username else "—"
    user_id = user.id if user else 0

    text = (
        "📥 <b>Новый лид (TransRussia)</b>\n"
        f"ID: <code>{lead_id}</code>\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Telegram: {username}\n"
        f"TG ID: <code>{user_id}</code>\n"
        f"Источник: <code>{lead.get('source', DEFAULT_SOURCE)}</code>\n"
        f"Кампания: <code>{campaign_tag}</code>"
    )
    sent = await bot.send_message(
        chat_id=MANAGER_GROUP_ID,
        text=text,
        reply_markup=manager_lead_card_keyboard(user_id=user_id),
    )
    _manager_msg_to_user[sent.message_id] = user_id


async def forward_client_text_to_managers(user_message: Message, is_new_question: bool) -> None:
    if not MANAGER_GROUP_ID:
        await user_message.answer("Вопрос принят, но чат менеджеров не настроен. Попробуйте позже.")
        return

    user = user_message.from_user
    if not user or not user_message.text:
        return

    _user_label[user.id] = user.full_name
    thread_id = await ensure_ticket_thread(user.id, user.full_name)

    header = "❓ Новый вопрос" if is_new_question else "💬 Сообщение клиента"
    manager_text = (
        f"{header}\n"
        f"Клиент: {_user_mention(user_message)}\n\n"
        f"{user_message.text}"
    )

    if thread_id is not None:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            message_thread_id=thread_id,
            text=manager_text,
        )
    else:
        sent = await bot.send_message(chat_id=MANAGER_GROUP_ID, text=manager_text)
        _manager_msg_to_user[sent.message_id] = user.id

    if is_new_question:
        await user_message.answer("Вопрос отправлен менеджеру. Напишите следующее сообщение, чтобы продолжить диалог.")


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject) -> None:
    if message.from_user:
        _pending_campaign_by_user[message.from_user.id] = _sanitize_campaign_tag(command.args)
    await message.answer(START_TEXT, reply_markup=contact_keyboard())


@router.message(F.contact)
async def handle_contact(message: Message) -> None:
    contact = message.contact
    user = message.from_user
    if not contact or not user:
        await message.answer("Не удалось получить контакт. Попробуйте ещё раз через кнопку.")
        return

    _user_label[user.id] = user.full_name
    campaign_tag = _pending_campaign_by_user.pop(user.id, "transrussia")
    lead_payload = {
        "tg_id": str(user.id),
        "name": contact.first_name or user.full_name or "",
        "phone": contact.phone_number or "",
        "username": user.username,
        "source": DEFAULT_SOURCE,
        "campaign_tag": campaign_tag,
        "status": "new",
        "meta": {"chat_id": message.chat.id, "shared_contact_user_id": contact.user_id},
    }

    try:
        saved = await save_lead(lead_payload)
        await notify_managers(lead_id=saved.get("id", "unknown"), lead=lead_payload, user_message=message)
        await message.answer(THANK_YOU_TEXT, reply_markup=ReplyKeyboardRemove())
        await message.answer("Выберите действие:", reply_markup=post_contact_keyboard())
    except Exception as exc:
        log.exception("Contact processing failed: %s", exc)
        await message.answer("Не удалось сохранить контакт. Попробуйте ещё раз чуть позже.", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: CallbackQuery) -> None:
    if callback.from_user:
        _awaiting_question_from_user.add(callback.from_user.id)
    if callback.message:
        await callback.message.answer("Напишите ваш вопрос")
    await callback.answer()


@router.callback_query(F.data.startswith("open_ticket:"))
async def open_ticket_callback(callback: CallbackQuery) -> None:
    user_id_str = (callback.data or "").split(":", 1)[1]
    if not user_id_str.isdigit():
        await callback.answer("Некорректный клиент", show_alert=True)
        return
    client_id = int(user_id_str)

    client_name = _user_label.get(client_id, str(client_id))
    thread_id = await ensure_ticket_thread(client_id, client_name)

    if thread_id is not None:
        await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            message_thread_id=thread_id,
            text=(
                f"🎫 Тикет клиента открыт\nКлиент: <code>{client_id}</code>\n"
                "Пишите сообщения в этом треде — они уйдут клиенту."
            ),
        )
        await callback.answer("Тикет открыт")
    else:
        sent = await bot.send_message(
            chat_id=MANAGER_GROUP_ID,
            text=(
                f"🎫 Тикет клиента <code>{client_id}</code>\n"
                "Форум-темы недоступны. Ответьте реплаем на это сообщение, и ответ уйдёт клиенту."
            ),
        )
        _manager_msg_to_user[sent.message_id] = client_id
        await callback.answer("Открыт режим reply")


@router.message(F.chat.id == MANAGER_GROUP_ID, F.text)
async def manager_message_router(message: Message) -> None:
    if message.from_user and message.from_user.is_bot:
        return

    # forum topic mode
    if message.message_thread_id and message.message_thread_id in _thread_ticket_user:
        client_id = _thread_ticket_user[message.message_thread_id]
        await bot.send_message(chat_id=client_id, text=message.text)
        return

    # reply mode fallback
    if message.reply_to_message:
        client_id = _manager_msg_to_user.get(message.reply_to_message.message_id)
        if client_id:
            await bot.send_message(chat_id=client_id, text=message.text)
            mirror = await bot.send_message(
                chat_id=MANAGER_GROUP_ID,
                text=f"↩️ Клиенту <code>{client_id}</code> отправлен ответ.",
                reply_to_message_id=message.reply_to_message.message_id,
            )
            _manager_msg_to_user[mirror.message_id] = client_id


@router.message(F.text)
async def user_text_router(message: Message) -> None:
    if message.chat.id == MANAGER_GROUP_ID:
        return
    user = message.from_user
    if not user:
        return

    is_new_question = user.id in _awaiting_question_from_user
    if is_new_question:
        _awaiting_question_from_user.discard(user.id)

    if is_new_question or user.id in _user_ticket_thread:
        try:
            await forward_client_text_to_managers(user_message=message, is_new_question=is_new_question)
        except Exception as exc:
            log.exception("Failed to forward client message: %s", exc)
            await message.answer("Не удалось отправить сообщение менеджеру, попробуйте ещё раз.")


async def main() -> None:
    log.info("Standalone TransRussia lead bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
