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
        "antilink": False,          # тайм-аут за любые ссылки
        "antilink_timeout": 3600,   # длительность тайм-аута, сек (1 час)
        "antiraid": True,
        "antiraid_joins": 6,
        "antiraid_interval": 10,
        "antinuke": True,
        "antinuke_limit": 3,
        "antinuke_interval": 12,
        "strict_mode": False,       # усиленная защита (преды + автобан)
        "strict_threshold": 3,      # сколько предупреждений до бана
        "antibot": True,            # банить любого бота, кроме разрешённых
        "bot_whitelist": [],        # id разрешённых ботов (не банятся)
        "antimention": True,        # удалять @everyone/@here и флуд упоминаниями
        "mention_limit": 6,         # сколько упоминаний = флуд
        "whitelist": [],
    },
    "warnings": {},                 # {user_id: количество предупреждений}
    "economy": {
        "emoji": "🪙",              # символ валюты
        "review_channel": None,     # канал проверки отчётов
        "reward": 100,              # награда за одобренный отчёт
        "counter": 0,               # номера отчётов
        "open": {},                 # {message_id: {"user": id, "status": ...}}
    },
    "shop": {
        "items": [],                # [{"name","price","description","emoji","role","stock"}]
        "log_channel": None,        # канал логов покупок
    },
    "coins": {},                    # {user_id: баланс монет}
    "menus": {},                    # {message_id: {"channel": id, "options": [...]}}
    "panel_channel": None,          # канал постоянной панели управления
    "panel_message": None,          # id сообщения постоянной панели
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


# Вопросы по умолчанию, если у типа не заданы свои
DEFAULT_QUESTIONS = [
    {"label": "Тема обращения", "style": "short", "required": True,
     "placeholder": "Кратко: в чём вопрос?"},
    {"label": "Подробное описание", "style": "paragraph", "required": True,
     "placeholder": "Что случилось? Когда? Что уже пробовали?"},
    {"label": "Приоритет (необязательно)", "style": "short", "required": False,
     "placeholder": "низкий / средний / высокий"},
]


