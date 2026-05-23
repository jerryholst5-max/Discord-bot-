import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
import io
from datetime import datetime, timedelta, timezone
from collections import defaultdict

OWNER_ID = 1408144132966322407

# ── In-Memory Config Cache (reduziert DB-Calls) ──
_config_cache = None
_config_cache_dirty = False

async def load_config():
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    db = get_db()
    doc = await db["config"].find_one({"_id": "config"})
    _config_cache = doc.get("data", {"alert_users": []}) if doc else {"alert_users": []}
    return _config_cache

async def save_config(data):
    global _config_cache
    _config_cache = data
    db = get_db()
    await db["config"].update_one({"_id": "config"}, {"$set": {"data": data}}, upsert=True)


def is_owner(user: discord.Member) -> bool:
    return user.id == OWNER_ID

def is_bot_owner(user) -> bool:
    """Gibt True zurück wenn der User der Bot-Owner ist."""
    return user.id == OWNER_ID

def has_permission(interaction: discord.Interaction, perm: str = "administrator") -> bool:
    """
    Gibt True zurück wenn:
    - Der User der Bot-Owner ist (OWNER_ID) → hat immer alle Rechte
    - Der User die angegebene Guild-Permission hat
    """
    if is_bot_owner(interaction.user):
        return True
    return getattr(interaction.user.guild_permissions, perm, False)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.bans = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ── MongoDB persistent storage (async via Motor) ──
from motor.motor_asyncio import AsyncIOMotorClient

_mongo_client = None
_db = None

def get_db():
    global _mongo_client, _db
    if _db is None:
        mongo_url = os.environ.get("MONGODB_URL")
        if mongo_url:
            _mongo_client = AsyncIOMotorClient(mongo_url)
            _db = _mongo_client["germanyrpbot"]
        else:
            raise RuntimeError("MONGODB_URL nicht gesetzt!")
    return _db

async def load_warnings():
    db = get_db()
    doc = await db["warnings"].find_one({"_id": "warnings"})
    return doc.get("data", {}) if doc else {}

async def save_warnings(data):
    db = get_db()
    await db["warnings"].update_one({"_id": "warnings"}, {"$set": {"data": data}}, upsert=True)

async def load_config():
    db = get_db()
    doc = await db["config"].find_one({"_id": "config"})
    return doc.get("data", {"alert_users": []}) if doc else {"alert_users": []}

async def save_config(data):
    db = get_db()
    await db["config"].update_one({"_id": "config"}, {"$set": {"data": data}}, upsert=True)

async def load_tickets():
    db = get_db()
    doc = await db["tickets"].find_one({"_id": "tickets"})
    return doc.get("data", {}) if doc else {}

async def save_tickets(data):
    db = get_db()
    await db["tickets"].update_one({"_id": "tickets"}, {"$set": {"data": data}}, upsert=True)

async def add_team_stat(guild_id: int, user_id: int, user_name: str, stat_type: str):
    """
    Erhöht einen Stat-Zähler für einen User.
    stat_type: "tickets_closed" oder "supports_accepted"
    """
    db = get_db()
    key = f"{guild_id}_{user_id}"
    # Erst sicherstellen dass das Dokument existiert (mit 0-Werten)
    await db["team_stats"].update_one(
        {"_id": key},
        {
            "$setOnInsert": {
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "user_name": user_name,
                "tickets_closed": 0,
                "supports_accepted": 0,
            }
        },
        upsert=True
    )
    # Dann den Zähler erhöhen + user_name aktualisieren
    await db["team_stats"].update_one(
        {"_id": key},
        {
            "$inc": {stat_type: 1},
            "$set": {
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "user_name": user_name,
            }
        }
    )

async def get_team_stats(guild_id: int) -> list:
    """Gibt alle Team-Stats für eine Guild zurück, sortiert nach Gesamt-Aktivität."""
    db = get_db()
    cursor = db["team_stats"].find({"guild_id": str(guild_id)})
    stats = []
    async for doc in cursor:
        doc.setdefault("tickets_closed", 0)
        doc.setdefault("supports_accepted", 0)
        stats.append(doc)
    stats.sort(key=lambda x: x["tickets_closed"] + x["supports_accepted"], reverse=True)
    return stats

warnings_data = {}
config_data = {"alert_users": []}

NUKE_WINDOW = 10
NUKE_THRESHOLD = 3
nuke_tracker = defaultdict(lambda: defaultdict(list))

# ─────────────────────────────────────────────
# Liquid-Glass Embed Helper
# ─────────────────────────────────────────────

def liquid_glass_embed(title: str, description: str = "", color: discord.Color = None, fields: list = None) -> discord.Embed:
    if color is None:
        color = discord.Color.from_rgb(140, 210, 255)

    divider = "```\n" + "┈" * 34 + "\n```"
    structured_desc = (description + "\n" + divider) if description else divider

    embed = discord.Embed(
        title=f"💠  {title}",
        description=structured_desc,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )

    if fields:
        for name, value, inline in fields:
            embed.add_field(name=f"▸  {name}", value=value, inline=inline)

    embed.set_footer(text="◆ GermanyRP • System  ◆")
    return embed


# ─────────────────────────────────────────────
# Anti-Nuke
# ─────────────────────────────────────────────

async def get_audit_executor(guild, action):
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            if (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 5:
                return entry.user
    except discord.Forbidden:
        pass
    return None

async def check_nuke(guild, user, action_label):
    if user is None or user.bot:
        return
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=NUKE_WINDOW)
    tracker = nuke_tracker[guild.id][user.id]
    tracker.append(now)
    nuke_tracker[guild.id][user.id] = [t for t in tracker if t > cutoff]
    count = len(nuke_tracker[guild.id][user.id])
    if count >= NUKE_THRESHOLD:
        nuke_tracker[guild.id][user.id] = []
        log_channel = guild.system_channel
        actions_taken = []
        try:
            member = guild.get_member(user.id)
            if member:
                bot_top_role = guild.me.top_role
                removable = [r for r in member.roles if r.name != "@everyone" and r < bot_top_role]
                if removable:
                    await member.remove_roles(*removable, reason="[Anti-Nuke]")
                    actions_taken.append("Rollen entfernt")
            try:
                await guild.ban(user, reason=f"[Anti-Nuke] {count}x {action_label}")
                actions_taken.append("Gebannt")
            except discord.Forbidden:
                actions_taken.append("Ban fehlgeschlagen")
            msg = f"Anti-Nuke: {user} - {count}x {action_label} - {', '.join(actions_taken)}"
        except Exception:
            msg = f"Anti-Nuke: {user} - Fehler"
        if log_channel:
            await log_channel.send(msg)

@bot.event
async def on_member_ban(guild, user):
    executor = await get_audit_executor(guild, discord.AuditLogAction.ban)
    await check_nuke(guild, executor, "Ban")
    # Ban Log
    cfg = await load_config()
    ch_id = cfg.get("logs", {}).get(str(guild.id), {}).get("ban_log")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            embed = discord.Embed(title="🔨 User gebannt", color=discord.Color.from_rgb(220, 60, 60), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            if executor:
                embed.add_field(name="Moderator", value=str(executor), inline=False)
            await ch.send(embed=embed)

@bot.event
async def on_member_remove(member):
    executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick)
    if executor:
        await check_nuke(member.guild, executor, "Kick")
    guild = member.guild
    cfg = await load_config()
    log_cfg = cfg.get("logs", {}).get(str(guild.id), {})
    # Leave Log
    ch_id = log_cfg.get("join_leave_log")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            embed = discord.Embed(title="👋 Mitglied verlassen", color=discord.Color.from_rgb(220, 80, 80), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=f"{member} ({member.id})", inline=False)
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
    # Abschiedsnachricht
    bye_cfg = cfg.get("goodbye", {}).get(str(guild.id), {})
    bye_ch_id = bye_cfg.get("channel_id")
    if bye_ch_id:
        bye_ch = guild.get_channel(int(bye_ch_id))
        if bye_ch:
            msg = bye_cfg.get("message", f"**{member}** hat den Server verlassen. 👋")
            msg = msg.replace("{user}", member.mention).replace("{name}", member.display_name).replace("{server}", guild.name).replace("{count}", str(guild.member_count))
            embed = discord.Embed(title="👋 Auf Wiedersehen!", description=msg, color=discord.Color.from_rgb(220, 80, 80))
            embed.set_thumbnail(url=member.display_avatar.url)
            image_url = bye_cfg.get("image_url")
            if image_url:
                embed.set_image(url=image_url)
            await bye_ch.send(embed=embed)


    executor = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete)
    await check_nuke(channel.guild, executor, "Channel-Loeschung")

@bot.event
async def on_guild_role_delete(role):
    executor = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete)
    await check_nuke(role.guild, executor, "Rollen-Loeschung")


@bot.event
async def on_ready():
    global warnings_data, config_data
    warnings_data = await load_warnings()
    config_data = await load_config()

    # Persistente Views registrieren
    try:
        bot.add_view(TicketView())
    except Exception as e:
        print(f"TicketView Fehler: {e}")
    try:
        bot.add_view(TicketCloseView())
    except Exception as e:
        print(f"TicketCloseView Fehler: {e}")

    # Slash Commands synchronisieren (nach Views!)
    try:
        synced = await tree.sync()
        print(f"Slash Commands synchronisiert: {len(synced)} Commands")
    except Exception as e:
        print(f"Sync Fehler: {e}")

    # Ticket-Manager Rolle in allen Guilds erstellen falls nicht vorhanden
    for guild in bot.guilds:
        if not discord.utils.get(guild.roles, name="ticket-manager"):
            try:
                await guild.create_role(
                    name="ticket-manager",
                    color=discord.Color.from_rgb(255, 165, 0),
                    reason="Automatisch erstellt: Ticket-Manager Rolle"
                )
                print(f"Rolle 'ticket-manager' in '{guild.name}' erstellt.")
            except Exception as e:
                print(f"Konnte Rolle nicht erstellen: {e}")
    print(f"Bot ist online als {bot.user}")

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """Globaler Fehler-Handler für alle Slash Commands."""
    msg = f"❌ Ein Fehler ist aufgetreten: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    print(f"[SLASH ERROR] {interaction.command} → {error}")


# ─────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────

@tree.command(name="help", description="Zeigt alle Befehle")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    # Dynamisch alle Commands aus dem Tree lesen
    commands = tree.get_commands()
    commands.sort(key=lambda c: c.name)

    # Kategorien anhand von Command-Namen
    categories = {
        "⚙️ Moderation": [],
        "🎫 Tickets": [],
        "🔗 Anti-Link": [],
        "🎤 Voice-Support": [],
        "🎉 Gewinnspiel": [],
        "📋 Logs & Setup": [],
        "📊 Server & Info": [],
        "🔔 Alerts": [],
        "📝 Abmeldung": [],
        "🤖 Sonstiges": [],
    }

    for cmd in commands:
        n = cmd.name
        if any(x in n for x in ["kick", "ban", "mute", "unmute", "warn", "clear", "softban", "massban", "nick", "slowmode", "kanal-sperren", "unban"]):
            categories["⚙️ Moderation"].append(f"`/{n}`")
        elif any(x in n for x in ["ticket"]):
            categories["🎫 Tickets"].append(f"`/{n}`")
        elif any(x in n for x in ["antilink"]):
            categories["🔗 Anti-Link"].append(f"`/{n}`")
        elif any(x in n for x in ["voice"]):
            categories["🎤 Voice-Support"].append(f"`/{n}`")
        elif any(x in n for x in ["gewinnspiel"]):
            categories["🎉 Gewinnspiel"].append(f"`/{n}`")
        elif any(x in n for x in ["logs", "modlog", "willkommen", "abmeldung", "auto-rolle"]):
            categories["📋 Logs & Setup"].append(f"`/{n}`")
        elif any(x in n for x in ["serverstatus", "userinfo", "status", "ankündigung", "team"]):
            categories["📊 Server & Info"].append(f"`/{n}`")
        elif any(x in n for x in ["alerts"]):
            categories["🔔 Alerts"].append(f"`/{n}`")
        elif any(x in n for x in ["abmeldung"]):
            categories["📝 Abmeldung"].append(f"`/{n}`")
        else:
            categories["🤖 Sonstiges"].append(f"`/{n}`")

    embed = liquid_glass_embed(
        "📖 Bot Befehle",
        f"Alle verfügbaren Commands – insgesamt **{len(commands)}** Befehle.",
        discord.Color.from_rgb(100, 180, 255)
    )
    for cat, cmds in categories.items():
        if cmds:
            embed.add_field(name=cat, value=" ".join(cmds), inline=False)

    await interaction.followup.send(embed=embed)


# ─────────────────────────────────────────────
# /hallo
# ─────────────────────────────────────────────

@tree.command(name="hallo", description="Begruessung")
async def hallo(interaction: discord.Interaction):
    embed = liquid_glass_embed("Hallo!", f"Hey {interaction.user.mention} 👋")
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /embed  – Liquid Glass Embed senden (Admin)
# ─────────────────────────────────────────────

@tree.command(name="embed", description="Sendet eine Nachricht als Liquid Glass Embed")
@app_commands.describe(titel="Titel des Embeds", nachricht="Nachricht / Beschreibung")
async def embed_cmd(interaction: discord.Interaction, titel: str, nachricht: str):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    embed = liquid_glass_embed(titel, nachricht)
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url
    )
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /serverstatus  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="serverstatus", description="Zeigt den aktuellen Server-Status")
async def serverstatus(interaction: discord.Interaction):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    guild = interaction.guild

    total_members   = guild.member_count
    online_members  = sum(1 for m in guild.members if m.status != discord.Status.offline) if guild.members else "N/A"
    bots            = sum(1 for m in guild.members if m.bot)
    text_channels   = len(guild.text_channels)
    voice_channels  = len(guild.voice_channels)
    roles           = len(guild.roles) - 1  # @everyone raus
    boost_level     = guild.premium_tier
    boosts          = guild.premium_subscription_count
    created_at      = guild.created_at.strftime("%d.%m.%Y")

    embed = liquid_glass_embed(
        f"Server Status – {guild.name}",
        color=discord.Color.from_rgb(80, 210, 200)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="👥  Mitglieder",    value=f"`{total_members}`",    inline=True)
    embed.add_field(name="🟢  Online",        value=f"`{online_members}`",   inline=True)
    embed.add_field(name="🤖  Bots",          value=f"`{bots}`",             inline=True)
    embed.add_field(name="💬  Text-Channels", value=f"`{text_channels}`",    inline=True)
    embed.add_field(name="🔊  Voice-Channels",value=f"`{voice_channels}`",   inline=True)
    embed.add_field(name="🎭  Rollen",        value=f"`{roles}`",            inline=True)
    embed.add_field(name="✨  Boost Level",   value=f"`{boost_level}`",      inline=True)
    embed.add_field(name="🚀  Boosts",        value=f"`{boosts}`",           inline=True)
    embed.add_field(name="📅  Erstellt am",   value=f"`{created_at}`",       inline=True)

    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /kick  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="kick", description="Kickt einen User vom Server")
