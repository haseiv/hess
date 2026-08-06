"""
Модуль защиты сервера:
  • Анти-спам      — мутит за флуд сообщениями
  • Анти-инвайт    — удаляет чужие приглашения в Discord
  • Анти-рейд      — реагирует на массовый заход ботов/аккаунтов
  • Анти-нюк        — ловит массовое удаление каналов/ролей и баны
"""

import time
import re
from collections import defaultdict, deque

import discord
from discord.ext import commands

from utils import storage

INVITE_RE = re.compile(r"(discord\.gg/|discord(app)?\.com/invite/)", re.IGNORECASE)


class Protection(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # трекеры в памяти (сбрасываются при перезапуске — это нормально)
        self._msg_times = defaultdict(lambda: deque(maxlen=25))       # (guild,user) -> времена сообщений
        self._joins = defaultdict(lambda: deque(maxlen=50))            # guild -> времена заходов
        self._nuke_actions = defaultdict(lambda: deque(maxlen=50))    # (guild,user) -> времена действий

    # ---------- вспомогательные ----------

    async def _log(self, guild, embed):
        cfg = storage.get_guild(guild.id)
        ch_id = cfg.get("log_channel")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

    def _is_whitelisted(self, member, cfg):
        if member.id == member.guild.owner_id:
            return True
        if member.id in cfg["protection"]["whitelist"]:
            return True
        # владельца бота и админов не трогаем автоматически? нет — админ может быть скомпрометирован,
        # но участников с whitelist пропускаем. Проверяем именно белый список.
        return False

    async def _find_actor(self, guild, action, target_id=None):
        """Определить, кто выполнил действие, через журнал аудита."""
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if target_id is None or (entry.target and entry.target.id == target_id):
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _punish_nuker(self, guild, user, reason):
        """Снять роли с нарушителя (мягко) — можно заменить на бан."""
        member = guild.get_member(user.id)
        if member is None:
            return
        try:
            # снимаем все роли, которые бот может снять
            removable = [r for r in member.roles if r < guild.me.top_role and not r.is_default()]
            if removable:
                await member.remove_roles(*removable, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            pass

        embed = discord.Embed(
            title="🛡️ Анти-нюк сработал",
            description=f"Пользователь {member.mention} (`{member.id}`) выполнял опасные действия "
                        f"слишком часто. Роли сняты.\n**Причина:** {reason}",
            color=discord.Color.red(),
        )
        await self._log(guild, embed)

    # ---------- анти-спам и анти-инвайт ----------

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        cfg = storage.get_guild(message.guild.id)
        member = message.author

        # даём пройти админам/whitelist
        if self._is_whitelisted(member, cfg) or member.guild_permissions.administrator:
            return

        prot = cfg["protection"]

        # --- анти-инвайт ---
        if prot["antiinvite"] and INVITE_RE.search(message.content):
            try:
                await message.delete()
                await message.channel.send(
                    f"{member.mention}, приглашения на другие серверы запрещены.",
                    delete_after=5,
                )
            except discord.HTTPException:
                pass
            embed = discord.Embed(
                title="🔗 Удалено приглашение",
                description=f"**Автор:** {member.mention}\n**Канал:** {message.channel.mention}",
                color=discord.Color.orange(),
            )
            await self._log(message.guild, embed)
            return

        # --- анти-спам ---
        if prot["antispam"]:
            key = (message.guild.id, member.id)
            now = time.time()
            times = self._msg_times[key]
            times.append(now)
            window = [t for t in times if now - t <= prot["antispam_interval"]]
            if len(window) >= prot["antispam_limit"]:
                times.clear()
                try:
                    await member.timeout(
                        discord.utils.utcnow() + discord.timedelta(seconds=prot["antispam_timeout"]),
                        reason="Анти-спам: флуд сообщениями",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
                try:
                    await message.channel.purge(
                        limit=prot["antispam_limit"] + 2,
                        check=lambda m: m.author.id == member.id,
                    )
                except discord.HTTPException:
                    pass
                embed = discord.Embed(
                    title="🚫 Анти-спам",
                    description=f"{member.mention} замучен за флуд "
                                f"({prot['antispam_limit']} сообщ. за {prot['antispam_interval']} с).",
                    color=discord.Color.red(),
                )
                await self._log(message.guild, embed)

    # ---------- анти-рейд ----------

    @commands.Cog.listener()
    async def on_member_join(self, member):
        cfg = storage.get_guild(member.guild.id)
        prot = cfg["protection"]
        if not prot["antiraid"]:
            return

        now = time.time()
        joins = self._joins[member.guild.id]
        joins.append(now)
        recent = [t for t in joins if now - t <= prot["antiraid_interval"]]

        if len(recent) >= prot["antiraid_joins"]:
            # поднимаем уровень проверки на максимум как быстрый барьер
            try:
                await member.guild.edit(
                    verification_level=discord.VerificationLevel.highest,
                    reason="Анти-рейд: массовый заход",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            embed = discord.Embed(
                title="⚠️ Возможный рейд!",
                description=f"За {prot['antiraid_interval']} с зашло {len(recent)} участников.\n"
                            f"Уровень проверки поднят до максимума. Проверьте новых участников.",
                color=discord.Color.dark_red(),
            )
            await self._log(member.guild, embed)

    # ---------- анти-нюк ----------

    async def _register_nuke_action(self, guild, actor, kind):
        cfg = storage.get_guild(guild.id)
        prot = cfg["protection"]
        if not prot["antinuke"] or actor is None or actor.bot:
            return
        if actor.id == guild.owner_id or actor.id in prot["whitelist"] or actor.id == self.bot.user.id:
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


async def setup(bot):
    await bot.add_cog(Protection(bot))
