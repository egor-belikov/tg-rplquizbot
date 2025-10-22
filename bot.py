# bot.py
import asyncio
import os
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import WebAppInfo
# Импортируем DefaultBotProperties
from aiogram.client.default import DefaultBotProperties

# --- Настройка ---
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Необходимо установить переменную окружения TELEGRAM_BOT_TOKEN")

# --- ИСПРАВЛЕНИЕ: УКАЗЫВАЕМ ПРЯМОЙ URL СЕРВЕРА ---
# Неправильно: "https://t.me/rplquizbot/rplquizbot" (это deep link для юзеров)
# Правильно:
WEB_APP_URL = "https://early-anallese-rplquizbot-4a7cc94e.koyeb.app"
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---


WELCOME_TEXT = """Привет, фанат футбола! ⚽️

Добро пожаловать в <b>RPL QuizBot</b> — главную викторину по Российской Премьер-Лиге!

Твоя задача — вспомнить и назвать как можно больше игроков из текущего состава клуба РПЛ. Думаешь, у тебя получится?

<pre>---</pre>

<b>Как играть:</b>

1.  <b>Выбери режим:</b>
    * 🏆 <b>PvP (1 на 1):</b> Соревнуйся с другими игроками! Ты делаешь ход, потом — соперник.
    * 🏋️ <b>Тренировка:</b> Играй в соло-режиме, чтобы отточить свои знания.

2.  <b>Называй игроков:</b>
    * Угадал фамилию — ход переходит к сопернику (в PvP).
    * Тайм-банк (по умолчанию 90 сек.) тратится, пока ты думаешь.

3.  <b>Зарабатывай очки (в PvP):</b>
    * Если соперник не угадал (время вышло или он сдался) — ты получаешь <b>+1 очко</b>.
    * Если вы вместе назвали <i>весь</i> состав — раунд "вничью", и вы оба получаете по <b>+0.5 очка</b>.

4.  <b>Стань лучшим:</b>
    * Побеждай в матчах, повышай свой рейтинг Glicko-2 и поднимайся в Таблице Лидеров!

<pre>---</pre>

Готов начать? 👇
"""

# Инициализируем бота и диспетчер
dp = Dispatcher()

bot = Bot(
    TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# --- Обработчик команды /start ---
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Начать игру!", 
        web_app=WebAppInfo(url=WEB_APP_URL)
    )
    await message.answer(
        WELCOME_TEXT,
        reply_markup=builder.as_markup()
    )

@dp.message()
async def any_text_handler(message: Message):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Запустить игру", 
        web_app=WebAppInfo(url=WEB_APP_URL)
    )
    await message.answer(
        text="Я не общаюсь в чате, моя задача — запускать игру-викторину.\n\nНажми кнопку ниже, чтобы начать 👇",
        reply_markup=builder.as_markup()
    )

# --- Функция запуска бота ---
async def main() -> None:
    print("Запускаю бота...")
    
    # Убираем этот вызов, так как он вызывает TimeoutError на Koyeb
    # await bot.delete_webhook(drop_pending_updates=True) 

    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())