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
# --- ИСПРАВЛЕНИЕ 1: ДОБАВЛЯЕМ НОВЫЙ ИМПОРТ ---
from aiogram.client.default import DefaultBotProperties

# --- Настройка ---
# Берем тот же токен, что и для веб-приложения
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TOKEN:
    raise ValueError("Необходимо установить переменную окружения TELEGRAM_BOT_TOKEN")

# Ссылка на твой Web App (должна совпадать с той, что ты давал BotFather)
# Формат: https://t.me/ИМЯ_ТВОЕГО_БОТА/ИМЯ_WEB_APP
WEB_APP_URL = "https://t.me/rplquizbot/rplquizbot"

# Текст приветствия
WELCOME_TEXT = """Привет, фанат футбола! ⚽️

Добро пожаловать в **RPL QuizBot** — главную викторину по Российской Премьер-Лиге!

Твоя задача — вспомнить и назвать как можно больше игроков из текущего состава клуба РПЛ. Думаешь, у тебя получится?

---

**Как играть:**

1.  **Выбери режим:**
    * **🏆 PvP (1 на 1):** Соревнуйся с другими игроками! Ты делаешь ход, потом — соперник.
    * **🏋️ Тренировка:** Играй в соло-режиме, чтобы отточить свои знания.

2.  **Называй игроков:**
    * Угадал фамилию — ход переходит к сопернику (в PvP).
    * Тайм-банк (по умолчанию 90 сек.) тратится, пока ты думаешь.

3.  **Зарабатывай очки (в PvP):**
    * Если соперник не угадал (время вышло или он сдался) — ты получаешь **+1 очко**.
    * Если вы вместе назвали *весь* состав — раунд "вничью", и вы оба получаете по **+0.5 очка**.

4.  **Стань лучшим:**
    * Побеждай в матчах, повышай свой рейтинг Glicko-2 и поднимайся в Таблице Лидеров!

---

Готов начать? 👇
"""

# Инициализируем бота и диспетчер
dp = Dispatcher()

# --- ИСПРАВЛЕНИЕ 2: ИЗМЕНЯЕМ ИНИЦИАЛИЗАЦИЮ БОТА ---
# Старый способ (parse_mode=...) удален в aiogram 3.7.0+
# bot = Bot(TOKEN, parse_mode=ParseMode.MARKDOWN) # <-- Твоя ошибка была тут
#
# Используем новый способ через DefaultBotProperties:
bot = Bot(
    TOKEN, 
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---


# --- Обработчик команды /start ---

@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """
    Этот обработчик ловит команду /start
    и отправляет приветствие с инлайн-кнопкой Web App.
    """
    # Создаем билдер для клавиатуры
    builder = InlineKeyboardBuilder()
    
    # Добавляем кнопку, которая открывает Web App
    builder.button(
        text="🚀 Начать игру!", 
        web_app=WebAppInfo(url=WEB_APP_URL)
    )
    
    # Отправляем сообщение
    await message.answer(
        WELCOME_TEXT,
        reply_markup=builder.as_markup()
    )

@dp.message()
async def any_text_handler(message: Message):
    """
    Ловит любой другой текст и вежливо напоминает, 
    как запустить игру.
    """
    # Создаем билдер для клавиатуры
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
    # Запускаем бота (polling - он будет сам опрашивать Telegram о новых сообщениях)
    print("Запускаю бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())