@app_commands.describe(member="Der User", grund="Grund")
async def kick(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund"):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    try:
        await member.kick(reason=grund)
        embed = liquid_glass_embed(
            "Kick",
            f"**{member}** wurde vom Server gekickt.\n**Grund:** {grund}",
            discord.Color.from_rgb(255, 160, 100)
        )
        embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("Fehlende Berechtigung.", ephemeral=True)

# ─────────────────────────────────────────────
# /teamkick  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="teamkick", description="Entfernt alle Rollen eines Users")
@app_commands.describe(member="Der User")
async def teamkick(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    roles = [r for r in member.roles if r.name != "@everyone"]
    if not roles:
        await interaction.response.send_message(f"**{member}** hat keine Rollen.")
        return
    await member.remove_roles(*roles)
    embed = liquid_glass_embed(
        "Team-Kick",
        f"Alle Rollen von **{member}** wurden entfernt.",
        discord.Color.from_rgb(255, 140, 80)
    )
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /tempmute  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="tempmute", description="Schaltet einen User stumm")
@app_commands.describe(member="Der User", minuten="Dauer in Minuten", grund="Grund")
async def tempmute(interaction: discord.Interaction, member: discord.Member, minuten: int, grund: str = "Kein Grund"):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    until = datetime.now(timezone.utc) + timedelta(minutes=minuten)
    try:
        await member.timeout(until, reason=grund)
        embed = liquid_glass_embed(
            "Temp-Mute",
            f"**{member}** wurde für **{minuten} Minuten** stummgeschaltet.\n**Grund:** {grund}",
            discord.Color.from_rgb(200, 100, 240)
        )
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("Fehlende Berechtigung.", ephemeral=True)

# ─────────────────────────────────────────────
# /unmute  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="unmute", description="Hebt den Mute auf")
@app_commands.describe(member="Der User")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    try:
        await member.timeout(None)
        embed = liquid_glass_embed(
            "Unmute",
            f"Der Mute von **{member}** wurde aufgehoben.",
            discord.Color.from_rgb(100, 220, 150)
        )
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("Fehlende Berechtigung.", ephemeral=True)

# ─────────────────────────────────────────────
# /teamwarn  – KEIN Ping (Admin)
# ─────────────────────────────────────────────

@tree.command(name="teamwarn", description="Verwarnt einen User (kein Ping)")
@app_commands.describe(member="Der User", grund="Grund")
async def teamwarn(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund"):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    user_id = str(member.id)
    if user_id not in warnings_data:
        warnings_data[user_id] = []
    warnings_data[user_id].append({
        "reason": grund,
        "by": str(interaction.user),
        "at": datetime.now(timezone.utc).isoformat()
    })
    await save_warnings(warnings_data)
    count = len(warnings_data[user_id])

    # Kein mention – stattdessen nur Name#Discriminator
    embed = liquid_glass_embed(
        "Verwarnung",
        f"**{member}** wurde verwarnt.\n**Grund:** {grund}\n**Verwarnung Nr.:** {count}",
        discord.Color.from_rgb(255, 200, 60)
    )
    embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    if count >= 3:
        try:
            await member.ban(reason=f"Auto-Ban nach {count} Verwarnungen")
            warnings_data[user_id] = []
            await save_warnings(warnings_data)
            ban_embed = liquid_glass_embed(
                "Auto-Ban",
                f"**{member}** wurde nach {count} Verwarnungen automatisch gebannt.",
                discord.Color.from_rgb(220, 60, 60)
            )
            await interaction.followup.send(embed=ban_embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await interaction.followup.send("Fehlende Berechtigung zum Bannen.")

# ─────────────────────────────────────────────
# /warnings  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="warnings", description="Zeigt Verwarnungen eines Users")
@app_commands.describe(member="Der User")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    user_warnings = warnings_data.get(str(member.id), [])
    if not user_warnings:
        await interaction.response.send_message(f"**{member}** hat keine Verwarnungen.")
        return
    embed = liquid_glass_embed(
        f"Verwarnungen – {member}",
        f"Insgesamt **{len(user_warnings)}** Verwarnung(en).",
        discord.Color.from_rgb(255, 180, 50)
    )
    for i, w in enumerate(user_warnings, 1):
        embed.add_field(name=f"#{i} – {w['reason']}", value=f"von {w['by']}", inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

# ─────────────────────────────────────────────
# /allwarnings  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="allwarnings", description="Alle aktiven Verwarnungen")
async def allwarnings(interaction: discord.Interaction):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    active = {uid: w for uid, w in warnings_data.items() if w}
    if not active:
        await interaction.response.send_message("Keine aktiven Verwarnungen.")
        return
    embed = liquid_glass_embed("Alle Verwarnungen", color=discord.Color.from_rgb(255, 160, 60))
    for uid, warns in sorted(active.items(), key=lambda x: len(x[1]), reverse=True):
        try:
            u = await bot.fetch_user(int(uid))
            name = str(u)
        except Exception:
            name = uid
        embed.add_field(name=f"{name} – {len(warns)}x", value=warns[-1]["reason"], inline=False)
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /clearwarnings  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="clearwarnings", description="Loescht alle Verwarnungen eines Users")
@app_commands.describe(member="Der User")
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    user_id = str(member.id)
    if not warnings_data.get(user_id):
        await interaction.response.send_message(f"**{member}** hat keine Verwarnungen.")
        return
    count = len(warnings_data[user_id])
    warnings_data[user_id] = []
    await save_warnings(warnings_data)
    embed = liquid_glass_embed(
        "Verwarnungen gelöscht",
        f"**{count}** Verwarnung(en) von **{member}** wurden gelöscht.",
        discord.Color.from_rgb(100, 220, 150)
    )
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

# ─────────────────────────────────────────────
# /bann  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="bann", description="Bannt einen User")
@app_commands.describe(member="Der User", grund="Grund")
async def bann(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund"):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.response.send_message("Der Eigentuemer ist immun!", ephemeral=True)
        return
    try:
        await member.ban(reason=grund)
        embed = liquid_glass_embed(
            "Ban",
            f"**{member}** wurde gebannt.\n**Grund:** {grund}",
            discord.Color.from_rgb(220, 60, 60)
        )
        embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("Fehlende Berechtigung.", ephemeral=True)

# ─────────────────────────────────────────────
# /unban  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="unban", description="Entbannt einen User per ID")
@app_commands.describe(user_id="Die User-ID")
async def unban(interaction: discord.Interaction, user_id: str):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        embed = liquid_glass_embed(
            "Unban",
            f"**{user}** wurde entbannt.",
            discord.Color.from_rgb(100, 220, 150)
        )
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("Fehler beim Entbannen.", ephemeral=True)

# ─────────────────────────────────────────────
# /setalerts & /removealerts
# ─────────────────────────────────────────────

@tree.command(name="setalerts", description="DM-Alerts aktivieren")
async def setalerts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid in config_data["alert_users"]:
        await interaction.response.send_message("Du hast bereits Alerts.", ephemeral=True)
        return
    config_data["alert_users"].append(uid)
    await save_config(config_data)
    await interaction.response.send_message("✅ DM-Alerts aktiviert!", ephemeral=True)

@tree.command(name="removealerts", description="DM-Alerts deaktivieren")
async def removealerts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in config_data["alert_users"]:
        await interaction.response.send_message("Keine aktiven Alerts.", ephemeral=True)
        return
    config_data["alert_users"].remove(uid)
    await save_config(config_data)
    await interaction.response.send_message("🔕 DM-Alerts deaktiviert.", ephemeral=True)

# ─────────────────────────────────────────────
# /clear  (Admin) – alle oder User-Nachrichten löschen
# ─────────────────────────────────────────────

@tree.command(name="clear", description="Löscht Nachrichten im Channel (optional: nur von einem User)")
@app_commands.describe(anzahl="Anzahl der Nachrichten (max. 100)", member="Nur Nachrichten dieses Users löschen")
async def clear(interaction: discord.Interaction, anzahl: int = 100, member: discord.Member = None):
    if not has_permission(interaction, "manage_messages"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if anzahl > 100:
        anzahl = 100
    if member:
        deleted = []
        async for msg in interaction.channel.history(limit=200):
            if msg.author == member:
                deleted.append(msg)
            if len(deleted) >= anzahl:
                break
        for msg in deleted:
            try:
                await msg.delete()
                await asyncio.sleep(0.5)
            except Exception:
                pass
        count = len(deleted)
        embed = liquid_glass_embed(
            "Clear",
            f"**{count}** Nachricht(en) von **{member}** wurden gelöscht.",
            discord.Color.from_rgb(255, 100, 100)
        )
    else:
        deleted = await interaction.channel.purge(limit=anzahl)
        count = len(deleted)
        embed = liquid_glass_embed(
            "Clear",
            f"**{count}** Nachricht(en) wurden gelöscht.",
            discord.Color.from_rgb(255, 100, 100)
        )
    await interaction.followup.send(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
# /userinfo
# ─────────────────────────────────────────────

@tree.command(name="userinfo", description="Zeigt Infos über einen User")
@app_commands.describe(member="Der User (leer = du selbst)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
    joined = member.joined_at.strftime("%d.%m.%Y %H:%M") if member.joined_at else "Unbekannt"
    created = member.created_at.strftime("%d.%m.%Y %H:%M")
    embed = liquid_glass_embed(
        f"Userinfo – {member}",
        color=discord.Color.from_rgb(130, 200, 240)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🆔  ID",             value=f"`{member.id}`",          inline=True)
    embed.add_field(name="📛  Name",            value=str(member),               inline=True)
    embed.add_field(name="🤖  Bot",             value="Ja" if member.bot else "Nein", inline=True)
    embed.add_field(name="📅  Account erstellt",value=f"`{created}`",            inline=True)
    embed.add_field(name="📥  Server beigetreten", value=f"`{joined}`",          inline=True)
    embed.add_field(name="🎭  Rollen",          value=", ".join(roles) if roles else "Keine", inline=False)
    await interaction.response.send_message(embed=embed)

# ─────────────────────────────────────────────
# /status  – Bot-Status setzen (Owner only)
# ─────────────────────────────────────────────

@tree.command(name="status", description="Setzt den Bot-Status (nur Owner)")
@app_commands.describe(text="Status-Text", typ="Typ: playing / watching / listening / streaming")
@app_commands.choices(typ=[
    app_commands.Choice(name="playing",   value="playing"),
    app_commands.Choice(name="watching",  value="watching"),
    app_commands.Choice(name="listening", value="listening"),
    app_commands.Choice(name="streaming", value="streaming"),
])
async def status_cmd(interaction: discord.Interaction, text: str, typ: str = "playing"):
    if not is_owner(interaction.user):
        await interaction.response.send_message("Nur der Owner kann den Status ändern!", ephemeral=True)
        return
    activity_map = {
        "playing":   discord.Game(name=text),
        "watching":  discord.Activity(type=discord.ActivityType.watching,  name=text),
        "listening": discord.Activity(type=discord.ActivityType.listening, name=text),
        "streaming": discord.Streaming(name=text, url="https://twitch.tv/placeholder"),
    }
    await bot.change_presence(activity=activity_map.get(typ, discord.Game(name=text)))
    embed = liquid_glass_embed("Status geändert", f"Status: **{typ}** `{text}`", discord.Color.from_rgb(100, 220, 150))
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
# /ankündigung  (Admin)
# ─────────────────────────────────────────────

@tree.command(name="ankündigung", description="Schickt eine Ankündigung in einen bestimmten Kanal")
@app_commands.describe(
    kanal="Ziel-Kanal",
    titel="Titel der Ankündigung",
    nachricht="Inhalt der Ankündigung",
    ping_rolle="Rolle die gepingt werden soll (optional)"
)
async def ankuendigung(interaction: discord.Interaction, kanal: discord.TextChannel, titel: str, nachricht: str, ping_rolle: discord.Role = None):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    embed = liquid_glass_embed(titel, nachricht, discord.Color.from_rgb(255, 200, 60))
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="📢 Ankündigung")
    ping_content = ping_rolle.mention if ping_rolle else None
    await kanal.send(content=ping_content, embed=embed)
    await interaction.followup.send(f"✅ Ankündigung wurde in {kanal.mention} gesendet!", ephemeral=True)

# ─────────────────────────────────────────────
# Anti-Link System
# ─────────────────────────────────────────────

import re
LINK_PATTERN = re.compile(r"https?://|discord\.gg/|www\.", re.IGNORECASE)

async def get_antilink(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("antilink", {}).get(str(guild_id), {
        "enabled": False,
        "timeout_minutes": 5,
        "delete_message": True,
        "ignored_users": [],
        "ignored_roles": []
    })

async def save_antilink(guild_id: int, data: dict):
    cfg = await load_config()
    if "antilink" not in cfg:
        cfg["antilink"] = {}
    cfg["antilink"][str(guild_id)] = data
    await save_config(cfg)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    settings = await get_antilink(message.guild.id)

    if settings.get("enabled"):
        # Check ignored users
        if message.author.id not in settings.get("ignored_users", []):
            # Check ignored roles
            member_role_ids = [r.id for r in message.author.roles]
            ignored_roles = settings.get("ignored_roles", [])
            if not any(r in member_role_ids for r in ignored_roles):
                if LINK_PATTERN.search(message.content):
                    # Delete message
                    if settings.get("delete_message", True):
                        try:
                            await message.delete()
                        except Exception:
                            pass
                    # Timeout user
                    minutes = settings.get("timeout_minutes", 5)
                    if minutes > 0:
                        try:
                            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                            await message.author.timeout(until, reason="Anti-Link: Link gesendet")
                        except Exception:
                            pass
                    # Warn in channel
                    embed = liquid_glass_embed(
                        "🔗 Anti-Link",
                        f"{message.author.mention} Links sind auf diesem Server nicht erlaubt!\nTimeout: **{minutes} Minuten**",
                        discord.Color.from_rgb(255, 80, 80)
                    )
                    try:
                        warn_msg = await message.channel.send(embed=embed)
                        await asyncio.sleep(5)
                        await warn_msg.delete()
                    except Exception:
                        pass

    await bot.process_commands(message)

# ─────────────────────────────────────────────
# /antilink setup
# ─────────────────────────────────────────────

@tree.command(name="antilink", description="Anti-Link System einstellen")
@app_commands.describe(
    aktiv="Anti-Link aktivieren oder deaktivieren",
    timeout_minuten="Wie lange wird der User getimeoutet (0 = kein Timeout)",
    nachricht_loeschen="Soll die Nachricht mit dem Link gelöscht werden?",
)
async def antilink(
    interaction: discord.Interaction,
    aktiv: bool,
    timeout_minuten: int = 5,
    nachricht_loeschen: bool = True,
):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = await get_antilink(interaction.guild.id)
    settings["enabled"] = aktiv
    settings["timeout_minutes"] = timeout_minuten
    settings["delete_message"] = nachricht_loeschen
    await save_antilink(interaction.guild.id, settings)
    status = "✅ Aktiviert" if aktiv else "❌ Deaktiviert"
    embed = liquid_glass_embed(
        "Anti-Link Einstellungen",
        f"**Status:** {status}\n**Timeout:** {timeout_minuten} Minuten\n**Nachricht löschen:** {'Ja' if nachricht_loeschen else 'Nein'}",
        discord.Color.from_rgb(100, 220, 150) if aktiv else discord.Color.from_rgb(255, 80, 80)
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-ignore-user", description="User vom Anti-Link System ignorieren/entfernen")
@app_commands.describe(member="Der User", aktion="Hinzufügen oder entfernen")
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen",  value="remove"),
])
async def antilink_ignore_user(interaction: discord.Interaction, member: discord.Member, aktion: str):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = await get_antilink(interaction.guild.id)
    ignored = settings.get("ignored_users", [])
    if aktion == "add":
        if member.id not in ignored:
            ignored.append(member.id)
        msg = f"**{member}** wird jetzt ignoriert."
    else:
        ignored = [u for u in ignored if u != member.id]
        msg = f"**{member}** wird nicht mehr ignoriert."
    settings["ignored_users"] = ignored
    await save_antilink(interaction.guild.id, settings)
    embed = liquid_glass_embed("Anti-Link • User", msg, discord.Color.from_rgb(130, 200, 240))
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-ignore-rolle", description="Rolle vom Anti-Link System ignorieren/entfernen")
@app_commands.describe(rolle="Die Rolle", aktion="Hinzufügen oder entfernen")
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen",  value="remove"),
])
async def antilink_ignore_rolle(interaction: discord.Interaction, rolle: discord.Role, aktion: str):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = await get_antilink(interaction.guild.id)
    ignored = settings.get("ignored_roles", [])
    if aktion == "add":
        if rolle.id not in ignored:
            ignored.append(rolle.id)
        msg = f"**{rolle.name}** wird jetzt ignoriert."
    else:
        ignored = [r for r in ignored if r != rolle.id]
        msg = f"**{rolle.name}** wird nicht mehr ignoriert."
    settings["ignored_roles"] = ignored
    await save_antilink(interaction.guild.id, settings)
    embed = liquid_glass_embed("Anti-Link • Rolle", msg, discord.Color.from_rgb(130, 200, 240))
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-status", description="Zeigt die aktuellen Anti-Link Einstellungen")
async def antilink_status(interaction: discord.Interaction):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = await get_antilink(interaction.guild.id)
    enabled = settings.get("enabled", False)
    timeout = settings.get("timeout_minutes", 5)
    delete = settings.get("delete_message", True)
    ignored_users = settings.get("ignored_users", [])
    ignored_roles = settings.get("ignored_roles", [])
    users_str = ", ".join(f"<@{u}>" for u in ignored_users) if ignored_users else "Keine"
    roles_str = ", ".join(f"<@&{r}>" for r in ignored_roles) if ignored_roles else "Keine"
    embed = liquid_glass_embed(
        "Anti-Link Status",
        f"**Status:** {'✅ Aktiv' if enabled else '❌ Inaktiv'}\n**Timeout:** {timeout} Minuten\n**Nachrichten löschen:** {'Ja' if delete else 'Nein'}\n**Ignorierte User:** {users_str}\n**Ignorierte Rollen:** {roles_str}",
        discord.Color.from_rgb(130, 200, 240)
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
# Ticket System
# ─────────────────────────────────────────────


TICKET_CATEGORIES = {
    "allgemeine_frage":       ("❓", "Allgemeine Frage",       "Stelle eine allgemeine Frage"),
    "support":                ("🛠️", "Support",                "Erhalte technischen Support"),
    "support_owner":          ("👑", "Support Owner",          "Direkte Unterstützung vom Owner"),
    "report":                 ("🚨", "Report",                 "Melde einen Spieler"),
    "unban_antrag":           ("🔓", "Unban-Antrag",           "Stelle einen Unban-Antrag"),
    "partner_bewerbung":      ("🤝", "Partner-Bewerbung",      "Bewirb dich als Partner"),
    "fraktions_bewerbung":    ("🚔", "Fraktions-Bewerbung",     "Bewirb dich bei einer Fraktion"),
}



async def get_ticket_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("ticket_config", {}).get(str(guild_id), {})

async def save_ticket_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "ticket_config" not in cfg:
        cfg["ticket_config"] = {}
    cfg["ticket_config"][str(guild_id)] = data
    await save_config(cfg)

# ── Close Button View ──

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Annehmen", style=discord.ButtonStyle.success, custom_id="ticket_accept")
    async def accept_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await get_ticket_config(interaction.guild.id)
        support_role_id = cfg.get("support_role")
        support_role = interaction.guild.get_role(int(support_role_id)) if support_role_id else None

        # Check if user has support role or manage_channels
        can_accept = (
            is_bot_owner(interaction.user)
            or interaction.user.guild_permissions.manage_channels
            or (support_role and support_role in interaction.user.roles)
        )

        if not can_accept:
            await interaction.response.send_message("Du hast keine Berechtigung dieses Ticket anzunehmen!", ephemeral=True)
            return

        # Disable accept button
        button.disabled = True
        button.label = f"✅ Angenommen von {interaction.user.display_name}"
        await interaction.message.edit(view=self)

        embed = liquid_glass_embed(
            "✅ Ticket angenommen",
            f"Dieses Ticket wurde von {interaction.user.mention} angenommen.",
            discord.Color.from_rgb(100, 220, 150)
        )
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="🔒 Ticket schließen", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_permission(interaction, "manage_channels"):
            await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
            return

        tickets = await load_tickets()
        guild_id = str(interaction.guild.id)
        channel_id = str(interaction.channel.id)

        ticket_data = None
        ticket_key = None
        channel_id_str = str(interaction.channel.id)

        for key, t in tickets.get(guild_id, {}).items():
            # Match by channel_id field OR by key (both formats)
            if str(t.get("channel_id", "")) == channel_id_str or key == channel_id_str:
                ticket_data = t
                ticket_key = key
                break

        # If still not found, create a minimal ticket_data so closing still works
        if not ticket_data:
            ticket_data = {
                "channel_id": channel_id_str,
                "user_id": interaction.user.id,
                "category": "Unbekannt",
            }
            ticket_key = channel_id_str

        await interaction.response.send_message(
            embed=liquid_glass_embed("🔒 Ticket schließen", "Wie soll das Ticket geschlossen werden?",
                                     discord.Color.from_rgb(255, 150, 50)),
            view=TicketCloseActionView(ticket_data, ticket_key),
            ephemeral=True
        )

class TicketCloseActionView(discord.ui.View):
    """
    Persistente View für das Schließen-Menü.
    ticket_key wird in custom_id eingebettet damit es nach Neustart funktioniert.
    Format: "tca_delete:<ticket_key>" / "tca_archive:<ticket_key>"
    """
    def __init__(self, ticket_data, ticket_key):
        super().__init__(timeout=None)
        self.ticket_data = ticket_data
        self.ticket_key = ticket_key

        del_btn = discord.ui.Button(
            label="🗑️ Löschen",
            style=discord.ButtonStyle.danger,
            custom_id=f"tca_delete:{ticket_key}"
        )
        arc_btn = discord.ui.Button(
            label="📁 Archivieren",
            style=discord.ButtonStyle.secondary,
            custom_id=f"tca_archive:{ticket_key}"
        )
        async def do_delete(i): await self._close(i, delete=True)
        async def do_archive(i): await self._close(i, delete=False)
        del_btn.callback = do_delete
        arc_btn.callback = do_archive
        self.add_item(del_btn)
        self.add_item(arc_btn)

    async def _close(self, interaction: discord.Interaction, delete: bool):
        """
        Komplett neu geschrieben - benutzt NUR channel.send() statt interaction.
        So kann kein Discord-Interaction-Timeout mehr den Lösch/Archiv-Vorgang blockieren.
        """
        channel = interaction.channel
        guild = interaction.guild
        guild_id = str(guild.id)

        # Interaction sofort bestätigen damit Discord keinen Fehler zeigt
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        try:
            await interaction.followup.send(
                "⏳ Ticket wird geschlossen..." if delete else "⏳ Ticket wird archiviert...",
                ephemeral=True
            )
        except Exception:
            pass

        # Ab hier: alles über channel direkt, KEINE Interaction mehr
        cfg = await get_ticket_config(guild.id)

        # Ticket-Daten frisch aus DB laden
        channel_id_str = str(channel.id)
        ticket_data = {"channel_id": channel_id_str, "user_id": 0, "category": "Unbekannt"}
        ticket_key = channel_id_str
        tickets_db = await load_tickets()
        for key, t in tickets_db.get(guild_id, {}).items():
            if str(t.get("channel_id", "")) == channel_id_str or key == channel_id_str:
                ticket_data = t
                ticket_key = key
                break

        # Transcript bauen
        import io as _io
        transcript_lines = [f"📄 Transcript – {channel.name}\n" + "="*40 + "\n"]
        try:
            async for msg in channel.history(limit=200, oldest_first=True):
                if not msg.author.bot:
                    transcript_lines.append(
                        f"[{msg.created_at.strftime('%d.%m.%Y %H:%M')}] {msg.author}: {msg.content}"
                    )
        except Exception:
            pass
        transcript_text = "\n".join(transcript_lines)

        # Transcript senden
        transcript_channel_id = cfg.get("transcript_channel")
        if transcript_channel_id:
            tc = guild.get_channel(int(transcript_channel_id))
            if tc:
                try:
                    user_id = ticket_data.get('user_id')
                    kategorie = ticket_data.get('category')
                    embed = liquid_glass_embed(
                        f"📄 Transcript – {channel.name}",
                        f"**Geöffnet von:** <@{user_id}>\n**Kategorie:** {kategorie}\n**Geschlossen von:** {interaction.user.mention}",
                        discord.Color.from_rgb(130, 200, 240)
                    )
                    file = discord.File(
                        fp=_io.BytesIO(transcript_text.encode("utf-8")),
                        filename=f"transcript-{channel.name}.txt"
                    )
                    await tc.send(embed=embed, file=file)
                except Exception as e:
                    print(f"[TICKET] Transcript Fehler: {e}")

        # Aus DB entfernen
        try:
            tickets = await load_tickets()
            if guild_id in tickets and ticket_key in tickets[guild_id]:
                del tickets[guild_id][ticket_key]
                await save_tickets(tickets)
        except Exception as e:
            print(f"[TICKET] DB Fehler: {e}")

        # Stat tracken
        try:
            await add_team_stat(guild.id, interaction.user.id, str(interaction.user), "tickets_closed")
        except Exception:
            pass

        # LÖSCHEN oder ARCHIVIEREN - direkt über channel, kein interaction nötig
        if delete:
            await asyncio.sleep(3)
            try:
                await channel.delete(reason=f"Ticket gelöscht von {interaction.user}")
            except Exception as e:
                print(f"[TICKET] Löschen fehlgeschlagen: {e}")
                try:
                    await channel.send(f"❌ Kanal konnte nicht gelöscht werden: {e}")
                except Exception:
                    pass
        else:
            archive_category_id = cfg.get("archive_category")
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            for role in guild.roles:
                if role.permissions.manage_channels:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
            try:
                await channel.edit(
                    name=f"archived-{channel.name}",
                    overwrites=overwrites,
                    category=guild.get_channel(int(archive_category_id)) if archive_category_id else channel.category,
                    reason=f"Ticket archiviert von {interaction.user}"
                )
                await channel.send(embed=liquid_glass_embed(
                    "📁 Archiviert",
                    f"Dieses Ticket wurde von {interaction.user.mention} archiviert.",
                    discord.Color.from_rgb(150, 150, 150)
                ))
            except Exception as e:
                print(f"[TICKET] Archivieren fehlgeschlagen: {e}")

# ── Category Buttons ──

async def create_ticket_for_category(interaction: discord.Interaction, category_key: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Lookup in Standard- UND Custom-Kategorien
        all_cats = await get_all_categories(guild.id)
        if category_key not in all_cats:
            await interaction.followup.send("❌ Kategorie nicht gefunden!", ephemeral=True)
            return
        emoji, label, _, restricted_role_id = all_cats[category_key]

        # BUG 1 FIX: Rollen-Beschränkung prüfen
        if restricted_role_id:
            restricted_role = guild.get_role(int(restricted_role_id))
            if restricted_role and restricted_role not in interaction.user.roles and not is_bot_owner(interaction.user):
                await interaction.followup.send(
                    f"❌ Diese Ticket-Kategorie ist nur für Mitglieder mit der Rolle **{restricted_role.name}** verfügbar!",
                    ephemeral=True
                )
                return

        tickets = await load_tickets()
        guild_id = str(guild.id)
        cfg = await get_ticket_config(guild.id)

        if guild_id not in tickets:
            tickets[guild_id] = {}

        ticket_category_id = cfg.get("ticket_category")
        ticket_category = None
        if ticket_category_id:
            try:
                ticket_category = guild.get_channel(int(ticket_category_id))
                if ticket_category is None:
                    ticket_category = await guild.fetch_channel(int(ticket_category_id))
            except Exception:
                ticket_category = None

        # BUG 4 FIX: Persistenter Zähler statt len() damit Nummern nie doppelt sind
        cfg_count = await get_ticket_config(guild.id)
        count = cfg_count.get("ticket_counter", 0) + 1
        cfg_count["ticket_counter"] = count
        await save_ticket_config(guild.id, cfg_count)
        channel_name = f"{category_key.replace('_', '-')}-{count:04d}"

        support_role_id = cfg.get("support_role")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if support_role_id:
            support_role = guild.get_role(int(support_role_id))
            if support_role:
                overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        owner = guild.get_member(OWNER_ID)
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=ticket_category,
            overwrites=overwrites,
            reason=f"Ticket von {interaction.user}"
        )

        tickets[guild_id][str(channel.id)] = {
            "channel_id": str(channel.id),
            "user_id": interaction.user.id,
            "category": label,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await save_tickets(tickets)

        embed = liquid_glass_embed(
            f"{emoji} {label}",
            f"Willkommen {interaction.user.mention}!\n\nBeschreibe dein Anliegen so genau wie möglich.\nUnser Team wird sich so schnell wie möglich bei dir melden.\n\n**Kategorie:** {label}",
            discord.Color.from_rgb(130, 200, 240)
        )

        ping_role_id = cfg.get("ping_role")
        ping_text = f"<@&{ping_role_id}>" if ping_role_id else ""

        # Build content message with all pings
        mentions = [interaction.user.mention]
        if category_key == "support_owner" and owner:
            mentions.append(owner.mention)
        if ping_role_id:
            mentions.append(ping_text)
        content_msg = " ".join(mentions)

        await channel.send(content=content_msg, embed=embed, view=TicketCloseView())

        # Send notification to notification channel
        notification_channel_id = cfg.get("notification_channel")
        if notification_channel_id:
            try:
                notif_channel = guild.get_channel(int(notification_channel_id))
                if notif_channel:
                    notif_embed = liquid_glass_embed(
                        "🎫 Neues Ticket",
                        f"**User:** {interaction.user.mention}\n**Kategorie:** {label}\n**Kanal:** {channel.mention}",
                        discord.Color.from_rgb(255, 200, 60)
                    )
                    await notif_channel.send(content=ping_text if ping_role_id else None, embed=notif_embed)
            except Exception:
                pass

        await interaction.followup.send(f"✅ Dein Ticket wurde erstellt: {channel.mention}", ephemeral=True)

class TicketButton(discord.ui.Button):
    def __init__(self, category_key: str, emoji: str, label: str):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji,
            custom_id=f"ticket_btn_{category_key}"
        )
        self.category_key = category_key

    async def callback(self, interaction: discord.Interaction):
        await create_ticket_for_category(interaction, self.category_key)

async def get_all_categories(guild_id: int) -> dict:
    """Gibt Standard + Custom Kategorien zurück.
    Format: {key: (emoji, label, description, restricted_role_id_or_None)}
    Standard-Kategorien haben restricted_role=None.
    """
    cfg = await get_ticket_config(guild_id)
    custom = cfg.get("custom_categories", {})
    # Standard-Kategorien: füge None als restricted_role hinzu
    all_cats = {}
    for key, (emoji, label, desc) in TICKET_CATEGORIES.items():
        all_cats[key] = (emoji, label, desc, None)
    for key, data in custom.items():
        all_cats[key] = (data["emoji"], data["label"], data["description"], data.get("restricted_role"))
    return all_cats

class TicketView(discord.ui.View):
    def __init__(self, all_categories: dict = None):
        super().__init__(timeout=None)
        cats = all_categories if all_categories else {k: (e, l, d, None) for k, (e, l, d) in TICKET_CATEGORIES.items()}
        for key, cat_data in cats.items():
            emoji, label, desc = cat_data[0], cat_data[1], cat_data[2]
            self.add_item(TicketButton(key, emoji, label))

async def build_ticket_view(guild_id: int) -> TicketView:
    """Erstellt TicketView mit allen Kategorien (Standard + Custom) aus der DB"""
    all_cats = await get_all_categories(guild_id)
    return TicketView(all_cats)

async def send_ticket_panel(kanal: discord.TextChannel, guild_id: int) -> discord.Message:
    """Postet ein neues Ticket-Panel mit allen Kategorien"""
    embed = discord.Embed(
        title="🎫 Helpdesk",
        description=(
            "Herzlich Willkommen bei unserem Support! 🛡️\n\n"
            "**1)** Wähle unterhalb eine Kategorie aus\n"
            "**2)** Beschreibe um was es geht\n"
            "**3)** Dein Ticket wird erstellt und du wirst gepingt\n"
            "Gerne kannst du vorab schon weitere Informationen (Screenshots, ...) übermitteln\n\n"
            "**4)** Warte bis ein Supporter dein Ticket übernimmt"
        ),
        color=discord.Color.from_rgb(140, 210, 255),
    )
    embed.set_footer(text="GermanyRP • Support")
    view = await build_ticket_view(guild_id)
    return await kanal.send(embed=embed, view=view)

# ── /ticket-setup ──

@tree.command(name="ticket-setup", description="Richtet das Ticket-System ein (Admin)")
@app_commands.describe(
    kanal="Kanal wo das Ticket-Panel gepostet wird",
    support_rolle="Rolle die Tickets sehen kann",
    neue_kategorie="Neue Kategorie automatisch erstellen? (Ja = neue erstellen, Nein = vorhandene wählen)",
    ticket_kategorie="Vorhandene Kategorie für Tickets (nur wenn neue_kategorie=Nein)",
    archiv_kategorie="Kategorie für archivierte Tickets (leer = neue wird erstellt)",
    transcript_kanal="Kanal wo Transcripts gespeichert werden",
    ping_rolle="Rolle die bei neuen Tickets gepingt wird",
    benachrichtigungs_kanal="Kanal wo neue Ticket-Benachrichtigungen gesendet werden",
)
@app_commands.choices(neue_kategorie=[
    app_commands.Choice(name="Ja – Neue Kategorien erstellen", value="yes"),
    app_commands.Choice(name="Nein – Vorhandene Kategorie nutzen", value="no"),
])
async def ticket_setup(
    interaction: discord.Interaction,
    kanal: discord.TextChannel,
    support_rolle: discord.Role,
    neue_kategorie: str,
    transcript_kanal: discord.TextChannel,
    benachrichtigungs_kanal: discord.TextChannel,
    ticket_kategorie: discord.CategoryChannel = None,
    archiv_kategorie: discord.CategoryChannel = None,
    ping_rolle: discord.Role = None,
):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    try:
        if neue_kategorie == "yes":
            support_role_overwrite = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                support_rolle: discord.PermissionOverwrite(view_channel=True),
            }
            ticket_kategorie = await guild.create_category("🎫 | Tickets", overwrites=support_role_overwrite)
            archiv_kategorie = await guild.create_category("📁 | Archiv", overwrites={
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                support_rolle: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            })
        else:
            if not ticket_kategorie:
                await interaction.followup.send("❌ Du musst eine Ticket-Kategorie auswählen!", ephemeral=True)
                return
            if not archiv_kategorie:
                archiv_kategorie = await guild.create_category("📁 | Archiv", overwrites={
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    support_rolle: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                })
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler beim Einrichten: `{e}`", ephemeral=True)
        return

    # Save config
    await save_ticket_config(interaction.guild.id, {
        "support_role": str(support_rolle.id),
        "ticket_category": str(ticket_kategorie.id),
        "archive_category": str(archiv_kategorie.id),
        "transcript_channel": str(transcript_kanal.id),
        "ping_role": str(ping_rolle.id) if ping_rolle else None,
        "notification_channel": str(benachrichtigungs_kanal.id),
    })

    # Send panel
    panel_msg = await send_ticket_panel(kanal, interaction.guild.id)

    # Save panel message ID for future updates
    cfg2 = await get_ticket_config(interaction.guild.id)
    cfg2["panel_message_id"] = str(panel_msg.id)
    cfg2["panel_channel_id"] = str(kanal.id)
    await save_ticket_config(interaction.guild.id, cfg2)

    ping_info = f"\n**Ping-Rolle:** {ping_rolle.mention}" if ping_rolle else ""
    notif_info = f"\n**Benachrichtigungs-Kanal:** {benachrichtigungs_kanal.mention}" if benachrichtigungs_kanal else ""
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Ticket-System eingerichtet!",
            f"**Panel:** {kanal.mention}\n**Support-Rolle:** {support_rolle.mention}\n**Ticket-Kategorie:** {ticket_kategorie.name}\n**Archiv:** {archiv_kategorie.name}\n**Transcript-Kanal:** {transcript_kanal.mention}{ping_info}{notif_info}",
            discord.Color.from_rgb(100, 220, 150)
        )
    )

# ── Register persistent views on startup ──


# ─────────────────────────────────────────────
# /ticket-schliessen, /ticket-add, /ticket-remove
# ─────────────────────────────────────────────

@tree.command(name="ticket-schliessen", description="Schließt das aktuelle Ticket")
@app_commands.describe(aktion="Ticket löschen oder archivieren")
@app_commands.choices(aktion=[
    app_commands.Choice(name="🗑️ Löschen", value="delete"),
    app_commands.Choice(name="📁 Archivieren", value="archive"),
])
async def ticket_schliessen(interaction: discord.Interaction, aktion: str):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = await load_tickets()
    guild_id = str(interaction.guild.id)
    ticket_key = None
    ticket_data = None

    for key, t in tickets.get(guild_id, {}).items():
        if int(t.get("channel_id", 0)) == interaction.channel.id:
            ticket_data = t
            ticket_key = key
            break

    if not ticket_data:
        await interaction.response.send_message("❌ Dieser Kanal ist kein Ticket!", ephemeral=True)
        return

    await interaction.response.defer()

    guild = interaction.guild
    channel = interaction.channel
    cfg = await get_ticket_config(guild.id)

    # Build & send transcript
    transcript_lines = [f"📄 Transcript – {channel.name}\n{'='*40}\n"]
    async for msg in channel.history(limit=200, oldest_first=True):
        if not msg.author.bot:
            transcript_lines.append(f"[{msg.created_at.strftime('%d.%m.%Y %H:%M')}] {msg.author}: {msg.content}")
    transcript_text = "\n".join(transcript_lines)

    transcript_channel_id = cfg.get("transcript_channel")
    if transcript_channel_id:
        tc = guild.get_channel(int(transcript_channel_id))
        if tc:
            embed = liquid_glass_embed(
                f"📄 Transcript – {channel.name}",
                f"**Geöffnet von:** <@{ticket_data.get('user_id')}>\n**Kategorie:** {ticket_data.get('category')}\n**Geschlossen von:** {interaction.user.mention}",
                discord.Color.from_rgb(130, 200, 240)
            )
            file = discord.File(fp=io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{channel.name}.txt")
            await tc.send(embed=embed, file=file)

    # Remove from tickets
    if guild_id in tickets and ticket_key in tickets[guild_id]:
        del tickets[guild_id][ticket_key]
        await save_tickets(tickets)

    if aktion == "delete":
        await interaction.followup.send("🗑️ Ticket wird in 3 Sekunden gelöscht...")
        await asyncio.sleep(3)
        await channel.delete(reason=f"Ticket gelöscht von {interaction.user}")
    else:
        archive_category_id = cfg.get("archive_category")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }
        for role in guild.roles:
            if role.permissions.manage_channels:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        await channel.edit(
            name=f"archived-{channel.name}",
            overwrites=overwrites,
            category=guild.get_channel(int(archive_category_id)) if archive_category_id else channel.category,
            reason=f"Ticket archiviert von {interaction.user}"
        )
        embed = liquid_glass_embed("📁 Archiviert", f"Ticket wurde von {interaction.user.mention} archiviert.", discord.Color.from_rgb(130, 200, 240))
        await interaction.followup.send(embed=embed)

@tree.command(name="ticket-add", description="Fügt einen User zum aktuellen Ticket hinzu")
@app_commands.describe(member="Der User der hinzugefügt werden soll")
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = await load_tickets()
    guild_id = str(interaction.guild.id)
    is_ticket = any(str(t.get("channel_id", "")) == str(interaction.channel.id) for t in tickets.get(guild_id, {}).values())

    if not is_ticket:
        await interaction.response.send_message("❌ Dieser Kanal ist kein Ticket!", ephemeral=True)
        return

    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    embed = liquid_glass_embed("✅ User hinzugefügt", f"{member.mention} wurde zum Ticket hinzugefügt.", discord.Color.from_rgb(100, 220, 150))
    await interaction.response.send_message(embed=embed)

@tree.command(name="ticket-remove", description="Entfernt einen User aus dem aktuellen Ticket")
@app_commands.describe(member="Der User der entfernt werden soll")
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = await load_tickets()
    guild_id = str(interaction.guild.id)
    is_ticket = any(str(t.get("channel_id", "")) == str(interaction.channel.id) for t in tickets.get(guild_id, {}).values())

    if not is_ticket:
        await interaction.response.send_message("❌ Dieser Kanal ist kein Ticket!", ephemeral=True)
        return

    await interaction.channel.set_permissions(member, overwrite=None)
    embed = liquid_glass_embed("🚫 User entfernt", f"{member.mention} wurde aus dem Ticket entfernt.", discord.Color.from_rgb(255, 100, 100))
    await interaction.response.send_message(embed=embed)


@tree.command(name="ticket-übertragen", description="Überträgt das Ticket an einen anderen User")
@app_commands.describe(member="Der User dem das Ticket übertragen werden soll")
async def ticket_uebertragen(interaction: discord.Interaction, member: discord.Member):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = await load_tickets()
    guild_id = str(interaction.guild.id)
    ticket_key = None
    ticket_data = None

    for key, t in tickets.get(guild_id, {}).items():
        if int(t.get("channel_id", 0)) == interaction.channel.id:
            ticket_data = t
            ticket_key = key
            break

    if not ticket_data:
        await interaction.response.send_message("❌ Dieser Kanal ist kein Ticket!", ephemeral=True)
        return

    old_user_id = ticket_data.get("user_id")

    # Keep old user in ticket, just add new user
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)

    # Update ticket data
    tickets[guild_id][ticket_key]["user_id"] = member.id
    await save_tickets(tickets)

    embed = liquid_glass_embed(
        "🔁 Ticket übertragen",
        f"Das Ticket wurde von <@{old_user_id}> an {member.mention} übertragen.",
        discord.Color.from_rgb(130, 200, 240)
    )
    await interaction.response.send_message(embed=embed)



# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# /ticket-kategorie-hinzufügen  (ticket-manager)
# /ticket-kategorie-entfernen   (ticket-manager)
# ─────────────────────────────────────────────

def has_ticket_manager(interaction: discord.Interaction) -> bool:
    role = discord.utils.get(interaction.guild.roles, name="ticket-manager")
    if not role:
        return False
    return is_bot_owner(interaction.user) or role in interaction.user.roles or interaction.user.guild_permissions.administrator

@tree.command(name="ticket-kategorie-hinzufügen", description="Fügt eine neue Ticket-Kategorie hinzu (ticket-manager)")
@app_commands.describe(
    key="Interner Name (kein Leerzeichen, z.B. vip_support)",
    emoji="Emoji für die Kategorie (z.B. ⭐)",
    label="Anzeigename (z.B. VIP Support)",
    beschreibung="Kurze Beschreibung",
    nur_rolle="Nur User mit dieser Rolle können das Ticket öffnen (optional)"
)
async def ticket_kategorie_hinzufuegen(
    interaction: discord.Interaction,
    key: str,
    emoji: str,
    label: str,
    beschreibung: str,
    nur_rolle: discord.Role = None
):
    if not has_ticket_manager(interaction):
        await interaction.response.send_message("Du benötigst die Rolle **ticket-manager**!", ephemeral=True)
        return

    key = key.replace(" ", "_").lower()
    if key in TICKET_CATEGORIES:
        await interaction.response.send_message(f"Der Key `{key}` ist ein Standard-Key und kann nicht überschrieben werden!", ephemeral=True)
        return

    cfg = await get_ticket_config(interaction.guild.id)
    if "custom_categories" not in cfg:
        cfg["custom_categories"] = {}

    cfg["custom_categories"][key] = {
        "emoji": emoji,
        "label": label,
        "description": beschreibung,
        "restricted_role": str(nur_rolle.id) if nur_rolle else None
    }
    await save_ticket_config(interaction.guild.id, cfg)

    rolle_info = f"\n**Nur für Rolle:** {nur_rolle.mention}" if nur_rolle else ""
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Kategorie hinzugefügt",
            f"**Key:** `{key}`\n**Emoji:** {emoji}\n**Name:** {label}\n**Beschreibung:** {beschreibung}{rolle_info}\n\nFühre `/ticket-setup` erneut aus, damit die neue Kategorie im Panel erscheint.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="ticket-kategorie-entfernen", description="Entfernt eine custom Ticket-Kategorie (ticket-manager)")
@app_commands.describe(key="Interner Name der Kategorie (z.B. vip_support)")
async def ticket_kategorie_entfernen(
    interaction: discord.Interaction,
    key: str
):
    if not has_ticket_manager(interaction):
        await interaction.response.send_message("Du benötigst die Rolle **ticket-manager**!", ephemeral=True)
        return

    cfg = await get_ticket_config(interaction.guild.id)
    custom = cfg.get("custom_categories", {})

    if key not in custom:
        await interaction.response.send_message(f"Kategorie `{key}` nicht gefunden!", ephemeral=True)
        return

    del custom[key]
    cfg["custom_categories"] = custom
    await save_ticket_config(interaction.guild.id, cfg)

    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "🗑️ Kategorie entfernt",
            f"Die Kategorie `{key}` wurde entfernt.\n\nFühre `/ticket-setup` erneut aus, damit die Änderung im Panel sichtbar ist.",
            discord.Color.from_rgb(255, 100, 100)
        ),
        ephemeral=True
    )

