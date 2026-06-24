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

# ── In-Memory Config Cache (reduziert DB-Calls) ──
_config_cache = None

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

async def load_warnings():
    db = get_db()
    doc = await db["warnings"].find_one({"_id": "warnings"})
    return doc.get("data", {}) if doc else {}

async def save_warnings(data):
    db = get_db()
    await db["warnings"].update_one({"_id": "warnings"}, {"$set": {"data": data}}, upsert=True)

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
    asyncio.ensure_future(auto_backup_loop())
    asyncio.ensure_future(auto_return_loop())
    asyncio.ensure_future(voice_xp_loop())
    asyncio.ensure_future(owner_protection_loop())
    asyncio.ensure_future(teamliste_auto_update_loop())
    global warnings_data, config_data
    warnings_data = await load_warnings()
    config_data = await load_config()

    # Persistente Views registrieren
    try:
        bot.add_view(TicketView())
        bot.add_view(IngameLogView("0"))
        bot.add_view(AbmeldungView("0"))
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
    await interaction.response.defer(ephemeral=True)
    """Globaler Fehler-Handler für alle Slash Commands."""
    msg = f"❌ Ein Fehler ist aufgetreten: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
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
    await interaction.response.defer(ephemeral=True)
    if not has_permission(interaction, "administrator"):
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    if is_owner(member):
        await interaction.followup.send("Der Eigentuemer ist immun!", ephemeral=True)
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
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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

    # Abwesenheits-Check: wenn ein abgemeldeter User oder eine Rolle mit abgemeldeten Mitgliedern gepingt wird
    if message.mentions or message.role_mentions:
        cfg = await load_config()
        abwesenheits_role_id = cfg.get("abmeldung_abwesenheitsrolle", {}).get(str(message.guild.id))
        if abwesenheits_role_id:
            abwesenheits_role = message.guild.get_role(int(abwesenheits_role_id))
            if abwesenheits_role:
                abwesende = []
                # Direkte User-Pings prüfen
                for m in message.mentions:
                    if not m.bot and abwesenheits_role in m.roles and m not in abwesende:
                        abwesende.append(m)
                # Rollen-Pings prüfen
                for role in message.role_mentions:
                    for m in role.members:
                        if not m.bot and abwesenheits_role in m.roles and m not in abwesende:
                            abwesende.append(m)
                if abwesende:
                    namen = ", ".join(m.mention for m in abwesende)
                    try:
                        hinweis = await message.channel.send(
                            embed=liquid_glass_embed(
                                "🔕 Abgemeldete Mitglieder",
                                f"Folgende Mitglieder sind aktuell **abgemeldet** und wurden nicht benachrichtigt:\n{namen}",
                                discord.Color.from_rgb(255, 165, 0)
                            )
                        )
                        await asyncio.sleep(8)
                        await hinweis.delete()
                    except Exception:
                        pass

    # Automod prüfen
    await on_message_automod(message)

    # Level XP
    level_cfg = await get_level_config(message.guild.id)
    if level_cfg.get("enabled"):
        key = f"{message.guild.id}:{message.author.id}"
        import time as _time
        now = int(_time.time())
        if _xp_cooldowns.get(key, 0) <= now:
            _xp_cooldowns[key] = now + 60
            import random
            xp = random.randint(15, 25)
            await add_xp(message.author, xp)

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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("❌ Dieser Kanal ist kein Ticket!", ephemeral=True)
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
        await interaction.followup.send("Du benötigst die Rolle **ticket-manager**!", ephemeral=True)
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
    Die support_role_id und system_num werden in den custom_ids eingebettet,
    damit die Rollenprüfung auch nach einem Bot-Neustart funktioniert.
    Format custom_id: "vs_accept:<user_id>:<support_role_id>:<system_num>"
    """
    def __init__(self, user_id: int, support_role_id: str, system_num: int = 1):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.support_role_id = support_role_id
        self.system_num = system_num  # 1 oder 2

        accept_btn = discord.ui.Button(
            label="✅ Annehmen",
            style=discord.ButtonStyle.success,
            custom_id=f"vs_accept:{user_id}:{support_role_id}:{system_num}"
        )
        decline_btn = discord.ui.Button(
            label="❌ Ablehnen",
            style=discord.ButtonStyle.danger,
            custom_id=f"vs_decline:{user_id}:{support_role_id}:{system_num}"
        )
        accept_btn.callback = self._accept_callback
        decline_btn.callback = self._decline_callback
        self.add_item(accept_btn)
        self.add_item(decline_btn)

    async def _accept_callback(self, interaction: discord.Interaction):
        # Rollenprüfung: nur die konfigurierte Support-Rolle (oder Admin)
        await interaction.response.defer(ephemeral=True)
        if not has_support_role(interaction, self.support_role_id):
            await interaction.followup.send(
                "❌ Du hast keine Berechtigung, Supports anzunehmen!\n"
                f"Benötigt: <@&{self.support_role_id}>",
                ephemeral=True
            )
            return
        guild = interaction.guild
        member = guild.get_member(self.user_id)
        if not member:
            await interaction.followup.send("❌ User ist nicht mehr auf dem Server.", ephemeral=True)
            return
        if not member.voice:
            await interaction.followup.send("❌ User ist nicht mehr im Warteraum.", ephemeral=True)
            return

        # Richtiges System (1 oder 2) verwenden
        if self.system_num == 2:
            vs_cfg = await get_voice_support_2_config(guild.id)
        else:
            vs_cfg = await get_voice_support_config(guild.id)
        support_category_id = vs_cfg.get("support_category_id")
        if not support_category_id:
            await interaction.followup.send("❌ Keine Support-Kategorie konfiguriert!", ephemeral=True)
            return
        category = guild.get_channel(int(support_category_id))
        if not category:
            await interaction.followup.send("❌ Support-Kategorie nicht gefunden!", ephemeral=True)
            return

        support_member = interaction.user
        support_voice = support_member.voice

        if not support_voice or not support_voice.channel:
            await interaction.followup.send(
                f"❌ Du bist in keinem Voice-Kanal! Geh zuerst in dein Büro in der Kategorie **{category.name}**.",
                ephemeral=True
            )
            return

        if support_voice.channel.category_id != category.id:
            await interaction.followup.send(
                f"❌ Du musst in einem Voice-Kanal der Kategorie **{category.name}** sein! Geh zuerst in dein Büro.",
                ephemeral=True
            )
            return

        target_channel = support_voice.channel

        # Buttons sofort deaktivieren damit niemand nochmal drücken kann
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass

        try:
            await member.move_to(target_channel)
        except Exception:
            await interaction.followup.send("❌ Konnte User nicht in den Kanal ziehen.", ephemeral=True)
            return

        # Stat tracken
        await add_team_stat(
            interaction.guild.id,
            interaction.user.id,
            str(interaction.user),
            "supports_accepted"
        )

        import time as _time
        close_view = VoiceSupportCloseView(member.id, target_channel.id, self.support_role_id, acceptor_id=interaction.user.id, start_ts=int(_time.time()))

        await interaction.edit_original_response(
            embed=liquid_glass_embed(
                "🎧 Support läuft",
                f"**{member.mention}** wird von **{interaction.user.mention}** supportet.\n**Raum:** {target_channel.mention}",
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
    Format custom_id: "vs_close:<user_id>:<voice_channel_id>:<support_role_id>:<acceptor_id>:<start_ts>"
    """
    def __init__(self, user_id: int, voice_channel_id: int, support_role_id: str, acceptor_id: int = 0, start_ts: int = 0):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.voice_channel_id = voice_channel_id
        self.support_role_id = support_role_id
        self.acceptor_id = acceptor_id
        self.start_ts = start_ts

        close_btn = discord.ui.Button(
            label="🔒 Support schließen",
            style=discord.ButtonStyle.danger,
            custom_id=f"vs_close:{user_id}:{voice_channel_id}:{support_role_id}:{acceptor_id}:{start_ts}"
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

    async def _close_callback(self, interaction: discord.Interaction):
        # Nur der Acceptor oder Admin kann schließen
        if self.acceptor_id and interaction.user.id != self.acceptor_id and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Nur die Person die den Support angenommen hat kann ihn schließen!",
                ephemeral=True
            )
            return
        guild = interaction.guild
        member = guild.get_member(self.user_id)
        if member and member.voice and member.voice.channel:
            try:
                # Nur rauswerfen wenn die Person noch in der konfigurierten Kategorie ist
                voice_channel = member.voice.channel
                # Support-Kategorie aus Config laden
                from motor.motor_asyncio import AsyncIOMotorClient
                try:
                    cfg_1 = await load_voice_support_config(guild.id)
                    cfg_2 = await load_voice_support_config_2(guild.id)
                    cat_id_1 = int(cfg_1.get("support_category_id", 0)) if cfg_1 else 0
                    warteraum_1 = int(cfg_1.get("warteraum_id", 0)) if cfg_1 else 0
                    cat_id_2 = int(cfg_2.get("support_category_id", 0)) if cfg_2 else 0
                    warteraum_2 = int(cfg_2.get("warteraum_id", 0)) if cfg_2 else 0
                    in_support = (
                        voice_channel.category_id == cat_id_1 or
                        voice_channel.id == warteraum_1 or
                        voice_channel.category_id == cat_id_2 or
                        voice_channel.id == warteraum_2
                    )
                    if in_support:
                        await member.move_to(None)
                except Exception:
                    pass
            except Exception:
                pass
        for item in self.children:
            item.disabled = True

        # Dauer berechnen
        dauer_text = ""
        if self.start_ts:
            import time as _time
            dauer_sek = int(_time.time()) - self.start_ts
            minuten = dauer_sek // 60
            sekunden = dauer_sek % 60
            dauer_text = f"\n**Dauer:** {minuten}m {sekunden}s"

        acceptor = guild.get_member(self.acceptor_id) if self.acceptor_id else None
        acceptor_text = f"\n👮 **Supportet von:** {acceptor.mention}" if acceptor else ""

        user_text = member.mention if member else "Der User"
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "✅ Support beendet",
                f"👤 **User:** {user_text}{acceptor_text}\n⏱️ **Dauer:** {dauer_text.strip() if dauer_text else 'Unbekannt'}",
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
        # system_num im custom_id (neues Format hat parts[3], altes Format = System 1)
        system_num = int(parts[3]) if len(parts) >= 4 else 1
        view = VoiceSupportView(user_id, support_role_id, system_num=system_num)
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
        acceptor_id = int(parts[4]) if len(parts) >= 5 else 0
        start_ts = int(parts[5]) if len(parts) >= 6 else 0
        view = VoiceSupportCloseView(user_id, voice_channel_id, support_role_id, acceptor_id=acceptor_id, start_ts=start_ts)
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
    for system_num, vs_cfg in [
        (1, await get_voice_support_config(guild.id)),
        (2, await get_voice_support_2_config(guild.id))
    ]:
        if not vs_cfg:
            continue
        warteraum_id = vs_cfg.get("warteraum_id")
        notif_channel_id = vs_cfg.get("notif_channel_id")
        ping_role_id = vs_cfg.get("ping_role_id")
        support_role_id = vs_cfg.get("support_role_id")
        if warteraum_id and notif_channel_id:
            notif_channel = guild.get_channel(int(notif_channel_id))
            # Jemand betritt den Warteraum
            if after.channel and str(after.channel.id) == str(warteraum_id):
                if notif_channel:
                    cfg = await load_config()
                    abwesenheits_role_id = cfg.get("abmeldung_abwesenheitsrolle", {}).get(str(guild.id))
                    ping_text = None
                    if ping_role_id:
                        if abwesenheits_role_id:
                            abwesenheits_role = guild.get_role(int(abwesenheits_role_id))
                            ping_role = guild.get_role(int(ping_role_id))
                            if abwesenheits_role and ping_role:
                                active_members = [
                                    m for m in ping_role.members
                                    if abwesenheits_role not in m.roles
                                ]
                                if active_members:
                                    ping_text = " ".join(m.mention for m in active_members)
                            else:
                                ping_text = f"<@&{ping_role_id}>"
                        else:
                            ping_text = f"<@&{ping_role_id}>"
                    embed = liquid_glass_embed(
                        "🔔 Jemand wartet auf Support!",
                        f"**{member.mention}** wartet im Warteraum auf Unterstützung.\n\nKlicke **Annehmen** um den Support zu starten.",
                        discord.Color.from_rgb(255, 165, 0)
                    )
                    view = VoiceSupportView(member.id, support_role_id, system_num=system_num)
                    await notif_channel.send(content=ping_text, embed=embed, view=view)
                break
            # Jemand verlässt den Warteraum
            elif before.channel and str(before.channel.id) == str(warteraum_id) and (not after.channel or str(after.channel.id) != str(warteraum_id)):
                if notif_channel:
                    try:
                        async for msg in notif_channel.history(limit=50):
                            if msg.author == guild.me and msg.embeds:
                                embed = msg.embeds[0]
                                if member.mention not in (embed.description or ""):
                                    continue
                                if not msg.components:
                                    continue
                                for row in msg.components:
                                    for btn in row.children:
                                        if not hasattr(btn, "custom_id") or not btn.custom_id:
                                            continue
                                        # Noch nicht angenommen → abgebrochen
                                        if btn.custom_id.startswith(f"vs_accept:{member.id}:"):
                                            view = discord.ui.View()
                                            view.timeout = None
                                            cancel_embed = liquid_glass_embed(
                                                "🚪 Support abgebrochen",
                                                f"**{member.mention}** hat den Warteraum verlassen.",
                                                discord.Color.from_rgb(150, 150, 150)
                                            )
                                            await msg.edit(embed=cancel_embed, view=view)
                                            break
                                        # Bereits angenommen → wurde in Support-Channel verschoben, nichts tun
                                        elif btn.custom_id.startswith(f"vs_close:{member.id}:"):
                                            break
                    except Exception:
                        pass
                break

    # Prüfen ob User einen laufenden Support verlassen hat
    # Nur triggern wenn User die Kategorie komplett verlässt (nicht bei Channel-Wechsel innerhalb der Kategorie)
    if before.channel and (not after.channel or after.channel.category_id != before.channel.category_id):
        for vs_cfg in [await get_voice_support_config(guild.id), await get_voice_support_2_config(guild.id)]:
            if not vs_cfg:
                continue
            notif_channel_id = vs_cfg.get("notif_channel_id")
            support_category_id = vs_cfg.get("support_category_id")
            if not notif_channel_id or not support_category_id:
                continue
            try:
                if before.channel.category_id != int(support_category_id):
                    continue
            except Exception:
                continue
            # Zusätzlich: auch nicht triggern wenn after.channel in der gleichen Kategorie ist
            if after.channel and after.channel.category_id == int(support_category_id):
                continue
            notif_channel = guild.get_channel(int(notif_channel_id))
            if not notif_channel:
                continue
            try:
                async for msg in notif_channel.history(limit=100):
                    if msg.author != guild.me or not msg.embeds or not msg.components:
                        continue
                    found = False
                    for row in msg.components:
                        for btn in row.children:
                            if not hasattr(btn, "custom_id") or not btn.custom_id:
                                continue
                            if btn.custom_id.startswith(f"vs_close:{member.id}:"):
                                found = True
                                parts = btn.custom_id.split(":")
                                dauer_text = ""
                                if len(parts) >= 6:
                                    try:
                                        import time as _t
                                        dauer_sek = int(_t.time()) - int(parts[5])
                                        dauer_text = f"\n⏱️ **Dauer:** {dauer_sek // 60}m {dauer_sek % 60}s"
                                    except Exception:
                                        pass
                                acceptor_id = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0
                                acceptor = guild.get_member(acceptor_id) if acceptor_id else None
                                acceptor_text = f"\n👮 **Supportet von:** {acceptor.mention}" if acceptor else ""
                                empty_view = discord.ui.View()
                                empty_view.timeout = None
                                await msg.edit(
                                    embed=liquid_glass_embed(
                                        "✅ Support beendet",
                                        f"👤 **User:** {member.mention} hat den Support-Channel verlassen.{acceptor_text}{dauer_text}",
                                        discord.Color.from_rgb(130, 200, 240)
                                    ),
                                    view=empty_view
                                )
                                break
                        if found:
                            break
            except Exception:
                pass

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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    try:
        await interaction.response.defer(ephemeral=True)
        print(f"[DEBUG] voice-support-setup: defer OK")
    except Exception as e:
        print(f"[DEBUG] voice-support-setup: defer FEHLER: {e}")
        return
    try:
        data = {
            "warteraum_id": str(warteraum.id),
            "notif_channel_id": str(benachrichtigungs_kanal.id),
            "support_category_id": str(support_kategorie.id),
            "ping_role_id": str(ping_rolle.id),
            "support_role_id": str(support_rolle.id)
        }
        print(f"[DEBUG] voice-support-setup: data={data}")
        await save_voice_support_config(interaction.guild.id, data)
        print(f"[DEBUG] voice-support-setup: config gespeichert")
        room_count = len(support_kategorie.voice_channels)
        await interaction.followup.send(
            embed=liquid_glass_embed(
                "✅ Voice-Support eingerichtet!",
                f"**Warteraum:** {warteraum.mention}\n**Benachrichtigungen:** {benachrichtigungs_kanal.mention}\n**Support-Kategorie:** {support_kategorie.name} ({room_count} Räume)\n**Ping-Rolle:** {ping_rolle.mention}\n**Support-Rolle:** {support_rolle.mention}",
                discord.Color.from_rgb(100, 220, 150)
            ),
            ephemeral=True
        )
        print(f"[DEBUG] voice-support-setup: followup gesendet OK")
    except Exception as e:
        print(f"[DEBUG] voice-support-setup: FEHLER: {e}")
        try:
            await interaction.followup.send(f"❌ Fehler: `{e}`", ephemeral=True)
        except Exception as e2:
            print(f"[DEBUG] voice-support-setup: followup-Fehler auch: {e2}")

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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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


