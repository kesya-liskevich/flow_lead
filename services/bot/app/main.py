# -*- coding: utf-8 -*-
"""Standalone TransRussia lead bot.

Flow:
1) /start (with optional campaign tag) -> greeting + request_contact button
2) contact shared -> save lead in API + notify manager group
3) show two CTA buttons
"""

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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("lead-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")
MANAGER_GROUP_ID = int(os.environ.get("MANAGER_GROUP_ID", "0"))

WEBSITE_URL = os.environ.get("WEBSITE_URL", "https://aeza-logistics.ru")
QUICK_CALC_URL = os.environ.get("QUICK_CALC_URL", WEBSITE_URL)
DEFAULT_SOURCE = os.environ.get("LEAD_SOURCE", "transrussia_qr")

START_TEXT = (
    "Привет! 👋\n\n"
    "Это отдельный Telegram-бот проекта для выставки TransRussia.\n"
    "Оставьте контакт одной кнопкой — менеджер свяжется с вами и поможет с расчётом."
)

# campaign tag from /start for users who haven't shared contact yet
_pending_campaign_by_user: dict[int, str] = {}

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

        await message.answer(
            "Спасибо! Контакт сохранён, менеджер скоро свяжется с вами.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "Что хотите сделать дальше?",
            reply_markup=post_contact_keyboard(),
        )
    except Exception as exc:
        log.exception("Contact processing failed: %s", exc)
        await message.answer(
            "Не удалось сохранить контакт. Попробуйте ещё раз чуть позже.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def main() -> None:
    log.info("Standalone TransRussia lead bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