@tree.command(name="ticket-kategorien", description="Zeigt alle Ticket-Kategorien (ticket-manager)")
async def ticket_kategorien(interaction: discord.Interaction):
    if not has_ticket_manager(interaction):
        await interaction.response.send_message("Du benötigst die Rolle **ticket-manager**!", ephemeral=True)
        return

    cfg = await get_ticket_config(interaction.guild.id)
    custom = cfg.get("custom_categories", {})

    std_list = "\n".join(f"{e} **{l}** (`{k}`)" for k, (e, l, _) in TICKET_CATEGORIES.items())
    custom_list = "\n".join(f"{d['emoji']} **{d['label']}** (`{k}`)" for k, d in custom.items()) if custom else "Keine"

    embed = liquid_glass_embed(
        "📋 Ticket-Kategorien",
        f"**Standard:**\n{std_list}\n\n**Custom:**\n{custom_list}",
        discord.Color.from_rgb(130, 200, 240)
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="ticket-panel-aktualisieren", description="Aktualisiert das Ticket-Panel mit allen aktuellen Kategorien (ticket-manager)")
@app_commands.describe(kanal="Der Kanal wo das Panel ist")
async def ticket_panel_aktualisieren(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not has_ticket_manager(interaction):
        await interaction.response.send_message("Du benötigst die Rolle **ticket-manager**!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    cfg = await get_ticket_config(interaction.guild.id)
    if not cfg:
        await interaction.followup.send("❌ Ticket-System ist noch nicht eingerichtet! Führe zuerst `/ticket-setup` aus.", ephemeral=True)
        return

    msg = await send_ticket_panel(kanal, interaction.guild.id)
    cfg["panel_message_id"] = str(msg.id)
    cfg["panel_channel_id"] = str(kanal.id)
    await save_ticket_config(interaction.guild.id, cfg)

    all_cats = await get_all_categories(interaction.guild.id)
    cat_names = ", ".join(f"{v[0]} {v[1]}" for v in all_cats.values())
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Panel aktualisiert!",
            f"Das Ticket-Panel wurde neu gepostet mit:\n{cat_names}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )



# ─────────────────────────────────────────────
# VOICE SUPPORT SYSTEM
# ─────────────────────────────────────────────

async def get_voice_support_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("voice_support", {}).get(str(guild_id), {})

async def save_voice_support_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "voice_support" not in cfg:
        cfg["voice_support"] = {}
    cfg["voice_support"][str(guild_id)] = data
    await save_config(cfg)

async def get_voice_support_2_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("voice_support_2", {}).get(str(guild_id), {})

async def save_voice_support_2_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "voice_support_2" not in cfg:
        cfg["voice_support_2"] = {}
    cfg["voice_support_2"][str(guild_id)] = data
    await save_config(cfg)

def has_support_role(interaction: discord.Interaction, support_role_id: str) -> bool:
    if is_bot_owner(interaction.user):
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    if not support_role_id:
        return False
    role = interaction.guild.get_role(int(support_role_id))
    return role in interaction.user.roles if role else False

class VoiceSupportView(discord.ui.View):
    """
    Persistente View für eingehende Support-Anfragen.
    Die support_role_id wird in den custom_ids eingebettet,
    damit die Rollenprüfung auch nach einem Bot-Neustart funktioniert.
    Format custom_id: "vs_accept:<user_id>:<support_role_id>"
    """
    def __init__(self, user_id: int, support_role_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.support_role_id = support_role_id

        accept_btn = discord.ui.Button(
            label="✅ Annehmen",
            style=discord.ButtonStyle.success,
            custom_id=f"vs_accept:{user_id}:{support_role_id}"
        )
        decline_btn = discord.ui.Button(
            label="❌ Ablehnen",
            style=discord.ButtonStyle.danger,
            custom_id=f"vs_decline:{user_id}:{support_role_id}"
        )
        accept_btn.callback = self._accept_callback
        decline_btn.callback = self._decline_callback
        self.add_item(accept_btn)
        self.add_item(decline_btn)

    async def _accept_callback(self, interaction: discord.Interaction):
        # Rollenprüfung: nur die konfigurierte Support-Rolle (oder Admin)
        if not has_support_role(interaction, self.support_role_id):
            await interaction.response.send_message(
                "❌ Du hast keine Berechtigung, Supports anzunehmen!\n"
                f"Benötigt: <@&{self.support_role_id}>",
                ephemeral=True
            )
            return
        guild = interaction.guild
        member = guild.get_member(self.user_id)
        if not member:
            await interaction.response.send_message("❌ User ist nicht mehr auf dem Server.", ephemeral=True)
            return
        if not member.voice:
            await interaction.response.send_message("❌ User ist nicht mehr im Warteraum.", ephemeral=True)
            return

        # Check both configs - find the one matching this support_role_id
        vs_cfg_1 = await get_voice_support_config(guild.id)
        vs_cfg_2 = await get_voice_support_2_config(guild.id)
        
        active_cfg = None
        for cfg in [vs_cfg_1, vs_cfg_2]:
            if cfg and cfg.get("support_role_id") == str(self.support_role_id):
                active_cfg = cfg
                break
        # Also check ping_role as fallback
        if not active_cfg:
            for cfg in [vs_cfg_1, vs_cfg_2]:
                if cfg and cfg.get("ping_role_id") == str(self.support_role_id):
                    active_cfg = cfg
                    break
        # Last resort - use whichever has a support_category_id
        if not active_cfg:
            active_cfg = vs_cfg_1 if vs_cfg_1 else vs_cfg_2

        if not active_cfg:
            await interaction.response.send_message("❌ Keine Voice-Support Konfiguration gefunden!", ephemeral=True)
            return

        support_category_id = active_cfg.get("support_category_id")
        if not support_category_id:
            await interaction.response.send_message("❌ Keine Support-Kategorie konfiguriert!", ephemeral=True)
            return
        category = guild.get_channel(int(support_category_id))
        if not category:
            try:
                category = await guild.fetch_channel(int(support_category_id))
            except Exception:
                pass
        if not category:
            await interaction.response.send_message("❌ Support-Kategorie nicht gefunden!", ephemeral=True)
            return

        support_member = interaction.user
        support_voice = support_member.voice

        if not support_voice or not support_voice.channel:
            await interaction.response.send_message(
                "❌ Du musst selbst in einem Voice-Kanal der Support-Kategorie sein, um den Support anzunehmen!",
                ephemeral=True
            )
            return

        if support_voice.channel.category_id != category.id:
            await interaction.response.send_message(
                f"❌ Du musst in einem Voice-Kanal der Kategorie **{category.name}** sein!",
                ephemeral=True
            )
            return

        target_channel = support_voice.channel

        try:
            await member.move_to(target_channel)
        except Exception:
            await interaction.response.send_message("❌ Konnte User nicht in den Kanal ziehen.", ephemeral=True)
            return

        for item in self.children:
            item.disabled = True
        # Stat tracken
        await add_team_stat(
            interaction.guild.id,
            interaction.user.id,
            str(interaction.user),
            "supports_accepted"
        )

        close_view = VoiceSupportCloseView(member.id, target_channel.id, self.support_role_id)
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "🎧 Support läuft",
                f"**User:** {member.mention}\n**Support:** {support_member.mention}\n**Raum:** {target_channel.mention}\n\nKlicke **Schließen** wenn der Support beendet ist.",
                discord.Color.from_rgb(100, 220, 150)
            ),
            view=close_view
        )

    async def _decline_callback(self, interaction: discord.Interaction):
        # Rollenprüfung: nur die konfigurierte Support-Rolle (oder Admin)
        if not has_support_role(interaction, self.support_role_id):
            await interaction.response.send_message(
                "❌ Du hast keine Berechtigung, Supports abzulehnen!\n"
                f"Benötigt: <@&{self.support_role_id}>",
                ephemeral=True
            )
            return
        guild = interaction.guild
        member = guild.get_member(self.user_id)

        # User aus dem Warteraum schmeißen
        kicked_from_voice = False
        if member and member.voice:
            try:
                await member.move_to(None)
                kicked_from_voice = True
            except Exception:
                pass

        for item in self.children:
            item.disabled = True

        extra = "\nDer User wurde aus dem Warteraum entfernt." if kicked_from_voice else ""
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "❌ Support abgelehnt",
                f"Der Support für {member.mention if member else 'Unbekannt'} wurde abgelehnt.{extra}",
                discord.Color.from_rgb(220, 80, 80)
            ),
            view=self
        )