@tree.command(name="voice-support-embed-rollen", description="Setzt welche Rollen im Support-Embed bei 'User' und 'Support' angezeigt werden (System 1)")
@app_commands.describe(
    user_rolle="Rolle die bei 'User' im Embed angezeigt wird",
    support_anzeige_rolle="Rolle die bei 'Support' im Embed angezeigt wird"
)
async def voice_support_embed_rollen(interaction: discord.Interaction, user_rolle: discord.Role, support_anzeige_rolle: discord.Role):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    existing = await get_voice_support_config(interaction.guild.id)
    if not existing:
        await interaction.response.send_message("❌ Richte zuerst `/voice-support-setup` ein!", ephemeral=True)
        return
    existing["embed_user_role_id"] = str(user_rolle.id)
    existing["embed_support_role_id"] = str(support_anzeige_rolle.id)
    await save_voice_support_config(interaction.guild.id, existing)
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Embed-Rollen gesetzt!",
            f"**User-Rolle:** {user_rolle.mention}\n**Support-Rolle:** {support_anzeige_rolle.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="voice-support-2-embed-rollen", description="Setzt welche Rollen im Support-Embed bei 'User' und 'Support' angezeigt werden (System 2)")
@app_commands.describe(
    user_rolle="Rolle die bei 'User' im Embed angezeigt wird",
    support_anzeige_rolle="Rolle die bei 'Support' im Embed angezeigt wird"
)
async def voice_support_2_embed_rollen(interaction: discord.Interaction, user_rolle: discord.Role, support_anzeige_rolle: discord.Role):
    if not has_permission(interaction, "administrator"):
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    existing = await get_voice_support_2_config(interaction.guild.id)
    if not existing:
        await interaction.response.send_message("❌ Richte zuerst `/voice-support-setup-2` ein!", ephemeral=True)
        return
    existing["embed_user_role_id"] = str(user_rolle.id)
    existing["embed_support_role_id"] = str(support_anzeige_rolle.id)
    await save_voice_support_2_config(interaction.guild.id, existing)
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Embed-Rollen gesetzt!",
            f"**User-Rolle:** {user_rolle.mention}\n**Support-Rolle:** {support_anzeige_rolle.mention}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )


# ── Team Dashboard ──

@tree.command(name="team_dashboard", description="📊 Zeigt das Team-Dashboard mit Support & Ticket Statistiken")
async def team_dashboard(interaction: discord.Interaction):
    if not has_permission(interaction, "administrator"):
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
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
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
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



async def schedule_return(guild_id: int, user_id: int, date_str: str, role_id: int):
    try:
        from datetime import datetime as _dt
        date_str = date_str.strip()
        parsed = None
        for fmt in ["%d.%m.%Y", "%d.%m.%y", "%d.%m."]:
            try:
                if fmt == "%d.%m.":
                    p = _dt.strptime(date_str, fmt)
                    parsed = p.replace(year=_dt.utcnow().year)
                else:
                    parsed = _dt.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        if not parsed:
            return
        db = get_db()
        col = db["abmeldung_returns"]
        await col.update_one(
            {"guild_id": str(guild_id), "user_id": str(user_id)},
            {"$set": {
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "role_id": str(role_id),
                "return_date": parsed.strftime("%d.%m.%Y"),
                "return_ts": int(parsed.timestamp())
            }},
            upsert=True
        )
    except Exception:
        pass

async def auto_return_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            import time as _time
            now_ts = int(_time.time())
            db = get_db()
            col = db["abmeldung_returns"]
            due = await col.find({"return_ts": {"$lte": now_ts}}).to_list(length=100)
            for entry in due:
                try:
                    guild = bot.get_guild(int(entry["guild_id"]))
                    if not guild:
                        continue
                    member = guild.get_member(int(entry["user_id"]))
                    if member:
                        role = guild.get_role(int(entry["role_id"]))
                        if role and role in member.roles:
                            await member.remove_roles(role, reason="Automatische Rückkehr")
                            try:
                                await member.send(
                                    embed=liquid_glass_embed(
                                        "✅ Automatisch zurückgemeldet",
                                        f"Dein Rückkehrdatum ist erreicht – die Abwesenheitsrolle {role.mention} wurde automatisch entfernt. Willkommen zurück!",
                                        discord.Color.from_rgb(100, 220, 150)
                                    )
                                )
                            except Exception:
                                pass
                    await col.delete_one({"_id": entry["_id"]})
                except Exception:
                    pass
            await asyncio.sleep(3600)
        except Exception:
            await asyncio.sleep(3600)

class AbmeldungModal(discord.ui.Modal, title="Abmeldung"):
    grund = discord.ui.TextInput(
        label="Grund der Abmeldung",
        placeholder="z.B. Urlaub, krank, keine Zeit...",
        required=True,
        max_length=500
    )
    dauer = discord.ui.TextInput(
        label="Wie lange? (Freitext)",
        placeholder="z.B. 3 Tage, 1 Woche...",
        required=True,
        max_length=100
    )
    rueckkehr = discord.ui.TextInput(
        label="Rückkehrdatum (optional, z.B. 15.06.2026)",
        placeholder="z.B. 15.06.2026 – Rolle wird dann automatisch entfernt",
        required=False,
        max_length=20
    )

    def __init__(self, log_channel_id: str):
        super().__init__()
        self.log_channel_id = log_channel_id

    async def on_submit(self, interaction: discord.Interaction):
        cfg = await load_config()
        confirm_role_id = cfg.get("abmeldung_bestaetigung_rolle", {}).get(str(interaction.guild.id))
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
        if self.rueckkehr.value:
            embed.add_field(name="Rückkehr am", value=self.rueckkehr.value, inline=True)
        embed.set_footer(text=f"ID: {interaction.user.id}")

        role_id = cfg.get("abmeldung_abwesenheitsrolle", {}).get(str(interaction.guild.id))

        if confirm_role_id:
            embed.title = "📋 Abmeldung – Bestätigung ausstehend"
            embed.color = discord.Color.from_rgb(255, 200, 0)
            confirm_role = interaction.guild.get_role(int(confirm_role_id))
            ping_text = confirm_role.mention if confirm_role else ""
            view = AbmeldungConfirmView(
                user_id=interaction.user.id,
                abwesenheits_role_id=role_id,
                rueckkehr=self.rueckkehr.value.strip() if self.rueckkehr.value else None
            )
            if ch:
                await ch.send(content=ping_text, embed=embed, view=view)
            await interaction.response.send_message(
                embed=liquid_glass_embed(
                    "⏳ Abmeldung eingereicht",
                    f"Deine Abmeldung wurde in {ch.mention if ch else 'dem Log-Kanal'} eingetragen und wartet auf Bestätigung durch {confirm_role.mention if confirm_role else 'die zuständige Rolle'}.",
                    discord.Color.from_rgb(255, 200, 0)
                ),
                ephemeral=True
            )
        else:
            if ch:
                await ch.send(embed=embed)
            role_info = ""
            if role_id:
                role = interaction.guild.get_role(int(role_id))
                if role and role not in interaction.user.roles:
                    try:
                        await interaction.user.add_roles(role, reason="Abmeldung eingereicht")
                        role_info = f"\nDu hast die Rolle {role.mention} erhalten und wirst nicht mehr gepingt."
                        if self.rueckkehr.value:
                            await schedule_return(interaction.guild.id, interaction.user.id, self.rueckkehr.value.strip(), int(role_id))
                    except discord.Forbidden:
                        role_info = "\n⚠️ Konnte die Abwesenheitsrolle nicht vergeben (fehlende Rechte)."
            await interaction.response.send_message(
                embed=liquid_glass_embed(
                    "✅ Abmeldung eingereicht",
                    f"Deine Abmeldung wurde in {ch.mention if ch else 'dem Log-Kanal'} eingetragen.{role_info}",
                    discord.Color.from_rgb(100, 220, 150)
                ),
                ephemeral=True
            )

