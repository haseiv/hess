"""
🛡️ Discord бот: защита сервера + тикеты — ВСЁ В ОДНОМ ФАЙЛЕ.

Однофайловая версия: нет импортов из cogs/utils, поэтому работает на любом
хостинге, даже если панель не сохраняет вложенные папки.

Запуск:
    pip install discord.py python-dotenv
    python main.py     (нужен файл .env с DISCORD_TOKEN, либо переменная окружения)

Настройки хранятся в DATA_DIR/data.json (по умолчанию рядом с файлом).
Для Docker: ENV DATA_DIR=/app/data + volume на /app/data.
"""

import os
import io
import re
import json
import copy
import time
import asyncio
import logging
from datetime import timedelta
from threading import Lock
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv не обязателен, если токен задан через переменную окружения

TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")


# ==========================================================================
#  ХРАНИЛИЩЕ НАСТРОЕК
# ==========================================================================

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)
_DATA_FILE = os.path.join(_DATA_DIR, "data.json")
_lock = Lock()

_DEFAULT_GUILD = {
    "log_channel": None,
    "protection": {
        "antispam": True,
        "antispam_limit": 5,
        "antispam_interval": 5,
        "antispam_timeout": 300,
        "antiinvite": True,
        "antiraid": True,
        "antiraid_joins": 6,
        "antiraid_interval": 10,
        "antinuke": True,
        "antinuke_limit": 3,
        "antinuke_interval": 12,
        "whitelist": [],
    },
    "tickets": {
        "category": None,
        "support_role": None,
        "log_channel": None,
        "panel_title": "🎫 Поддержка",
        "panel_description": "Нажмите кнопку ниже, чтобы создать тикет. "
                             "Команда поддержки ответит вам как можно скорее.",
        "counter": 0,
        "open": {},
    },
}


def _load():
    if not os.path.exists(_DATA_FILE):
        return {}
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data):
    tmp = _DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DATA_FILE)


def _deep_merge(defaults, actual):
    result = copy.deepcopy(defaults)
    for key, value in actual.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_guild(guild_id):
    with _lock:
        return _deep_merge(_DEFAULT_GUILD, _load().get(str(guild_id), {}))


def save_guild(guild_id, settings):
    with _lock:
        data = _load()
        data[str(guild_id)] = settings
        _save(data)


def update_guild(guild_id, **changes):
    settings = get_guild(guild_id)
    settings.update(changes)
    save_guild(guild_id, settings)
    return settings


