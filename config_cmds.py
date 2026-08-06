"""
Команды настройки защиты:
  • /set_log        — задать канал логов защиты
  • /protection     — включить/выключить конкретную защиту
  • /whitelist_add  — добавить пользователя в белый список (доверенный)
  • /whitelist_remove
  • /settings       — показать текущие настройки
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import storage

TOGGLES = {
    "антиспам": "antispam",
    "антиинвайт": "antiinvite",
    "антирейд": "antiraid",
    "антинюк": "antinuke",
}


class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="set_log", description="Задать канал логов защиты")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log(self, interaction: discord.Interaction, channel: discord.TextChannel):
        storage.update_guild(interaction.guild.id, log_channel=channel.id)
        await interaction.response.send_message(
            f"✅ Логи защиты будут в {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="protection", description="Включить/выключить защиту")
    @app_commands.describe(тип="Какую защиту переключить", включить="True — включить, False — выключить")
    @app_commands.choices(
        тип=[app_commands.Choice(name=k, value=v) for k, v in TOGGLES.items()]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def protection(
        self, interaction: discord.Interaction, тип: app_commands.Choice[str], включить: bool
    ):
        cfg = storage.get_guild(interaction.guild.id)
        cfg["protection"][тип.value] = включить
        storage.save_guild(interaction.guild.id, cfg)
        state = "включена" if включить else "выключена"
        await interaction.response.send_message(
            f"✅ Защита **{тип.name}** {state}.", ephemeral=True
        )

    @app_commands.command(name="whitelist_add", description="Добавить доверенного пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelist_add(self, interaction: discord.Interaction, пользователь: discord.Member):
        cfg = storage.get_guild(interaction.guild.id)
        wl = cfg["protection"]["whitelist"]
        if пользователь.id not in wl:
            wl.append(пользователь.id)
            storage.save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {пользователь.mention} добавлен в белый список.", ephemeral=True
        )

    @app_commands.command(name="whitelist_remove", description="Убрать пользователя из белого списка")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelist_remove(self, interaction: discord.Interaction, пользователь: discord.Member):
        cfg = storage.get_guild(interaction.guild.id)
        wl = cfg["protection"]["whitelist"]
        if пользователь.id in wl:
            wl.remove(пользователь.id)
            storage.save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {пользователь.mention} убран из белого списка.", ephemeral=True
        )

    @app_commands.command(name="settings", description="Показать текущие настройки сервера")
    @app_commands.checks.has_permissions(administrator=True)
    async def settings(self, interaction: discord.Interaction):
        cfg = storage.get_guild(interaction.guild.id)
        p = cfg["protection"]
        t = cfg["tickets"]

        def yn(v):
            return "✅" if v else "❌"

        log_ch = f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "не задан"
        wl = ", ".join(f"<@{u}>" for u in p["whitelist"]) or "пусто"

        embed = discord.Embed(title="⚙️ Настройки сервера", color=discord.Color.blurple())
        embed.add_field(
            name="🛡️ Защита",
            value=(
                f"{yn(p['antispam'])} Анти-спам ({p['antispam_limit']}/{p['antispam_interval']}с)\n"
                f"{yn(p['antiinvite'])} Анти-инвайт\n"
                f"{yn(p['antiraid'])} Анти-рейд ({p['antiraid_joins']}/{p['antiraid_interval']}с)\n"
                f"{yn(p['antinuke'])} Анти-нюк ({p['antinuke_limit']}/{p['antinuke_interval']}с)\n"
                f"Логи: {log_ch}\nБелый список: {wl}"
            ),
            inline=False,
        )
        cat = f"<#{t['category']}>" if t["category"] else "не задана"
        role = f"<@&{t['support_role']}>" if t["support_role"] else "не задана"
        tlog = f"<#{t['log_channel']}>" if t["log_channel"] else "не задан"
        embed.add_field(
            name="🎫 Тикеты",
            value=f"Категория: {cat}\nРоль поддержки: {role}\nЛоги: {tlog}\n"
                  f"Открыто сейчас: {len(t['open'])}",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Нужны права администратора."
        else:
            msg = f"Ошибка: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Config(bot))