class VoiceSupportCloseView(discord.ui.View):
    """
    Persistente View für den laufenden Support.
    Format custom_id: "vs_close:<user_id>:<voice_channel_id>:<support_role_id>"
    """
    def __init__(self, user_id: int, voice_channel_id: int, support_role_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.voice_channel_id = voice_channel_id
        self.support_role_id = support_role_id

        close_btn = discord.ui.Button(
            label="🔒 Support schließen",
            style=discord.ButtonStyle.danger,
            custom_id=f"vs_close:{user_id}:{voice_channel_id}:{support_role_id}"
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

    async def _close_callback(self, interaction: discord.Interaction):
        if not has_support_role(interaction, self.support_role_id):
            await interaction.response.send_message(
                "❌ Du hast keine Berechtigung, diesen Support zu schließen!\n"
                f"Benötigt: <@&{self.support_role_id}>",
                ephemeral=True
            )
            return
        guild = interaction.guild
        member = guild.get_member(self.user_id)
        if member and member.voice:
            try:
                await member.move_to(None)
            except Exception:
                pass
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "✅ Support beendet",
                f"{member.mention if member else 'Der User'} wurde aus dem Support-Raum entfernt.",
                discord.Color.from_rgb(130, 200, 240)
            ),
            view=self
        )

@bot.event
async def on_interaction(interaction: discord.Interaction):
    """
    Behandelt Klicks auf persistente Voice-Support-Buttons nach einem Bot-Neustart.
    Die custom_id enthält alle nötigen Daten (user_id, support_role_id).
    """
    if interaction.type != discord.InteractionType.component:
        return
    cid = interaction.data.get("custom_id", "")

    if cid.startswith("vs_accept:") or cid.startswith("vs_decline:"):
        parts = cid.split(":")
        if len(parts) < 3:
            return
        prefix = parts[0]
        user_id = int(parts[1])
        support_role_id = parts[2]
        view = VoiceSupportView(user_id, support_role_id)
        if prefix == "vs_accept":
            await view._accept_callback(interaction)
        else:
            await view._decline_callback(interaction)

    elif cid.startswith("vs_close:"):
        parts = cid.split(":")
        if len(parts) < 4:
            return
        user_id = int(parts[1])
        voice_channel_id = int(parts[2])
        support_role_id = parts[3]
        view = VoiceSupportCloseView(user_id, voice_channel_id, support_role_id)
        await view._close_callback(interaction)

    elif cid.startswith("tca_delete:") or cid.startswith("tca_archive:"):
        # Ticket schließen nach Bot-Neustart – Daten aus MongoDB laden
        parts = cid.split(":", 1)
        action = parts[0]
        ticket_key = parts[1] if len(parts) > 1 else str(interaction.channel.id)

        if not has_permission(interaction, "manage_channels"):
            await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
            return

        guild_id = str(interaction.guild.id)
        tickets = await load_tickets()
        ticket_data = tickets.get(guild_id, {}).get(ticket_key, {
            "channel_id": str(interaction.channel.id),
            "user_id": interaction.user.id,
            "category": "Unbekannt",
        })

        view = TicketCloseActionView(ticket_data, ticket_key)
        delete = (action == "tca_delete")
        await view._close(interaction, delete=delete)

    # Alte Buttons ohne tca_ Prefix (von Tickets die vor dem Update erstellt wurden)
    elif "Löschen" in str(interaction.data.get("custom_id", "")) or          any(comp.get("label") in ["🗑️ Löschen", "📁 Archivieren"] 
             for comp in interaction.data.get("components", [])):
        pass  # Wird durch View-Klasse selbst behandelt
    


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    guild = member.guild

    # Voice Support System - check both configs
    for vs_cfg in [
        await get_voice_support_config(guild.id),
        await get_voice_support_2_config(guild.id)
    ]:
        if not vs_cfg:
            continue
        warteraum_id = vs_cfg.get("warteraum_id")
        notif_channel_id = vs_cfg.get("notif_channel_id")
        ping_role_id = vs_cfg.get("ping_role_id")
        support_role_id = vs_cfg.get("support_role_id")
        if warteraum_id and notif_channel_id:
            if after.channel and str(after.channel.id) == str(warteraum_id):
                notif_channel = guild.get_channel(int(notif_channel_id))
                if notif_channel:
                    ping_text = f"<@&{ping_role_id}>" if ping_role_id else None
                    embed = liquid_glass_embed(
                        "🔔 Jemand wartet auf Support!",
                        f"**{member.mention}** wartet im Warteraum auf Unterstützung.\n\nKlicke **Annehmen** um den Support zu starten.",
                        discord.Color.from_rgb(255, 165, 0)
                    )
                    view = VoiceSupportView(member.id, support_role_id)
                    await notif_channel.send(content=ping_text, embed=embed, view=view)
                break

    # Voice Log
    log_cfg = await get_log_config(guild.id)
    ch_id = log_cfg.get("voice_log")
    if ch_id and before.channel != after.channel:
        ch = guild.get_channel(int(ch_id))
        if ch:
            if not before.channel and after.channel:
                desc = f"{member.mention} hat **{after.channel.name}** betreten."
                color, title = discord.Color.from_rgb(100, 220, 150), "🔊 Voice beigetreten"
            elif before.channel and not after.channel:
                desc = f"{member.mention} hat **{before.channel.name}** verlassen."
                color, title = discord.Color.from_rgb(220, 80, 80), "🔇 Voice verlassen"
            else:
                desc = f"{member.mention} hat von **{before.channel.name}** zu **{after.channel.name}** gewechselt."
                color, title = discord.Color.from_rgb(255, 200, 0), "🔄 Voice gewechselt"
            embed = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now(timezone.utc))
            await ch.send(embed=embed)

