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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.bans = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

WARNINGS_FILE = "warnings.json"
CONFIG_FILE = "config.json"

def load_warnings():
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_warnings(data):
    with open(WARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"alert_users": []}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

warnings_data = load_warnings()
config_data = load_config()

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
    await tree.sync()
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    save_warnings(warnings_data)
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
            save_warnings(warnings_data)
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    user_id = str(member.id)
    if not warnings_data.get(user_id):
        await interaction.response.send_message(f"**{member}** hat keine Verwarnungen.")
        return
    count = len(warnings_data[user_id])
    warnings_data[user_id] = []
    save_warnings(warnings_data)
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
    if not interaction.user.guild_permissions.administrator:
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
    if not interaction.user.guild_permissions.administrator:
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
    save_config(config_data)
    await interaction.response.send_message("✅ DM-Alerts aktiviert!", ephemeral=True)

@tree.command(name="removealerts", description="DM-Alerts deaktivieren")
async def removealerts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in config_data["alert_users"]:
        await interaction.response.send_message("Keine aktiven Alerts.", ephemeral=True)
        return
    config_data["alert_users"].remove(uid)
    save_config(config_data)
    await interaction.response.send_message("🔕 DM-Alerts deaktiviert.", ephemeral=True)

# ─────────────────────────────────────────────
# /clear  (Admin) – alle oder User-Nachrichten löschen
# ─────────────────────────────────────────────

@tree.command(name="clear", description="Löscht Nachrichten im Channel (optional: nur von einem User)")
@app_commands.describe(anzahl="Anzahl der Nachrichten (max. 100)", member="Nur Nachrichten dieses Users löschen")
async def clear(interaction: discord.Interaction, anzahl: int = 100, member: discord.Member = None):
    if not interaction.user.guild_permissions.manage_messages:
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
@app_commands.describe(kanal="Ziel-Kanal", titel="Titel der Ankündigung", nachricht="Inhalt der Ankündigung")
async def ankuendigung(interaction: discord.Interaction, kanal: discord.TextChannel, titel: str, nachricht: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    embed = liquid_glass_embed(titel, nachricht, discord.Color.from_rgb(255, 200, 60))
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="📢 Ankündigung")
    await kanal.send(embed=embed)
    await interaction.response.send_message(f"✅ Ankündigung wurde in {kanal.mention} gesendet!", ephemeral=True)

# ─────────────────────────────────────────────
# Anti-Link System
# ─────────────────────────────────────────────

import re
LINK_PATTERN = re.compile(r"https?://|discord\.gg/|www\.", re.IGNORECASE)

def get_antilink(guild_id: int) -> dict:
    cfg = load_config()
    return cfg.get("antilink", {}).get(str(guild_id), {
        "enabled": False,
        "timeout_minutes": 5,
        "delete_message": True,
        "ignored_users": [],
        "ignored_roles": []
    })

