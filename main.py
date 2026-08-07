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
        "strict_mode": False,       # усиленная защита (преды + автобан)
        "strict_threshold": 3,      # сколько предупреждений до бана
        "whitelist": [],
    },
    "warnings": {},                 # {user_id: количество предупреждений}
    "menus": {},                    # {message_id: {"channel": id, "options": [...]}}
    "tickets": {
        "category": None,
        "support_role": None,
        "log_channel": None,
        "panel_title": "🎫 Поддержка",
        "panel_description": "Нажмите кнопку ниже, чтобы создать тикет. "
                             "Команда поддержки ответит вам как можно скорее.",
        "counter": 0,
        "open": {},
        "types": [],   # [{"label","category","roles":[...],"emoji","description"}]
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


def load_all():
    """Все серверы как есть (без подстановки значений по умолчанию)."""
    with _lock:
        return _load()


def parse_color(value):
    """Разобрать цвет из строки вида '#5865F2' или '5865F2'."""
    if not value:
        return discord.Color.blurple()
    try:
        return discord.Color(int(value.strip().lstrip("#"), 16))
    except (ValueError, AttributeError):
        return discord.Color.blurple()


# ==========================================================================
#  ТИКЕТЫ: кнопки (persistent views)
# ==========================================================================

def _default_type(cfg):
    """Тип по умолчанию: первый из настроенных, иначе из старых одиночных
    настроек (category + support_role) для обратной совместимости."""
    tcfg = cfg["tickets"]
    types = tcfg.get("types", [])
    if types:
        return types[0]
    if tcfg.get("category") and tcfg.get("support_role"):
        return {"label": "Поддержка", "category": tcfg["category"],
                "roles": [tcfg["support_role"]], "emoji": None, "description": None}
    return None


def _slug(text):
    s = re.sub(r"[^\w-]+", "-", text.lower(), flags=re.UNICODE).strip("-")
    return s or "тикет"


class TicketModal(discord.ui.Modal, title="Создание тикета"):
    """Форма создания тикета. Тип тикета передаётся в конструктор."""

    тема = discord.ui.TextInput(
        label="Тема обращения", placeholder="Кратко: в чём вопрос?",
        max_length=100, required=True)
    описание = discord.ui.TextInput(
        label="Подробное описание", style=discord.TextStyle.paragraph,
        placeholder="Что случилось? Когда? Что уже пробовали?",
        max_length=1000, required=True)
    приоритет = discord.ui.TextInput(
        label="Приоритет (необязательно)", placeholder="низкий / средний / высокий",
        max_length=20, required=False)

    def __init__(self, ticket_type):
        super().__init__()
        self.ticket_type = ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        cfg = get_guild(guild.id)
        tcfg = cfg["tickets"]
        t = self.ticket_type

        category = guild.get_channel(t.get("category")) if t.get("category") else None
        roles = [guild.get_role(r) for r in t.get("roles", [])]
        roles = [r for r in roles if r]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True,
                read_message_history=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True),
        }
        for r in roles:
            overwrites[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True)

        tcfg["counter"] += 1
        number = tcfg["counter"]
        prefix = _slug(t.get("label", "тикет"))[:20]
        try:
            channel = await guild.create_text_channel(
                name=f"{prefix}-{number:04d}", category=category, overwrites=overwrites,
                topic=f"Тикет пользователя {interaction.user} ({interaction.user.id})",
                reason=f"Открыт тикет пользователем {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                "Не удалось создать канал. Проверьте права бота (Управление каналами).",
                ephemeral=True)

        tcfg["open"][str(channel.id)] = {
            "user": interaction.user.id, "claimed_by": None,
            "roles": [r.id for r in roles], "type": t.get("label")}
        save_guild(guild.id, cfg)

        mention = " ".join(r.mention for r in roles)
        embed = discord.Embed(
            title=f"Тикет #{number:04d} — {self.тема.value}",
            description=self.описание.value,
            color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Автор", value=interaction.user.mention, inline=True)
        embed.add_field(name="Тип", value=t.get("label", "—"), inline=True)
        if self.приоритет.value:
            embed.add_field(name="Приоритет", value=self.приоритет.value, inline=True)
        embed.set_footer(text="Поддержка скоро подключится")
        await channel.send(content=mention or None, embed=embed, view=TicketControlView())
        await interaction.followup.send(f"Ваш тикет создан: {channel.mention}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Ошибка при создании тикета", exc_info=error)
        msg = "Не удалось создать тикет. Сообщите администрации."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def _has_open_ticket(guild, cfg, user_id):
    for ch_id, info in cfg["tickets"]["open"].items():
        if info.get("user") == user_id and guild.get_channel(int(ch_id)):
            return ch_id
    return None


class TicketSelect(discord.ui.Select):
    """Селект под эмбедом: выбор типа тикета. value каждой опции = label типа,
    поэтому меню не хранит состояние и переживает перезапуск бота."""

    def __init__(self, types):
        options = [
            discord.SelectOption(
                label=t["label"][:100], value=t["label"][:100],
                description=(t.get("description") or None),
                emoji=(t.get("emoji") or None))
            for t in (types or [])
        ] or [discord.SelectOption(label="Нет доступных типов", value="—")]
        super().__init__(custom_id="ticket:select",
                         placeholder="Выберите тип обращения",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = get_guild(interaction.guild.id)
        types = cfg["tickets"].get("types", [])
        chosen = self.values[0]
        t = next((x for x in types if x["label"] == chosen), None)
        if t is None:
            return await interaction.response.send_message(
                "Этот тип тикета больше недоступен. Обратитесь к администрации.",
                ephemeral=True)
        existing = _has_open_ticket(interaction.guild, cfg, interaction.user.id)
        if existing:
            return await interaction.response.send_message(
                f"У вас уже есть открытый тикет: <#{existing}>", ephemeral=True)
        await interaction.response.send_modal(TicketModal(t))


class TicketSelectView(discord.ui.View):
    def __init__(self, types=None):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(types))


class TicketPanelView(discord.ui.View):
    """Старая панель с кнопкой — оставлена для совместимости с уже
    размещёнными панелями. Использует тип по умолчанию."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать тикет", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="ticket:create")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild(interaction.guild.id)
        existing = _has_open_ticket(interaction.guild, cfg, interaction.user.id)
        if existing:
            return await interaction.response.send_message(
                f"У вас уже есть открытый тикет: <#{existing}>", ephemeral=True)
        t = _default_type(cfg)
        if t is None:
            return await interaction.response.send_message(
                "Тикеты ещё не настроены. Обратитесь к администрации.", ephemeral=True)
        await interaction.response.send_modal(TicketModal(t))


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _is_support(self, member, cfg, channel_id=None):
        if member.guild_permissions.administrator:
            return True
        role_ids = set()
        rec = cfg["tickets"]["open"].get(str(channel_id)) if channel_id else None
        if rec:
            role_ids |= set(rec.get("roles", []))
        # запасной вариант — старая одиночная роль поддержки
        if cfg["tickets"].get("support_role"):
            role_ids.add(cfg["tickets"]["support_role"])
        return any(r.id in role_ids for r in member.roles)

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild(interaction.guild.id)
        if not self._is_support(interaction.user, cfg, interaction.channel.id):
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
#  КАСТОМНЫЙ ЭМБЕД С СЕЛЕКТОМ (меню выбора ролей)
# ==========================================================================

class RoleSelect(discord.ui.Select):
    """Выпадающий список ролей. Значение каждой опции = id роли,
    поэтому меню работает без внешнего хранилища состояния и переживает
    перезапуск (привязка к сообщению по message_id при регистрации)."""

    def __init__(self, options):
        opts = [
            discord.SelectOption(label=o["label"][:100], value=str(o["id"]),
                                 description=o.get("description"))
            for o in options
        ] or [discord.SelectOption(label="—", value="0")]
        super().__init__(
            custom_id="rolemenu:select",
            placeholder="Выберите роли, чтобы получить или снять их",
            min_values=0, max_values=len(opts), options=opts,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.user
        added, removed, failed = [], [], []
        # переключаем ТОЛЬКО выбранные роли (безопасно: не трогаем остальные)
        for value in self.values:
            if value == "0":
                continue
            role = interaction.guild.get_role(int(value))
            if role is None:
                continue
            try:
                if role in member.roles:
                    await member.remove_roles(role, reason="Меню ролей")
                    removed.append(role.name)
                else:
                    await member.add_roles(role, reason="Меню ролей")
                    added.append(role.name)
            except discord.Forbidden:
                failed.append(role.name)

        parts = []
        if added:
            parts.append("✅ Выданы: " + ", ".join(added))
        if removed:
            parts.append("➖ Сняты: " + ", ".join(removed))
        if failed:
            parts.append("⚠️ Не хватило прав для: " + ", ".join(failed)
                         + " (поднимите роль бота выше этих ролей)")
        await interaction.followup.send("\n".join(parts) or "Изменений нет.", ephemeral=True)


def build_rolemenu_view(options):
    """Собрать View с меню ролей из списка опций [{'id':int,'label':str}]."""
    view = discord.ui.View(timeout=None)
    view.add_item(RoleSelect(options))
    return view


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

    # ---------- Усиленная защита: предупреждения + автобан ----------

    async def _find_actor_kick(self, guild, target_id):
        """Отличить кик от обычного выхода: ищем свежую запись kick в аудите."""
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == target_id:
                    # запись должна быть свежей (последние ~10 секунд)
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if age <= 10:
                        return entry.user
                    return None
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _strict_warn(self, guild, actor, reason):
        """Выдать предупреждение нарушителю; при достижении порога — бан."""
        cfg = get_guild(guild.id)
        prot = cfg["protection"]
        if not prot.get("strict_mode") or actor is None or actor.bot:
            return
        if (actor.id == guild.owner_id or actor.id in prot["whitelist"]
                or actor.id == self.bot.user.id):
            return

        warns = cfg.setdefault("warnings", {})
        threshold = prot.get("strict_threshold", 3)
        count = warns.get(str(actor.id), 0) + 1
        warns[str(actor.id)] = count

        if count >= threshold:
            warns[str(actor.id)] = 0
            save_guild(guild.id, cfg)
            member = guild.get_member(actor.id)
            banned = False
            if member:
                try:
                    await guild.ban(member, reason=f"Усиленная защита: {threshold} предупреждения",
                                    delete_message_seconds=0)
                    banned = True
                except (discord.Forbidden, discord.HTTPException):
                    pass
            ban_note = ("Забанен." if banned else
                        "НЕ забанен — не хватило прав (поднимите роль бота выше нарушителя).")
            await self._log(guild, discord.Embed(
                title="⛔ Усиленная защита: бан",
                description=f"{actor.mention} (`{actor.id}`) набрал {threshold} предупреждения.\n"
                            f"{ban_note}\n"
                            f"**Последнее действие:** {reason}",
                color=discord.Color.dark_red()))
        else:
            save_guild(guild.id, cfg)
            await self._log(guild, discord.Embed(
                title="⚠️ Усиленная защита: предупреждение",
                description=f"{actor.mention} получил предупреждение **{count}/{threshold}**.\n"
                            f"**Действие:** {reason}",
                color=discord.Color.orange()))

    # ---------- Слушатели опасных действий ----------

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        actor = await self._find_actor(channel.guild, discord.AuditLogAction.channel_delete)
        await self._register_nuke_action(channel.guild, actor, "удаление каналов")
        await self._strict_warn(channel.guild, actor, f"удаление канала «{channel.name}»")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        actor = await self._find_actor(role.guild, discord.AuditLogAction.role_delete)
        await self._register_nuke_action(role.guild, actor, "удаление ролей")
        await self._strict_warn(role.guild, actor, f"удаление роли «{role.name}»")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        actor = await self._find_actor(guild, discord.AuditLogAction.ban, target_id=user.id)
        await self._register_nuke_action(guild, actor, "массовые баны")
        await self._strict_warn(guild, actor, f"бан участника {user}")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        # ловим именно кик (выход по своей воле не наказываем)
        if not get_guild(member.guild.id)["protection"].get("strict_mode"):
            return
        actor = await self._find_actor_kick(member.guild, member.id)
        if actor:
            await self._strict_warn(member.guild, actor, f"кик участника {member}")

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        # наказываем за снятие ролей у участников
        if not get_guild(before.guild.id)["protection"].get("strict_mode"):
            return
        removed = set(before.roles) - set(after.roles)
        if not removed:
            return
        actor = await self._find_actor(before.guild,
                                       discord.AuditLogAction.member_role_update,
                                       target_id=after.id)
        if actor:
            names = ", ".join(r.name for r in removed)
            await self._strict_warn(before.guild, actor, f"снятие ролей ({names}) у {after}")


# ==========================================================================
#  COG: КОМАНДЫ ТИКЕТОВ
# ==========================================================================

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket_setup",
                          description="Задать канал логов тикетов (и запасной тип по умолчанию)")
    @app_commands.describe(log_channel="Канал для истории закрытых тикетов",
                           category="Категория по умолчанию (необязательно)",
                           support_role="Роль поддержки по умолчанию (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction, log_channel: discord.TextChannel,
                           category: discord.CategoryChannel = None,
                           support_role: discord.Role = None):
        cfg = get_guild(interaction.guild.id)
        cfg["tickets"]["log_channel"] = log_channel.id
        if category:
            cfg["tickets"]["category"] = category.id
        if support_role:
            cfg["tickets"]["support_role"] = support_role.id
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Логи тикетов: {log_channel.mention}\n\n"
            f"Дальше добавьте типы тикетов командой `/ticket_type_add` "
            f"(у каждого своя категория и роли), затем разместите меню `/ticket_panel`.",
            ephemeral=True)

    @app_commands.command(name="ticket_type_add",
                          description="Добавить тип тикета (своя категория + до 3 ролей поддержки)")
    @app_commands.describe(
        name="Название типа (видно в меню)", category="Категория, куда падают эти тикеты",
        support_role1="Роль поддержки 1", support_role2="Роль поддержки 2",
        support_role3="Роль поддержки 3", emoji="Эмодзи (необязательно)",
        description="Короткое описание в меню (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_type_add(self, interaction, name: str,
                              category: discord.CategoryChannel,
                              support_role1: discord.Role,
                              support_role2: discord.Role = None,
                              support_role3: discord.Role = None,
                              emoji: str = None, description: str = None):
        cfg = get_guild(interaction.guild.id)
        types = cfg["tickets"].setdefault("types", [])
        label = name[:100]
        roles = [r.id for r in (support_role1, support_role2, support_role3) if r]
        entry = {"label": label, "category": category.id, "roles": roles,
                 "emoji": (emoji or None), "description": (description or None)}
        existed = any(t["label"] == label for t in types)
        types[:] = [t for t in types if t["label"] != label]
        if len(types) >= 25:
            return await interaction.response.send_message(
                "Достигнут предел в 25 типов (ограничение Discord).", ephemeral=True)
        types.append(entry)
        save_guild(interaction.guild.id, cfg)
        role_mentions = ", ".join(f"<@&{r}>" for r in roles)
        action = "обновлён" if existed else "добавлен"
        await interaction.response.send_message(
            f"✅ Тип **{label}** {action}.\n• Категория: **{category.name}**\n"
            f"• Роли: {role_mentions}\n\n"
            f"Не забудьте пересоздать меню `/ticket_panel`, чтобы новый тип появился.",
            ephemeral=True)

    @app_commands.command(name="ticket_type_remove", description="Удалить тип тикета")
    @app_commands.describe(name="Название типа")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_type_remove(self, interaction, name: str):
        cfg = get_guild(interaction.guild.id)
        types = cfg["tickets"].get("types", [])
        before = len(types)
        types[:] = [t for t in types if t["label"] != name]
        save_guild(interaction.guild.id, cfg)
        if len(types) < before:
            await interaction.response.send_message(
                f"✅ Тип **{name}** удалён. Пересоздайте меню `/ticket_panel`.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Тип **{name}** не найден.", ephemeral=True)

    @app_commands.command(name="ticket_types", description="Список типов тикетов")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_types(self, interaction):
        cfg = get_guild(interaction.guild.id)
        types = cfg["tickets"].get("types", [])
        if not types:
            return await interaction.response.send_message(
                "Типы ещё не добавлены. Используйте `/ticket_type_add`.", ephemeral=True)
        lines = []
        for t in types:
            cat = f"<#{t['category']}>" if t.get("category") else "—"
            roles = ", ".join(f"<@&{r}>" for r in t.get("roles", [])) or "—"
            emoji = (t.get("emoji") + " ") if t.get("emoji") else ""
            lines.append(f"{emoji}**{t['label']}** → категория {cat}, роли: {roles}")
        embed = discord.Embed(title="🎫 Типы тикетов", description="\n".join(lines),
                              color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_panel",
                          description="Разместить эмбед с селект-меню выбора типа тикета")
    @app_commands.describe(channel="Канал для меню", title="Заголовок (необязательно)",
                           description="Текст (необязательно)", color="Цвет HEX (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction, channel: discord.TextChannel,
                           title: str = None, description: str = None, color: str = None):
        cfg = get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        types = tcfg.get("types", [])
        if not types:
            # миграция старых одиночных настроек в один тип
            d = _default_type(cfg)
            if d:
                types = [d]
                tcfg["types"] = types
                save_guild(interaction.guild.id, cfg)
            else:
                return await interaction.response.send_message(
                    "Сначала добавьте хотя бы один тип: `/ticket_type_add`.", ephemeral=True)
        if title:
            tcfg["panel_title"] = title
        if description:
            tcfg["panel_description"] = description
        save_guild(interaction.guild.id, cfg)

        embed = discord.Embed(title=tcfg["panel_title"], description=tcfg["panel_description"],
                              color=parse_color(color))
        view = TicketSelectView(types)
        try:
            await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Нет прав писать в {channel.mention}.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Меню тикетов ({len(types)} тип(ов)) размещено в {channel.mention}.",
            ephemeral=True)


# ==========================================================================
#  COG: КОМАНДЫ НАСТРОЙКИ ЗАЩИТЫ
# ==========================================================================

TOGGLES = {"антиспам": "antispam", "антиинвайт": "antiinvite",
           "антирейд": "antiraid", "антинюк": "antinuke",
           "усиленная защита": "strict_mode"}


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
            f"{yn(p.get('strict_mode'))} Усиленная защита "
            f"(бан за {p.get('strict_threshold', 3)} преда)\n"
            f"Логи: {log_ch}\nБелый список: {wl}"), inline=False)
        cat = f"<#{t['category']}>" if t["category"] else "не задана"
        role = f"<@&{t['support_role']}>" if t["support_role"] else "не задана"
        tlog = f"<#{t['log_channel']}>" if t["log_channel"] else "не задан"
        types = t.get("types", [])
        types_line = (", ".join(x["label"] for x in types)) if types else "нет (добавьте /ticket_type_add)"
        embed.add_field(name="🎫 Тикеты", value=(
            f"Логи: {tlog}\nТипы: {types_line}\n"
            f"Открыто сейчас: {len(t['open'])}\n"
            f"По умолчанию (старое): категория {cat}, роль {role}"), inline=False)
        warns = cfg.get("warnings", {})
        active = {u: c for u, c in warns.items() if c > 0}
        if active:
            embed.add_field(name="⚠️ Предупреждения", value=(
                "\n".join(f"<@{u}> — {c}" for u, c in active.items())), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Меню ролей (кастомный эмбед с селектом) ----------

    @app_commands.command(name="rolemenu",
                          description="Создать эмбед с выпадающим меню выбора ролей")
    @app_commands.describe(
        channel="Канал для эмбеда", title="Заголовок эмбеда", description="Текст эмбеда",
        role1="Роль 1", role2="Роль 2", role3="Роль 3", role4="Роль 4", role5="Роль 5",
        color="Цвет в HEX, напр. #5865F2 (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def rolemenu(self, interaction, channel: discord.TextChannel, title: str,
                       description: str, role1: discord.Role, role2: discord.Role = None,
                       role3: discord.Role = None, role4: discord.Role = None,
                       role5: discord.Role = None, color: str = None):
        roles = [r for r in (role1, role2, role3, role4, role5) if r]
        # предупреждаем, если бот не сможет выдавать какие-то роли
        too_high = [r.name for r in roles if r >= interaction.guild.me.top_role]
        options = [{"id": r.id, "label": r.name} for r in roles]

        embed = discord.Embed(title=title, description=description, color=parse_color(color))
        view = build_rolemenu_view(options)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Нет прав писать в {channel.mention}.", ephemeral=True)

        cfg = get_guild(interaction.guild.id)
        cfg.setdefault("menus", {})[str(msg.id)] = {"channel": channel.id, "options": options}
        save_guild(interaction.guild.id, cfg)
        self.bot.add_view(view, message_id=msg.id)  # чтобы работало сразу

        note = ""
        if too_high:
            note = ("\n⚠️ Роль бота ниже этих ролей — их не получится выдавать: "
                    + ", ".join(too_high) + ". Поднимите роль бота выше в настройках сервера.")
        await interaction.response.send_message(
            f"✅ Меню ролей размещено в {channel.mention}.{note}", ephemeral=True)

    # ---------- Управление предупреждениями ----------

    @app_commands.command(name="warnings", description="Показать предупреждения пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def warnings(self, interaction, пользователь: discord.Member):
        cfg = get_guild(interaction.guild.id)
        count = cfg.get("warnings", {}).get(str(пользователь.id), 0)
        threshold = cfg["protection"].get("strict_threshold", 3)
        await interaction.response.send_message(
            f"{пользователь.mention}: **{count}/{threshold}** предупреждений.", ephemeral=True)

    @app_commands.command(name="warnings_reset", description="Сбросить предупреждения пользователя")
    @app_commands.checks.has_permissions(administrator=True)
    async def warnings_reset(self, interaction, пользователь: discord.Member):
        cfg = get_guild(interaction.guild.id)
        cfg.setdefault("warnings", {})[str(пользователь.id)] = 0
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Предупреждения {пользователь.mention} сброшены.", ephemeral=True)


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
        self.add_view(TicketSelectView())

        # восстанавливаем меню ролей, чтобы селекты работали после перезапуска
        restored = 0
        for gset in load_all().values():
            for mid, menu in gset.get("menus", {}).items():
                try:
                    self.add_view(build_rolemenu_view(menu["options"]), message_id=int(mid))
                    restored += 1
                except Exception as e:
                    log.warning("Не удалось восстановить меню %s: %s", mid, e)
        if restored:
            log.info("Восстановлено меню ролей: %d", restored)

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