@tree.command(name="voice-support-setup", description="Richtet das Voice-Support-System ein (Admin)")
@app_commands.describe(
    warteraum="Der Voice-Warteraum den User betreten",
    benachrichtigungs_kanal="Kanal wo Support-Anfragen erscheinen",
    support_kategorie="Kategorie wo die Support-Räume sind (Bot nimmt ersten freien)",
    ping_rolle="Rolle die angepingt wird wenn jemand wartet",
    support_rolle="Rolle die Anfragen annehmen und schließen darf"
)
async def voice_support_setup(
    interaction: discord.Interaction,
    warteraum: discord.VoiceChannel,
    benachrichtigungs_kanal: discord.TextChannel,
    support_kategorie: discord.CategoryChannel,
    ping_rolle: discord.Role,
    support_rolle: discord.Role
):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    data = {
        "warteraum_id": str(warteraum.id),
        "notif_channel_id": str(benachrichtigungs_kanal.id),
        "support_category_id": str(support_kategorie.id),
        "ping_role_id": str(ping_rolle.id),
        "support_role_id": str(support_rolle.id)
    }
    await save_voice_support_config(interaction.guild.id, data)
    room_count = len(support_kategorie.voice_channels)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Voice-Support eingerichtet!",
            f"**Warteraum:** {warteraum.mention}\n**Benachrichtigungen:** {benachrichtigungs_kanal.mention}\n**Support-Kategorie:** {support_kategorie.name} ({room_count} Räume)\n**Ping-Rolle:** {ping_rolle.mention}\n**Support-Rolle:** {support_rolle.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