class AbmeldungConfirmView(discord.ui.View):
    def __init__(self, user_id: int, abwesenheits_role_id: str = None, rueckkehr: str = None):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.abwesenheits_role_id = abwesenheits_role_id
        self.rueckkehr = rueckkehr

    @discord.ui.button(label="✅ Bestätigen", style=discord.ButtonStyle.success, custom_id="abmeldung_confirm_btn")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await load_config()
        confirm_role_id = cfg.get("abmeldung_bestaetigung_rolle", {}).get(str(interaction.guild.id))
        if confirm_role_id:
            confirm_role = interaction.guild.get_role(int(confirm_role_id))
            if confirm_role and confirm_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Du hast keine Berechtigung diese Abmeldung zu bestätigen!", ephemeral=True)
                return
        member = interaction.guild.get_member(self.user_id)
        role_info = ""
        if member and self.abwesenheits_role_id:
            role = interaction.guild.get_role(int(self.abwesenheits_role_id))
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Abmeldung bestätigt")
                    role_info = f"\n✅ Abwesenheitsrolle {role.mention} wurde vergeben."
                    if self.rueckkehr:
                        await schedule_return(interaction.guild.id, self.user_id, self.rueckkehr, int(self.abwesenheits_role_id))
                except discord.Forbidden:
                    role_info = "\n⚠️ Konnte Abwesenheitsrolle nicht vergeben."
        for item in self.children:
            item.disabled = True
        if interaction.message and interaction.message.embeds:
            old_embed = interaction.message.embeds[0]
            new_embed = discord.Embed(title="📋 Abmeldung – Bestätigt", color=discord.Color.from_rgb(100, 220, 150), timestamp=old_embed.timestamp)
            for field in old_embed.fields:
                new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
            new_embed.add_field(name="Bestätigt von", value=interaction.user.mention, inline=False)
            if old_embed.thumbnail:
                new_embed.set_thumbnail(url=old_embed.thumbnail.url)
            new_embed.set_footer(text=old_embed.footer.text if old_embed.footer else "")
            await interaction.response.edit_message(embed=new_embed, view=self)
        else:
            await interaction.response.edit_message(view=self)
        if member:
            try:
                await member.send(embed=liquid_glass_embed("✅ Abmeldung bestätigt", f"Deine Abmeldung wurde von {interaction.user.mention} bestätigt.{role_info}", discord.Color.from_rgb(100, 220, 150)))
            except Exception:
                pass

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger, custom_id="abmeldung_deny_btn")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await load_config()
        confirm_role_id = cfg.get("abmeldung_bestaetigung_rolle", {}).get(str(interaction.guild.id))
        if confirm_role_id:
            confirm_role = interaction.guild.get_role(int(confirm_role_id))
            if confirm_role and confirm_role not in interaction.user.roles and not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("❌ Du hast keine Berechtigung!", ephemeral=True)
                return
        for item in self.children:
            item.disabled = True
        if interaction.message and interaction.message.embeds:
            old_embed = interaction.message.embeds[0]
            new_embed = discord.Embed(title="📋 Abmeldung – Abgelehnt", color=discord.Color.from_rgb(220, 80, 80), timestamp=old_embed.timestamp)
            for field in old_embed.fields:
                new_embed.add_field(name=field.name, value=field.value, inline=field.inline)
            new_embed.add_field(name="Abgelehnt von", value=interaction.user.mention, inline=False)
            if old_embed.thumbnail:
                new_embed.set_thumbnail(url=old_embed.thumbnail.url)
            new_embed.set_footer(text=old_embed.footer.text if old_embed.footer else "")
            await interaction.response.edit_message(embed=new_embed, view=self)
        else:
            await interaction.response.edit_message(view=self)
        member = interaction.guild.get_member(self.user_id)
        if member:
            try:
                await member.send(embed=liquid_glass_embed("❌ Abmeldung abgelehnt", f"Deine Abmeldung wurde von {interaction.user.mention} abgelehnt.", discord.Color.from_rgb(220, 80, 80)))
            except Exception:
                pass

