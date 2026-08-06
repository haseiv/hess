"""
Система тикетов:
  • /ticket_setup — задать категорию, роль поддержки и канал логов
  • /ticket_panel — разместить панель с кнопкой в выбранном канале
  • Кнопка «Создать тикет» -> приватный канал для пользователя и поддержки
  • Внутри тикета: кнопки «Принять» (claim) и «Закрыть»
Кнопки — persistent: работают даже после перезапуска бота.
"""

import io
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from utils import storage


# ---------------- Панель (кнопка создания) ----------------

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="Создать тикет",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="ticket:create",
    )
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        cfg = storage.get_guild(guild.id)
        tcfg = cfg["tickets"]

        # проверка, что нет уже открытого тикета у этого пользователя
        for ch_id, info in tcfg["open"].items():
            if info.get("user") == interaction.user.id and guild.get_channel(int(ch_id)):
                return await interaction.followup.send(
                    f"У вас уже есть открытый тикет: <#{ch_id}>", ephemeral=True
                )

        # категория
        category = guild.get_channel(tcfg["category"]) if tcfg["category"] else None

        # права: видит только автор, поддержка и бот
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
            ),
        }
        support_role = guild.get_role(tcfg["support_role"]) if tcfg["support_role"] else None
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        # увеличиваем счётчик номеров
        tcfg["counter"] += 1
        number = tcfg["counter"]

        try:
            channel = await guild.create_text_channel(
                name=f"тикет-{number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Тикет пользователя {interaction.user} ({interaction.user.id})",
                reason=f"Открыт тикет пользователем {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "Не удалось создать канал. Проверьте права бота (Управление каналами).",
                ephemeral=True,
            )

        tcfg["open"][str(channel.id)] = {"user": interaction.user.id, "claimed_by": None}
        storage.save_guild(guild.id, cfg)

        mention = support_role.mention if support_role else ""
        embed = discord.Embed(
            title=f"Тикет #{number:04d}",
            description=f"{interaction.user.mention}, опишите вашу проблему — "
                        f"поддержка скоро подключится.",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow(),
        )
        await channel.send(content=mention, embed=embed, view=TicketControlView())
        await interaction.followup.send(f"Ваш тикет создан: {channel.mention}", ephemeral=True)


# ---------------- Управление тикетом (принять/закрыть) ----------------

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    def _is_support(self, member, cfg):
        role_id = cfg["tickets"]["support_role"]
        if member.guild_permissions.administrator:
            return True
        return role_id and any(r.id == role_id for r in member.roles)

    @discord.ui.button(
        label="Принять", style=discord.ButtonStyle.success, emoji="✅", custom_id="ticket:claim"
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = storage.get_guild(interaction.guild.id)
        if not self._is_support(interaction.user, cfg):
            return await interaction.response.send_message(
                "Только поддержка может принимать тикеты.", ephemeral=True
            )
        info = cfg["tickets"]["open"].get(str(interaction.channel.id))
        if info and info.get("claimed_by"):
            return await interaction.response.send_message(
                f"Тикет уже принят <@{info['claimed_by']}>.", ephemeral=True
            )
        if info:
            info["claimed_by"] = interaction.user.id
            storage.save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} принял тикет и займётся вашим вопросом."
        )

    @discord.ui.button(
        label="Закрыть", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = storage.get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        if str(interaction.channel.id) not in tcfg["open"]:
            return await interaction.response.send_message(
                "Это не активный тикет.", ephemeral=True
            )
        await interaction.response.send_message("🔒 Закрываю тикет и сохраняю историю...")

        # собираем текстовую историю (transcript)
        transcript_lines = []
        try:
            async for msg in interaction.channel.history(limit=500, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                transcript_lines.append(f"[{ts}] {msg.author}: {msg.content}")
        except discord.HTTPException:
            pass
        transcript = "\n".join(transcript_lines) or "Сообщений нет."

        # логируем
        log_ch = interaction.guild.get_channel(tcfg["log_channel"]) if tcfg["log_channel"] else None
        if log_ch:
            info = tcfg["open"].get(str(interaction.channel.id), {})
            opener = interaction.guild.get_member(info.get("user"))
            embed = discord.Embed(
                title="Тикет закрыт",
                description=f"**Канал:** {interaction.channel.name}\n"
                            f"**Открыл:** {opener.mention if opener else info.get('user')}\n"
                            f"**Закрыл:** {interaction.user.mention}",
                color=discord.Color.greyple(),
                timestamp=datetime.utcnow(),
            )
            file = discord.File(
                io.BytesIO(transcript.encode("utf-8")),
                filename=f"{interaction.channel.name}.txt",
            )
            try:
                await log_ch.send(embed=embed, file=file)
            except discord.HTTPException:
                pass

        # удаляем из хранилища и удаляем канал
        tcfg["open"].pop(str(interaction.channel.id), None)
        storage.save_guild(interaction.guild.id, cfg)
        try:
            await interaction.channel.delete(reason=f"Тикет закрыт {interaction.user}")
        except discord.HTTPException:
            pass


# ---------------- Команды настройки ----------------

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket_setup", description="Настроить систему тикетов")
    @app_commands.describe(
        category="Категория, где будут создаваться тикеты",
        support_role="Роль поддержки, которая видит тикеты",
        log_channel="Канал для логов и истории закрытых тикетов",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        support_role: discord.Role,
        log_channel: discord.TextChannel,
    ):
        cfg = storage.get_guild(interaction.guild.id)
        cfg["tickets"]["category"] = category.id
        cfg["tickets"]["support_role"] = support_role.id
        cfg["tickets"]["log_channel"] = log_channel.id
        storage.save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Тикеты настроены.\n"
            f"• Категория: **{category.name}**\n"
            f"• Роль поддержки: {support_role.mention}\n"
            f"• Логи: {log_channel.mention}\n\n"
            f"Теперь разместите панель командой `/ticket_panel`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="ticket_panel", description="Разместить панель с кнопкой создания тикета"
    )
    @app_commands.describe(
        channel="Канал, в котором появится панель",
        title="Заголовок панели (необязательно)",
        description="Текст панели (необязательно)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = None,
        description: str = None,
    ):
        cfg = storage.get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        if not tcfg["category"] or not tcfg["support_role"]:
            return await interaction.response.send_message(
                "Сначала настройте тикеты командой `/ticket_setup`.", ephemeral=True
            )
        if title:
            tcfg["panel_title"] = title
        if description:
            tcfg["panel_description"] = description
        storage.save_guild(interaction.guild.id, cfg)

        embed = discord.Embed(
            title=tcfg["panel_title"],
            description=tcfg["panel_description"],
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message(
            f"✅ Панель размещена в {channel.mention}.", ephemeral=True
        )

    @ticket_setup.error
    @ticket_panel.error
    async def _perm_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "Нужны права администратора."
        else:
            msg = f"Ошибка: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