# ─────────────────────────────────────────────
# /voice-support-setup-2
# ─────────────────────────────────────────────

@tree.command(name="voice-support-setup-2", description="Richtet ein zweites Voice-Support-System ein (Admin)")
@app_commands.describe(
    warteraum="Der Voice-Warteraum den User betreten",
    benachrichtigungs_kanal="Kanal wo Support-Anfragen erscheinen",
    support_kategorie="Kategorie wo die Support-Räume sind",
    ping_rolle="Rolle die angepingt wird wenn jemand wartet",
)
async def voice_support_setup_2(
    interaction: discord.Interaction,
    warteraum: discord.VoiceChannel,
    benachrichtigungs_kanal: discord.TextChannel,
    support_kategorie: discord.CategoryChannel,
    ping_rolle: discord.Role,
):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # Keep existing support_role_id if already set
    existing = await get_voice_support_2_config(interaction.guild.id)
    data = {
        "warteraum_id": str(warteraum.id),
        "notif_channel_id": str(benachrichtigungs_kanal.id),
        "support_category_id": str(support_kategorie.id),
        "ping_role_id": str(ping_rolle.id),
        "support_role_id": existing.get("support_role_id")
    }
    await save_voice_support_2_config(interaction.guild.id, data)
    room_count = len(support_kategorie.voice_channels)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Voice-Support 2 eingerichtet!",
            f"**Warteraum:** {warteraum.mention}\n**Benachrichtigungen:** {benachrichtigungs_kanal.mention}\n**Support-Kategorie:** {support_kategorie.name} ({room_count} Räume)\n**Ping-Rolle:** {ping_rolle.mention}\n\n⚠️ Benutze `/voice-support-2-rolle` um die Support-Rolle einzustellen!",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="voice-support-2-rolle", description="Setzt die Support-Rolle für Voice-Support 2")
@app_commands.describe(support_rolle="Rolle die Anfragen annehmen und schließen darf")
async def voice_support_2_rolle(interaction: discord.Interaction, support_rolle: discord.Role):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    existing = await get_voice_support_2_config(interaction.guild.id)
    if not existing:
        await interaction.response.send_message("❌ Richte zuerst `/voice-support-setup-2` ein!", ephemeral=True)
        return
    existing["support_role_id"] = str(support_rolle.id)
    await save_voice_support_2_config(interaction.guild.id, existing)
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Support-Rolle gesetzt!",
            f"**Support-Rolle:** {support_rolle.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )


# ── Team Dashboard ──

@tree.command(name="team_dashboard", description="📊 Zeigt das Team-Dashboard mit Support & Ticket Statistiken")
async def team_dashboard(interaction: discord.Interaction):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    stats = await get_team_stats(interaction.guild.id)
    guild = interaction.guild

    medals = ["🥇", "🥈", "🥉"]

    if not stats:
        await interaction.followup.send(
            embed=liquid_glass_embed(
                "📊 Team Dashboard",
                "Noch keine Statistiken vorhanden.\n\nStats werden automatisch erfasst sobald:\n• Tickets geschlossen werden\n• Voice-Supports angenommen werden",
                discord.Color.from_rgb(130, 200, 240)
            )
        )
        return

    # Gesamt-Stats
    total_tickets = sum(s.get("tickets_closed", 0) for s in stats)
    total_supports = sum(s.get("supports_accepted", 0) for s in stats)
    total_actions = total_tickets + total_supports

    # Leaderboard aufbauen
    lines = []
    for i, s in enumerate(stats[:10]):
        member = guild.get_member(int(s["user_id"]))
        name = member.display_name if member else s.get("user_name", "Unbekannt")
        tickets = s.get("tickets_closed", 0)
        supports = s.get("supports_accepted", 0)
        total = tickets + supports
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        lines.append(
            f"{medal} **{name}**\n"
            f"┣ 🎫 Tickets: **{tickets}** ┃ 🎧 Supports: **{supports}**\n"
            f"┗ Gesamt: **{total}** Aktionen"
        )

    # Top-Performer
    top = stats[0]
    top_member = guild.get_member(int(top["user_id"]))
    top_name = top_member.display_name if top_member else top.get("user_name", "Unbekannt")
    top_total = top.get("tickets_closed", 0) + top.get("supports_accepted", 0)

    description = (
        f"**🏆 Server:** {guild.name}\n"
        f"**👥 Team-Mitglieder:** {len(stats)}\n"
        f"**🎫 Tickets gesamt:** {total_tickets}\n"
        f"**🎧 Supports gesamt:** {total_supports}\n"
        f"**⚡ Aktionen gesamt:** {total_actions}\n"
        f"**👑 MVP:** {top_name} ({top_total} Aktionen)\n"
        f"\n{'─' * 30}\n\n"
        + "\n\n".join(lines)
    )

    embed = liquid_glass_embed(
        "📊 Team Dashboard",
        description,
        discord.Color.from_rgb(130, 200, 240)
    )
    embed.set_footer(text=f"Top {min(len(stats), 10)} von {len(stats)} Team-Mitgliedern • Stats seit Bot-Start")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    await interaction.followup.send(embed=embed)


@tree.command(name="team_stats_reset", description="🗑️ Setzt alle Team-Statistiken zurück (nur Bot-Owner)")
async def team_stats_reset(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user):
        await interaction.response.send_message("❌ Nur der Bot-Owner kann Stats zurücksetzen!", ephemeral=True)
        return
    db = get_db()
    await db["team_stats"].delete_many({"guild_id": str(interaction.guild.id)})
    await interaction.response.send_message(
        embed=liquid_glass_embed("✅ Stats zurückgesetzt", "Alle Team-Statistiken wurden gelöscht.", discord.Color.from_rgb(220, 80, 80)),
        ephemeral=True
    )



# ═══════════════════════════════════════════════
# MODERATION EXTRAS
# ═══════════════════════════════════════════════