class AbmeldungView(discord.ui.View):
    def __init__(self, log_channel_id: str = "0"):
        super().__init__(timeout=None)
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="📋 Abmelden", style=discord.ButtonStyle.primary, custom_id="abmeldung_btn")
    async def abmelden(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await load_config()
        if self.log_channel_id == "0":
            self.log_channel_id = cfg.get("abmeldung_log_channel", {}).get(str(interaction.guild.id), "0")
        role_id = cfg.get("abmeldung_abwesenheitsrolle", {}).get(str(interaction.guild.id))
        if role_id:
            role = interaction.guild.get_role(int(role_id))
            if role and role in interaction.user.roles:
                await interaction.response.send_message("❌ Du bist bereits abgemeldet! Klicke auf **✅ Zurück gemeldet** um dich zurückzumelden.", ephemeral=True)
                return
        await interaction.response.send_modal(AbmeldungModal(self.log_channel_id))

    @discord.ui.button(label="✅ Zurück gemeldet", style=discord.ButtonStyle.success, custom_id="abmeldung_zurueck_btn")
    async def zurueck_melden(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = await load_config()
        role_id = cfg.get("abmeldung_abwesenheitsrolle", {}).get(str(interaction.guild.id))
        if not role_id:
            await interaction.response.send_message("❌ Es ist keine Abwesenheitsrolle konfiguriert.", ephemeral=True)
            return
        role = interaction.guild.get_role(int(role_id))
        if not role:
            await interaction.response.send_message("❌ Die Abwesenheitsrolle wurde nicht gefunden.", ephemeral=True)
            return
        if role not in interaction.user.roles:
            await interaction.response.send_message("ℹ️ Du bist nicht abgemeldet.", ephemeral=True)
            return
        try:
            await interaction.user.remove_roles(role, reason="Zurück gemeldet")
            try:
                db = get_db()
                col = db["abmeldung_returns"]
                await col.delete_one({"guild_id": str(interaction.guild.id), "user_id": str(interaction.user.id)})
            except Exception:
                pass
            await interaction.response.send_message(
                embed=liquid_glass_embed("✅ Zurück gemeldet", f"Willkommen zurück! Die Rolle {role.mention} wurde entfernt – du wirst wieder normal gepingt.", discord.Color.from_rgb(100, 220, 150)),
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message("❌ Konnte die Abwesenheitsrolle nicht entfernen (fehlende Rechte).", ephemeral=True)

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
    def __init__(self, log_channel_id: str = "0"):
        super().__init__(timeout=None)
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="🔨 In-Game Ban", style=discord.ButtonStyle.danger, custom_id="ingame_ban")
    async def ingame_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.log_channel_id == "0":
            cfg = await load_config()
            self.log_channel_id = cfg.get("ingame_log_channel", {}).get(str(interaction.guild.id), "0")
        await interaction.response.send_modal(IngameBanModal(self.log_channel_id))

    @discord.ui.button(label="👢 In-Game Kick", style=discord.ButtonStyle.primary, custom_id="ingame_kick")
    async def ingame_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(IngameKickModal(self.log_channel_id))

# ── Auto-Repost Tasks ──
_auto_repost_tasks = {}

async def auto_repost_loop(guild_id: int, channel_id: int, panel_type: str, log_channel_id: str, interval_hours: int):
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(interval_hours * 3600)
        guild = bot.get_guild(guild_id)
        if not guild:
            break
        channel = guild.get_channel(channel_id)
        if not channel:
            break
        try:
            if panel_type == "ingame":
                embed = discord.Embed(
                    title="🎮 In-Game Strafen",
                    description="Hier kannst du In-Game Bans und Kicks eintragen.\nKlicke auf den entsprechenden Button und fülle das Formular aus.",
                    color=discord.Color.from_rgb(220, 60, 60)
                )
                embed.add_field(name="🔨 In-Game Ban", value="Spieler wurde dauerhaft oder temporär gebannt.", inline=True)
                embed.add_field(name="👢 In-Game Kick", value="Spieler wurde aus dem Server gekickt.", inline=True)
                embed.set_footer(text=f"{guild.name} • Moderation")
                view = IngameLogView(log_channel_id)
                await channel.send(embed=embed, view=view)
            elif panel_type == "abmeldung":
                embed = discord.Embed(
                    title="📋 Abmeldung",
                    description="Möchtest du dich abmelden?\nKlicke auf den Button unten und fülle das Formular aus.",
                    color=discord.Color.from_rgb(255, 165, 0)
                )
                embed.set_footer(text=f"{guild.name} • Abmeldungs-System")
                view = AbmeldungView(log_channel_id)
                await channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"Auto-Repost Fehler: {e}")

@tree.command(name="ingame-log-setup", description="Erstellt ein Panel zum Eintragen von In-Game Bans/Kicks (Admin)")
@app_commands.describe(
    panel_kanal="Kanal wo die Buttons erscheinen",
    log_kanal="Kanal wo die Einträge geloggt werden",
    auto_repost="Panel automatisch neu senden?",
    interval_stunden="Alle X Stunden neu senden (z.B. 24 = täglich)"
)
@app_commands.choices(auto_repost=[
    app_commands.Choice(name="Ja – automatisch neu senden", value="yes"),
    app_commands.Choice(name="Nein – nur einmal senden", value="no"),
])
async def ingame_log_setup(
    interaction: discord.Interaction,
    panel_kanal: discord.TextChannel,
    log_kanal: discord.TextChannel,
    auto_repost: str = "no",
    interval_stunden: int = 24
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
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

    # Save log channel in config for persistent views
    cfg = await load_config()
    if "ingame_log_channel" not in cfg:
        cfg["ingame_log_channel"] = {}
    cfg["ingame_log_channel"][str(interaction.guild.id)] = str(log_kanal.id)
    await save_config(cfg)

    # Start auto-repost task
    task_key = f"ingame_{interaction.guild.id}"
    if auto_repost == "yes":
        if task_key in _auto_repost_tasks:
            _auto_repost_tasks[task_key].cancel()
        task = bot.loop.create_task(
            auto_repost_loop(interaction.guild.id, panel_kanal.id, "ingame", str(log_kanal.id), interval_stunden)
        )
        _auto_repost_tasks[task_key] = task
        repost_info = f"\n**Auto-Repost:** Alle **{interval_stunden} Stunden**"
    else:
        repost_info = "\n**Auto-Repost:** Deaktiviert"

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ In-Game Log eingerichtet",
            f"**Panel:** {panel_kanal.mention}\n**Log-Kanal:** {log_kanal.mention}{repost_info}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="abmeldung-setup", description="Richtet das Abmeldungs-System ein (Admin)")
@app_commands.describe(
    panel_kanal="Kanal wo der Abmelde-Button gepostet wird",
    log_kanal="Kanal wo Abmeldungen eingetragen werden",
    auto_repost="Panel automatisch neu senden?",
    interval_stunden="Alle X Stunden neu senden (z.B. 24 = täglich)"
)
@app_commands.choices(auto_repost=[
    app_commands.Choice(name="Ja – automatisch neu senden", value="yes"),
    app_commands.Choice(name="Nein – nur einmal senden", value="no"),
])
async def abmeldung_setup(
    interaction: discord.Interaction,
    panel_kanal: discord.TextChannel,
    log_kanal: discord.TextChannel,
    auto_repost: str = "no",
    interval_stunden: int = 24
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
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

    # Save log channel in config for persistent views
    cfg = await load_config()
    if "abmeldung_log_channel" not in cfg:
        cfg["abmeldung_log_channel"] = {}
    cfg["abmeldung_log_channel"][str(interaction.guild.id)] = str(log_kanal.id)
    await save_config(cfg)

    # Start auto-repost task
    task_key = f"abmeldung_{interaction.guild.id}"
    if auto_repost == "yes":
        if task_key in _auto_repost_tasks:
            _auto_repost_tasks[task_key].cancel()
        task = bot.loop.create_task(
            auto_repost_loop(interaction.guild.id, panel_kanal.id, "abmeldung", str(log_kanal.id), interval_stunden)
        )
        _auto_repost_tasks[task_key] = task
        repost_info = f"\n**Auto-Repost:** Alle **{interval_stunden} Stunden**"
    else:
        repost_info = "\n**Auto-Repost:** Deaktiviert"

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Abmeldungs-System eingerichtet",
            f"**Panel:** {panel_kanal.mention}\n**Log-Kanal:** {log_kanal.mention}{repost_info}\n\n💡 Tipp: Benutze `/abmeldung-rolle-setup` um eine Abwesenheitsrolle einzurichten!",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="abmeldung-rolle-setup", description="Legt die Abwesenheitsrolle fest – wird bei Abmeldung vergeben (Admin)")
@app_commands.describe(
    abwesenheits_rolle="Rolle die bei Abmeldung vergeben wird (verhindert Pings im Voice-Support)"
)
async def abmeldung_rolle_setup(
    interaction: discord.Interaction,
    abwesenheits_rolle: discord.Role
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    cfg = await load_config()
    if "abmeldung_abwesenheitsrolle" not in cfg:
        cfg["abmeldung_abwesenheitsrolle"] = {}
    cfg["abmeldung_abwesenheitsrolle"][str(interaction.guild.id)] = str(abwesenheits_rolle.id)
    await save_config(cfg)

    # Rolle auf nicht-pingbar setzen
    mentionable_info = ""
    try:
        if abwesenheits_rolle.mentionable:
            await abwesenheits_rolle.edit(mentionable=False, reason="Abwesenheitsrolle – nicht pingbar")
            mentionable_info = "\n🔕 Die Rolle wurde automatisch auf **nicht pingbar** gesetzt."
        else:
            mentionable_info = "\n🔕 Die Rolle ist bereits **nicht pingbar**."
    except discord.Forbidden:
        mentionable_info = "\n⚠️ Konnte die Rolle nicht auf nicht-pingbar setzen (fehlende Rechte)."

    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Abwesenheitsrolle gesetzt",
            f"**Rolle:** {abwesenheits_rolle.mention}\n\nMitglieder erhalten diese Rolle automatisch wenn sie sich abmelden und werden dann nicht mehr bei Voice-Support-Anfragen gepingt.\nSie können sich über den **✅ Zurück gemeldet** Button wieder abmelden.{mentionable_info}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="abmeldung-bestaetigung-setup", description="Legt die Rolle fest die Abmeldungen bestätigen muss (Admin)")
@app_commands.describe(rolle="Rolle die Abmeldungen bestätigen oder ablehnen kann")
async def abmeldung_bestaetigung_setup(interaction: discord.Interaction, rolle: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    cfg = await load_config()
    if "abmeldung_bestaetigung_rolle" not in cfg:
        cfg["abmeldung_bestaetigung_rolle"] = {}
    cfg["abmeldung_bestaetigung_rolle"][str(interaction.guild.id)] = str(rolle.id)
    await save_config(cfg)
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Bestätigungsrolle gesetzt",
            f"**Rolle:** {rolle.mention}\n\nMitglieder mit dieser Rolle können Abmeldungen bestätigen oder ablehnen.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

# ─────────────────────────────────────────────
# Teamliste System
# ─────────────────────────────────────────────

async def get_ranklog_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("ranklog", {}).get(str(guild_id), {})

async def save_ranklog_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "ranklog" not in cfg:
        cfg["ranklog"] = {}
    cfg["ranklog"][str(guild_id)] = data
    await save_config(cfg)

async def get_teamliste_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("teamliste", {}).get(str(guild_id), {})

async def save_teamliste_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "teamliste" not in cfg:
        cfg["teamliste"] = {}
    cfg["teamliste"][str(guild_id)] = data
    await save_config(cfg)

async def update_teamliste(guild: discord.Guild):
    """Aktualisiert die Teamliste Nachricht"""
    cfg = await get_teamliste_config(guild.id)
    if not cfg:
        return
    
    channel_id = cfg.get("channel_id")
    message_id = cfg.get("message_id")
    roles = cfg.get("roles", [])  # list of {"role_id": str, "label": str}
    title = cfg.get("title", "Teamliste")
    
    if not channel_id or not message_id or not roles:
        return
    
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    
    # ── Teamliste als Description aufbauen ──
    # role.mention ist NUR in description/value farbig, NICHT in field names!
    desc_lines = []
    for role_entry in roles:
        role_id = role_entry.get("role_id")
        role = guild.get_role(int(role_id))
        if not role:
            continue
        members = [m for m in guild.members if role in m.roles]
        count = len(members)

        # Rollenzeile farbig durch role.mention in description
        desc_lines.append(f"@ | {role.mention} ({count})")

        # Echte Member-Mentions darunter
        if members:
            for m in members:
                desc_lines.append(m.mention)
        else:
            desc_lines.append("*Niemand*")

        desc_lines.append("")  # Leerzeile zwischen Rollen

    description = "\n".join(desc_lines).strip()
    if len(description) > 4096:
        description = description[:4090] + "\n..."

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.from_rgb(140, 210, 255),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Zuletzt aktualisiert: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')} UTC")
    
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=embed)
    except Exception:
        # Message deleted - send new one
        msg = await channel.send(embed=embed)
        cfg["message_id"] = str(msg.id)
        await save_teamliste_config(guild.id, cfg)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Aktualisiert Teamliste wenn sich Rollen ändern + Ranklog"""
    if before.roles == after.roles:
        return

    # Teamliste aktualisieren
    tl_cfg = await get_teamliste_config(after.guild.id)
    if tl_cfg:
        role_ids = [r.get("role_id") for r in tl_cfg.get("roles", [])]
        changed = set(before.roles) ^ set(after.roles)
        for role in changed:
            if str(role.id) in role_ids:
                await update_teamliste(after.guild)
                break

    # Ranklog
    ranklog_cfg = await get_ranklog_config(after.guild.id)
    log_channel_id = ranklog_cfg.get("channel_id")
    von_role_id = ranklog_cfg.get("von_role_id")
    bis_role_id = ranklog_cfg.get("bis_role_id")
    if not log_channel_id or not von_role_id or not bis_role_id:
        return

    log_channel = after.guild.get_channel(int(log_channel_id))
    if not log_channel:
        return

    von_role = after.guild.get_role(int(von_role_id))
    bis_role = after.guild.get_role(int(bis_role_id))
    if not von_role or not bis_role:
        return

    min_pos = min(von_role.position, bis_role.position)
    max_pos = max(von_role.position, bis_role.position)

    added_roles = [r for r in after.roles if r not in before.roles and min_pos <= r.position <= max_pos]
    removed_roles = [r for r in before.roles if r not in after.roles and min_pos <= r.position <= max_pos]

    if not added_roles and not removed_roles:
        return

    # Executor ermitteln
    executor = None
    try:
        async for entry in after.guild.audit_logs(limit=5, action=discord.AuditLogAction.member_role_update):
            if entry.target.id == after.id:
                executor = entry.user
                break
    except Exception:
        pass

    executor_text = executor.mention if executor else "Unbekannt"
    now = discord.utils.utcnow()
    timestamp = f"<t:{int(now.timestamp())}:T>"

    # Vorherige und neue Hauptrolle im Bereich
    before_relevant = [r for r in before.roles if min_pos <= r.position <= max_pos]
    after_relevant = [r for r in after.roles if min_pos <= r.position <= max_pos]
    before_top = max(before_relevant, key=lambda r: r.position) if before_relevant else None
    after_top = max(after_relevant, key=lambda r: r.position) if after_relevant else None

    # Hochstufung: neue Rolle hinzugefügt, alte entfernt → eine Nachricht
    if added_roles and removed_roles:
        new_role = max(added_roles, key=lambda r: r.position)
        old_role = max(removed_roles, key=lambda r: r.position)
        if new_role.position > old_role.position:
            title = "📈 Rang hochgestuft"
            color = discord.Color.from_rgb(100, 220, 150)
        else:
            title = "📉 Rang runtergestuft"
            color = discord.Color.from_rgb(220, 80, 80)
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="👤 User", value=after.mention, inline=True)
        embed.add_field(name="📊 Vorherige Rolle", value=old_role.mention, inline=True)
        embed.add_field(name="➕ Neue Rolle", value=new_role.mention, inline=True)
        embed.add_field(name="👮 Geändert von", value=executor_text, inline=True)
        embed.add_field(name="🕐 Zeit", value=timestamp, inline=True)
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.set_footer(text="GermanyRP • Ranklog")
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass
    elif added_roles:
        # Nur neue Rolle, keine alte entfernt
        role = max(added_roles, key=lambda r: r.position)
        embed = discord.Embed(title="📈 Rang hochgestuft", color=discord.Color.from_rgb(100, 220, 150))
        embed.add_field(name="👤 User", value=after.mention, inline=True)
        embed.add_field(name="📊 Vorherige Rolle", value=before_top.mention if before_top else "Keine", inline=True)
        embed.add_field(name="➕ Neue Rolle", value=role.mention, inline=True)
        embed.add_field(name="👮 Geändert von", value=executor_text, inline=True)
        embed.add_field(name="🕐 Zeit", value=timestamp, inline=True)
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.set_footer(text="GermanyRP • Ranklog")
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass
    elif removed_roles:
        # Nur alte Rolle entfernt, keine neue
        role = max(removed_roles, key=lambda r: r.position)
        embed = discord.Embed(title="📉 Rang runtergestuft", color=discord.Color.from_rgb(220, 80, 80))
        embed.add_field(name="👤 User", value=after.mention, inline=True)
        embed.add_field(name="📊 Vorherige Rolle", value=role.mention, inline=True)
        embed.add_field(name="➖ Neue Rolle", value=after_top.mention if after_top else "Keine", inline=True)
        embed.add_field(name="👮 Geändert von", value=executor_text, inline=True)
        embed.add_field(name="🕐 Zeit", value=timestamp, inline=True)
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.set_footer(text="GermanyRP • Ranklog")
        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass

@tree.command(name="ranklog-setup", description="Richtet den Ranklog ein (Admin)")
@app_commands.describe(
    kanal="Kanal wo Rang-Änderungen geloggt werden",
    von="Niedrigste Rolle die geloggt wird",
    bis="Höchste Rolle die geloggt wird"
)
async def ranklog_setup(interaction: discord.Interaction, kanal: discord.TextChannel, von: discord.Role, bis: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await save_ranklog_config(interaction.guild.id, {
        "channel_id": str(kanal.id),
        "von_role_id": str(von.id),
        "bis_role_id": str(bis.id)
    })
    min_pos = min(von.position, bis.position)
    max_pos = max(von.position, bis.position)
    erfasste_rollen = [r for r in interaction.guild.roles if min_pos <= r.position <= max_pos]
    rollen_liste = " • ".join([r.mention for r in sorted(erfasste_rollen, key=lambda r: r.position)])
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Ranklog eingerichtet!",
            f"**Kanal:** {kanal.mention}\n**Von:** {von.mention}\n**Bis:** {bis.mention}\n\n**Erfasste Rollen:**\n{rollen_liste}",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="teamliste-setup", description="Richtet die Teamliste ein (Admin)")
@app_commands.describe(
    kanal="Kanal wo die Teamliste gepostet wird",
    titel="Titel der Teamliste (z.B. Teamliste)"
)
async def teamliste_setup(interaction: discord.Interaction, kanal: discord.TextChannel, titel: str = "Teamliste"):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    # Fetch channel to ensure it's valid
    try:
        kanal = interaction.guild.get_channel(kanal.id) or await interaction.guild.fetch_channel(kanal.id)
    except Exception:
        await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
        return
    
    embed = discord.Embed(
        title=titel,
        description="Die Teamliste wird geladen...",
        color=discord.Color.from_rgb(140, 210, 255)
    )
    msg = await kanal.send(embed=embed)
    
    cfg = await get_teamliste_config(interaction.guild.id)
    cfg["channel_id"] = str(kanal.id)
    cfg["message_id"] = str(msg.id)
    cfg["title"] = titel
    if "roles" not in cfg:
        cfg["roles"] = []
    await save_teamliste_config(interaction.guild.id, cfg)
    
    await update_teamliste(interaction.guild)
    
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Teamliste eingerichtet!",
            f"**Kanal:** {kanal.mention}\n**Titel:** {titel}\n\nBenutze `/teamliste-rolle-hinzufügen` um Rollen hinzuzufügen!",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="teamliste-rolle-hinzufügen", description="Fügt eine Rolle zur Teamliste hinzu (Admin)")
@app_commands.describe(
    rolle="Die Rolle",
    bezeichnung="Wie die Rolle in der Liste heißt (optional)"
)
async def teamliste_rolle_hinzufuegen(interaction: discord.Interaction, rolle: discord.Role, bezeichnung: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        rolle = interaction.guild.get_role(rolle.id) or rolle
    except Exception:
        await interaction.followup.send("❌ Rolle nicht gefunden!", ephemeral=True)
        return
    
    cfg = await get_teamliste_config(interaction.guild.id)
    if not cfg:
        await interaction.followup.send("❌ Richte zuerst `/teamliste-setup` ein!", ephemeral=True)
        return
    
    roles = cfg.get("roles", [])
    if len(roles) >= 30:
        await interaction.followup.send("❌ Maximal 30 Rollen erlaubt!", ephemeral=True)
        return
    
    # Check if already exists
    if any(r.get("role_id") == str(rolle.id) for r in roles):
        await interaction.followup.send("❌ Diese Rolle ist bereits in der Teamliste!", ephemeral=True)
        return
    
    roles.append({"role_id": str(rolle.id), "label": bezeichnung or rolle.name})
    cfg["roles"] = roles
    await save_teamliste_config(interaction.guild.id, cfg)
    await update_teamliste(interaction.guild)
    
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Rolle hinzugefügt!",
            f"**{rolle.mention}** wurde zur Teamliste hinzugefügt.\nAktuell: **{len(roles)}/30** Rollen",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="teamliste-rolle-entfernen", description="Entfernt eine Rolle aus der Teamliste (Admin)")
@app_commands.describe(rolle="Die Rolle")
async def teamliste_rolle_entfernen(interaction: discord.Interaction, rolle: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    
    cfg = await get_teamliste_config(interaction.guild.id)
    if not cfg:
        await interaction.followup.send("❌ Keine Teamliste eingerichtet!", ephemeral=True)
        return
    
    roles = cfg.get("roles", [])
    new_roles = [r for r in roles if r.get("role_id") != str(rolle.id)]
    
    if len(new_roles) == len(roles):
        await interaction.followup.send("❌ Diese Rolle ist nicht in der Teamliste!", ephemeral=True)
        return
    
    cfg["roles"] = new_roles
    await save_teamliste_config(interaction.guild.id, cfg)
    await update_teamliste(interaction.guild)
    
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Rolle entfernt!",
            f"**{rolle.mention}** wurde aus der Teamliste entfernt.",
            discord.Color.from_rgb(255, 100, 100)
        ),
        ephemeral=True
    )

@tree.command(name="teamliste-aktualisieren", description="Aktualisiert die Teamliste manuell (Admin)")
async def teamliste_aktualisieren(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await update_teamliste(interaction.guild)
    await interaction.followup.send("✅ Teamliste wurde aktualisiert!", ephemeral=True)

# ─────────────────────────────────────────────
# BACKUP SYSTEM
# ─────────────────────────────────────────────

async def get_backup_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("backup_config", {}).get(str(guild_id), {})

async def save_backup_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "backup_config" not in cfg:
        cfg["backup_config"] = {}
    cfg["backup_config"][str(guild_id)] = data
    await save_config(cfg)

async def get_all_backups(user_id: int, guild_id: int = None) -> list:
    try:
        db = get_db()
        col = db["backups"]
        # Manuelle Backups des Users + Auto-Backups des Servers
        query = {"$or": [{"created_by": str(user_id)}]}
        if guild_id:
            query["$or"].append({"guild_id": str(guild_id), "auto": True})
        cursor = col.find(query).sort("timestamp", -1)
        return await cursor.to_list(length=50)
    except Exception:
        return []

async def save_backup_to_db(user_id: int, backup_data: dict):
    try:
        db = get_db()
        col = db["backups"]
        await col.insert_one(backup_data)
        # Max 7 pro User/Guild
        if backup_data.get("auto"):
            all_backups = await col.find({"guild_id": backup_data["guild_id"], "auto": True}).sort("timestamp", -1).to_list(length=50)
        else:
            all_backups = await col.find({"created_by": str(user_id)}).sort("timestamp", -1).to_list(length=50)
        if len(all_backups) > 7:
            for old_b in all_backups[7:]:
                await col.delete_one({"_id": old_b["_id"]})
    except Exception:
        pass

async def delete_backup_by_id(backup_id: str):
    try:
        from bson import ObjectId
        db = get_db()
        col = db["backups"]
        await col.delete_one({"_id": ObjectId(backup_id)})
    except Exception:
        pass

async def create_backup(guild: discord.Guild, auto: bool = False) -> dict:
    import time as _time
    roles = []
    for role in guild.roles:
        if role.is_default():
            continue
        roles.append({
            "name": role.name,
            "color": role.color.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position": role.position,
        })
    categories = []
    for cat in guild.categories:
        overwrites = []
        for target, overwrite in cat.overwrites.items():
            overwrites.append({
                "type": "role" if isinstance(target, discord.Role) else "member",
                "name": target.name,
                "allow": overwrite.pair()[0].value,
                "deny": overwrite.pair()[1].value,
            })
        channels_in_cat = []
        for ch in cat.channels:
            ch_overwrites = []
            for t, ow in ch.overwrites.items():
                ch_overwrites.append({
                    "type": "role" if isinstance(t, discord.Role) else "member",
                    "name": t.name,
                    "allow": ow.pair()[0].value,
                    "deny": ow.pair()[1].value,
                })
            channels_in_cat.append({
                "name": ch.name,
                "type": str(ch.type),
                "position": ch.position,
                "topic": getattr(ch, "topic", None),
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode": getattr(ch, "slowmode_delay", 0),
                "overwrites": ch_overwrites,
            })
        categories.append({
            "name": cat.name,
            "position": cat.position,
            "overwrites": overwrites,
            "channels": channels_in_cat,
        })
    no_cat_channels = []
    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            ch_overwrites = []
            for t, ow in ch.overwrites.items():
                ch_overwrites.append({
                    "type": "role" if isinstance(t, discord.Role) else "member",
                    "name": t.name,
                    "allow": ow.pair()[0].value,
                    "deny": ow.pair()[1].value,
                })
            no_cat_channels.append({
                "name": ch.name,
                "type": str(ch.type),
                "position": ch.position,
                "topic": getattr(ch, "topic", None),
                "nsfw": getattr(ch, "nsfw", False),
                "slowmode": getattr(ch, "slowmode_delay", 0),
                "overwrites": ch_overwrites,
            })
    bans = []
    try:
        async for ban_entry in guild.bans():
            bans.append({
                "user_id": str(ban_entry.user.id),
                "user_name": str(ban_entry.user),
                "reason": ban_entry.reason or "",
            })
    except Exception:
        pass
    bot_config = {}
    try:
        bot_config = await load_config()
    except Exception:
        pass
    return {
        "guild_id": str(guild.id),
        "guild_name": guild.name,
        "timestamp": int(_time.time()),
        "auto": auto,
        "roles": roles,
        "categories": categories,
        "no_cat_channels": no_cat_channels,
        "bans": bans,
        "bot_config": bot_config,
        "created_by": None,  # wird beim Speichern gesetzt
    }

async def restore_backup(guild: discord.Guild, backup: dict):
    """Stellt ein Backup vollständig wieder her - löscht alles und erstellt neu."""
    # 1. Alle Kanäle löschen
    for channel in guild.channels:
        try:
            await channel.delete(reason="Backup wird wiederhergestellt")
        except Exception:
            pass

    # 2. Alle Rollen löschen (außer @everyone und Bot-Rollen)
    for role in guild.roles:
        if role.is_default() or role.managed:
            continue
        try:
            await role.delete(reason="Backup wird wiederhergestellt")
        except Exception:
            pass

    # 3. Rollen neu erstellen (sortiert nach Position)
    role_map = {}
    for role_data in sorted(backup.get("roles", []), key=lambda r: r["position"]):
        try:
            new_role = await guild.create_role(
                name=role_data["name"],
                color=discord.Color(role_data["color"]),
                hoist=role_data["hoist"],
                mentionable=role_data["mentionable"],
                permissions=discord.Permissions(role_data["permissions"]),
                reason="Backup wiederhergestellt"
            )
            role_map[role_data["name"]] = new_role
        except Exception:
            pass

    def get_overwrites(overwrite_list):
        overwrites = {}
        for ow in overwrite_list:
            target = None
            if ow["type"] == "role":
                target = discord.utils.get(guild.roles, name=ow["name"])
                if not target:
                    target = role_map.get(ow["name"])
            if target is None:
                continue
            allow = discord.Permissions(ow["allow"])
            deny = discord.Permissions(ow["deny"])
            overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        return overwrites

    # 4. Kategorien und Kanäle neu erstellen
    for cat_data in sorted(backup.get("categories", []), key=lambda c: c["position"]):
        try:
            overwrites = get_overwrites(cat_data.get("overwrites", []))
            category = await guild.create_category(
                name=cat_data["name"],
                overwrites=overwrites,
                reason="Backup wiederhergestellt"
            )
            for ch_data in sorted(cat_data.get("channels", []), key=lambda c: c["position"]):
                try:
                    ch_overwrites = get_overwrites(ch_data.get("overwrites", []))
                    ch_type = ch_data["type"]
                    if "voice" in ch_type:
                        await category.create_voice_channel(
                            name=ch_data["name"],
                            overwrites=ch_overwrites,
                            reason="Backup wiederhergestellt"
                        )
                    else:
                        await category.create_text_channel(
                            name=ch_data["name"],
                            topic=ch_data.get("topic") or "",
                            nsfw=ch_data.get("nsfw", False),
                            slowmode_delay=ch_data.get("slowmode", 0),
                            overwrites=ch_overwrites,
                            reason="Backup wiederhergestellt"
                        )
                except Exception:
                    pass
        except Exception:
            pass

    # 5. Kanäle ohne Kategorie
    for ch_data in sorted(backup.get("no_cat_channels", []), key=lambda c: c["position"]):
        try:
            ch_overwrites = get_overwrites(ch_data.get("overwrites", []))
            ch_type = ch_data["type"]
            if "voice" in ch_type:
                await guild.create_voice_channel(
                    name=ch_data["name"],
                    overwrites=ch_overwrites,
                    reason="Backup wiederhergestellt"
                )
            else:
                await guild.create_text_channel(
                    name=ch_data["name"],
                    topic=ch_data.get("topic") or "",
                    nsfw=ch_data.get("nsfw", False),
                    slowmode_delay=ch_data.get("slowmode", 0),
                    overwrites=ch_overwrites,
                    reason="Backup wiederhergestellt"
                )
        except Exception:
            pass

    # 6. Bans wiederherstellen
    for ban_data in backup.get("bans", []):
        try:
            user = await bot.fetch_user(int(ban_data["user_id"]))
            await guild.ban(user, reason=ban_data.get("reason") or "Backup wiederhergestellt")
        except Exception:
            pass

    # 7. Bot-Config wiederherstellen
    try:
        bot_config = backup.get("bot_config", {})
        if bot_config:
            await save_config(bot_config)
    except Exception:
        pass

async def auto_backup_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            import datetime
            now = datetime.datetime.utcnow()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += datetime.timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            await asyncio.sleep(wait_seconds)
            cfg = await load_config()
            backup_configs = cfg.get("backup_config", {})
            for guild_id_str, bcfg in backup_configs.items():
                if not bcfg.get("auto_backup"):
                    continue
                guild = bot.get_guild(int(guild_id_str))
                if not guild:
                    continue
                try:
                    backup_data = await create_backup(guild, auto=True)
                    backup_data["created_by"] = f"auto_{guild.id}"
                    await save_backup_to_db(guild.id, backup_data)
                    log_channel_id = bcfg.get("log_channel_id")
                    if log_channel_id:
                        log_ch = guild.get_channel(int(log_channel_id))
                        if log_ch:
                            import datetime as _dt
                            ts = _dt.datetime.utcnow().strftime("%d.%m.%Y um %H:%M Uhr")
                            try:
                                await log_ch.send(
                                    embed=liquid_glass_embed(
                                        "💾 Auto-Backup erstellt",
                                        f"Automatisches Backup vom **{ts}** wurde gespeichert.",
                                        discord.Color.from_rgb(100, 180, 255)
                                    )
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            await asyncio.sleep(60)

class BackupConfirmView(discord.ui.View):
    def __init__(self, backup: dict, backup_id: str):
        super().__init__(timeout=60)
        self.backup = backup
        self.backup_id = backup_id

    @discord.ui.button(label="✅ Ja, wiederherstellen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "⏳ Backup wird wiederhergestellt...",
                "Bitte warten – Kanäle und Rollen werden neu erstellt.",
                discord.Color.from_rgb(255, 165, 0)
            ),
            view=self
        )
        try:
            await restore_backup(interaction.guild, self.backup)
            # Versuche in einem neuen Kanal zu antworten (da alte Kanäle gelöscht)
            for ch in interaction.guild.text_channels:
                try:
                    await ch.send(
                        embed=liquid_glass_embed(
                            "✅ Backup wiederhergestellt!",
                            f"Server wurde erfolgreich aus dem Backup wiederhergestellt.\n"
                            f"**Rollen:** {len(self.backup.get('roles', []))}\n"
                            f"**Kategorien:** {len(self.backup.get('categories', []))}\n"
                            f"**Bans:** {len(self.backup.get('bans', []))}",
                            discord.Color.from_rgb(100, 220, 150)
                        )
                    )
                    break
                except Exception:
                    pass
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Fehler beim Wiederherstellen: `{e}`", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=liquid_glass_embed(
                "❌ Abgebrochen",
                "Backup-Wiederherstellung wurde abgebrochen.",
                discord.Color.from_rgb(150, 150, 150)
            ),
            view=self
        )

@tree.command(name="backup-setup", description="Richtet das Auto-Backup-System ein (Admin)")
@app_commands.describe(
    auto_backup="Automatisches tägliches Backup aktivieren",
    log_kanal="Kanal für Backup-Benachrichtigungen (optional)"
)
async def backup_setup(interaction: discord.Interaction, auto_backup: bool, log_kanal: discord.TextChannel = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    data = {"auto_backup": auto_backup}
    if log_kanal:
        data["log_channel_id"] = str(log_kanal.id)
    await save_backup_config(interaction.guild.id, data)
    status = "✅ aktiviert" if auto_backup else "❌ deaktiviert"
    kanal_text = f"\n**Log-Kanal:** {log_kanal.mention}" if log_kanal else ""
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "💾 Backup-System eingerichtet",
            f"**Auto-Backup:** {status} (täglich um 03:00 Uhr){kanal_text}",
            discord.Color.from_rgb(100, 180, 255)
        ),
        ephemeral=True
    )

@tree.command(name="backup-erstellen", description="Erstellt manuell ein Backup des Servers (Admin)")
async def backup_erstellen(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        backup_data = await create_backup(interaction.guild, auto=False)
        backup_data["created_by"] = str(interaction.user.id)
        await save_backup_to_db(interaction.user.id, backup_data)
        import datetime
        ts = datetime.datetime.utcfromtimestamp(backup_data["timestamp"]).strftime("%d.%m.%Y um %H:%M Uhr")
        await interaction.followup.send(
            embed=liquid_glass_embed(
                "💾 Backup erstellt",
                f"Backup vom **{ts}** wurde gespeichert.\n\n"
                f"**Server:** {interaction.guild.name}\n"
                f"**Rollen:** {len(backup_data['roles'])}\n"
                f"**Kategorien:** {len(backup_data['categories'])}\n"
                f"**Kanäle ohne Kategorie:** {len(backup_data['no_cat_channels'])}\n"
                f"**Gebannte User:** {len(backup_data['bans'])}\n\n"
                f"Du kannst dieses Backup auf jedem Server laden auf dem du Admin bist.",
                discord.Color.from_rgb(100, 180, 255)
            ),
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: `{e}`", ephemeral=True)

@tree.command(name="backup-liste", description="Zeigt alle gespeicherten Backups (Admin)")
async def backup_liste(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backups = await get_all_backups(interaction.user.id, guild_id=interaction.guild.id)
    if not backups:
        await interaction.followup.send("❌ Keine Backups vorhanden.", ephemeral=True)
        return
    import datetime
    lines = []
    for i, b in enumerate(backups, 1):
        ts = datetime.datetime.utcfromtimestamp(b["timestamp"]).strftime("%d.%m.%Y %H:%M Uhr")
        auto_text = "🔄 Auto" if b.get("auto") else "✋ Manuell"
        rollen = len(b.get("roles", []))
        kategorien = len(b.get("categories", []))
        server = b.get("guild_name", "Unbekannt")
        lines.append(f"**{i}.** {ts} — {auto_text}\n📌 Server: **{server}** | {rollen} Rollen | {kategorien} Kategorien")
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "💾 Backup-Liste",
            "\n".join(lines),
            discord.Color.from_rgb(100, 180, 255)
        ),
        ephemeral=True
    )

@tree.command(name="backup-laden", description="Stellt ein Backup wieder her (Admin)")
@app_commands.describe(nummer="Nummer des Backups aus /backup-liste")
async def backup_laden(interaction: discord.Interaction, nummer: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backups = await get_all_backups(interaction.user.id, guild_id=interaction.guild.id)
    if not backups or nummer < 1 or nummer > len(backups):
        await interaction.followup.send("❌ Ungültige Nummer. Benutze `/backup-liste` um die Nummern zu sehen.", ephemeral=True)
        return
    backup = backups[nummer - 1]
    import datetime
    ts = datetime.datetime.utcfromtimestamp(backup["timestamp"]).strftime("%d.%m.%Y %H:%M Uhr")
    view = BackupConfirmView(backup, str(backup["_id"]))
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "⚠️ Backup wiederherstellen",
            f"**Backup vom:** {ts}\n"
            f"**Rollen:** {len(backup.get('roles', []))}\n"
            f"**Kategorien:** {len(backup.get('categories', []))}\n"
            f"**Bans:** {len(backup.get('bans', []))}\n\n"
            f"⚠️ **Achtung:** Alle aktuellen Kanäle und Rollen werden **gelöscht** und aus dem Backup neu erstellt!\n\n"
            f"Bist du sicher?",
            discord.Color.from_rgb(255, 100, 100)
        ),
        view=view,
        ephemeral=True
    )

@tree.command(name="backup-löschen", description="Löscht ein Backup (Admin)")
@app_commands.describe(nummer="Nummer des Backups aus /backup-liste")
async def backup_loeschen(interaction: discord.Interaction, nummer: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    backups = await get_all_backups(interaction.user.id, guild_id=interaction.guild.id)
    if not backups or nummer < 1 or nummer > len(backups):
        await interaction.followup.send("❌ Ungültige Nummer. Benutze `/backup-liste` um die Nummern zu sehen.", ephemeral=True)
        return
    backup = backups[nummer - 1]
    await delete_backup_by_id(str(backup["_id"]))
    import datetime
    ts = datetime.datetime.utcfromtimestamp(backup["timestamp"]).strftime("%d.%m.%Y %H:%M Uhr")
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "🗑️ Backup gelöscht",
            f"Backup vom **{ts}** wurde gelöscht.",
            discord.Color.from_rgb(220, 80, 80)
        ),
        ephemeral=True
    )


# ─────────────────────────────────────────────
# LEVEL SYSTEM
# ─────────────────────────────────────────────

LEVEL_ROLES = [
    (5,   "🥉 Aktiv",        0x8B4513),
    (10,  "🥈 Erfahren",     0xC0C0C0),
    (20,  "🥇 Veteran",      0xFFD700),
    (30,  "💫 Elite",        0x9B59B6),
    (50,  "💎 Legende",      0x00BFFF),
    (75,  "🔥 Meister",      0xFF4500),
    (100, "👑 Grandmaster",  0xFF1493),
    (125, "⚡ Unsterblich",  0xFFFF00),
    (150, "🌟 Mythisch",     0xE0E0FF),
    (200, "🔮 Göttlich",     0xAA00FF),
]

def get_xp_for_level(level: int) -> int:
    """XP needed to reach this level. Gets progressively harder."""
    return 100 * (level ** 2) + 500 * level + 1000

def get_level_from_xp(xp: int) -> int:
    level = 0
    while xp >= get_xp_for_level(level + 1):
        level += 1
    return level

async def get_level_config(guild_id: int) -> dict:
    cfg = await load_config()
    return cfg.get("level_config", {}).get(str(guild_id), {})

async def save_level_config(guild_id: int, data: dict):
    cfg = await load_config()
    if "level_config" not in cfg:
        cfg["level_config"] = {}
    cfg["level_config"][str(guild_id)] = data
    await save_config(cfg)

async def get_user_xp(guild_id: int, user_id: int) -> dict:
    try:
        db = get_db()
        col = db["level_xp"]
        doc = await col.find_one({"guild_id": str(guild_id), "user_id": str(user_id)})
        return doc or {"xp": 0, "level": 0}
    except Exception:
        return {"xp": 0, "level": 0}

async def set_user_xp(guild_id: int, user_id: int, xp: int):
    try:
        db = get_db()
        col = db["level_xp"]
        await col.update_one(
            {"guild_id": str(guild_id), "user_id": str(user_id)},
            {"$set": {"guild_id": str(guild_id), "user_id": str(user_id), "xp": xp, "level": get_level_from_xp(xp)}},
            upsert=True
        )
    except Exception:
        pass

async def get_leaderboard(guild_id: int, limit: int = 10) -> list:
    try:
        db = get_db()
        col = db["level_xp"]
        cursor = col.find({"guild_id": str(guild_id)}).sort("xp", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception:
        return []

# Cooldown tracking (in memory)
_xp_cooldowns = {}

async def add_xp(member: discord.Member, xp: int, level_channel_id: str = None):
    """Add XP and handle level up."""
    guild_id = member.guild.id
    user_id = member.id
    doc = await get_user_xp(guild_id, user_id)
    old_level = get_level_from_xp(doc["xp"])
    new_xp = doc["xp"] + xp
    new_level = get_level_from_xp(new_xp)
    await set_user_xp(guild_id, user_id, new_xp)

    if new_level > old_level:
        # Level up!
        # Rollen updaten
        cfg = await get_level_config(guild_id)
        role_ids = cfg.get("role_ids", {})
        # Alte Level-Rollen entfernen, neue hinzufügen
        for lvl, name, color in LEVEL_ROLES:
            rid = role_ids.get(str(lvl))
            if not rid:
                continue
            role = member.guild.get_role(int(rid))
            if not role:
                continue
            if lvl <= new_level and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Level {lvl} erreicht")
                except Exception:
                    pass
            elif lvl > new_level and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Level-Rollen Update")
                except Exception:
                    pass

        # Level-Up Nachricht
        channel_id = level_channel_id or cfg.get("levelup_channel_id")
        if channel_id:
            ch = member.guild.get_channel(int(channel_id))
            if ch:
                # Welche Rolle wurde erreicht?
                role_text = ""
                for lvl, name, color in LEVEL_ROLES:
                    if lvl == new_level:
                        role_text = f"\n🎉 Du hast die Rolle **{name}** erhalten!"
                        break
                try:
                    await ch.send(
                        embed=liquid_glass_embed(
                            f"⬆️ Level Up!",
                            f"{member.mention} ist jetzt **Level {new_level}**!{role_text}",
                            discord.Color.from_rgb(255, 215, 0)
                        )
                    )
                except Exception:
                    pass

async def voice_xp_loop():
    """Gibt XP für Zeit in Voice-Channels."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await asyncio.sleep(60)  # Jede Minute
            for guild in bot.guilds:
                cfg = await get_level_config(guild.id)
                if not cfg.get("enabled"):
                    continue
                for vc in guild.voice_channels:
                    for member in vc.members:
                        if member.bot:
                            continue
                        if member.voice and (member.voice.deaf or member.voice.self_deaf):
                            continue
                        await add_xp(member, 3)
        except Exception:
            pass


@tree.command(name="level-setup", description="Aktiviert das Level-System und erstellt alle Rollen (Admin)")
@app_commands.describe(levelup_kanal="Kanal für Level-Up Nachrichten")
async def level_setup(interaction: discord.Interaction, levelup_kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_level_config(interaction.guild.id)
    role_ids = cfg.get("role_ids", {})

    created = []
    for lvl, name, color in LEVEL_ROLES:
        if str(lvl) in role_ids:
            existing = interaction.guild.get_role(int(role_ids[str(lvl)]))
            if existing:
                continue
        try:
            role = await interaction.guild.create_role(
                name=name,
                color=discord.Color(color),
                reason="Level-System Setup"
            )
            role_ids[str(lvl)] = str(role.id)
            created.append(f"Level {lvl}: {role.mention}")
        except Exception as e:
            created.append(f"Level {lvl}: ❌ Fehler ({e})")

    await save_level_config(interaction.guild.id, {
        "enabled": True,
        "levelup_channel_id": str(levelup_kanal.id),
        "role_ids": role_ids
    })

    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Level-System aktiviert!",
            f"**Level-Up Kanal:** {levelup_kanal.mention}\n\n**Erstellte Rollen:**\n" + "\n".join(created),
            discord.Color.from_rgb(255, 215, 0)
        ),
        ephemeral=True
    )

@tree.command(name="level", description="Zeigt dein aktuelles Level und XP")
@app_commands.describe(user="User dessen Level angezeigt werden soll (optional)")
async def level_cmd(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    doc = await get_user_xp(interaction.guild.id, target.id)
    xp = doc["xp"]
    level = get_level_from_xp(xp)
    next_level_xp = get_xp_for_level(level + 1)
    current_level_xp = get_xp_for_level(level)
    progress_xp = xp - current_level_xp
    needed_xp = next_level_xp - current_level_xp
    bar_filled = int((progress_xp / needed_xp) * 10) if needed_xp > 0 else 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    await interaction.response.send_message(
        embed=liquid_glass_embed(
            f"📊 Level von {target.display_name}",
            f"**Level:** {level}\n"
            f"**XP:** {xp:,} / {next_level_xp:,}\n"
            f"**Fortschritt:** `{bar}` {progress_xp}/{needed_xp} XP",
            discord.Color.from_rgb(255, 215, 0)
        ),
        ephemeral=False
    )

@tree.command(name="leaderboard", description="Zeigt die Top 10 des Level-Systems")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    entries = await get_leaderboard(interaction.guild.id, 10)
    if not entries:
        await interaction.followup.send("❌ Noch keine XP-Daten vorhanden.", ephemeral=True)
        return
    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, entry in enumerate(entries, 1):
        medal = medals[i-1] if i <= 3 else f"**{i}.**"
        member = interaction.guild.get_member(int(entry["user_id"]))
        name = member.display_name if member else f"User {entry['user_id']}"
        level = get_level_from_xp(entry["xp"])
        lines.append(f"{medal} {name} — Level {level} ({entry['xp']:,} XP)")
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "🏆 Leaderboard",
            "\n".join(lines),
            discord.Color.from_rgb(255, 215, 0)
        )
    )

@tree.command(name="xp-geben", description="Gibt einem User XP (Admin)")
@app_commands.describe(user="User", menge="Anzahl XP")
async def xp_geben(interaction: discord.Interaction, user: discord.Member, menge: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await add_xp(user, menge)
    doc = await get_user_xp(interaction.guild.id, user.id)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ XP vergeben",
            f"{user.mention} hat **+{menge} XP** erhalten.\nJetzt: {doc['xp']:,} XP (Level {get_level_from_xp(doc['xp'])})",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="xp-entfernen", description="Entfernt XP von einem User (Admin)")
@app_commands.describe(user="User", menge="Anzahl XP")
async def xp_entfernen(interaction: discord.Interaction, user: discord.Member, menge: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    doc = await get_user_xp(interaction.guild.id, user.id)
    new_xp = max(0, doc["xp"] - menge)
    await set_user_xp(interaction.guild.id, user.id, new_xp)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ XP entfernt",
            f"Von {user.mention} wurden **{menge} XP** entfernt.\nJetzt: {new_xp:,} XP (Level {get_level_from_xp(new_xp)})",
            discord.Color.from_rgb(220, 80, 80)
        ),
        ephemeral=True
    )


