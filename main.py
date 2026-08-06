"""
Точка входа бота.
Загружает токен из .env, подключает модули (cogs), регистрирует
persistent-кнопки тикетов и синхронизирует слэш-команды.
"""

import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from cogs.tickets import TicketPanelView, TicketControlView

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

# Нужные intents. ВАЖНО: включите их в Developer Portal (см. README).
intents = discord.Intents.default()
intents.members = True          # для отслеживания заходов/участников
intents.message_content = True  # для анти-спама и анти-инвайта
intents.guilds = True

EXTENSIONS = ["cogs.protection", "cogs.tickets", "cogs.config_cmds"]


class GuardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # регистрируем persistent-кнопки, чтобы они работали после перезапуска
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())

        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Загружен модуль: %s", ext)

        # синхронизация слэш-команд (глобально; появятся в течение ~1 часа,
        # либо мгновенно на серверах, где бот уже есть — Discord кэширует)
        synced = await self.tree.sync()
        log.info("Синхронизировано слэш-команд: %d", len(synced))

    async def on_ready(self):
        log.info("Бот запущен как %s (id: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="за безопасностью сервера 🛡️"
            )
        )


async def main():
    if not TOKEN:
        raise SystemExit("Не найден DISCORD_TOKEN. Создайте файл .env (см. README).")
    bot = GuardBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