def save_antilink(guild_id: int, data: dict):
    cfg = load_config()
    if "antilink" not in cfg:
        cfg["antilink"] = {}
    cfg["antilink"][str(guild_id)] = data
    save_config(cfg)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    settings = get_antilink(message.guild.id)

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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = get_antilink(interaction.guild.id)
    settings["enabled"] = aktiv
    settings["timeout_minutes"] = timeout_minuten
    settings["delete_message"] = nachricht_loeschen
    save_antilink(interaction.guild.id, settings)
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
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = get_antilink(interaction.guild.id)
    ignored = settings.get("ignored_users", [])
    if aktion == "add":
        if member.id not in ignored:
            ignored.append(member.id)
        msg = f"**{member}** wird jetzt ignoriert."
    else:
        ignored = [u for u in ignored if u != member.id]
        msg = f"**{member}** wird nicht mehr ignoriert."
    settings["ignored_users"] = ignored
    save_antilink(interaction.guild.id, settings)
    embed = liquid_glass_embed("Anti-Link • User", msg, discord.Color.from_rgb(130, 200, 240))
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-ignore-rolle", description="Rolle vom Anti-Link System ignorieren/entfernen")
@app_commands.describe(rolle="Die Rolle", aktion="Hinzufügen oder entfernen")
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen",  value="remove"),
])
async def antilink_ignore_rolle(interaction: discord.Interaction, rolle: discord.Role, aktion: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = get_antilink(interaction.guild.id)
    ignored = settings.get("ignored_roles", [])
    if aktion == "add":
        if rolle.id not in ignored:
            ignored.append(rolle.id)
        msg = f"**{rolle.name}** wird jetzt ignoriert."
    else:
        ignored = [r for r in ignored if r != rolle.id]
        msg = f"**{rolle.name}** wird nicht mehr ignoriert."
    settings["ignored_roles"] = ignored
    save_antilink(interaction.guild.id, settings)
    embed = liquid_glass_embed("Anti-Link • Rolle", msg, discord.Color.from_rgb(130, 200, 240))
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-status", description="Zeigt die aktuellen Anti-Link Einstellungen")
async def antilink_status(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return
    settings = get_antilink(interaction.guild.id)
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

TICKET_FILE = "tickets.json"

TICKET_CATEGORIES = {
    "allgemeine_frage":       ("❓", "Allgemeine Frage",       "Stelle eine allgemeine Frage"),
    "support":                ("🛠️", "Support",                "Erhalte technischen Support"),
    "support_owner":          ("👑", "Support Owner",          "Direkte Unterstützung vom Owner"),
    "report":                 ("🚨", "Report",                 "Melde einen Spieler"),
    "unban_antrag":           ("🔓", "Unban-Antrag",           "Stelle einen Unban-Antrag"),
    "partner_bewerbung":      ("🤝", "Partner-Bewerbung",      "Bewirb dich als Partner"),
    "fraktions_bewerbung":    ("🚔", "Fraktions-Bewerbung",     "Bewirb dich bei einer Fraktion"),
}

def load_tickets():
    if os.path.exists(TICKET_FILE):
        with open(TICKET_FILE, "r") as f:
            return json.load(f)
    return {}

def save_tickets(data):
    with open(TICKET_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_ticket_config(guild_id: int) -> dict:
    cfg = load_config()
    return cfg.get("ticket_config", {}).get(str(guild_id), {})

def save_ticket_config(guild_id: int, data: dict):
    cfg = load_config()
    if "ticket_config" not in cfg:
        cfg["ticket_config"] = {}
    cfg["ticket_config"][str(guild_id)] = data
    save_config(cfg)

# ── Close Button View ──

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Ticket schließen", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
            return

        tickets = load_tickets()
        guild_id = str(interaction.guild.id)
        channel_id = str(interaction.channel.id)

        ticket_data = None
        ticket_key = None
        for key, t in tickets.get(guild_id, {}).items():
            if str(t.get("channel_id", "")) == str(interaction.channel.id):
                ticket_data = t
                ticket_key = key
                break

        if not ticket_data:
            await interaction.response.send_message("Kein Ticket gefunden!", ephemeral=True)
            return

        await interaction.response.send_message(
            embed=liquid_glass_embed("🔒 Ticket schließen", "Wie soll das Ticket geschlossen werden?",
                                     discord.Color.from_rgb(255, 150, 50)),
            view=TicketCloseActionView(ticket_data, ticket_key),
            ephemeral=True
        )

class TicketCloseActionView(discord.ui.View):
    def __init__(self, ticket_data, ticket_key):
        super().__init__(timeout=60)
        self.ticket_data = ticket_data
        self.ticket_key = ticket_key

    @discord.ui.button(label="🗑️ Löschen", style=discord.ButtonStyle.danger)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close(interaction, delete=True)

    @discord.ui.button(label="📁 Archivieren", style=discord.ButtonStyle.secondary)
    async def archive_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close(interaction, delete=False)

    async def _close(self, interaction: discord.Interaction, delete: bool):
        channel = interaction.channel
        guild = interaction.guild
        guild_id = str(guild.id)
        cfg = get_ticket_config(guild.id)

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
        tickets = load_tickets()
        if guild_id in tickets and self.ticket_key in tickets[guild_id]:
            del tickets[guild_id][self.ticket_key]
            save_tickets(tickets)

        await interaction.response.send_message("✅ Ticket wird geschlossen...", ephemeral=True)

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

# ── Category Select ──

class TicketCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                description=desc,
                emoji=emoji
            )
            for key, (emoji, label, desc) in TICKET_CATEGORIES.items()
        ]
        super().__init__(placeholder="Wähle eine Kategorie...", options=options, custom_id="ticket_select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category_key = self.values[0]
        emoji, label, _ = TICKET_CATEGORIES[category_key]
        tickets = load_tickets()
        guild_id = str(guild.id)
        cfg = get_ticket_config(guild.id)

        if guild_id not in tickets:
            tickets[guild_id] = {}

        # Get ticket category
        ticket_category_id = cfg.get("ticket_category")
        ticket_category = guild.get_channel(int(ticket_category_id)) if ticket_category_id else None

        # Count tickets
        count = len(tickets[guild_id]) + 1
        channel_name = f"{emoji}-{category_key.replace('_', '-')}-{count:04d}"

        # Create channel with permissions
        support_role_id = cfg.get("support_role")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if support_role_id:
            support_role = guild.get_role(int(support_role_id))
            if support_role:
                overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        # Add owner to overwrites directly so no second API call needed
        owner = guild.get_member(OWNER_ID)
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=ticket_category,
            overwrites=overwrites,
            reason=f"Ticket von {interaction.user}"
        )

        # Save ticket
        tickets[guild_id][str(channel.id)] = {
            "channel_id": str(channel.id),
            "user_id": interaction.user.id,
            "category": label,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        save_tickets(tickets)

        # Send ticket embed
        embed = liquid_glass_embed(
            f"{emoji} {label}",
            f"Willkommen {interaction.user.mention}!\n\nBeschreibe dein Anliegen so genau wie möglich.\nUnser Team wird sich so schnell wie möglich bei dir melden.\n\n**Kategorie:** {label}",
            discord.Color.from_rgb(130, 200, 240)
        )

        # Combine ping and embed into one message
        if category_key == "support_owner" and owner:
            content_msg = f"{interaction.user.mention} {owner.mention}"
        else:
            content_msg = interaction.user.mention

        await channel.send(
            content=content_msg,
            embed=embed,
            view=TicketCloseView()
        )

        # Send notification to notification channel
        notification_channel_id = cfg.get("notification_channel")
        ping_role_id = cfg.get("ping_role")
        ping_text = f"<@&{ping_role_id}>" if ping_role_id else ""

        # Ping role in the ticket channel itself
        if ping_role_id:
            await channel.send(content=ping_text)

        # Also send to notification channel if set
        if notification_channel_id:
            notif_channel = guild.get_channel(int(notification_channel_id))
            if notif_channel:
                notif_embed = liquid_glass_embed(
                    "🎫 Neues Ticket",
                    f"**User:** {interaction.user.mention}\n**Kategorie:** {label}\n**Kanal:** {channel.mention}",
                    discord.Color.from_rgb(255, 200, 60)
                )
                await notif_channel.send(content=ping_text if ping_role_id else None, embed=notif_embed)

        await interaction.followup.send(
            f"✅ Dein Ticket wurde erstellt: {channel.mention}", ephemeral=True
        )

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())

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
    if not interaction.user.guild_permissions.administrator:
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
    save_ticket_config(interaction.guild.id, {
        "support_role": str(support_rolle.id),
        "ticket_category": str(ticket_kategorie.id),
        "archive_category": str(archiv_kategorie.id),
        "transcript_channel": str(transcript_kanal.id),
        "ping_role": str(ping_rolle.id) if ping_rolle else None,
        "notification_channel": str(benachrichtigungs_kanal.id),
    })

    # Send panel
    embed = liquid_glass_embed(
        "🎫 Support Tickets",
        "Wähle unten eine Kategorie aus um ein Ticket zu öffnen.\nUnser Team hilft dir so schnell wie möglich!",
        discord.Color.from_rgb(140, 210, 255)
    )
    embed.add_field(name="❓ Allgemeine Frage", value="Stelle eine allgemeine Frage", inline=True)
    embed.add_field(name="🛠️ Support", value="Erhalte technischen Support", inline=True)
    embed.add_field(name="👑 Support Owner", value="Direkte Unterstützung vom Owner", inline=True)
    embed.add_field(name="🚨 Report", value="Melde einen Spieler", inline=True)
    embed.add_field(name="🔓 Unban-Antrag", value="Stelle einen Unban-Antrag", inline=True)
    embed.add_field(name="🤝 Partner-Bewerbung", value="Bewirb dich als Partner", inline=True)
    embed.add_field(name="🚔 Fraktions-Bewerbung", value="Bewirb dich bei einer Fraktion", inline=True)

    await kanal.send(embed=embed, view=TicketView())

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
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = load_tickets()
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
    cfg = get_ticket_config(guild.id)

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
        save_tickets(tickets)

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
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = load_tickets()
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
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = load_tickets()
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
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    tickets = load_tickets()
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
    save_tickets(tickets)

    embed = liquid_glass_embed(
        "🔁 Ticket übertragen",
        f"Das Ticket wurde von <@{old_user_id}> an {member.mention} übertragen.",
        discord.Color.from_rgb(130, 200, 240)
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="ticket-panel-aktualisieren", description="Aktualisiert das Ticket-Panel in einem Kanal")
@app_commands.describe(kanal="Der Kanal wo das Panel gepostet werden soll")
async def ticket_panel_aktualisieren(interaction: discord.Interaction, kanal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Keine Berechtigung!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    cfg = get_ticket_config(interaction.guild.id)
    if not cfg:
        await interaction.followup.send("❌ Ticket-System ist noch nicht eingerichtet! Benutze zuerst `/ticket-setup`.", ephemeral=True)
        return

    embed = liquid_glass_embed(
        "🎫 Support Tickets",
        "Wähle unten eine Kategorie aus um ein Ticket zu öffnen.\nUnser Team hilft dir so schnell wie möglich!",
        discord.Color.from_rgb(140, 210, 255)
    )
    embed.add_field(name="❓ Allgemeine Frage", value="Stelle eine allgemeine Frage", inline=True)
    embed.add_field(name="🛠️ Support", value="Erhalte technischen Support", inline=True)
    embed.add_field(name="👑 Support Owner", value="Direkte Unterstützung vom Owner", inline=True)
    embed.add_field(name="🚨 Report", value="Melde einen Spieler", inline=True)
    embed.add_field(name="🔓 Unban-Antrag", value="Stelle einen Unban-Antrag", inline=True)
    embed.add_field(name="🤝 Partner-Bewerbung", value="Bewirb dich als Partner", inline=True)
    embed.add_field(name="🚔 Fraktions-Bewerbung", value="Bewirb dich bei einer Fraktion", inline=True)

    await kanal.send(embed=embed, view=TicketView())
    await interaction.followup.send(
        embed=liquid_glass_embed("✅ Panel aktualisiert!", f"Das Ticket-Panel wurde in {kanal.mention} neu gepostet.", discord.Color.from_rgb(100, 220, 150)),
        ephemeral=True
    )

# ─────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN nicht gesetzt!")

bot.run(token)
