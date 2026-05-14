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
    await db["team_stats"].update_one(
        {"_id": key},
        {
            "$inc": {stat_type: 1},
            "$set": {
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "user_name": user_name,
            },
            "$setOnInsert": {"tickets_closed": 0, "supports_accepted": 0}
        },
        upsert=True
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

@bot.event
async def on_guild_channel_delete(channel):
    executor = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete)
    await check_nuke(channel.guild, executor, "Channel-Loeschung")

@bot.event
async def on_guild_role_delete(role):
    executor = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete)
    await check_nuke(role.guild, executor, "Rollen-Loeschung")

@bot.event
async def on_member_remove(member):
    executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick)
    if executor:
        await check_nuke(member.guild, executor, "Kick")

@bot.event
async def on_ready():
    global warnings_data, config_data
    warnings_data = await load_warnings()
    config_data = await load_config()
    await tree.sync()
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())
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
    print("Slash Commands synchronisiert!")

# ─────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────

@tree.command(name="help", description="Zeigt alle Befehle")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    embed = liquid_glass_embed(
        "Bot Befehle",
        "Alle verfügbaren Slash-Commands auf einen Blick.",
        discord.Color.from_rgb(100, 180, 255)
    )
    embed.add_field(
        name="⚙️  Moderation",
        value="`/kick` `/teamkick` `/tempmute` `/unmute` `/teamwarn`\n`/warnings` `/allwarnings` `/clearwarnings` `/bann` `/unban` `/clear`",
        inline=False
    )
    embed.add_field(
        name="🔔  Alerts",
        value="`/setalerts` `/removealerts`",
        inline=False
    )
    embed.add_field(
        name="📊  Server",
        value="`/serverstatus` `/userinfo` `/status` `/ankündigung`",
        inline=False
    )
    embed.add_field(
        name="🎫  Tickets",
        value="`/ticket-setup` `/ticket-schliessen` `/ticket-add` `/ticket-remove` `/ticket-übertragen`",
        inline=False
    )
    embed.add_field(
        name="🔗  Anti-Link",
        value="`/antilink` `/antilink-ignore-user` `/antilink-ignore-rolle` `/antilink-status`",
        inline=False
    )
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
        has_permission = interaction.user.guild_permissions.manage_channels
        if support_role and support_role in interaction.user.roles:
            has_permission = True

        if not has_permission:
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
        del_btn.callback = lambda i: self._close(i, delete=True)
        arc_btn.callback = lambda i: self._close(i, delete=False)
        self.add_item(del_btn)
        self.add_item(arc_btn)

    async def _close(self, interaction: discord.Interaction, delete: bool):
        # Defer immediately so Discord doesn't time out (transcript building takes time)
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        guild_id = str(guild.id)
        cfg = await get_ticket_config(guild.id)

        # Build transcript
        transcript_lines = [f"📄 Transcript – {channel.name}\n{'='*40}\n"]
        async for msg in channel.history(limit=200, oldest_first=True):
            if not msg.author.bot:
                transcript_lines.append(f"[{msg.created_at.strftime('%d.%m.%Y %H:%M')}] {msg.author}: {msg.content}")
        transcript_text = "\n".join(transcript_lines)

        # Send transcript
        transcript_channel_id = cfg.get("transcript_channel")
        if transcript_channel_id:
            tc = guild.get_channel(int(transcript_channel_id))
            if tc:
                embed = liquid_glass_embed(
                    f"📄 Transcript – {channel.name}",
                    f"**Geöffnet von:** <@{self.ticket_data.get('user_id')}>\n**Kategorie:** {self.ticket_data.get('category')}\n**Geschlossen von:** {interaction.user.mention}",
                    discord.Color.from_rgb(130, 200, 240)
                )
                file_content = transcript_text.encode("utf-8")
                file = discord.File(
                    fp=__import__("io").BytesIO(file_content),
                    filename=f"transcript-{channel.name}.txt"
                )
                await tc.send(embed=embed, file=file)

        # Remove from tickets
        tickets = await load_tickets()
        if guild_id in tickets and self.ticket_key in tickets[guild_id]:
            del tickets[guild_id][self.ticket_key]
            await save_tickets(tickets)

        # Stat tracken
        await add_team_stat(
            guild.id,
            interaction.user.id,
            str(interaction.user),
            "tickets_closed"
        )

        await interaction.followup.send("✅ Ticket wird geschlossen...", ephemeral=True)

        if delete:
            await asyncio.sleep(3)
            await channel.delete(reason=f"Ticket gelöscht von {interaction.user}")
        else:
            # Archive: rename and remove permissions
            archive_category_id = cfg.get("archive_category")
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            for role in guild.roles:
                if role.permissions.manage_channels:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
            await channel.edit(
                name=f"archived-{channel.name}",
                overwrites=overwrites,
                category=guild.get_channel(int(archive_category_id)) if archive_category_id else channel.category,
                reason=f"Ticket archiviert von {interaction.user}"
            )

# ── Category Buttons ──

async def create_ticket_for_category(interaction: discord.Interaction, category_key: str):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # Lookup in Standard- UND Custom-Kategorien
        all_cats = await get_all_categories(guild.id)
        if category_key not in all_cats:
            await interaction.followup.send("❌ Kategorie nicht gefunden!", ephemeral=True)
            return
        emoji, label, _ = all_cats[category_key]
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

        count = len(tickets[guild_id]) + 1
        channel_name = f"{emoji}-{category_key.replace('_', '-')}-{count:04d}"

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
    """Gibt Standard + Custom Kategorien zurück"""
    cfg = await get_ticket_config(guild_id)
    custom = cfg.get("custom_categories", {})
    all_cats = dict(TICKET_CATEGORIES)
    for key, data in custom.items():
        all_cats[key] = (data["emoji"], data["label"], data["description"])
    return all_cats

class TicketView(discord.ui.View):
    def __init__(self, all_categories: dict = None):
        super().__init__(timeout=None)
        cats = all_categories if all_categories else dict(TICKET_CATEGORIES)
        for key, (emoji, label, desc) in cats.items():
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
    cat_names = ", ".join(f"{e} {l}" for k, (e, l, d) in all_cats.items())
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

        vs_cfg = await get_voice_support_config(guild.id)
        support_category_id = vs_cfg.get("support_category_id")
        if not support_category_id:
            await interaction.response.send_message("❌ Keine Support-Kategorie konfiguriert!", ephemeral=True)
            return
        category = guild.get_channel(int(support_category_id))
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
        action = parts[0]   # "tca_delete" oder "tca_archive"
        ticket_key = parts[1] if len(parts) > 1 else ""

        # Berechtigungsprüfung
        if not has_permission(interaction, "manage_channels"):
            await interaction.response.send_message("❌ Keine Berechtigung!", ephemeral=True)
            return

        # Ticket-Daten aus DB laden
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


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    guild = member.guild
    vs_cfg = await get_voice_support_config(guild.id)
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



# ── Team Dashboard ──

@bot.tree.command(name="team_dashboard", description="📊 Zeigt das Team-Dashboard mit Support & Ticket Statistiken")
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


@bot.tree.command(name="team_stats_reset", description="🗑️ Setzt alle Team-Statistiken zurück (nur Bot-Owner)")
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


token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN nicht gesetzt!")

bot.run(token)
