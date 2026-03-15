# -*- coding: utf-8 -*-
"""Standalone lead bot for TransRussia exhibition.

Flow:
1) /start -> greeting + contact request button
2) user shares contact -> save lead in API storage
3) notify manager group
4) show buttons: quick estimate + website
"""

import logging
import os
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
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

START_TEXT = (
    "Привет! 👋\n\n"
    "Это отдельный Telegram-бот для сбора лидов на выставке TransRussia.\n"
    "Нажмите кнопку «Оставить контакт», чтобы менеджер связался с вами."
)

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
    username = f"@{user.from_user.username}" if user.from_user and user.from_user.username else "—"
    text = (
        "📥 <b>Новый лид (TransRussia)</b>\n"
        f"ID: <code>{lead_id}</code>\n"
        f"Имя: {name}\n"
        f"Телефон: {phone}\n"
        f"Telegram: {username}\n"
        f"TG ID: <code>{user.from_user.id if user.from_user else '—'}</code>"
    )
    await bot.send_message(chat_id=MANAGER_GROUP_ID, text=text)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=contact_keyboard())


@router.message(F.contact)
async def handle_contact(message: Message) -> None:
    contact = message.contact
    user = message.from_user

    if not contact or not user:
        await message.answer("Не удалось получить контакт. Попробуйте ещё раз через кнопку.")
        return

    lead_payload = {
        "tg_id": str(user.id),
        "name": contact.first_name or user.full_name or "",
        "phone": contact.phone_number or "",
        "username": user.username,
        "source": "telegram_transrussia_bot",
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
            "Вы можете сразу перейти к следующим шагам:",
            reply_markup=post_contact_keyboard(),
        )
    except Exception as exc:
        log.exception("Contact processing failed: %s", exc)
        await message.answer(
            "Не удалось сохранить контакт. Попробуйте ещё раз чуть позже.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def main() -> None:
    log.info("Lead bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