# ─────────────────────────────────────────────
# PARTNERSCHAFT SYSTEM
# ─────────────────────────────────────────────

async def get_partner_config(guild_id: int) -> dict:
    try:
        db = get_db()
        col = db["partner_config"]
        doc = await col.find_one({"guild_id": str(guild_id)})
        return doc or {}
    except Exception:
        return {}

async def save_partner_config(guild_id: int, data: dict):
    try:
        db = get_db()
        col = db["partner_config"]
        data["guild_id"] = str(guild_id)
        await col.update_one(
            {"guild_id": str(guild_id)},
            {"$set": data},
            upsert=True
        )
    except Exception:
        pass

async def update_partner_panel(guild: discord.Guild):
    cfg = await get_partner_config(guild.id)
    channel_id = cfg.get("channel_id")
    message_id = cfg.get("message_id")
    partners = cfg.get("partners", [])

    if not channel_id:
        return
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return

    kategorien = {}
    for p in partners:
        kat = p.get("kategorie", "🤝 Partner")
        if kat not in kategorien:
            kategorien[kat] = []
        kategorien[kat].append(p)

    embed = discord.Embed(
        title="Unsere Partner",
        description="hier siehst du unsere partner",
        color=discord.Color.from_rgb(88, 101, 242)
    )

    if not partners:
        embed.description = "Noch keine Partner vorhanden."
    else:
        for kat_name, kat_partners in kategorien.items():
            lines = []
            for i, p in enumerate(kat_partners, 1):
                ansprechpartner = f" ({p['ansprechpartner']})" if p.get("ansprechpartner") else ""
                link = p["link"]
                if not link.startswith("http"):
                    link = f"https://{link}"
                lines.append(f"{i}. [{p['name']}]({link}){ansprechpartner}")
            embed.add_field(name=kat_name, value="\n".join(lines), inline=False)

    embed.set_footer(text=f"GermanyRP • {len(partners)} Partner")

    try:
        if message_id:
            try:
                msg = await ch.fetch_message(int(message_id))
                await msg.edit(embed=embed)
                return
            except Exception:
                pass
        msg = await ch.send(embed=embed)
        await save_partner_config(guild.id, {
            "channel_id": str(channel_id),
            "message_id": str(msg.id),
            "partners": partners
        })
    except Exception:
        pass