# /softban
@tree.command(name="softban", description="Bannt und entbannt sofort (löscht Nachrichten der letzten 7 Tage)")
@app_commands.describe(mitglied="Das Mitglied", grund="Grund")
async def softban(interaction: discord.Interaction, mitglied: discord.Member, grund: str = "Kein Grund angegeben"):
    if not has_permission(interaction, "ban_members"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await mitglied.ban(reason=f"Softban: {grund}", delete_message_days=7)
        await interaction.guild.unban(mitglied, reason="Softban abgeschlossen")
        await interaction.followup.send(embed=liquid_glass_embed("🔨 Softban", f"**{mitglied}** wurde soft-gebannt.\n**Grund:** {grund}", discord.Color.from_rgb(255, 140, 0)), ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Fehlende Berechtigung.", ephemeral=True)

# /massban
@tree.command(name="massban", description="Bannt mehrere User per ID (durch Komma getrennt)")
@app_commands.describe(user_ids="User-IDs durch Komma getrennt", grund="Grund")
async def massban(interaction: discord.Interaction, user_ids: str, grund: str = "Massban"):
    if not has_permission(interaction, "ban_members"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ids = [uid.strip() for uid in user_ids.split(",")]
    success, failed = [], []
    for uid in ids:
        try:
            user = discord.Object(id=int(uid))
            await interaction.guild.ban(user, reason=grund)
            success.append(uid)
        except Exception:
            failed.append(uid)
    msg = f"✅ Gebannt: {len(success)}\n❌ Fehlgeschlagen: {len(failed)}"
    await interaction.followup.send(embed=liquid_glass_embed("🔨 Massban", msg, discord.Color.from_rgb(220, 50, 50)), ephemeral=True)

# /nick
@tree.command(name="nick", description="Ändert den Nickname eines Mitglieds")
@app_commands.describe(mitglied="Das Mitglied", nickname="Neuer Nickname (leer = zurücksetzen)")
async def nick(interaction: discord.Interaction, mitglied: discord.Member, nickname: str = None):
    if not has_permission(interaction, "manage_nicknames"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    try:
        await mitglied.edit(nick=nickname)
        text = f"Nickname von **{mitglied}** wurde auf **{nickname or 'Standard'}** gesetzt."
        await interaction.response.send_message(embed=liquid_glass_embed("✏️ Nickname", text, discord.Color.from_rgb(130, 200, 240)), ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Fehlende Berechtigung.", ephemeral=True)

# /slowmode
@tree.command(name="slowmode", description="Setzt den Slowmode in einem Kanal")
@app_commands.describe(sekunden="Sekunden (0 = deaktivieren)", kanal="Kanal (Standard: aktueller)")
async def slowmode(interaction: discord.Interaction, sekunden: int, kanal: discord.TextChannel = None):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    kanal = kanal or interaction.channel
    await kanal.edit(slowmode_delay=sekunden)
    text = f"Slowmode in {kanal.mention} auf **{sekunden}s** gesetzt." if sekunden > 0 else f"Slowmode in {kanal.mention} **deaktiviert**."
    await interaction.response.send_message(embed=liquid_glass_embed("🐌 Slowmode", text, discord.Color.from_rgb(130, 200, 240)), ephemeral=True)

# /kanal-sperren
@tree.command(name="kanal-sperren", description="Sperrt oder entsperrt einen Kanal für @everyone")
@app_commands.describe(kanal="Kanal (Standard: aktueller)", sperren="True = sperren, False = entsperren")
async def kanal_sperren(interaction: discord.Interaction, sperren: bool = True, kanal: discord.TextChannel = None):
    if not has_permission(interaction, "manage_channels"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    kanal = kanal or interaction.channel
    overwrite = kanal.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = not sperren
    await kanal.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    text = f"{kanal.mention} wurde **{'gesperrt' if sperren else 'entsperrt'}**."
    await interaction.response.send_message(embed=liquid_glass_embed("🔒 Kanal", text, discord.Color.from_rgb(220, 80, 80) if sperren else discord.Color.from_rgb(100, 220, 150)))

# ═══════════════════════════════════════════════
# GEWINNSPIEL
# ═══════════════════════════════════════════════

import random

active_giveaways = {}  # message_id -> {channel_id, prize, participants}

class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎉 Teilnehmen", style=discord.ButtonStyle.primary, custom_id="giveaway_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        if msg_id not in active_giveaways:
            await interaction.response.send_message("❌ Dieses Gewinnspiel ist nicht mehr aktiv.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in active_giveaways[msg_id]["participants"]:
            active_giveaways[msg_id]["participants"].remove(uid)
            await interaction.response.send_message("❌ Du hast dich vom Gewinnspiel abgemeldet.", ephemeral=True)
        else:
            active_giveaways[msg_id]["participants"].append(uid)
            count = len(active_giveaways[msg_id]["participants"])
            await interaction.response.send_message(f"✅ Du nimmst teil! Aktuell **{count}** Teilnehmer.", ephemeral=True)

@tree.command(name="gewinnspiel-starten", description="Startet ein Gewinnspiel")
@app_commands.describe(preis="Was gibt es zu gewinnen?", kanal="Kanal für das Gewinnspiel")
async def gewinnspiel_starten(interaction: discord.Interaction, preis: str, kanal: discord.TextChannel = None):
    if not has_permission(interaction, "manage_guild"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    kanal = kanal or interaction.channel
    embed = liquid_glass_embed(
        "🎉 Gewinnspiel!",
        f"**Preis:** {preis}\n\nKlicke auf 🎉 um teilzunehmen!\n**Veranstalter:** {interaction.user.mention}",
        discord.Color.from_rgb(255, 215, 0)
    )
    view = GiveawayView()
    msg = await kanal.send(embed=embed, view=view)
    active_giveaways[msg.id] = {"channel_id": kanal.id, "prize": preis, "participants": []}
    await interaction.response.send_message(f"✅ Gewinnspiel in {kanal.mention} gestartet!", ephemeral=True)

@tree.command(name="gewinner-ziehen", description="Zieht einen Gewinner aus dem aktuellen Gewinnspiel")
@app_commands.describe(message_id="Die Message-ID des Gewinnspiels", anzahl="Anzahl Gewinner (Standard: 1)")
async def gewinner_ziehen(interaction: discord.Interaction, message_id: str, anzahl: int = 1):
    if not has_permission(interaction, "manage_guild"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    mid = int(message_id)
    if mid not in active_giveaways:
        await interaction.response.send_message("❌ Kein aktives Gewinnspiel mit dieser ID.", ephemeral=True)
        return
    participants = active_giveaways[mid]["participants"]
    if not participants:
        await interaction.response.send_message("❌ Keine Teilnehmer!", ephemeral=True)
        return
    anzahl = min(anzahl, len(participants))
    winners = random.sample(participants, anzahl)
    winner_mentions = ", ".join(f"<@{w}>" for w in winners)
    prize = active_giveaways[mid]["prize"]
    await interaction.response.send_message(embed=liquid_glass_embed(
        "🎉 Gewinner!",
        f"**Preis:** {prize}\n**Gewinner:** {winner_mentions}\nHerzlichen Glückwunsch! 🎊",
        discord.Color.from_rgb(255, 215, 0)
    ))

@tree.command(name="neu-rollen", description="Rollt das Gewinnspiel erneut (neuer Gewinner)")
@app_commands.describe(message_id="Die Message-ID des Gewinnspiels")
async def neu_rollen(interaction: discord.Interaction, message_id: str):
    if not has_permission(interaction, "manage_guild"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    mid = int(message_id)
    if mid not in active_giveaways or not active_giveaways[mid]["participants"]:
        await interaction.response.send_message("❌ Kein aktives Gewinnspiel oder keine Teilnehmer.", ephemeral=True)
        return
    winner = random.choice(active_giveaways[mid]["participants"])
    prize = active_giveaways[mid]["prize"]
    await interaction.response.send_message(embed=liquid_glass_embed(
        "🔄 Neu gerollt!",
        f"**Preis:** {prize}\n**Neuer Gewinner:** <@{winner}> 🎊",
        discord.Color.from_rgb(255, 215, 0)
    ))

# ═══════════════════════════════════════════════
# LOGGING SYSTEM
# ═══════════════════════════════════════════════

async def get_log_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("logs", {}).get(str(guild_id), {})

async def save_log_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "logs" not in cfg:
        cfg["logs"] = {}
    cfg["logs"][str(guild_id)] = data
    await save_config(cfg)

async def send_log(guild: discord.Guild, log_type: str, embed: discord.Embed):
    cfg = await get_log_config(guild.id)
    channel_id = cfg.get(log_type)
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            pass

@tree.command(name="log-setup", description="Richtet die Logging-Kanäle ein (Admin)")
@app_commands.describe(
    message_log="Kanal für bearbeitete/gelöschte Nachrichten",
    join_leave_log="Kanal für Beitritte und Abgänge",
    ban_log="Kanal für Bans/Unbans",
    voice_log="Kanal für Voice-Aktivität"
)
async def log_setup(
    interaction: discord.Interaction,
    message_log: discord.TextChannel = None,
    join_leave_log: discord.TextChannel = None,
    ban_log: discord.TextChannel = None,
    voice_log: discord.TextChannel = None
):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_log_config(interaction.guild.id)
    if message_log:
        cfg["message_log"] = str(message_log.id)
    if join_leave_log:
        cfg["join_leave_log"] = str(join_leave_log.id)
    if ban_log:
        cfg["ban_log"] = str(ban_log.id)
    if voice_log:
        cfg["voice_log"] = str(voice_log.id)
    await save_log_config(interaction.guild.id, cfg)

    lines = []
    if message_log: lines.append(f"📝 Message-Log: {message_log.mention}")
    if join_leave_log: lines.append(f"👋 Join/Leave-Log: {join_leave_log.mention}")
    if ban_log: lines.append(f"🔨 Ban-Log: {ban_log.mention}")
    if voice_log: lines.append(f"🎙️ Voice-Log: {voice_log.mention}")
    await interaction.followup.send(embed=liquid_glass_embed("✅ Log-Setup", "\n".join(lines) or "Nichts geändert.", discord.Color.from_rgb(100, 220, 150)), ephemeral=True)

# Message Logs
@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    embed = discord.Embed(title="🗑️ Nachricht gelöscht", color=discord.Color.from_rgb(220, 80, 80), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Autor", value=message.author.mention, inline=True)
    embed.add_field(name="Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="Inhalt", value=message.content or "*Kein Text*", inline=False)
    await send_log(message.guild, "message_log", embed)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    embed = discord.Embed(title="✏️ Nachricht bearbeitet", color=discord.Color.from_rgb(255, 165, 0), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Autor", value=before.author.mention, inline=True)
    embed.add_field(name="Kanal", value=before.channel.mention, inline=True)
    embed.add_field(name="Vorher", value=before.content or "*Leer*", inline=False)
    embed.add_field(name="Nachher", value=after.content or "*Leer*", inline=False)
    await send_log(before.guild, "message_log", embed)


# ─────────────────────────────────────────────
# WILLKOMMEN / ABSCHIED / AUTO-ROLLE SETUP
# ─────────────────────────────────────────────

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    cfg = await load_config()
    log_cfg = cfg.get("logs", {}).get(str(guild.id), {})
    # Join Log
    ch_id = log_cfg.get("join_leave_log")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            embed = discord.Embed(title="✅ Mitglied beigetreten", color=discord.Color.from_rgb(100, 220, 150), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
            embed.add_field(name="Account erstellt", value=member.created_at.strftime("%d.%m.%Y"), inline=True)
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
    # Willkommensnachricht
    welcome_cfg = cfg.get("welcome", {}).get(str(guild.id), {})
    wc_ch_id = welcome_cfg.get("channel_id")
    if wc_ch_id:
        wc_ch = guild.get_channel(int(wc_ch_id))
        if wc_ch:
            msg = welcome_cfg.get("message", f"Willkommen {member.mention} auf **{guild.name}**! 🎉")
            msg = msg.replace("{user}", member.mention).replace("{name}", member.display_name).replace("{server}", guild.name).replace("{count}", str(guild.member_count))
            embed = discord.Embed(
                title=f"👋 Willkommen auf {guild.name}!",
                description=msg,
                color=discord.Color.from_rgb(100, 220, 150)
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Mitglied #{guild.member_count}")
            image_url = welcome_cfg.get("image_url")
            if image_url:
                embed.set_image(url=image_url)
            await wc_ch.send(embed=embed)
    # Auto-Rolle
    auto_role_data = cfg.get("auto_role", {}).get(str(guild.id))
    if auto_role_data:
        # Support both old string format and new list format
        role_ids = auto_role_data if isinstance(auto_role_data, list) else [auto_role_data]
        for rid in role_ids:
            role = guild.get_role(int(rid))
            if role:
                try:
                    await member.add_roles(role, reason="Auto-Rolle bei Beitritt")
                except Exception:
                    pass

@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    cfg = await load_config()
    ch_id = cfg.get("logs", {}).get(str(guild.id), {}).get("ban_log")
    if ch_id:
        ch = guild.get_channel(int(ch_id))
        if ch:
            embed = discord.Embed(title="✅ User entbannt", color=discord.Color.from_rgb(100, 220, 150), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            await ch.send(embed=embed)


    if member.bot:
        return
    guild = member.guild

    # Voice Log
    if before.channel != after.channel:
        if after.channel and not before.channel:
            embed = discord.Embed(title="🎙️ Voice beigetreten", color=discord.Color.from_rgb(100, 220, 150), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Kanal", value=after.channel.mention, inline=True)
            await send_log(guild, "voice_log", embed)
        elif before.channel and not after.channel:
            embed = discord.Embed(title="🔇 Voice verlassen", color=discord.Color.from_rgb(220, 80, 80), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Kanal", value=before.channel.mention, inline=True)
            await send_log(guild, "voice_log", embed)
        elif before.channel and after.channel:
            embed = discord.Embed(title="🔀 Voice gewechselt", color=discord.Color.from_rgb(255, 165, 0), timestamp=datetime.now(timezone.utc))
            embed.add_field(name="User", value=member.mention, inline=True)
            embed.add_field(name="Von", value=before.channel.mention, inline=True)
            embed.add_field(name="Nach", value=after.channel.mention, inline=True)
            await send_log(guild, "voice_log", embed)

    # Voice Support System
    # Check both voice support configs
    vs_cfg = await get_voice_support_config(guild.id)
    vs_cfg_2 = await get_voice_support_2_config(guild.id)

    # Determine which config applies
    active_cfg = None
    if vs_cfg and str(after.channel.id if after.channel else 0) == vs_cfg.get("warteraum_id"):
        active_cfg = vs_cfg
    elif vs_cfg_2 and str(after.channel.id if after.channel else 0) == vs_cfg_2.get("warteraum_id"):
        active_cfg = vs_cfg_2

    if not active_cfg:
        vs_cfg = vs_cfg  # keep for leave handling
    else:
        vs_cfg = active_cfg

    if not vs_cfg:
        return
    warteraum_id = vs_cfg.get("warteraum_id")
    notif_channel_id = vs_cfg.get("notif_channel_id")
    ping_role_id = vs_cfg.get("ping_role_id")
    support_role_id = vs_cfg.get("support_role_id")
    if not warteraum_id or not notif_channel_id:
        return
    if after.channel and str(after.channel.id) == str(warteraum_id):
        notif_channel = guild.get_channel(int(notif_channel_id))
        if not notif_channel:
            return
        ping_text = f"<@&{ping_role_id}>" if ping_role_id else None
        embed = liquid_glass_embed(
            "🔔 Jemand wartet auf Support!",
            f"**{member.mention}** wartet im Warteraum auf Unterstützung.\n\nKlicke **Annehmen** um den Support zu starten.",
            discord.Color.from_rgb(255, 165, 0)
        )
        view = VoiceSupportView(member.id, support_role_id)
        await notif_channel.send(content=ping_text, embed=embed, view=view)

# ═══════════════════════════════════════════════
# WILLKOMMEN / ABSCHIED / AUTO-ROLLE
# ═══════════════════════════════════════════════

@tree.command(name="willkommen-setup", description="Richtet die Willkommensnachricht ein (Admin)")
@app_commands.describe(
    kanal="Kanal für Willkommensnachrichten",
    nachricht="Nachricht ({user} = Mention, {name} = Username, {server} = Servername, {count} = Memberanzahl)",
    bild_url="URL eines Bildes das im Embed angezeigt wird (optional)"
)
async def willkommen_setup(interaction: discord.Interaction, kanal: discord.TextChannel, nachricht: str = None, bild_url: str = None):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await load_config()
    if "welcome" not in cfg:
        cfg["welcome"] = {}
    cfg["welcome"][str(interaction.guild.id)] = {
        "channel_id": str(kanal.id),
        "message": nachricht or "Willkommen {user} auf **{server}**! Du bist Mitglied #{count}. 🎉",
        "image_url": bild_url or None
    }
    await save_config(cfg)

    # Vorschau
    preview = discord.Embed(
        title=f"👋 Willkommen auf {interaction.guild.name}!",
        description=(nachricht or "Willkommen {user} auf **{server}**! Du bist Mitglied #{count}. 🎉")
            .replace("{user}", interaction.user.mention)
            .replace("{name}", interaction.user.display_name)
            .replace("{server}", interaction.guild.name)
            .replace("{count}", str(interaction.guild.member_count)),
        color=discord.Color.from_rgb(100, 220, 150)
    )
    preview.set_thumbnail(url=interaction.user.display_avatar.url)
    if bild_url:
        preview.set_image(url=bild_url)
    preview.set_footer(text="Vorschau – so sieht die Willkommensnachricht aus")

    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Willkommen-Setup gespeichert", f"**Kanal:** {kanal.mention}\n**Bild:** {'✅ gesetzt' if bild_url else '❌ kein Bild'}", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )
    await interaction.followup.send(embed=preview, ephemeral=True)

@tree.command(name="abschied-setup", description="Richtet die Abschiedsnachricht ein (Admin)")
@app_commands.describe(
    kanal="Kanal für Abschiedsnachrichten",
    nachricht="Nachricht ({user} = Mention, {name} = Username, {server} = Servername, {count} = Memberanzahl)",
    bild_url="URL eines Bildes das im Embed angezeigt wird (optional)"
)
async def abschied_setup(interaction: discord.Interaction, kanal: discord.TextChannel, nachricht: str = None, bild_url: str = None):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await load_config()
    if "goodbye" not in cfg:
        cfg["goodbye"] = {}
    cfg["goodbye"][str(interaction.guild.id)] = {
        "channel_id": str(kanal.id),
        "message": nachricht or "**{name}** hat **{server}** verlassen. 👋",
        "image_url": bild_url or None
    }
    await save_config(cfg)

    # Vorschau
    preview = discord.Embed(
        title=f"👋 Auf Wiedersehen!",
        description=(nachricht or "**{name}** hat **{server}** verlassen. 👋")
            .replace("{user}", interaction.user.mention)
            .replace("{name}", interaction.user.display_name)
            .replace("{server}", interaction.guild.name)
            .replace("{count}", str(interaction.guild.member_count)),
        color=discord.Color.from_rgb(220, 80, 80)
    )
    preview.set_thumbnail(url=interaction.user.display_avatar.url)
    if bild_url:
        preview.set_image(url=bild_url)
    preview.set_footer(text="Vorschau – so sieht die Abschiedsnachricht aus")

    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Abschied-Setup gespeichert", f"**Kanal:** {kanal.mention}\n**Bild:** {'✅ gesetzt' if bild_url else '❌ kein Bild'}", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )
    await interaction.followup.send(embed=preview, ephemeral=True)

@tree.command(name="auto-rolle", description="Fügt Auto-Rollen bei Beitritt hinzu oder entfernt sie (Admin)")
@app_commands.describe(
    rolle="Rolle die neue Mitglieder automatisch bekommen",
    aktion="Rolle hinzufügen, entfernen oder Liste anzeigen"
)
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen", value="remove"),
    app_commands.Choice(name="liste anzeigen", value="list"),
])
async def auto_rolle(interaction: discord.Interaction, rolle: discord.Role = None, aktion: str = "add"):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await load_config()
    if "auto_role" not in cfg:
        cfg["auto_role"] = {}

    guild_id = str(interaction.guild.id)

    # Migrate old single-role format to list
    existing = cfg["auto_role"].get(guild_id)
    if isinstance(existing, str):
        cfg["auto_role"][guild_id] = [existing]
    elif existing is None:
        cfg["auto_role"][guild_id] = []

    if aktion == "list":
        role_ids = cfg["auto_role"].get(guild_id, [])
        if not role_ids:
            await interaction.followup.send(embed=liquid_glass_embed("ℹ️ Auto-Rollen", "Keine Auto-Rollen konfiguriert.", discord.Color.from_rgb(150, 150, 255)), ephemeral=True)
        else:
            mentions = []
            for rid in role_ids:
                r = interaction.guild.get_role(int(rid))
                mentions.append(r.mention if r else f"(unbekannte Rolle {rid})")
            await interaction.followup.send(embed=liquid_glass_embed("📋 Auto-Rollen", "\n".join(mentions), discord.Color.from_rgb(150, 150, 255)), ephemeral=True)
        return

    if rolle is None:
        await interaction.followup.send("❌ Bitte eine Rolle angeben!", ephemeral=True)
        return

    role_ids = cfg["auto_role"][guild_id]

    if aktion == "add":
        if str(rolle.id) in role_ids:
            await interaction.followup.send(embed=liquid_glass_embed("⚠️ Auto-Rolle", f"{rolle.mention} ist bereits in der Liste.", discord.Color.from_rgb(255, 165, 0)), ephemeral=True)
            return
        role_ids.append(str(rolle.id))
        await save_config(cfg)
        await interaction.followup.send(embed=liquid_glass_embed("✅ Auto-Rolle hinzugefügt", f"{rolle.mention} wird neuen Mitgliedern automatisch gegeben.\n**Gesamt:** {len(role_ids)} Rolle(n)", discord.Color.from_rgb(100, 220, 150)), ephemeral=True)

    elif aktion == "remove":
        if str(rolle.id) not in role_ids:
            await interaction.followup.send(embed=liquid_glass_embed("⚠️ Auto-Rolle", f"{rolle.mention} ist nicht in der Liste.", discord.Color.from_rgb(255, 165, 0)), ephemeral=True)
            return
        role_ids.remove(str(rolle.id))
        await save_config(cfg)
        await interaction.followup.send(embed=liquid_glass_embed("✅ Auto-Rolle entfernt", f"{rolle.mention} wurde aus den Auto-Rollen entfernt.\n**Gesamt:** {len(role_ids)} Rolle(n)", discord.Color.from_rgb(100, 220, 150)), ephemeral=True)


# ─────────────────────────────────────────────
# /modlog-setup – Ban/Kick Log Kanal
# ─────────────────────────────────────────────

@tree.command(name="modlog-setup", description="Legt den Kanal fest wo Bans und Kicks eingetragen werden (Admin)")
@app_commands.describe(kanal="Der Kanal wo Bans und Kicks geloggt werden sollen")
async def modlog_setup(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    # Kanal direkt aus der Guild holen als Fallback
    channel = interaction.guild.get_channel(kanal.id) or kanal
    log_cfg = await get_log_config(interaction.guild.id)
    log_cfg["ban_log"] = str(channel.id)
    await save_log_config(interaction.guild.id, log_cfg)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Modlog eingerichtet",
            f"Bans und Kicks werden ab jetzt in <#{channel.id}> eingetragen.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

# ─────────────────────────────────────────────
# /modlog-eintrag – Manueller Ban/Kick Eintrag
# ─────────────────────────────────────────────

@tree.command(name="modlog-eintrag", description="Trägt einen Ban oder Kick manuell ins Modlog ein (Mod)")
@app_commands.describe(
    aktion="Ban oder Kick",
    user="Der betroffene User",
    grund="Grund für die Aktion"
)
@app_commands.choices(aktion=[
    app_commands.Choice(name="🔨 Ban", value="ban"),
    app_commands.Choice(name="👢 Kick", value="kick"),
])
async def modlog_eintrag(interaction: discord.Interaction, aktion: str, user: discord.Member, grund: str = "Kein Grund angegeben"):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    log_cfg = await get_log_config(interaction.guild.id)
    ch_id = log_cfg.get("ban_log")
    if not ch_id:
        await interaction.followup.send("❌ Kein Modlog-Kanal eingerichtet! Nutze `/modlog-setup`.", ephemeral=True)
        return

    ch = interaction.guild.get_channel(int(ch_id))
    if not ch:
        await interaction.followup.send("❌ Modlog-Kanal nicht gefunden!", ephemeral=True)
        return

    farbe = discord.Color.from_rgb(220, 60, 60) if aktion == "ban" else discord.Color.from_rgb(255, 140, 0)
    titel = "🔨 Ban eingetragen" if aktion == "ban" else "👢 Kick eingetragen"

    embed = discord.Embed(title=titel, color=farbe, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="User", value=f"{user.mention} ({user})", inline=True)
    embed.add_field(name="Moderator", value=interaction.user.mention, inline=True)
    embed.add_field(name="Grund", value=grund, inline=False)
    embed.set_footer(text=f"User-ID: {user.id}")
    await ch.send(embed=embed)

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Eintrag erstellt",
            f"Der {'Ban' if aktion == 'ban' else 'Kick'} von {user.mention} wurde in <#{ch_id}> eingetragen.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )



class AbmeldungModal(discord.ui.Modal, title="Abmeldung"):
    grund = discord.ui.TextInput(
        label="Grund der Abmeldung",
        placeholder="z.B. Urlaub, krank, keine Zeit...",
        required=True,
        max_length=500
    )
    dauer = discord.ui.TextInput(
        label="Wie lange?",
        placeholder="z.B. 3 Tage, 1 Woche...",
        required=True,
        max_length=100
    )

    def __init__(self, log_channel_id: str):
        super().__init__()
        self.log_channel_id = log_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(int(self.log_channel_id))
        embed = discord.Embed(
            title="📋 Abmeldung",
            color=discord.Color.from_rgb(255, 165, 0),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="Grund", value=self.grund.value, inline=False)
        embed.add_field(name="Dauer", value=self.dauer.value, inline=True)
        embed.set_footer(text=f"ID: {interaction.user.id}")
        if ch:
            await ch.send(embed=embed)
        await interaction.response.send_message(
            embed=liquid_glass_embed(
                "✅ Abmeldung eingereicht",
                f"Deine Abmeldung wurde in {ch.mention if ch else 'dem Log-Kanal'} eingetragen.",
                discord.Color.from_rgb(100, 220, 150)
            ),
            ephemeral=True
        )

class AbmeldungView(discord.ui.View):
    def __init__(self, log_channel_id: str):
        super().__init__(timeout=None)
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="📋 Abmelden", style=discord.ButtonStyle.primary, custom_id="abmeldung_btn")
    async def abmelden(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AbmeldungModal(self.log_channel_id))

# ─────────────────────────────────────────────
# /ingame-log-setup – In-Game Ban/Kick Panel
# ─────────────────────────────────────────────

class IngameBanModal(discord.ui.Modal, title="🔨 In-Game Ban eintragen"):
    spieler = discord.ui.TextInput(label="Spielername / ID", placeholder="z.B. Max_Mustermann", required=True, max_length=100)
    grund = discord.ui.TextInput(label="Grund", placeholder="z.B. Cheating, RDM, Exploiting...", required=True, max_length=500)
    dauer = discord.ui.TextInput(label="Dauer", placeholder="z.B. Permanent, 7 Tage, 24 Stunden", required=True, max_length=100)
    beweis = discord.ui.TextInput(label="Beweis (Link oder Beschreibung)", placeholder="z.B. Screenshot-Link, Clip...", required=False, max_length=500, style=discord.TextStyle.paragraph)

    def __init__(self, log_channel_id: str):
        super().__init__()
        self.log_channel_id = log_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(int(self.log_channel_id))
        embed = discord.Embed(title="🔨 In-Game Ban", color=discord.Color.from_rgb(220, 60, 60), timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Spieler", value=self.spieler.value, inline=True)
        embed.add_field(name="Dauer", value=self.dauer.value, inline=True)
        embed.add_field(name="Grund", value=self.grund.value, inline=False)
        if self.beweis.value:
            embed.add_field(name="Beweis", value=self.beweis.value, inline=False)
        embed.add_field(name="Eingetragen von", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.set_footer(text=f"Mod-ID: {interaction.user.id}")
        if ch:
            await ch.send(embed=embed)
        await interaction.response.send_message(embed=liquid_glass_embed("✅ Ban eingetragen", "Der In-Game Ban wurde erfolgreich eingetragen.", discord.Color.from_rgb(100, 220, 150)), ephemeral=True)

class IngameKickModal(discord.ui.Modal, title="👢 In-Game Kick eintragen"):
    spieler = discord.ui.TextInput(label="Spielername / ID", placeholder="z.B. Max_Mustermann", required=True, max_length=100)
    grund = discord.ui.TextInput(label="Grund", placeholder="z.B. Fail-RP, Störung, NLR...", required=True, max_length=500)
    beweis = discord.ui.TextInput(label="Beweis (Link oder Beschreibung)", placeholder="z.B. Screenshot-Link, Clip...", required=False, max_length=500, style=discord.TextStyle.paragraph)

    def __init__(self, log_channel_id: str):
        super().__init__()
        self.log_channel_id = log_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(int(self.log_channel_id))
        embed = discord.Embed(title="👢 In-Game Kick", color=discord.Color.from_rgb(255, 140, 0), timestamp=datetime.now(timezone.utc))
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Spieler", value=self.spieler.value, inline=True)
        embed.add_field(name="Grund", value=self.grund.value, inline=False)
        if self.beweis.value:
            embed.add_field(name="Beweis", value=self.beweis.value, inline=False)
        embed.add_field(name="Eingetragen von", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.set_footer(text=f"Mod-ID: {interaction.user.id}")
        if ch:
            await ch.send(embed=embed)
        await interaction.response.send_message(embed=liquid_glass_embed("✅ Kick eingetragen", "Der In-Game Kick wurde erfolgreich eingetragen.", discord.Color.from_rgb(100, 220, 150)), ephemeral=True)

class IngameLogView(discord.ui.View):
    def __init__(self, log_channel_id: str):
        super().__init__(timeout=None)
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="🔨 In-Game Ban", style=discord.ButtonStyle.danger, custom_id="ingame_ban")
    async def ingame_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IngameBanModal(self.log_channel_id))

    @discord.ui.button(label="👢 In-Game Kick", style=discord.ButtonStyle.primary, custom_id="ingame_kick")
    async def ingame_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IngameKickModal(self.log_channel_id))

@tree.command(name="ingame-log-setup", description="Erstellt ein Panel zum Eintragen von In-Game Bans/Kicks (Admin)")
@app_commands.describe(
    panel_kanal="Kanal wo die Buttons erscheinen",
    log_kanal="Kanal wo die Einträge geloggt werden"
)
async def ingame_log_setup(interaction: discord.Interaction, panel_kanal: discord.TextChannel, log_kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="🎮 In-Game Strafen",
        description="Hier kannst du In-Game Bans und Kicks eintragen.\nKlicke auf den entsprechenden Button und fülle das Formular aus.",
        color=discord.Color.from_rgb(220, 60, 60)
    )
    embed.add_field(name="🔨 In-Game Ban", value="Spieler wurde dauerhaft oder temporär gebannt.", inline=True)
    embed.add_field(name="👢 In-Game Kick", value="Spieler wurde aus dem Server gekickt.", inline=True)
    embed.set_footer(text=f"{interaction.guild.name} • Moderation")

    view = IngameLogView(str(log_kanal.id))
    await panel_kanal.send(embed=embed, view=view)

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ In-Game Log eingerichtet",
            f"**Panel:** {panel_kanal.mention}\n**Log-Kanal:** {log_kanal.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="abmeldung-setup", description="Richtet das Abmeldungs-System ein (Admin)")
@app_commands.describe(
    panel_kanal="Kanal wo der Abmelde-Button gepostet wird",
    log_kanal="Kanal wo Abmeldungen eingetragen werden"
)
async def abmeldung_setup(interaction: discord.Interaction, panel_kanal: discord.TextChannel, log_kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="📋 Abmeldung",
        description="Möchtest du dich abmelden?\nKlicke auf den Button unten und fülle das Formular aus.",
        color=discord.Color.from_rgb(255, 165, 0)
    )
    embed.set_footer(text=f"{interaction.guild.name} • Abmeldungs-System")

    view = AbmeldungView(str(log_kanal.id))
    await panel_kanal.send(embed=embed, view=view)

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Abmeldungs-System eingerichtet",
            f"**Panel:** {panel_kanal.mention}\n**Log-Kanal:** {log_kanal.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )


token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN nicht gesetzt!")

import time
print("[INFO] Warte 15 Sekunden vor dem Start (Rate-Limit-Schutz)...")
time.sleep(15)


bot.run(token)