# ==========================================================================
#  ТИКЕТЫ: кнопки (persistent views)
# ==========================================================================

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать тикет", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="ticket:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        cfg = get_guild(guild.id)
        tcfg = cfg["tickets"]

        for ch_id, info in tcfg["open"].items():
            if info.get("user") == interaction.user.id and guild.get_channel(int(ch_id)):
                return await interaction.followup.send(
                    f"У вас уже есть открытый тикет: <#{ch_id}>", ephemeral=True)

        category = guild.get_channel(tcfg["category"]) if tcfg["category"] else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True),
        }
        support_role = guild.get_role(tcfg["support_role"]) if tcfg["support_role"] else None
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)

        tcfg["counter"] += 1
        number = tcfg["counter"]
        try:
            channel = await guild.create_text_channel(
                name=f"тикет-{number:04d}", category=category, overwrites=overwrites,
                topic=f"Тикет пользователя {interaction.user} ({interaction.user.id})",
                reason=f"Открыт тикет пользователем {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "Не удалось создать канал. Проверьте права бота (Управление каналами).",
                ephemeral=True)

        tcfg["open"][str(channel.id)] = {"user": interaction.user.id, "claimed_by": None}
        save_guild(guild.id, cfg)

        mention = support_role.mention if support_role else ""
        embed = discord.Embed(
            title=f"Тикет #{number:04d}",
            description=f"{interaction.user.mention}, опишите вашу проблему — "
                        f"поддержка скоро подключится.",
            color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        await channel.send(content=mention, embed=embed, view=TicketControlView())
        await interaction.followup.send(f"Ваш тикет создан: {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _is_support(self, member, cfg):
        role_id = cfg["tickets"]["support_role"]
        if member.guild_permissions.administrator:
            return True
        return role_id and any(r.id == role_id for r in member.roles)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild(interaction.guild.id)
        if not self._is_support(interaction.user, cfg):
            return await interaction.response.send_message(
                "Только поддержка может принимать тикеты.", ephemeral=True)
        info = cfg["tickets"]["open"].get(str(interaction.channel.id))
        if info and info.get("claimed_by"):
            return await interaction.response.send_message(
                f"Тикет уже принят <@{info['claimed_by']}>.", ephemeral=True)
        if info:
            info["claimed_by"] = interaction.user.id
            save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} принял тикет и займётся вашим вопросом.")

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        if str(interaction.channel.id) not in tcfg["open"]:
            return await interaction.response.send_message("Это не активный тикет.", ephemeral=True)
        await interaction.response.send_message("🔒 Закрываю тикет и сохраняю историю...")

        lines = []
        try:
            async for msg in interaction.channel.history(limit=500, oldest_first=True):
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                lines.append(f"[{ts}] {msg.author}: {msg.content}")
        except discord.HTTPException:
            pass
        transcript = "\n".join(lines) or "Сообщений нет."

        log_ch = interaction.guild.get_channel(tcfg["log_channel"]) if tcfg["log_channel"] else None
        if log_ch:
            info = tcfg["open"].get(str(interaction.channel.id), {})
            opener = interaction.guild.get_member(info.get("user"))
            embed = discord.Embed(
                title="Тикет закрыт",
                description=f"**Канал:** {interaction.channel.name}\n"
                            f"**Открыл:** {opener.mention if opener else info.get('user')}\n"
                            f"**Закрыл:** {interaction.user.mention}",
                color=discord.Color.greyple(), timestamp=discord.utils.utcnow())
            file = discord.File(io.BytesIO(transcript.encode("utf-8")),
                                filename=f"{interaction.channel.name}.txt")
            try:
                await log_ch.send(embed=embed, file=file)
            except discord.HTTPException:
                pass

        tcfg["open"].pop(str(interaction.channel.id), None)
        save_guild(interaction.guild.id, cfg)
        try:
            await interaction.channel.delete(reason=f"Тикет закрыт {interaction.user}")
        except discord.HTTPException:
            pass


# ==========================================================================
#  COG: ЗАЩИТА
# ==========================================================================

INVITE_RE = re.compile(r"(discord\.gg/|discord(app)?\.com/invite/)", re.IGNORECASE)


class Protection(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._msg_times = defaultdict(lambda: deque(maxlen=25))
        self._joins = defaultdict(lambda: deque(maxlen=50))
        self._nuke_actions = defaultdict(lambda: deque(maxlen=50))

    async def _log(self, guild, embed):
        ch_id = get_guild(guild.id).get("log_channel")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    def _is_whitelisted(self, member, cfg):
        return (member.id == member.guild.owner_id
                or member.id in cfg["protection"]["whitelist"])

    async def _find_actor(self, guild, action, target_id=None):
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if target_id is None or (entry.target and entry.target.id == target_id):
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _punish_nuker(self, guild, user, reason):
        member = guild.get_member(user.id)
        if member is None:
            return
        try:
            removable = [r for r in member.roles if r < guild.me.top_role and not r.is_default()]
            if removable:
                await member.remove_roles(*removable, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass
        embed = discord.Embed(
            title="🛡️ Анти-нюк сработал",
            description=f"Пользователь {member.mention} (`{member.id}`) выполнял опасные "
                        f"действия слишком часто. Роли сняты.\n**Причина:** {reason}",
            color=discord.Color.red())
        await self._log(guild, embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        cfg = get_guild(message.guild.id)
        member = message.author
        if self._is_whitelisted(member, cfg) or member.guild_permissions.administrator:
            return
        prot = cfg["protection"]

        if prot["antiinvite"] and INVITE_RE.search(message.content):
            try:
                await message.delete()
                await message.channel.send(
                    f"{member.mention}, приглашения на другие серверы запрещены.",
                    delete_after=5)
            except discord.HTTPException:
                pass
            await self._log(message.guild, discord.Embed(
                title="🔗 Удалено приглашение",
                description=f"**Автор:** {member.mention}\n**Канал:** {message.channel.mention}",
                color=discord.Color.orange()))
            return

        if prot["antispam"]:
            key = (message.guild.id, member.id)
            now = time.time()
            times = self._msg_times[key]
            times.append(now)
            window = [t for t in times if now - t <= prot["antispam_interval"]]
            if len(window) >= prot["antispam_limit"]:
                times.clear()
                try:
                    # discord.py 2.x: timeout принимает timedelta напрямую
                    await member.timeout(timedelta(seconds=prot["antispam_timeout"]),
                                         reason="Анти-спам: флуд сообщениями")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                try:
                    await message.channel.purge(
                        limit=prot["antispam_limit"] + 2,
                        check=lambda m: m.author.id == member.id)
                except discord.HTTPException:
                    pass
                await self._log(message.guild, discord.Embed(
                    title="🚫 Анти-спам",
                    description=f"{member.mention} замучен за флуд "
                                f"({prot['antispam_limit']} сообщ. за "
                                f"{prot['antispam_interval']} с).",
                    color=discord.Color.red()))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        prot = get_guild(member.guild.id)["protection"]
        if not prot["antiraid"]:
            return
        now = time.time()
        joins = self._joins[member.guild.id]
        joins.append(now)
        recent = [t for t in joins if now - t <= prot["antiraid_interval"]]
        if len(recent) >= prot["antiraid_joins"]:
            try:
                await member.guild.edit(
                    verification_level=discord.VerificationLevel.highest,
                    reason="Анти-рейд: массовый заход")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._log(member.guild, discord.Embed(
                title="⚠️ Возможный рейд!",
                description=f"За {prot['antiraid_interval']} с зашло {len(recent)} участников.\n"
                            f"Уровень проверки поднят до максимума.",
                color=discord.Color.dark_red()))

    async def _register_nuke_action(self, guild, actor, kind):
        prot = get_guild(guild.id)["protection"]
        if not prot["antinuke"] or actor is None or actor.bot:
            return
        if (actor.id == guild.owner_id or actor.id in prot["whitelist"]
                or actor.id == self.bot.user.id):
            return
        key = (guild.id, actor.id)
        now = time.time()
        acts = self._nuke_actions[key]
        acts.append(now)
        recent = [t for t in acts if now - t <= prot["antinuke_interval"]]
        if len(recent) >= prot["antinuke_limit"]:
            acts.clear()
            await self._punish_nuker(guild, actor, f"Массовое действие: {kind}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        actor = await self._find_actor(channel.guild, discord.AuditLogAction.channel_delete)
        await self._register_nuke_action(channel.guild, actor, "удаление каналов")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        actor = await self._find_actor(role.guild, discord.AuditLogAction.role_delete)
        await self._register_nuke_action(role.guild, actor, "удаление ролей")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        actor = await self._find_actor(guild, discord.AuditLogAction.ban, target_id=user.id)
        await self._register_nuke_action(guild, actor, "массовые баны")


# ==========================================================================
#  COG: КОМАНДЫ ТИКЕТОВ
# ==========================================================================

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket_setup", description="Настроить систему тикетов")
    @app_commands.describe(category="Категория для тикетов", support_role="Роль поддержки",
                           log_channel="Канал логов тикетов")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction, category: discord.CategoryChannel,
                           support_role: discord.Role, log_channel: discord.TextChannel):
        cfg = get_guild(interaction.guild.id)
        cfg["tickets"].update(category=category.id, support_role=support_role.id,
                              log_channel=log_channel.id)
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Тикеты настроены.\n• Категория: **{category.name}**\n"
            f"• Роль: {support_role.mention}\n• Логи: {log_channel.mention}\n\n"
            f"Теперь разместите панель командой `/ticket_panel`.", ephemeral=True)

    @app_commands.command(name="ticket_panel", description="Разместить панель с кнопкой")
    @app_commands.describe(channel="Канал для панели", title="Заголовок (необязательно)",
                           description="Текст (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction, channel: discord.TextChannel,
                           title: str = None, description: str = None):
        cfg = get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        if not tcfg["category"] or not tcfg["support_role"]:
            return await interaction.response.send_message(
                "Сначала настройте тикеты командой `/ticket_setup`.", ephemeral=True)
        if title:
            tcfg["panel_title"] = title
        if description:
            tcfg["panel_description"] = description
        save_guild(interaction.guild.id, cfg)
        embed = discord.Embed(title=tcfg["panel_title"], description=tcfg["panel_description"],
                              color=discord.Color.blurple())
        await channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message(f"✅ Панель размещена в {channel.mention}.",
                                                ephemeral=True)


# ==========================================================================
#  COG: КОМАНДЫ НАСТРОЙКИ ЗАЩИТЫ
# ==========================================================================

TOGGLES = {"антиспам": "antispam", "антиинвайт": "antiinvite",
           "антирейд": "antiraid", "антинюк": "antinuke"}


class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="set_log", description="Задать канал логов защиты")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log(self, interaction, channel: discord.TextChannel):
        update_guild(interaction.guild.id, log_channel=channel.id)
        await interaction.response.send_message(f"✅ Логи защиты: {channel.mention}.",
                                                ephemeral=True)

    @app_commands.command(name="protection", description="Включить/выключить защиту")
    @app_commands.describe(тип="Какую защиту", включить="True — вкл, False — выкл")
    @app_commands.choices(тип=[app_commands.Choice(name=k, value=v) for k, v in TOGGLES.items()])
    @app_commands.checks.has_permissions(administrator=True)
    async def protection(self, interaction, тип: app_commands.Choice[str], включить: bool):
        cfg = get_guild(interaction.guild.id)
        cfg["protection"][тип.value] = включить
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Защита **{тип.name}** {'включена' if включить else 'выключена'}.",
            ephemeral=True)

    @app_commands.command(name="whitelist_add", description="Добавить доверенного пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelist_add(self, interaction, пользователь: discord.Member):
        cfg = get_guild(interaction.guild.id)
        wl = cfg["protection"]["whitelist"]
        if пользователь.id not in wl:
            wl.append(пользователь.id)
            save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {пользователь.mention} в белом списке.", ephemeral=True)

    @app_commands.command(name="whitelist_remove", description="Убрать из белого списка")
    @app_commands.checks.has_permissions(administrator=True)
    async def whitelist_remove(self, interaction, пользователь: discord.Member):
        cfg = get_guild(interaction.guild.id)
        wl = cfg["protection"]["whitelist"]
        if пользователь.id in wl:
            wl.remove(пользователь.id)
            save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {пользователь.mention} убран из белого списка.", ephemeral=True)

    @app_commands.command(name="settings", description="Показать настройки сервера")
    @app_commands.checks.has_permissions(administrator=True)
    async def settings(self, interaction):
        cfg = get_guild(interaction.guild.id)
        p, t = cfg["protection"], cfg["tickets"]
        yn = lambda v: "✅" if v else "❌"
        log_ch = f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "не задан"
        wl = ", ".join(f"<@{u}>" for u in p["whitelist"]) or "пусто"
        embed = discord.Embed(title="⚙️ Настройки сервера", color=discord.Color.blurple())
        embed.add_field(name="🛡️ Защита", value=(
            f"{yn(p['antispam'])} Анти-спам ({p['antispam_limit']}/{p['antispam_interval']}с)\n"
            f"{yn(p['antiinvite'])} Анти-инвайт\n"
            f"{yn(p['antiraid'])} Анти-рейд ({p['antiraid_joins']}/{p['antiraid_interval']}с)\n"
            f"{yn(p['antinuke'])} Анти-нюк ({p['antinuke_limit']}/{p['antinuke_interval']}с)\n"
            f"Логи: {log_ch}\nБелый список: {wl}"), inline=False)
        cat = f"<#{t['category']}>" if t["category"] else "не задана"
        role = f"<@&{t['support_role']}>" if t["support_role"] else "не задана"
        tlog = f"<#{t['log_channel']}>" if t["log_channel"] else "не задан"
        embed.add_field(name="🎫 Тикеты", value=(
            f"Категория: {cat}\nРоль поддержки: {role}\nЛоги: {tlog}\n"
            f"Открыто сейчас: {len(t['open'])}"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================================================
#  ЗАПУСК
# ==========================================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True


class GuardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        await self.add_cog(Protection(self))
        await self.add_cog(Tickets(self))
        await self.add_cog(Config(self))
        synced = await self.tree.sync()
        log.info("Синхронизировано слэш-команд: %d", len(synced))

    async def on_ready(self):
        log.info("Бот запущен как %s (id: %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, name="за безопасностью 🛡️"))


# Единый обработчик ошибок прав для всех слэш-команд
async def _on_app_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Нужны права администратора."
    else:
        msg = f"Ошибка: {error}"
        log.exception("Ошибка команды", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def main():
    if not TOKEN:
        raise SystemExit("Не найден DISCORD_TOKEN. Задайте .env или переменную окружения.")
    bot = GuardBot()
    bot.tree.on_error = _on_app_error
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