class TicketModal(discord.ui.Modal):
    """Форма создания тикета. Поля строятся динамически из вопросов типа
    (или из DEFAULT_QUESTIONS, если у типа своих вопросов нет)."""

    def __init__(self, ticket_type):
        super().__init__(title=f"Тикет: {ticket_type.get('label', 'обращение')}"[:45])
        self.ticket_type = ticket_type
        questions = ticket_type.get("questions") or DEFAULT_QUESTIONS
        self._inputs = []  # [(label, TextInput)]
        for q in questions[:5]:  # Discord: не более 5 полей
            is_para = q.get("style") == "paragraph"
            ti = discord.ui.TextInput(
                label=q["label"][:45],
                style=discord.TextStyle.paragraph if is_para else discord.TextStyle.short,
                placeholder=(q.get("placeholder") or None),
                required=q.get("required", True),
                max_length=1000 if is_para else 300,
            )
            self.add_item(ti)
            self._inputs.append((q["label"], ti))

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

        # собираем ответы; первый вопрос идёт в заголовок, остальные — полями
        answers = [(label, ti.value) for (label, ti) in self._inputs]
        head = answers[0][1] if answers else ""
        mention = " ".join(r.mention for r in roles)
        embed = discord.Embed(
            title=f"Тикет #{number:04d} — {head[:200]}" if head else f"Тикет #{number:04d}",
            color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Автор", value=interaction.user.mention, inline=True)
        embed.add_field(name="Тип", value=t.get("label", "—"), inline=True)
        for label, value in answers[1:]:
            if value:
                embed.add_field(name=label[:256], value=value[:1024], inline=False)
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
#  ЭКОНОМИКА: монеты, магазин, отчёты
# ==========================================================================

def _eco_get_coins(cfg, user_id):
    return cfg.get("coins", {}).get(str(user_id), 0)


def _eco_change_coins(cfg, user_id, delta):
    """Изменить баланс; при списании баланс не уходит ниже нуля."""
    bal = cfg.setdefault("coins", {})
    cur = bal.get(str(user_id), 0)
    bal[str(user_id)] = max(0, cur + delta)
    return bal[str(user_id)]


class ShopSelect(discord.ui.Select):
    """Селект магазина: value каждой опции = название товара, поэтому меню
    не хранит состояние и переживает перезапуск бота."""

    def __init__(self):
        super().__init__(custom_id="shop:select",
                         placeholder="Выберите товар для покупки",
                         min_values=1, max_values=1, options=[
                             discord.SelectOption(label="Магазин пуст", value="—")])

    def refresh(self, items):
        opts = [
            discord.SelectOption(
                label=f"{it['name']} — {it['price']}🪙"[:100],
                value=it["name"][:100],
                description=(it.get("description") or None)[:100] if it.get("description") else None,
                emoji=(it.get("emoji") or None))
            for it in (items or [])
        ]
        if opts:
            self.options = opts
        return self

    async def callback(self, interaction: discord.Interaction):
        cfg = get_guild(interaction.guild.id)
        items = cfg["shop"].get("items", [])
        chosen = self.values[0]
        it = next((x for x in items if x["name"] == chosen), None)
        if it is None:
            return await interaction.response.send_message(
                "Этот товар больше недоступен.", ephemeral=True)

        balance = _eco_get_coins(cfg, interaction.user.id)
        stock = it.get("stock")
        stock_line = f"В наличии: **{stock}**" if stock is not None else "В наличии: ∞"
        e = discord.Embed(title="🛒 Покупка",
                          color=discord.Color.gold())
        emoji = (it.get("emoji") + " ") if it.get("emoji") else ""
        e.add_field(name=f"{emoji}{it['name']}", value=(
            f"**Цена:** {it['price']}🪙\n{stock_line}\n"
            f"{it.get('description') or ''}"), inline=False)
        role = interaction.guild.get_role(it["role"]) if it.get("role") else None
        if role:
            e.add_field(name="Роль", value=role.mention, inline=False)
        e.add_field(name="Ваш баланс", value=f"**{balance}**🪙", inline=True)
        await interaction.response.send_message(
            embed=e, view=ConfirmBuyView(it["name"]), ephemeral=True)


class ShopPanelView(discord.ui.View):
    """Постоянная панель магазина в канале."""

    def __init__(self, with_items=None):
        super().__init__(timeout=None)
        sel = ShopSelect()
        if with_items:
            sel.refresh(with_items)
        self.add_item(sel)


class ConfirmBuyView(discord.ui.View):
    """Подтверждение покупки (эфемерное — живёт до перезапуска, ок)."""

    def __init__(self, item_name):
        super().__init__(timeout=120)
        self.item_name = item_name

    async def _finish(self, text, interaction):
        for c in self.children:
            c.disabled = True
        try:
            await interaction.response.edit_message(content=text, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Купить", style=discord.ButtonStyle.success, emoji="✅")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild(interaction.guild.id)
        it = next((x for x in cfg["shop"].get("items", [])
                   if x["name"] == self.item_name), None)
        if it is None:
            return await self._finish("❌ Товар уже не продаётся.", interaction)

        price = it.get("price", 0)
        balance = _eco_get_coins(cfg, interaction.user.id)
        if balance < price:
            return await self._finish(
                f"❌ Не хватает монет: нужно **{price}**, у вас **{balance}**.", interaction)

        stock = it.get("stock")
        if stock is not None:
            if stock <= 0:
                return await self._finish("❌ Товар распродан.", interaction)
            it["stock"] = stock - 1

        new_balance = _eco_change_coins(cfg, interaction.user.id, -price)
        save_guild(interaction.guild.id, cfg)

        role_line = ""
        role = interaction.guild.get_role(it["role"]) if it.get("role") else None
        if role:
            member = interaction.guild.get_member(interaction.user.id) or interaction.user
            if role >= interaction.guild.me.top_role:
                role_line = ("\n⚠️ Роль бота ниже нужной роли — она НЕ выдана, "
                             "обратитесь к администрации.")
            else:
                try:
                    await member.add_roles(role, reason=f"Покупка в магазине: {it['name']}")
                    role_line = f"\n✅ Выдана роль {role.mention}."
                except discord.Forbidden:
                    role_line = ("\n⚠️ Не хватило прав выдать роль — "
                                 "обратитесь к администрации.")

        log_ch = (interaction.guild.get_channel(cfg["shop"]["log_channel"])
                  if cfg["shop"].get("log_channel") else None)
        if log_ch:
            try:
                await log_ch.send(embed=discord.Embed(
                    title="🛒 Покупка",
                    description=f"{interaction.user.mention} купил **{it['name']}** "
                                f"за **{price}**🪙.",
                    color=discord.Color.gold()))
            except discord.HTTPException:
                pass

        await self._finish(
            f"✅ Куплено: **{it['name']}** за **{price}**🪙.\n"
            f"Остаток: **{new_balance}**🪙.{role_line}", interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish("Покупка отменена.", interaction)


class ReportPanelView(discord.ui.View):
    """Постоянная панель подачи отчётов на монеты."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Подать отчёт на монеты", style=discord.ButtonStyle.primary,
                       emoji="📝", custom_id="eco:report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        eco = get_guild(interaction.guild.id)["economy"]
        if not eco.get("review_channel"):
            return await interaction.response.send_message(
                "Отчёты ещё не настроены. Обратитесь к администрации.", ephemeral=True)
        await interaction.response.send_modal(ReportModal())


class ReportModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Отчёт на монеты")
        self.what = discord.ui.TextInput(
            label="Что было сделано", style=discord.TextStyle.paragraph,
            required=True, max_length=1500,
            placeholder="Опишите, что вы сделали за отчётный период...")
        self.proof = discord.ui.TextInput(
            label="Доказательства / ссылка", style=discord.TextStyle.short,
            required=False, max_length=300,
            placeholder="Ссылка на скриншот/работу или описание")
        self.add_item(self.what)
        self.add_item(self.proof)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        cfg = get_guild(guild.id)
        eco = cfg["economy"]
        ch = guild.get_channel(eco.get("review_channel"))
        if ch is None:
            return await interaction.followup.send(
                "Канал проверки отчётов недоступен. Обратитесь к администрации.",
                ephemeral=True)

        eco["counter"] += 1
        number = eco["counter"]
        e = discord.Embed(
            title=f"📝 Отчёт #{number:04d}",
            description=self.what.value[:2000],
            color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        e.add_field(name="Автор", value=interaction.user.mention, inline=True)
        e.add_field(name="Награда", value=f"{eco.get('reward', 0)}🪙", inline=True)
        if self.proof.value:
            e.add_field(name="Доказательства", value=self.proof.value[:1024], inline=False)
        view = build_review_view(interaction.user.id)
        try:
            msg = await ch.send(embed=e, view=view)
        except discord.Forbidden:
            return await interaction.followup.send(
                "Не удалось отправить отчёт (нет прав в канале проверки).", ephemeral=True)

        eco.setdefault("open", {})[str(msg.id)] = {
            "user": interaction.user.id, "status": "pending"}
        save_guild(guild.id, cfg)
        await interaction.followup.send(
            f"✅ Отчёт отправлен на проверку: {msg.jump_url}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Ошибка при отправке отчёта", exc_info=error)
        msg = "Не удалось отправить отчёт. Сообщите администрации."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


async def _review_report(interaction: discord.Interaction, action, report_user):
    """Общая логика одобрения/отклонения отчёта (вызывается из кнопок)."""
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "Только администраторы могут проверять отчёты.", ephemeral=True)

    cfg = get_guild(interaction.guild.id)
    eco = cfg["economy"]
    rec = eco.setdefault("open", {}).get(str(interaction.message.id))
    if rec and rec.get("status") != "pending":
        return await interaction.response.send_message(
            "Этот отчёт уже обработан.", ephemeral=True)

    reward = eco.get("reward", 0)
    user = interaction.guild.get_member(report_user)

    if rec:
        rec["status"] = ("approved" if action == "approve" else "denied")
        rec["reviewer"] = interaction.user.id

    if action == "approve":
        balance = _eco_change_coins(cfg, report_user, reward)
        save_guild(interaction.guild.id, cfg)
        if user:
            try:
                await user.send(f"✅ Ваш отчёт на сервере **{interaction.guild.name}** "
                                f"одобрен: +{reward}🪙. Баланс: **{balance}**🪙.")
            except discord.HTTPException:
                pass
        e = interaction.message.embeds[0] if interaction.message.embeds else None
        if e:
            e.color = discord.Color.green()
            e.add_field(name="Результат",
                        value=f"Одобрено {interaction.user.mention}: +{reward}🪙",
                        inline=False)
    else:
        if rec:
            save_guild(interaction.guild.id, cfg)
        e = interaction.message.embeds[0] if interaction.message.embeds else None
        if e:
            e.color = discord.Color.red()
            e.add_field(name="Результат",
                        value=f"Отклонено {interaction.user.mention}", inline=False)
        if user:
            try:
                await user.send(f"❌ Ваш отчёт на сервере **{interaction.guild.name}** "
                                f"отклонён. Уточните у администрации причину.")
            except discord.HTTPException:
                pass

    # отключаем кнопки на сообщении
    try:
        new_view = discord.ui.View(timeout=None)
        enabled_any = False
        for r in interaction.message.components:
            for c in r.children:
                clone = None
                if isinstance(c, discord.ui.Button):
                    clone = discord.ui.Button(
                        style=c.style, label=c.label, emoji=c.emoji,
                        url=c.url, disabled=True)
                elif isinstance(c, discord.ui.Select):
                    clone = discord.ui.Select(
                        placeholder=c.placeholder, options=c.options,
                        disabled=True)
                if clone:
                    new_view.add_item(clone)
                    enabled_any = True
        view_to_send = new_view if enabled_any else None
    except Exception:
        view_to_send = None

    await interaction.response.edit_message(embed=e, view=view_to_send)


class ApproveReport(discord.ui.DynamicItem,
                    template=r"eco:approve:(?P<user>\d+)"):
    """Кнопка «Одобрить»: user_id зашит в custom_id, поэтому работает
    даже после перезапуска бота."""

    def __init__(self, user_id):
        self.report_user = user_id
        super().__init__(discord.ui.Button(
            label="Одобрить", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"eco:approve:{user_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        await _review_report(interaction, "approve", self.report_user)


class DenyReport(discord.ui.DynamicItem,
                 template=r"eco:deny:(?P<user>\d+)"):

    def __init__(self, user_id):
        self.report_user = user_id
        super().__init__(discord.ui.Button(
            label="Отклонить", style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"eco:deny:{user_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        await _review_report(interaction, "deny", self.report_user)


def build_review_view(user_id):
    """Ряд кнопок ✅/❌ для сообщения с отчётом."""
    v = discord.ui.View(timeout=None)
    v.add_item(ApproveReport(user_id))
    v.add_item(DenyReport(user_id))
    return v


# ==========================================================================
#  COG: ЗАЩИТА
# ==========================================================================

INVITE_RE = re.compile(r"(discord\.gg/|discord(app)?\.com/invite/)", re.IGNORECASE)
LINK_RE = re.compile(
    r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(?:com|net|org|gg|io|ru|xyz|me|tv|co|app|dev|"
    r"info|biz|link|shop|store|online|site|club|fun|top|pw|cc)\b\S*)",
    re.IGNORECASE)


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

        # анти-упоминания: @everyone/@here и флуд упоминаниями
        if prot.get("antimention", True):
            everyone_ping = ("@everyone" in message.content
                             or "@here" in message.content)
            mention_count = len(message.mentions) + len(message.mention_roles)
            limit = prot.get("mention_limit", 6)
            if everyone_ping or mention_count >= limit:
                try:
                    await message.delete()
                    await message.channel.send(
                        f"{member.mention}, массовые упоминания запрещены.",
                        delete_after=5)
                except discord.HTTPException:
                    pass
                await self._log(message.guild, discord.Embed(
                    title="📣 Удалены массовые упоминания",
                    description=f"**Автор:** {member.mention}\n"
                                f"**Канал:** {message.channel.mention}\n"
                                f"@everyone/@here: {'да' if everyone_ping else 'нет'}, "
                                f"упоминаний: {mention_count}",
                    color=discord.Color.orange()))
                return

        if prot.get("antilink") and LINK_RE.search(message.content):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            secs = prot.get("antilink_timeout", 3600)
            muted = False
            try:
                await member.timeout(timedelta(seconds=secs),
                                     reason="Антиссылки: отправка ссылки")
                muted = True
            except (discord.Forbidden, discord.HTTPException):
                pass
            # предупреждение ЛИЧНО нарушителю (в ЛС), без спама в канал
            hours = max(1, round(secs / 3600))
            warn = (f"На сервере **{message.guild.name}** запрещена отправка ссылок. "
                    f"Ваше сообщение удалено"
                    + (f", выдан тайм-аут на {hours} ч." if muted else "."))
            try:
                await member.send(warn)
            except discord.HTTPException:
                # если ЛС закрыты — короткое авто-удаляемое сообщение в канале
                try:
                    await message.channel.send(f"{member.mention}, ссылки запрещены.",
                                               delete_after=7)
                except discord.HTTPException:
                    pass
            await self._log(message.guild, discord.Embed(
                title="🔗 Антиссылки",
                description=f"**Автор:** {member.mention}\n"
                            f"**Канал:** {message.channel.mention}\n"
                            f"{'Выдан тайм-аут на ' + str(hours) + ' ч.' if muted else 'Тайм-аут не выдан (нет прав).'}",
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

        # --- Антибот: банить любого бота, кроме разрешённых ---
        if member.bot and prot.get("antibot", True):
            if (member.id not in prot.get("bot_whitelist", [])
                    and member.id != self.bot.user.id):
                adder = await self._find_actor(
                    member.guild, discord.AuditLogAction.bot_add, target_id=member.id)
                try:
                    await member.guild.ban(
                        member, reason="Антибот: неразрешённый бот", delete_message_seconds=0)
                    banned = True
                except (discord.Forbidden, discord.HTTPException):
                    banned = False
                who = adder.mention if adder else "неизвестно"
                await self._log(member.guild, discord.Embed(
                    title="🤖⛔ Антибот",
                    description=f"Бот **{member}** (`{member.id}`) "
                                f"{'забанен' if banned else 'НЕ забанен (не хватило прав)'}.\n"
                                f"**Кто добавил:** {who}\n\n"
                                f"Если бот нужен — добавьте его в разрешённые "
                                f"командой `/bot_allow` и разбаньте.",
                    color=discord.Color.dark_red()))
                return  # дальше анти-рейд для бота не нужен

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

    DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles",
                       "manage_channels", "manage_webhooks", "ban_members",
                       "manage_nicknames", "moderate_members")

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        # выдача роли опасных прав — классика захвата сервера
        gained = [p for p in self.DANGEROUS_PERMS
                  if getattr(after.permissions, p) and not getattr(before.permissions, p)]
        if not gained:
            return
        actor = await self._find_actor(after.guild, discord.AuditLogAction.role_update)
        reason = f"выдача опасных прав роли «{after.name}»: {', '.join(gained)}"
        await self._register_nuke_action(after.guild, actor, "раздача прав ролям")
        await self._strict_warn(after.guild, actor, reason)

    @commands.Cog.listener()
    async def on_guild_update(self, before, after):
        # смена названия/иконки/валюты сервера без ведома администрации
        changed = []
        if before.name != after.name:
            changed.append(f"название → «{after.name}»")
        if before.vanity_url_code != after.vanity_url_code:
            changed.append("vanity-ссылка")
        if not changed:
            return
        try:
            entry = None
            async for e in after.guild.audit_logs(limit=5,
                                                  action=discord.AuditLogAction.guild_update):
                entry = e
                break
        except (discord.Forbidden, discord.HTTPException):
            entry = None
        actor = entry.user if entry else None
        await self._register_nuke_action(after.guild, actor, "изменение настроек сервера")
        await self._strict_warn(after.guild, actor, "изменение настроек сервера: "
                                + ", ".join(changed))


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

    # ---------- Вопросы формы у каждого типа ----------

    @app_commands.command(name="ticket_question_add",
                          description="Добавить вопрос в форму типа тикета (до 5)")
    @app_commands.describe(
        type="Название типа тикета", label="Текст вопроса (до 45 символов)",
        style="Тип поля: короткое или многострочное",
        required="Обязательное ли поле", placeholder="Подсказка внутри поля (необязательно)")
    @app_commands.choices(style=[
        app_commands.Choice(name="короткое", value="short"),
        app_commands.Choice(name="многострочное", value="paragraph"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_question_add(self, interaction, type: str, label: str,
                                  style: app_commands.Choice[str] = None,
                                  required: bool = True, placeholder: str = None):
        cfg = get_guild(interaction.guild.id)
        t = next((x for x in cfg["tickets"].get("types", []) if x["label"] == type), None)
        if t is None:
            return await interaction.response.send_message(
                f"Тип **{type}** не найден. Список: `/ticket_types`.", ephemeral=True)
        questions = t.setdefault("questions", [])
        if len(questions) >= 5:
            return await interaction.response.send_message(
                "У типа уже 5 вопросов — это максимум для формы Discord. "
                "Удалите лишний через `/ticket_question_remove`.", ephemeral=True)
        questions.append({
            "label": label[:45],
            "style": (style.value if style else "short"),
            "required": required,
            "placeholder": (placeholder or None),
        })
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Вопрос добавлен в тип **{type}** (всего {len(questions)}/5). "
            f"Меню пересоздавать не нужно — форма обновится сразу.", ephemeral=True)

    @app_commands.command(name="ticket_question_remove",
                          description="Удалить вопрос из формы типа по номеру")
    @app_commands.describe(type="Название типа", number="Номер вопроса из /ticket_questions")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_question_remove(self, interaction, type: str, number: int):
        cfg = get_guild(interaction.guild.id)
        t = next((x for x in cfg["tickets"].get("types", []) if x["label"] == type), None)
        if t is None:
            return await interaction.response.send_message(
                f"Тип **{type}** не найден.", ephemeral=True)
        questions = t.get("questions", [])
        if not questions:
            return await interaction.response.send_message(
                "У этого типа сейчас стандартные вопросы (свои не заданы). "
                "Добавьте свои через `/ticket_question_add`.", ephemeral=True)
        if number < 1 or number > len(questions):
            return await interaction.response.send_message(
                f"Нет вопроса с номером {number}. Всего вопросов: {len(questions)}.",
                ephemeral=True)
        removed = questions.pop(number - 1)
        if not questions:
            t.pop("questions", None)  # вернётся к стандартным вопросам
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Вопрос «{removed['label']}» удалён из типа **{type}**.", ephemeral=True)

    @app_commands.command(name="ticket_questions",
                          description="Показать вопросы формы у типа тикета")
    @app_commands.describe(type="Название типа")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_questions(self, interaction, type: str):
        cfg = get_guild(interaction.guild.id)
        t = next((x for x in cfg["tickets"].get("types", []) if x["label"] == type), None)
        if t is None:
            return await interaction.response.send_message(
                f"Тип **{type}** не найден.", ephemeral=True)
        questions = t.get("questions")
        default = questions is None
        questions = questions or DEFAULT_QUESTIONS
        style_ru = {"short": "короткое", "paragraph": "многострочное"}
        lines = []
        for i, q in enumerate(questions, 1):
            req = "обязательное" if q.get("required", True) else "необязательное"
            lines.append(f"**{i}.** {q['label']} — {style_ru.get(q.get('style'), 'короткое')}, {req}")
        note = "\n\n_Сейчас используются стандартные вопросы. Добавьте свой через "\
               "`/ticket_question_add`, и они заменят стандартные._" if default else ""
        embed = discord.Embed(
            title=f"Вопросы формы: {type}", description="\n".join(lines) + note,
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
#  COG: КОМАНДЫ ЭКОНОМИКИ
# ==========================================================================

class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- Магазин: настройка товаров ----------

    @app_commands.command(name="shop_add",
                          description="Добавить/обновить товар в магазине")
    @app_commands.describe(name="Название товара", price="Цена в монетах",
                           description="Описание (необязательно)",
                           emoji="Эмодзи (необязательно)",
                           role="Роль-награда при покупке (необязательно)",
                           stock="Количество на складе, пусто = ∞ (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_add(self, interaction, name: str, price: app_commands.Range[int, 0],
                       description: str = None, emoji: str = None,
                       role: discord.Role = None,
                       stock: app_commands.Range[int, 0] = None):
        cfg = get_guild(interaction.guild.id)
        items = cfg["shop"].setdefault("items", [])
        label = name[:100]
        entry = {"name": label, "price": price, "description": description,
                 "emoji": emoji or None, "role": role.id if role else None,
                 "stock": stock}
        existed = any(x["name"] == label for x in items)
        items[:] = [x for x in items if x["name"] != label]
        if len(items) >= 25:
            return await interaction.response.send_message(
                "Максимум 25 товаров (ограничение меню Discord).", ephemeral=True)
        items.append(entry)
        save_guild(interaction.guild.id, cfg)
        action = "обновлён" if existed else "добавлен"
        stock_line = f"склад: {stock if stock is not None else '∞'}"
        await interaction.response.send_message(
            f"✅ Товар **{label}** {action}: цена **{price}**🪙, {stock_line}"
            + (f", роль {role.mention}" if role else "")
            + ". Пересоздайте панель `/shop_panel`, чтобы товар появился.",
            ephemeral=True)

    @app_commands.command(name="shop_remove", description="Удалить товар из магазина")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_remove(self, interaction, name: str):
        cfg = get_guild(interaction.guild.id)
        items = cfg["shop"].get("items", [])
        before = len(items)
        cfg["shop"]["items"] = [x for x in items if x["name"] != name]
        save_guild(interaction.guild.id, cfg)
        if len(cfg["shop"]["items"]) < before:
            await interaction.response.send_message(
                f"✅ Товар **{name}** удалён.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Товар **{name}** не найден.", ephemeral=True)

    @app_commands.command(name="shop_list", description="Список товаров магазина")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_list(self, interaction):
        cfg = get_guild(interaction.guild.id)
        items = cfg["shop"].get("items", [])
        if not items:
            return await interaction.response.send_message(
                "Товаров нет. Добавьте через `/shop_add`.", ephemeral=True)
        lines = []
        for it in items:
            emoji = (it.get("emoji") + " ") if it.get("emoji") else ""
            stock = ("склад: " + str(it["stock"])) if it.get("stock") is not None else "склад: ∞"
            role = f", роль <@&{it['role']}>" if it.get("role") else ""
            lines.append(f"{emoji}**{it['name']}** — {it['price']}🪙, {stock}{role}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="shop_panel",
                          description="Разместить магазин с товарами в канале")
    @app_commands.describe(channel="Канал для магазина", title="Заголовок (необязательно)",
                           description="Текст (необязательно)", color="Цвет HEX (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_panel(self, interaction, channel: discord.TextChannel,
                         title: str = None, description: str = None, color: str = None):
        cfg = get_guild(interaction.guild.id)
        items = cfg["shop"].setdefault("items", [])
        if not items:
            return await interaction.response.send_message(
                "Сначала добавьте товары: `/shop_add`.", ephemeral=True)
        e = discord.Embed(title=title or "🛒 Магазин",
                          description=description or
                          "Выберите товар в меню ниже и подтвердите покупку.\n"
                          f"Баланс: `/balance`. Заработать монеты — отчётом "
                          f"(кнопка «Подать отчёт»).",
                          color=parse_color(color) or discord.Color.gold())
        view = ShopPanelView(with_items=items)
        try:
            await channel.send(embed=e, view=view)
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Нет прав писать в {channel.mention}.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Магазин ({len(items)} товар(ов)) размещён в {channel.mention}.",
            ephemeral=True)

    @app_commands.command(name="shop_log", description="Задать канал логов покупок")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_log(self, interaction, channel: discord.TextChannel):
        cfg = get_guild(interaction.guild.id)
        cfg["shop"]["log_channel"] = channel.id
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Логи покупок: {channel.mention}.", ephemeral=True)

    # ---------- Отчёты на монеты ----------

    @app_commands.command(name="report_setup",
                          description="Настроить систему отчётов на монеты")
    @app_commands.describe(review_channel="Канал, куда падают отчёты на проверку",
                           reward="Награда за одобренный отчёт (монет)",
                           panel_channel="Канал с кнопкой подачи отчётов (необязательно)")
    @app_commands.checks.has_permissions(administrator=True)
    async def report_setup(self, interaction, review_channel: discord.TextChannel,
                           reward: app_commands.Range[int, 1],
                           panel_channel: discord.TextChannel = None):
        cfg = get_guild(interaction.guild.id)
        eco = cfg["economy"]
        eco["review_channel"] = review_channel.id
        eco["reward"] = reward
        save_guild(interaction.guild.id, cfg)
        msg = f"✅ Отчёты настроены:\n• Проверка: {review_channel.mention}\n• Награда: **{reward}**🪙"
        if panel_channel:
            e = discord.Embed(
                title="📝 Отчёты за активность",
                description=("Нажмите кнопку ниже и заполните форму. "
                             f"Одобренный отчёт = **{reward}** монет."),
                color=discord.Color.gold())
            try:
                await panel_channel.send(embed=e, view=ReportPanelView())
            except discord.Forbidden:
                return await interaction.response.send_message(
                    msg + f"\n⚠️ Нет прав писать в {panel_channel.mention} — "
                          f"панель не размещена.", ephemeral=True)
            msg += f"\n• Кнопка подачи размещена в {panel_channel.mention}"
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------- Баланс ----------

    @app_commands.command(name="balance", description="Показать баланс монет")
    async def balance(self, interaction, участник: discord.Member = None):
        member = участник or interaction.user
        cfg = get_guild(interaction.guild.id)
        bal = _eco_get_coins(cfg, member.id)
        await interaction.response.send_message(
            f"💰 Баланс {member.mention}: **{bal}**🪙", ephemeral=True)

    @app_commands.command(name="top", description="Топ 10 богатейших участников")
    async def top(self, interaction):
        cfg = get_guild(interaction.guild.id)
        coins = sorted(cfg.get("coins", {}).items(),
                       key=lambda kv: kv[1], reverse=True)[:10]
        lines = [f"**{i}.** <@{uid}> — {amount}🪙"
                 for i, (uid, amount) in enumerate(coins, 1) if amount > 0]
        await interaction.response.send_message(
            "🏆 **Топ по монетам:**\n" + ("\n".join(lines) or "Пока пусто."),
            ephemeral=True)

    @app_commands.command(name="coins_add", description="Выдать монеты участнику")
    @app_commands.checks.has_permissions(administrator=True)
    async def coins_add(self, interaction, участник: discord.Member,
                        количество: app_commands.Range[int, 1]):
        cfg = get_guild(interaction.guild.id)
        new = _eco_change_coins(cfg, участник.id, количество)
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {участник.mention}: +{количество}🪙 (итого **{new}**).", ephemeral=True)

    @app_commands.command(name="coins_remove", description="Списать монеты у участника")
    @app_commands.checks.has_permissions(administrator=True)
    async def coins_remove(self, interaction, участник: discord.Member,
                           количество: app_commands.Range[int, 1]):
        cfg = get_guild(interaction.guild.id)
        new = _eco_change_coins(cfg, участник.id, -количество)
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ {участник.mention}: −{количество}🪙 (итого **{new}**).", ephemeral=True)


# ==========================================================================
#  COG: КОМАНДЫ НАСТРОЙКИ ЗАЩИТЫ
# ==========================================================================

TOGGLES = {"антиспам": "antispam", "антиинвайт": "antiinvite",
           "антиссылки": "antilink", "антиупоминания": "antimention",
           "антирейд": "antiraid", "антинюк": "antinuke",
           "усиленная защита": "strict_mode", "антибот": "antibot"}

# читаемые названия для панели
PROT_LABELS = {
    "antispam": "Анти-спам", "antiinvite": "Анти-инвайт", "antilink": "Антиссылки",
    "antimention": "Анти-упоминания", "antiraid": "Анти-рейд", "antinuke": "Анти-нюк",
    "strict_mode": "Усиленная защита", "antibot": "Антибот",
}


# ==========================================================================
#  БОЛЬШАЯ ПАНЕЛЬ УПРАВЛЕНИЯ  (/panel)
# ==========================================================================

class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ Назад", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        view = EphemeralMainView(interaction.guild.id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class CloseButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Закрыть", style=discord.ButtonStyle.danger, row=4)

    async def callback(self, interaction):
        await interaction.response.edit_message(
            content="Панель закрыта.", embed=None, view=None)


def panel_main_embed(guild_id):
    """Статус-эмбед главного меню (общий для постоянной и приватной панели)."""
    cfg = get_guild(guild_id)
    p, t = cfg["protection"], cfg["tickets"]
    yn = lambda v: "✅" if v else "❌"
    prot = " ".join(f"{yn(p.get(k))}{PROT_LABELS[k]}" for k in PROT_LABELS)
    types = ", ".join(x["label"] for x in t.get("types", [])) or "нет"
    e = discord.Embed(
        title="🎛️ Панель управления",
        description="Нажмите кнопку раздела — настройки откроются лично вам.",
        color=discord.Color.blurple())
    e.add_field(name="🛡️ Защита", value=prot, inline=False)
    e.add_field(name="🎫 Тикеты",
                value=f"Типы: {types}\nОткрыто: {len(t.get('open', {}))}", inline=False)
    log_ch = f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "не задан"
    tlog = f"<#{t['log_channel']}>" if t.get("log_channel") else "не задан"
    eco, sh = cfg["economy"], cfg["shop"]
    review = f"<#{eco['review_channel']}>" if eco.get("review_channel") else "не задан"
    shop_ch = f"<#{sh['log_channel']}>" if sh.get("log_channel") else "не задан"
    e.add_field(name="⚙️ Логи", value=f"Защита: {log_ch}\nТикеты: {tlog}", inline=False)
    e.add_field(name="🪙 Экономика",
                value=f"Товаров: {len(sh.get('items', []))}, логи: {shop_ch}\n"
                      f"Отчёты: проверка {review}, награда {eco.get('reward', 0)}🪙",
                inline=False)
    return e


def _panel_deny(interaction):
    """True + ответ, если нажавший не администратор."""
    if interaction.user.guild_permissions.administrator:
        return False
    return True


# ---------- Постоянная панель в канале (статичное сообщение) ----------

class PersistentPanelView(discord.ui.View):
    """Живёт в канале постоянно. Само сообщение не меняется от навигации —
    разделы открываются приватно (ephemeral) каждому нажавшему."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction, view):
        if _panel_deny(interaction):
            return await interaction.response.send_message(
                "Эта панель только для администраторов.", ephemeral=True)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="🛡️ Защита", style=discord.ButtonStyle.primary,
                       custom_id="panel:prot")
    async def prot(self, interaction, button):
        await self._open(interaction, ProtectionPanelView(interaction.guild.id))

    @discord.ui.button(label="🎫 Тикеты", style=discord.ButtonStyle.primary,
                       custom_id="panel:tickets")
    async def tickets(self, interaction, button):
        await self._open(interaction, TicketsPanelView(interaction.guild.id))

    @discord.ui.button(label="⚙️ Логи", style=discord.ButtonStyle.primary,
                       custom_id="panel:logs")
    async def logs(self, interaction, button):
        await self._open(interaction, LogsPanelView(interaction.guild.id))

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.secondary,
                       custom_id="panel:refresh")
    async def refresh(self, interaction, button):
        if _panel_deny(interaction):
            return await interaction.response.send_message(
                "Только для администраторов.", ephemeral=True)
        await interaction.response.edit_message(
            embed=panel_main_embed(interaction.guild.id), view=PersistentPanelView())


# ---------- Приватное главное меню (внутри личной сессии) ----------

class EphemeralMainView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    def embed(self):
        return panel_main_embed(self.guild_id)

    @discord.ui.button(label="🛡️ Защита", style=discord.ButtonStyle.primary)
    async def protection(self, interaction, button):
        view = ProtectionPanelView(self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="🎫 Тикеты", style=discord.ButtonStyle.primary)
    async def tickets(self, interaction, button):
        view = TicketsPanelView(self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="⚙️ Логи", style=discord.ButtonStyle.primary)
    async def logs(self, interaction, button):
        view = LogsPanelView(self.guild_id)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="Закрыть", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        await interaction.response.edit_message(content="Панель закрыта.", embed=None, view=None)


# ---------- Раздел: Защита ----------

class ProtToggleButton(discord.ui.Button):
    def __init__(self, key, enabled):
        super().__init__(
            label=f"{PROT_LABELS[key]}: {'ВКЛ' if enabled else 'ВЫКЛ'}",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary)
        self.key = key

    async def callback(self, interaction):
        cfg = get_guild(interaction.guild.id)
        cfg["protection"][self.key] = not cfg["protection"].get(self.key)
        save_guild(interaction.guild.id, cfg)
        view = ProtectionPanelView(interaction.guild.id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ProtectionPanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        p = get_guild(guild_id)["protection"]
        for key in PROT_LABELS:
            self.add_item(ProtToggleButton(key, p.get(key)))
        self.add_item(BackButton())

    def embed(self):
        p = get_guild(self.guild_id)["protection"]
        yn = lambda v: "✅ включена" if v else "❌ выключена"
        lines = [f"**{PROT_LABELS[k]}** — {yn(p.get(k))}" for k in PROT_LABELS]
        lines.append(f"\nБан по усиленной защите: за **{p.get('strict_threshold', 3)}** преда")
        return discord.Embed(title="🛡️ Защита сервера",
                             description="Нажимайте кнопки, чтобы включать/выключать.\n\n"
                                         + "\n".join(lines),
                             color=discord.Color.blurple())


# ---------- Раздел: Логи ----------

class LogSelect(discord.ui.ChannelSelect):
    def __init__(self, target, placeholder):
        super().__init__(channel_types=[discord.ChannelType.text],
                         placeholder=placeholder, min_values=1, max_values=1)
        self.target = target  # "protection" или "tickets"

    async def callback(self, interaction):
        ch = self.values[0]
        cfg = get_guild(interaction.guild.id)
        if self.target == "protection":
            cfg["log_channel"] = ch.id
        else:
            cfg["tickets"]["log_channel"] = ch.id
        save_guild(interaction.guild.id, cfg)
        view = LogsPanelView(interaction.guild.id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class LogsPanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(LogSelect("protection", "Канал логов защиты"))
        self.add_item(LogSelect("tickets", "Канал логов тикетов"))
        self.add_item(BackButton())

    def embed(self):
        cfg = get_guild(self.guild_id)
        log_ch = f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "не задан"
        tlog = f"<#{cfg['tickets']['log_channel']}>" if cfg["tickets"].get("log_channel") else "не задан"
        return discord.Embed(
            title="⚙️ Каналы логов",
            description=f"**Логи защиты:** {log_ch}\n**Логи тикетов:** {tlog}\n\n"
                        f"Выберите каналы в меню ниже.",
            color=discord.Color.blurple())


# ---------- Раздел: Тикеты ----------

class PostMenuSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(channel_types=[discord.ChannelType.text],
                         placeholder="Разместить меню тикетов в канале…",
                         min_values=1, max_values=1)

    async def callback(self, interaction):
        cfg = get_guild(interaction.guild.id)
        tcfg = cfg["tickets"]
        types = tcfg.get("types", [])
        if not types:
            d = _default_type(cfg)
            if d:
                types = [d]; tcfg["types"] = types; save_guild(interaction.guild.id, cfg)
            else:
                return await interaction.response.send_message(
                    "Сначала добавьте тип тикета в этом разделе.", ephemeral=True)
        channel = interaction.guild.get_channel(self.values[0].id)
        embed = discord.Embed(title=tcfg["panel_title"], description=tcfg["panel_description"],
                              color=discord.Color.blurple())
        try:
            await channel.send(embed=embed, view=TicketSelectView(types))
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Нет прав писать в {channel.mention}.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Меню тикетов размещено в {channel.mention}.", ephemeral=True)


class RemoveTypeSelect(discord.ui.Select):
    def __init__(self, types):
        options = [discord.SelectOption(label=t["label"][:100], value=t["label"][:100])
                   for t in types] or [discord.SelectOption(label="—", value="—")]
        super().__init__(placeholder="Удалить тип…", min_values=1, max_values=1,
                         options=options)

    async def callback(self, interaction):
        cfg = get_guild(interaction.guild.id)
        types = cfg["tickets"].get("types", [])
        cfg["tickets"]["types"] = [t for t in types if t["label"] != self.values[0]]
        save_guild(interaction.guild.id, cfg)
        view = TicketsPanelView(interaction.guild.id)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class TicketsPanelView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(PostMenuSelect())
        types = get_guild(guild_id)["tickets"].get("types", [])
        if types:
            self.add_item(RemoveTypeSelect(types))
        self.add_item(BackButton())

    def embed(self):
        cfg = get_guild(self.guild_id)
        t = cfg["tickets"]
        types = t.get("types", [])
        if types:
            lines = []
            for x in types:
                cat = f"<#{x['category']}>" if x.get("category") else "—"
                roles = ", ".join(f"<@&{r}>" for r in x.get("roles", [])) or "—"
                q = x.get("questions")
                qn = f"{len(q)} свои" if q else "стандартные"
                lines.append(f"**{x['label']}** → {cat}, роли: {roles}, вопросы: {qn}")
            desc = "\n".join(lines)
        else:
            desc = "Типов пока нет."
        desc += ("\n\n**Добавление типов и вопросов** — командами "
                 "`/ticket_type_add` и `/ticket_question_add` "
                 "(там нужны выбор категории, ролей и текст).")
        return discord.Embed(title="🎫 Тикеты", description=desc, color=discord.Color.blurple())


def _panel_is_admin(interaction):
    return interaction.user.guild_permissions.administrator


class Config(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="panel",
                          description="Открыть панель управления лично (разово)")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel(self, interaction):
        view = EphemeralMainView(interaction.guild.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="panel_setup",
                          description="Разместить постоянную панель управления в канале")
    @app_commands.describe(channel="Канал для постоянной панели (лучше закрытый, для админов)")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel_setup(self, interaction, channel: discord.TextChannel):
        try:
            msg = await channel.send(embed=panel_main_embed(interaction.guild.id),
                                     view=PersistentPanelView())
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Нет прав писать в {channel.mention}.", ephemeral=True)
        cfg = get_guild(interaction.guild.id)
        cfg["panel_channel"] = channel.id
        cfg["panel_message"] = msg.id
        save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Постоянная панель размещена в {channel.mention}.\n"
            f"Само сообщение статично, а разделы открываются приватно каждому админу. "
            f"Убедитесь, что канал виден только администрации.", ephemeral=True)

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

    @app_commands.command(name="bot_allow",
                          description="Разрешить бота (антибот не будет его банить)")
    @app_commands.describe(bot="Бот, которого разрешить (упоминанием или ID)")
    @app_commands.checks.has_permissions(administrator=True)
    async def allow_bot(self, interaction, bot: discord.Member):
        if not bot.bot:
            return await interaction.response.send_message(
                "Это не бот.", ephemeral=True)
        cfg = get_guild(interaction.guild.id)
        bl = cfg["protection"].setdefault("bot_whitelist", [])
        if bot.id not in bl:
            bl.append(bot.id)
            save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Бот {bot.mention} разрешён. Если он был забанен — разбаньте его вручную.",
            ephemeral=True)

    @app_commands.command(name="bot_disallow", description="Убрать бота из разрешённых")
    @app_commands.describe(bot_id="ID бота")
    @app_commands.checks.has_permissions(administrator=True)
    async def disallow_bot(self, interaction, bot_id: str):
        if not bot_id.isdigit():
            return await interaction.response.send_message("Укажите числовой ID.", ephemeral=True)
        cfg = get_guild(interaction.guild.id)
        bl = cfg["protection"].setdefault("bot_whitelist", [])
        bid = int(bot_id)
        if bid in bl:
            bl.remove(bid)
            save_guild(interaction.guild.id, cfg)
        await interaction.response.send_message(
            f"✅ Бот `{bot_id}` убран из разрешённых.", ephemeral=True)

    @app_commands.command(name="bots_allowed", description="Список разрешённых ботов")
    @app_commands.checks.has_permissions(administrator=True)
    async def list_allowed_bots(self, interaction):
        cfg = get_guild(interaction.guild.id)
        bl = cfg["protection"].get("bot_whitelist", [])
        text = "\n".join(f"<@{b}> (`{b}`)" for b in bl) or "пусто"
        await interaction.response.send_message(
            f"🤖 Разрешённые боты:\n{text}", ephemeral=True)

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
            f"{yn(p.get('antilink'))} Антиссылки (тайм-аут "
            f"{max(1, round(p.get('antilink_timeout', 3600)/3600))} ч)\n"
            f"{yn(p['antiraid'])} Анти-рейд ({p['antiraid_joins']}/{p['antiraid_interval']}с)\n"
            f"{yn(p['antinuke'])} Анти-нюк ({p['antinuke_limit']}/{p['antinuke_interval']}с)\n"
            f"{yn(p.get('antibot', True))} Антибот\n"
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
        self.add_view(PersistentPanelView())
        self.add_view(ReportPanelView())
        self.add_view(ShopPanelView())          # колбэк селекта читает конфиг
        # кнопки одобрения/отклонения отчётов работают после перезапуска
        self.add_dynamic_items(ApproveReport, DenyReport)

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
        await self.add_cog(Economy(self))

        # Мгновенная синхронизация на конкретный сервер, если задан GUILD_ID.
        # Глобальная синхронизация Discord обновляет команды у клиентов до часа,
        # а синхронизация на сервер — за секунды. Для одного сервера удобнее GUILD_ID.
        guild_id = os.getenv("GUILD_ID")
        if guild_id and guild_id.isdigit():
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Синхронизировано команд на сервер %s: %d", guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Синхронизировано глобальных команд: %d "
                     "(появятся у клиентов в течение ~часа)", len(synced))

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