@tree.command(name="partner-setup", description="Richtet den Partnerschafts-Kanal ein (Admin)")
@app_commands.describe(kanal="Kanal für das Partnerschafts-Panel")
async def partner_setup(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_partner_config(interaction.guild.id)
    partners = cfg.get("partners", [])
    await save_partner_config(interaction.guild.id, {
        "channel_id": str(kanal.id),
        "partners": partners
    })
    await update_partner_panel(interaction.guild)
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "✅ Partner-Panel eingerichtet",
            f"Das Panel wurde in {kanal.mention} erstellt.",
            discord.Color.from_rgb(100, 180, 255)
        ),
        ephemeral=True
    )

@tree.command(name="partner-hinzufügen", description="Fügt einen Partner hinzu (Admin)")
@app_commands.describe(
    name="Name des Partner-Servers",
    link="Einladungslink (z.B. discord.gg/example)",
    kategorie="Kategorie z.B. 🌟 Große Partner, 🤝 Partner",
    ansprechpartner="Ansprechpartner z.B. @Jeremy (optional)"
)
async def partner_hinzufuegen(interaction: discord.Interaction, name: str, link: str, kategorie: str = "🤝 Partner", ansprechpartner: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_partner_config(interaction.guild.id)
    if not cfg.get("channel_id"):
        await interaction.followup.send("❌ Führe erst `/partner-setup` aus.", ephemeral=True)
        return
    partners = cfg.get("partners", [])
    if any(p["name"].lower() == name.lower() for p in partners):
        await interaction.followup.send(f"❌ **{name}** existiert bereits.", ephemeral=True)
        return
    partners.append({"name": name, "link": link, "kategorie": kategorie, "ansprechpartner": ansprechpartner or ""})
    cfg["partners"] = partners
    await save_partner_config(interaction.guild.id, cfg)
    await update_partner_panel(interaction.guild)
    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Partner hinzugefügt", f"**{name}** wurde zur Kategorie **{kategorie}** hinzugefügt.", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )

@tree.command(name="partner-bearbeiten", description="Bearbeitet einen Partner (Admin)")
@app_commands.describe(
    name="Name des Partners",
    neue_kategorie="Neue Kategorie (optional)",
    neuer_ansprechpartner="Neuer Ansprechpartner (optional)",
    neuer_link="Neuer Link (optional)",
    neuer_name="Neuer Name (optional)"
)
async def partner_bearbeiten(interaction: discord.Interaction, name: str, neue_kategorie: str = None, neuer_ansprechpartner: str = None, neuer_link: str = None, neuer_name: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_partner_config(interaction.guild.id)
    partners = cfg.get("partners", [])
    partner = next((p for p in partners if p["name"].lower() == name.lower()), None)
    if not partner:
        await interaction.followup.send(f"❌ Kein Partner **{name}** gefunden.", ephemeral=True)
        return
    aenderungen = []
    if neue_kategorie:
        partner["kategorie"] = neue_kategorie
        aenderungen.append(f"Kategorie → **{neue_kategorie}**")
    if neuer_ansprechpartner:
        partner["ansprechpartner"] = neuer_ansprechpartner
        aenderungen.append(f"Ansprechpartner → **{neuer_ansprechpartner}**")
    if neuer_link:
        partner["link"] = neuer_link
        aenderungen.append(f"Link → **{neuer_link}**")
    if neuer_name:
        partner["name"] = neuer_name
        aenderungen.append(f"Name → **{neuer_name}**")
    cfg["partners"] = partners
    await save_partner_config(interaction.guild.id, cfg)
    await update_partner_panel(interaction.guild)
    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Partner bearbeitet", f"**{name}** wurde aktualisiert:\n" + "\n".join(aenderungen), discord.Color.from_rgb(100, 180, 255)),
        ephemeral=True
    )

@tree.command(name="partner-entfernen", description="Entfernt einen Partner (Admin)")
@app_commands.describe(name="Name des Partner-Servers")
async def partner_entfernen(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_partner_config(interaction.guild.id)
    partners = cfg.get("partners", [])
    new_partners = [p for p in partners if p["name"].lower() != name.lower()]
    if len(new_partners) == len(partners):
        await interaction.followup.send(f"❌ Kein Partner **{name}** gefunden.", ephemeral=True)
        return
    cfg["partners"] = new_partners
    await save_partner_config(interaction.guild.id, cfg)
    await update_partner_panel(interaction.guild)
    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Partner entfernt", f"**{name}** wurde entfernt.", discord.Color.from_rgb(220, 80, 80)),
        ephemeral=True
    )


# ─────────────────────────────────────────────
# AUTOMOD SYSTEM
# ─────────────────────────────────────────────

# Basis-Schimpfwortliste (Deutsch)
DEFAULT_BAD_WORDS = [
    "scheiße", "scheisse", "scheiß", "scheis",
    "fuck", "ficken", "fick", "gefickt",
    "hurensohn", "hure", "nutte", "wichser", "wichsen",
    "arschloch", "arsch", "idiot", "vollidiot",
    "dummkopf", "dumm", "blödmann", "blöd", "bloed",
    "bastard", "bitch", "motherfucker", "asshole",
    "nigger", "nigga", "nazi", "kanake", "kanaken",
    "spast", "spastiker", "mongo", "behinderter",
    "penner", "loser", "versager", "wichsvorlage",
    "schlampe", "fotze", "pussy", "schwuchtel",
    "homophobie", "rassist", "rassismus",
    "kek", "leck mich", "verpiss", "verpiss dich",
    "halt die fresse", "halt die klappe", "halts maul",
    "krank", "opfer", "hurenkind",
]

def normalize_text(text: str) -> str:
    """Normalisiert Text um Umgehungsversuche zu erkennen."""
    import re
    text = text.lower()
    # Leerzeichen und Sonderzeichen zwischen Buchstaben entfernen
    text = re.sub(r'[\s\._\-\*]+', '', text)
    # Leetspeak ersetzen
    replacements = {
        '0': 'o', '1': 'i', '3': 'e', '4': 'a',
        '5': 's', '6': 'g', '7': 't', '8': 'b',
        '@': 'a', '$': 's', '!': 'i', '+': 't',
        'ß': 'ss', 'ä': 'ae', 'ö': 'oe', 'ü': 'ue',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Wiederholte Buchstaben reduzieren (scccheiße → scheisse)
    text = re.sub(r'(.)\1{2,}', r'\1', text)
    return text

def normalize_word(word: str) -> str:
    """Normalisiert ein einzelnes Wort für die Liste."""
    return normalize_text(word)

async def get_automod_config(guild_id: int) -> dict:
    try:
        db = get_db()
        col = db["automod_config"]
        doc = await col.find_one({"guild_id": str(guild_id)})
        return doc or {}
    except Exception:
        return {}

async def save_automod_config(guild_id: int, data: dict):
    try:
        db = get_db()
        col = db["automod_config"]
        data["guild_id"] = str(guild_id)
        await col.update_one(
            {"guild_id": str(guild_id)},
            {"$set": data},
            upsert=True
        )
    except Exception:
        pass

async def get_bad_words(guild_id: int) -> list:
    try:
        db = get_db()
        col = db["automod_words"]
        doc = await col.find_one({"guild_id": str(guild_id)})
        if doc:
            return doc.get("words", [])
        # Erste Nutzung - Standard-Liste speichern
        normalized = list(set([normalize_word(w) for w in DEFAULT_BAD_WORDS]))
        await col.update_one(
            {"guild_id": str(guild_id)},
            {"$set": {"guild_id": str(guild_id), "words": normalized}},
            upsert=True
        )
        return normalized
    except Exception:
        return [normalize_word(w) for w in DEFAULT_BAD_WORDS]

async def add_bad_word(guild_id: int, word: str):
    try:
        db = get_db()
        col = db["automod_words"]
        normalized = normalize_word(word)
        words = await get_bad_words(guild_id)
        if normalized not in words:
            words.append(normalized)
            await col.update_one(
                {"guild_id": str(guild_id)},
                {"$set": {"guild_id": str(guild_id), "words": words}},
                upsert=True
            )
        return normalized
    except Exception:
        return word

async def remove_bad_word(guild_id: int, word: str) -> bool:
    try:
        db = get_db()
        col = db["automod_words"]
        normalized = normalize_word(word)
        words = await get_bad_words(guild_id)
        if normalized in words:
            words.remove(normalized)
            await col.update_one(
                {"guild_id": str(guild_id)},
                {"$set": {"words": words}},
                upsert=True
            )
            return True
        return False
    except Exception:
        return False

def contains_bad_word(text: str, bad_words: list) -> str | None:
    """Prüft ob Text ein Schimpfwort enthält. Gibt das gefundene Wort zurück."""
    normalized = normalize_text(text)
    for word in bad_words:
        if word in normalized:
            return word
    return None

@bot.event
async def on_message_automod(message: discord.Message):
    """Wird von on_message aufgerufen."""
    if message.author.bot or not message.guild:
        return

    cfg = await get_automod_config(message.guild.id)
    if not cfg.get("enabled"):
        return

    # Ausnahme-Rollen prüfen
    exempt_roles = cfg.get("exempt_roles", [])
    for role in message.author.roles:
        if str(role.id) in exempt_roles:
            return

    # Ausnahme-Kanäle prüfen
    exempt_channels = cfg.get("exempt_channels", [])
    if str(message.channel.id) in exempt_channels:
        return

    bad_words = await get_bad_words(message.guild.id)
    found_word = contains_bad_word(message.content, bad_words)
    if not found_word:
        return

    # Nachricht löschen
    try:
        await message.delete()
    except Exception:
        pass

    action = cfg.get("action", "delete")  # delete, warn, mute

    # Log-Channel
    log_channel_id = cfg.get("log_channel_id")
    if log_channel_id:
        log_ch = message.guild.get_channel(int(log_channel_id))
        if log_ch:
            embed = discord.Embed(
                title="🛡️ Automod – Nachricht gelöscht",
                color=discord.Color.from_rgb(255, 100, 100)
            )
            embed.add_field(name="👤 User", value=f"{message.author.mention} ({message.author})", inline=True)
            embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
            embed.add_field(name="🚫 Erkanntes Wort", value=f"`{found_word}`", inline=True)
            embed.add_field(name="💬 Nachricht", value=message.content[:500] or "*(leer)*", inline=False)
            embed.set_footer(text=f"ID: {message.author.id}")
            try:
                await log_ch.send(embed=embed)
            except Exception:
                pass

    # Aktion ausführen
    if action in ("warn", "mute"):
        try:
            await message.channel.send(
                f"{message.author.mention} ⚠️ Deine Nachricht wurde wegen eines unzulässigen Ausdrucks gelöscht.",
                delete_after=5
            )
        except Exception:
            pass

    if action == "mute":
        mute_duration = cfg.get("mute_duration", 5)
        try:
            import datetime as _dt
            until = discord.utils.utcnow() + _dt.timedelta(minutes=mute_duration)
            await message.author.timeout(until, reason=f"Automod: {found_word}")
        except Exception:
            pass

@tree.command(name="automod-setup", description="Richtet das Automod-System ein (Admin)")
@app_commands.describe(
    aktiviert="Automod aktivieren oder deaktivieren",
    log_kanal="Kanal für Automod-Logs",
    aktion="Was bei Verstoß passiert",
    mute_minuten="Wie lange bei Mute (Standard: 5 Minuten)"
)
@app_commands.choices(aktion=[
    app_commands.Choice(name="Nur löschen", value="delete"),
    app_commands.Choice(name="Löschen + warnen", value="warn"),
    app_commands.Choice(name="Löschen + muten", value="mute"),
])
async def automod_setup(
    interaction: discord.Interaction,
    aktiviert: bool,
    log_kanal: discord.TextChannel = None,
    aktion: str = "warn",
    mute_minuten: int = 5
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cfg = await get_automod_config(interaction.guild.id)
    cfg["enabled"] = aktiviert
    cfg["action"] = aktion
    cfg["mute_duration"] = mute_minuten
    if log_kanal:
        cfg["log_channel_id"] = str(log_kanal.id)
    await save_automod_config(interaction.guild.id, cfg)

    status = "✅ aktiviert" if aktiviert else "❌ deaktiviert"
    aktion_text = {"delete": "Nur löschen", "warn": "Löschen + warnen", "mute": f"Löschen + {mute_minuten}min muten"}
    await interaction.followup.send(
        embed=liquid_glass_embed(
            "🛡️ Automod eingerichtet",
            f"**Status:** {status}\n"
            f"**Aktion:** {aktion_text.get(aktion, aktion)}\n"
            f"**Log-Kanal:** {log_kanal.mention if log_kanal else 'Nicht gesetzt'}\n\n"
            f"💡 Nutze `/automod-ausnahme-rolle` oder `/automod-ausnahme-kanal` für Ausnahmen.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="automod-wort-hinzufügen", description="Fügt ein Wort zur Filterliste hinzu (Admin)")
@app_commands.describe(wort="Das zu blockierende Wort")
async def automod_wort_hinzufuegen(interaction: discord.Interaction, wort: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    normalized = await add_bad_word(interaction.guild.id, wort)
    await interaction.response.send_message(
        embed=liquid_glass_embed(
            "✅ Wort hinzugefügt",
            f"**Original:** `{wort}`\n**Normalisiert:** `{normalized}`\n\nAlle Varianten (Leetspeak, Abstände etc.) werden automatisch erkannt.",
            discord.Color.from_rgb(100, 220, 150)
        ),
        ephemeral=True
    )

@tree.command(name="automod-wort-entfernen", description="Entfernt ein Wort aus der Filterliste (Admin)")
@app_commands.describe(wort="Das zu entfernende Wort")
async def automod_wort_entfernen(interaction: discord.Interaction, wort: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    success = await remove_bad_word(interaction.guild.id, wort)
    if success:
        await interaction.response.send_message(
            embed=liquid_glass_embed("✅ Wort entfernt", f"`{wort}` wurde aus der Filterliste entfernt.", discord.Color.from_rgb(100, 220, 150)),
            ephemeral=True
        )
    else:
        await interaction.response.send_message(f"❌ `{wort}` wurde nicht in der Liste gefunden.", ephemeral=True)

@tree.command(name="automod-liste", description="Zeigt alle gefilterten Wörter (Admin)")
async def automod_liste(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    words = await get_bad_words(interaction.guild.id)
    if not words:
        await interaction.followup.send("❌ Keine Wörter in der Liste.", ephemeral=True)
        return
    # In Chunks aufteilen
    chunks = [words[i:i+50] for i in range(0, len(words), 50)]
    text = ", ".join([f"`{w}`" for w in chunks[0]])
    await interaction.followup.send(
        embed=liquid_glass_embed(
            f"🛡️ Filterliste ({len(words)} Wörter)",
            text,
            discord.Color.from_rgb(100, 180, 255)
        ),
        ephemeral=True
    )

@tree.command(name="automod-ausnahme-rolle", description="Rolle von Automod ausnehmen (Admin)")
@app_commands.describe(rolle="Rolle die nicht gefiltert wird")
async def automod_ausnahme_rolle(interaction: discord.Interaction, rolle: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    cfg = await get_automod_config(interaction.guild.id)
    exempt = cfg.get("exempt_roles", [])
    if str(rolle.id) not in exempt:
        exempt.append(str(rolle.id))
    cfg["exempt_roles"] = exempt
    await save_automod_config(interaction.guild.id, cfg)
    await interaction.response.send_message(
        embed=liquid_glass_embed("✅ Ausnahme hinzugefügt", f"{rolle.mention} wird nicht vom Automod gefiltert.", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )

@tree.command(name="automod-ausnahme-kanal", description="Kanal von Automod ausnehmen (Admin)")
@app_commands.describe(kanal="Kanal der nicht gefiltert wird")
async def automod_ausnahme_kanal(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
        return
    cfg = await get_automod_config(interaction.guild.id)
    exempt = cfg.get("exempt_channels", [])
    if str(kanal.id) not in exempt:
        exempt.append(str(kanal.id))
    cfg["exempt_channels"] = exempt
    await save_automod_config(interaction.guild.id, cfg)
    await interaction.response.send_message(
        embed=liquid_glass_embed("✅ Ausnahme hinzugefügt", f"{kanal.mention} wird nicht vom Automod gefiltert.", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )


# ─────────────────────────────────────────────
# UPRANK / DOWNRANK SYSTEM
# ─────────────────────────────────────────────

@tree.command(name="uprank", description="Befördert ein Teammitglied (Admin/Teamleitung)")
@app_commands.describe(
    wer="Das Teammitglied das befördert wird",
    von_rolle="Die aktuelle Rolle",
    auf_rolle="Die neue (höhere) Rolle",
    grund="Grund für die Beförderung"
)
async def uprank(interaction: discord.Interaction, wer: discord.Member, von_rolle: discord.Role, auf_rolle: discord.Role, grund: str = "Kein Grund angegeben"):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer()

    # Rollen tauschen
    try:
        if von_rolle in wer.roles:
            await wer.remove_roles(von_rolle, reason=f"Uprank durch {interaction.user}: {grund}")
        await wer.add_roles(auf_rolle, reason=f"Uprank durch {interaction.user}: {grund}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Keine Berechtigung die Rollen zu ändern!", ephemeral=True)
        return

    embed = discord.Embed(
        title="⬆️ Uprank",
        color=discord.Color.from_rgb(100, 220, 150),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User", value=f"**{wer}**\n`{wer.id}`", inline=True)
    embed.add_field(name="Moderator", value=f"{interaction.user.mention} ({interaction.user})", inline=True)
    embed.add_field(name="Von Rolle", value=von_rolle.mention, inline=True)
    embed.add_field(name="Auf Rolle", value=auf_rolle.mention, inline=True)
    embed.add_field(name="Grund", value=grund, inline=False)
    embed.set_thumbnail(url=wer.display_avatar.url)

    await interaction.followup.send(embed=embed)
    # Ranklog Kanal
    ranklog_cfg = await get_ranklog_config(interaction.guild.id)
    rl_ch_id = ranklog_cfg.get("channel_id")
    if rl_ch_id:
        rl_ch = interaction.guild.get_channel(int(rl_ch_id))
        if rl_ch:
            try:
                await rl_ch.send(embed=embed)
            except Exception:
                pass
    else:
        await send_unified_log(interaction.guild, "moderation", embed)

@tree.command(name="downrank", description="Degradiert ein Teammitglied (Admin/Teamleitung)")
@app_commands.describe(
    wer="Das Teammitglied das degradiert wird",
    von_rolle="Die aktuelle Rolle",
    auf_rolle="Die neue (niedrigere) Rolle",
    grund="Grund für die Degradierung"
)
async def downrank(interaction: discord.Interaction, wer: discord.Member, von_rolle: discord.Role, auf_rolle: discord.Role, grund: str = "Kein Grund angegeben"):
    if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("❌ Keine Berechtigung!", ephemeral=True)
        return
    await interaction.response.defer()

    # Rollen tauschen
    try:
        if von_rolle in wer.roles:
            await wer.remove_roles(von_rolle, reason=f"Downrank durch {interaction.user}: {grund}")
        await wer.add_roles(auf_rolle, reason=f"Downrank durch {interaction.user}: {grund}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Keine Berechtigung die Rollen zu ändern!", ephemeral=True)
        return

    embed = discord.Embed(
        title="⬇️ Downrank",
        color=discord.Color.from_rgb(255, 150, 50),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User", value=f"**{wer}**\n`{wer.id}`", inline=True)
    embed.add_field(name="Moderator", value=f"{interaction.user.mention} ({interaction.user})", inline=True)
    embed.add_field(name="Von Rolle", value=von_rolle.mention, inline=True)
    embed.add_field(name="Auf Rolle", value=auf_rolle.mention, inline=True)
    embed.add_field(name="Grund", value=grund, inline=False)
    embed.set_thumbnail(url=wer.display_avatar.url)

    await interaction.followup.send(embed=embed)
    # Ranklog Kanal
    ranklog_cfg = await get_ranklog_config(interaction.guild.id)
    rl_ch_id = ranklog_cfg.get("channel_id")
    if rl_ch_id:
        rl_ch = interaction.guild.get_channel(int(rl_ch_id))
        if rl_ch:
            try:
                await rl_ch.send(embed=embed)
            except Exception:
                pass
    else:
        await send_unified_log(interaction.guild, "moderation", embed)

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN nicht gesetzt!")

bot.run(token, reconnect=True)

