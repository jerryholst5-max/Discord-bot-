import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import json
import asyncio
import logging
import yt_dlp
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ══════════════════════════════════════════════════════════════════
#  Logging
# ══════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("GermanyRP")

# ══════════════════════════════════════════════════════════════════
#  Konstanten
# ══════════════════════════════════════════════════════════════════

OWNER_ID       : int = 1408144132966322407
WARNINGS_FILE  : str = "warnings.json"
CONFIG_FILE    : str = "config.json"
NUKE_WINDOW    : int = 10   # Sekunden
NUKE_THRESHOLD : int = 3    # Aktionen innerhalb NUKE_WINDOW

# ══════════════════════════════════════════════════════════════════
#  Intents & Bot
# ══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.bans            = True
intents.guilds          = True
intents.moderation      = True

bot  = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ══════════════════════════════════════════════════════════════════
#  JSON-Helfer
# ══════════════════════════════════════════════════════════════════

def load_warnings() -> dict:
    if os.path.exists(WARNINGS_FILE):
        with open(WARNINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_warnings(data: dict) -> None:
    with open(WARNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"alert_users": []}

def save_config(data: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

warnings_data : dict = load_warnings()
config_data   : dict = load_config()

# ══════════════════════════════════════════════════════════════════
#  Anti-Nuke Tracker
# ══════════════════════════════════════════════════════════════════

nuke_tracker: dict = defaultdict(lambda: defaultdict(list))

# ══════════════════════════════════════════════════════════════════
#  Owner-Check
# ══════════════════════════════════════════════════════════════════

def is_owner(user) -> bool:
    return user.id == OWNER_ID

# ══════════════════════════════════════════════════════════════════
#  LIQUID-GLASS EMBED HELPER  (Discord Embed V2-Style)
# ══════════════════════════════════════════════════════════════════

DIVIDER = "```\n" + "┈" * 34 + "\n```"

def liquid_glass_embed(
    title:       str,
    description: str           = "",
    color:       discord.Color = None,
    fields:      list          = None,
    thumbnail:   str           = None,
    image:       str           = None,
    author_name: str           = None,
    author_icon: str           = None,
    footer_text: str           = "◆ GermanyRP • System  ◆",
) -> discord.Embed:
    """
    Erstellt ein einheitliches Liquid-Glass-Embed (Discord Embed V2-Style).
    fields-Format: [(name, value, inline), ...]
    """
    if color is None:
        color = discord.Color.from_rgb(140, 210, 255)

    body = (description + "\n" + DIVIDER) if description else DIVIDER

    embed = discord.Embed(
        title=f"💠  {title}",
        description=body,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if author_name:
        embed.set_author(
            name=author_name,
            icon_url=author_icon or discord.utils.MISSING,
        )
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=f"▸  {name}", value=value, inline=inline)

    embed.set_footer(text=footer_text)
    return embed


# Kurzformen
def _ok(title: str, desc: str = "") -> discord.Embed:
    return liquid_glass_embed(title, desc, discord.Color.from_rgb(100, 220, 150))

def _err(title: str, desc: str = "") -> discord.Embed:
    return liquid_glass_embed(title, desc, discord.Color.from_rgb(220, 60, 60))

def _warn(title: str, desc: str = "") -> discord.Embed:
    return liquid_glass_embed(title, desc, discord.Color.from_rgb(255, 200, 60))

def _info(title: str, desc: str = "") -> discord.Embed:
    return liquid_glass_embed(title, desc, discord.Color.from_rgb(100, 180, 255))

# ══════════════════════════════════════════════════════════════════
#  Permission Guards
# ══════════════════════════════════════════════════════════════════

async def _require_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    await interaction.response.send_message(
        embed=_err("Keine Berechtigung", "Du benötigst Administrator-Rechte."),
        ephemeral=True,
    )
    return False

async def _require_owner(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user):
        return True
    await interaction.response.send_message(
        embed=_err("Keine Berechtigung", "Nur der Bot-Owner kann das."),
        ephemeral=True,
    )
    return False

async def _owner_immune(interaction: discord.Interaction, member: discord.Member) -> bool:
    if is_owner(member):
        await interaction.response.send_message(
            embed=_warn("Immun", "Der Eigentümer kann nicht moderiert werden."),
            ephemeral=True,
        )
        return True
    return False

# ══════════════════════════════════════════════════════════════════
#  Anti-Nuke – Kernlogik
# ══════════════════════════════════════════════════════════════════

async def get_audit_executor(guild: discord.Guild, action: discord.AuditLogAction, max_age: float = 5.0):
    try:
        async for entry in guild.audit_logs(limit=1, action=action):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if age <= max_age:
                return entry.user
    except discord.Forbidden:
        pass
    return None


async def check_nuke(guild: discord.Guild, user, action_label: str) -> None:
    if user is None or user.bot:
        return

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=NUKE_WINDOW)

    bucket = nuke_tracker[guild.id][user.id]
    bucket.append(now)
    nuke_tracker[guild.id][user.id] = [t for t in bucket if t > cutoff]
    count = len(nuke_tracker[guild.id][user.id])

    if count < NUKE_THRESHOLD:
        return

    nuke_tracker[guild.id][user.id] = []
    log_channel   = guild.system_channel
    actions_taken = []

    try:
        member = guild.get_member(user.id)
        if member:
            bot_top_role = guild.me.top_role
            removable = [r for r in member.roles if r.name != "@everyone" and r < bot_top_role]
            if removable:
                await member.remove_roles(*removable, reason="[Anti-Nuke] Automatisch")
                actions_taken.append("Rollen entfernt")

        await guild.ban(user, reason=f"[Anti-Nuke] {count}x {action_label}")
        actions_taken.append("Gebannt")
        log.warning("[Anti-Nuke] %s (%s) in %s – %dx %s → %s",
                    user, user.id, guild.name, count, action_label, ", ".join(actions_taken))

    except discord.Forbidden:
        actions_taken.append("Ban fehlgeschlagen (fehlende Rechte)")
        log.error("[Anti-Nuke] Konnte %s nicht bannen – fehlende Rechte.", user)
    except Exception as e:
        actions_taken.append(f"Fehler: {e}")
        log.exception("[Anti-Nuke] Fehler bei %s.", user)

    if log_channel:
        embed = _err(
            "🛡️  Anti-Nuke ausgelöst",
            f"**Nutzer:** {user.mention} (`{user.id}`)\n"
            f"**Aktion:** {count}x {action_label}\n"
            f"**Maßnahmen:** {', '.join(actions_taken)}",
        )
        await log_channel.send(embed=embed)


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    executor = await get_audit_executor(guild, discord.AuditLogAction.ban)
    await check_nuke(guild, executor, "Ban")

@bot.event
async def on_guild_channel_delete(channel):
    executor = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete)
    await check_nuke(channel.guild, executor, "Channel-Löschung")

@bot.event
async def on_guild_role_delete(role: discord.Role):
    executor = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete)
    await check_nuke(role.guild, executor, "Rollen-Löschung")

@bot.event
async def on_member_remove(member: discord.Member):
    executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick)
    if executor:
        await check_nuke(member.guild, executor, "Kick")

# ══════════════════════════════════════════════════════════════════
#  on_ready
# ══════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    await tree.sync()
    log.info("Bot online als %s  |  Slash-Commands synchronisiert", bot.user)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="GermanyRP • /help",
        )
    )

# ══════════════════════════════════════════════════════════════════
#  /help
# ══════════════════════════════════════════════════════════════════

@tree.command(name="help", description="Zeigt alle verfügbaren Befehle")
async def help_cmd(interaction: discord.Interaction):
    embed = liquid_glass_embed(
        "Bot Befehle",
        "Alle verfügbaren Slash-Commands auf einen Blick.",
        discord.Color.from_rgb(100, 180, 255),
    )
    embed.add_field(
        name="⚙️  Moderation",
        value=(
            "`/kick`  `/teamkick`  `/tempmute`  `/unmute`  `/teamwarn`\n"
            "`/warnings`  `/allwarnings`  `/clearwarnings`  `/bann`  `/unban`  `/clear`"
        ),
        inline=False,
    )
    embed.add_field(name="🔔  Alerts",    value="`/setalerts`  `/removealerts`",                                                      inline=False)
    embed.add_field(name="📊  Server",    value="`/serverstatus`  `/userinfo`  `/status`  `/ankündigung`",                            inline=False)
    embed.add_field(name="🎵  Musik",     value="`/play`  `/skip`  `/stop`",                                                          inline=False)
    embed.add_field(name="🔗  Anti-Link", value="`/antilink`  `/antilink-ignore-user`  `/antilink-ignore-rolle`  `/antilink-status`", inline=False)
    embed.add_field(name="🛠️  Sonstiges", value="`/hallo`  `/embed`  `/help`",                                                       inline=False)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /hallo
# ══════════════════════════════════════════════════════════════════

@tree.command(name="hallo", description="Begrüßung")
async def hallo(interaction: discord.Interaction):
    embed = liquid_glass_embed("Willkommen!", f"Hey {interaction.user.mention} 👋\nSchön dass du da bist!")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /embed  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="embed", description="Sendet eine Nachricht als Liquid-Glass-Embed (Admin)")
@app_commands.describe(titel="Titel des Embeds", nachricht="Nachricht / Beschreibung")
async def embed_cmd(interaction: discord.Interaction, titel: str, nachricht: str):
    if not await _require_admin(interaction):
        return
    embed = liquid_glass_embed(
        titel, nachricht,
        author_name=interaction.user.display_name,
        author_icon=interaction.user.display_avatar.url,
    )
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /serverstatus  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="serverstatus", description="Zeigt den aktuellen Server-Status")
async def serverstatus(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return

    guild          = interaction.guild
    total_members  = guild.member_count
    online_members = sum(1 for m in guild.members if m.status != discord.Status.offline) if guild.members else "N/A"
    bots           = sum(1 for m in guild.members if m.bot)
    text_channels  = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    roles          = len(guild.roles) - 1
    boost_level    = guild.premium_tier
    boosts         = guild.premium_subscription_count
    created_at     = guild.created_at.strftime("%d.%m.%Y")

    embed = liquid_glass_embed(
        f"Server Status – {guild.name}",
        color=discord.Color.from_rgb(80, 210, 200),
        thumbnail=guild.icon.url if guild.icon else None,
    )
    embed.add_field(name="👥  Mitglieder",     value=f"`{total_members}`",    inline=True)
    embed.add_field(name="🟢  Online",         value=f"`{online_members}`",   inline=True)
    embed.add_field(name="🤖  Bots",           value=f"`{bots}`",             inline=True)
    embed.add_field(name="💬  Text-Channels",  value=f"`{text_channels}`",    inline=True)
    embed.add_field(name="🔊  Voice-Channels", value=f"`{voice_channels}`",   inline=True)
    embed.add_field(name="🎭  Rollen",         value=f"`{roles}`",            inline=True)
    embed.add_field(name="✨  Boost Level",    value=f"`{boost_level}`",      inline=True)
    embed.add_field(name="🚀  Boosts",         value=f"`{boosts}`",           inline=True)
    embed.add_field(name="📅  Erstellt am",    value=f"`{created_at}`",       inline=True)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /kick  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="kick", description="Kickt einen User vom Server")
@app_commands.describe(member="Der User", grund="Grund")
async def kick(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund"):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return
    try:
        await member.kick(reason=grund)
        embed = liquid_glass_embed(
            "👢  Kick",
            f"**{member}** wurde vom Server gekickt.\n**Grund:** {grund}",
            discord.Color.from_rgb(255, 160, 100),
            fields=[("Moderator", interaction.user.mention, True)],
        )
        await interaction.response.send_message(embed=embed)
        log.info("[Kick] %s kickte %s – Grund: %s", interaction.user, member, grund)
    except discord.Forbidden:
        await interaction.response.send_message(embed=_err("Fehler", "Fehlende Berechtigung."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /teamkick  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="teamkick", description="Entfernt alle Rollen eines Users")
@app_commands.describe(member="Der User")
async def teamkick(interaction: discord.Interaction, member: discord.Member):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return

    roles = [r for r in member.roles if r.name != "@everyone"]
    if not roles:
        await interaction.response.send_message(
            embed=_warn("Keine Rollen", f"**{member}** hat keine Rollen."), ephemeral=True
        )
        return

    await member.remove_roles(*roles, reason=f"[Teamkick] durch {interaction.user}")
    embed = liquid_glass_embed(
        "🔰  Team-Kick",
        f"Alle Rollen von **{member}** wurden entfernt.",
        discord.Color.from_rgb(255, 140, 80),
        fields=[("Entfernte Rollen", str(len(roles)), True), ("Moderator", interaction.user.mention, True)],
    )
    await interaction.response.send_message(embed=embed)
    log.info("[Teamkick] %s entfernte Rollen von %s", interaction.user, member)

# ══════════════════════════════════════════════════════════════════
#  /tempmute  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="tempmute", description="Schaltet einen User temporär stumm")
@app_commands.describe(member="Der User", minuten="Dauer in Minuten", grund="Grund")
async def tempmute(interaction: discord.Interaction, member: discord.Member, minuten: int, grund: str = "Kein Grund"):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return
    if minuten <= 0:
        await interaction.response.send_message(embed=_err("Ungültige Dauer", "Bitte gib eine positive Minutenzahl an."), ephemeral=True)
        return

    until = datetime.now(timezone.utc) + timedelta(minutes=minuten)
    try:
        await member.timeout(until, reason=grund)
        embed = liquid_glass_embed(
            "🔇  Temp-Mute",
            f"**{member}** wurde für **{minuten} Minuten** stummgeschaltet.\n**Grund:** {grund}",
            discord.Color.from_rgb(200, 100, 240),
            fields=[
                ("Moderator", interaction.user.mention, True),
                ("Endet", f"<t:{int(until.timestamp())}:R>", True),
            ],
        )
        await interaction.response.send_message(embed=embed)
        log.info("[Mute] %s mutete %s für %d Min – Grund: %s", interaction.user, member, minuten, grund)
    except discord.Forbidden:
        await interaction.response.send_message(embed=_err("Fehler", "Fehlende Berechtigung."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /unmute  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="unmute", description="Hebt den Timeout eines Users auf")
@app_commands.describe(member="Der User")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return
    try:
        await member.timeout(None)
        embed = _ok("🔊  Unmute", f"Der Timeout von **{member}** wurde aufgehoben.")
        await interaction.response.send_message(embed=embed)
        log.info("[Unmute] %s unmutete %s", interaction.user, member)
    except discord.Forbidden:
        await interaction.response.send_message(embed=_err("Fehler", "Fehlende Berechtigung."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /teamwarn  (Admin) – kein Ping, Auto-Ban bei 3 Verwarnungen
# ══════════════════════════════════════════════════════════════════

@tree.command(name="teamwarn", description="Verwarnt einen User (kein Ping, Auto-Ban bei 3 Verwarnungen)")
@app_commands.describe(member="Der User", grund="Grund")
async def teamwarn(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund"):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return

    user_id = str(member.id)
    warnings_data.setdefault(user_id, [])
    warnings_data[user_id].append({
        "reason": grund,
        "by":     str(interaction.user),
        "at":     datetime.now(timezone.utc).isoformat(),
    })
    save_warnings(warnings_data)
    count = len(warnings_data[user_id])

    embed = _warn(
        "⚠️  Verwarnung",
        f"**{member}** wurde verwarnt.\n**Grund:** {grund}\n**Verwarnung Nr.:** {count} / 3",
    )
    embed.add_field(name="Moderator", value=str(interaction.user), inline=True)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    log.info("[Warn] %s verwarnete %s (#%d) – Grund: %s", interaction.user, member, count, grund)

    if count >= 3:
        try:
            await member.ban(reason=f"[Auto-Ban] {count} Verwarnungen")
            warnings_data[user_id] = []
            save_warnings(warnings_data)
            ban_embed = _err("🔨  Auto-Ban", f"**{member}** wurde nach **{count} Verwarnungen** automatisch gebannt.")
            await interaction.followup.send(embed=ban_embed, allowed_mentions=discord.AllowedMentions.none())
            log.info("[Auto-Ban] %s nach %d Verwarnungen gebannt.", member, count)
        except discord.Forbidden:
            await interaction.followup.send(embed=_err("Auto-Ban fehlgeschlagen", "Fehlende Berechtigung zum Bannen."))

# ══════════════════════════════════════════════════════════════════
#  /warnings  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="warnings", description="Zeigt Verwarnungen eines Users")
@app_commands.describe(member="Der User")
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    if not await _require_admin(interaction):
        return

    user_warnings = warnings_data.get(str(member.id), [])
    if not user_warnings:
        await interaction.response.send_message(
            embed=_info("Keine Verwarnungen", f"**{member}** hat keine aktiven Verwarnungen."),
            ephemeral=True,
        )
        return

    embed = liquid_glass_embed(
        f"⚠️  Verwarnungen – {member}",
        f"Insgesamt **{len(user_warnings)}** Verwarnung(en).",
        discord.Color.from_rgb(255, 180, 50),
    )
    for i, w in enumerate(user_warnings, 1):
        ts = w.get("at", "")[:10]
        embed.add_field(name=f"#{i}  –  {w['reason']}", value=f"von `{w['by']}`  •  {ts}", inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

# ══════════════════════════════════════════════════════════════════
#  /allwarnings  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="allwarnings", description="Zeigt alle aktiven Verwarnungen auf dem Server")
async def allwarnings(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return

    active = {uid: w for uid, w in warnings_data.items() if w}
    if not active:
        await interaction.response.send_message(embed=_ok("Keine Verwarnungen", "Aktuell gibt es keine aktiven Verwarnungen."), ephemeral=True)
        return

    embed = liquid_glass_embed("📋  Alle Verwarnungen", color=discord.Color.from_rgb(255, 160, 60))
    for uid, warns in sorted(active.items(), key=lambda x: len(x[1]), reverse=True):
        try:
            u    = await bot.fetch_user(int(uid))
            name = str(u)
        except Exception:
            name = uid
        embed.add_field(name=f"{name}  –  {len(warns)}x", value=warns[-1]["reason"], inline=False)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /clearwarnings  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="clearwarnings", description="Löscht alle Verwarnungen eines Users")
@app_commands.describe(member="Der User")
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    if not await _require_admin(interaction):
        return

    user_id = str(member.id)
    if not warnings_data.get(user_id):
        await interaction.response.send_message(embed=_info("Keine Verwarnungen", f"**{member}** hat keine Verwarnungen."), ephemeral=True)
        return

    count = len(warnings_data[user_id])
    warnings_data[user_id] = []
    save_warnings(warnings_data)
    embed = _ok("✅  Verwarnungen gelöscht", f"**{count}** Verwarnung(en) von **{member}** wurden gelöscht.")
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    log.info("[ClearWarnings] %s löschte %d Verwarnungen von %s", interaction.user, count, member)

# ══════════════════════════════════════════════════════════════════
#  /bann  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="bann", description="Bannt einen User vom Server")
@app_commands.describe(member="Der User", grund="Grund", delete_days="Nachrichten löschen (0–7 Tage)")
async def bann(interaction: discord.Interaction, member: discord.Member, grund: str = "Kein Grund", delete_days: int = 0):
    if not await _require_admin(interaction):
        return
    if await _owner_immune(interaction, member):
        return

    delete_days = max(0, min(delete_days, 7))
    try:
        await member.ban(reason=grund, delete_message_days=delete_days)
        embed = _err(
            "🔨  Ban",
            f"**{member}** wurde vom Server gebannt.\n**Grund:** {grund}",
        )
        embed.add_field(name="Moderator",       value=str(interaction.user),  inline=True)
        embed.add_field(name="Nachr. gelöscht", value=f"{delete_days} Tag(e)", inline=True)
        await interaction.response.send_message(embed=embed)
        log.info("[Ban] %s bannte %s – Grund: %s", interaction.user, member, grund)
    except discord.Forbidden:
        await interaction.response.send_message(embed=_err("Fehler", "Fehlende Berechtigung."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /unban  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="unban", description="Entbannt einen User per ID")
@app_commands.describe(user_id="Die User-ID des gebannten Nutzers")
async def unban(interaction: discord.Interaction, user_id: str):
    if not await _require_admin(interaction):
        return
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user)
        embed = _ok("✅  Unban", f"**{user}** wurde erfolgreich entbannt.")
        await interaction.response.send_message(embed=embed)
        log.info("[Unban] %s entbannte %s", interaction.user, user)
    except ValueError:
        await interaction.response.send_message(embed=_err("Fehler", "Ungültige User-ID."), ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message(embed=_err("Fehler", "Nutzer nicht gefunden oder nicht gebannt."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(embed=_err("Fehler", f"`{e}`"), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /setalerts & /removealerts
# ══════════════════════════════════════════════════════════════════

@tree.command(name="setalerts", description="Aktiviert DM-Alerts für dich")
async def setalerts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid in config_data["alert_users"]:
        await interaction.response.send_message(embed=_warn("Bereits aktiv", "Du hast bereits Alerts aktiviert."), ephemeral=True)
        return
    config_data["alert_users"].append(uid)
    save_config(config_data)
    await interaction.response.send_message(embed=_ok("🔔  DM-Alerts aktiviert", "Du erhältst ab jetzt Benachrichtigungen per DM."), ephemeral=True)

@tree.command(name="removealerts", description="Deaktiviert DM-Alerts für dich")
async def removealerts(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in config_data["alert_users"]:
        await interaction.response.send_message(embed=_warn("Nicht aktiv", "Du hast keine aktiven Alerts."), ephemeral=True)
        return
    config_data["alert_users"].remove(uid)
    save_config(config_data)
    await interaction.response.send_message(embed=_ok("🔕  DM-Alerts deaktiviert", "Du erhältst keine DM-Benachrichtigungen mehr."), ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  /clear  (Manage Messages)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="clear", description="Löscht Nachrichten im Channel (optional: nur von einem User)")
@app_commands.describe(anzahl="Anzahl der Nachrichten (max. 100)", member="Nur Nachrichten dieses Users löschen")
async def clear(interaction: discord.Interaction, anzahl: int = 100, member: discord.Member = None):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message(embed=_err("Keine Berechtigung", "Du benötigst `Nachrichten verwalten`."), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    anzahl = min(max(anzahl, 1), 100)

    if member:
        deleted_msgs = []
        async for msg in interaction.channel.history(limit=300):
            if msg.author == member:
                deleted_msgs.append(msg)
            if len(deleted_msgs) >= anzahl:
                break
        for msg in deleted_msgs:
            try:
                await msg.delete()
                await asyncio.sleep(0.4)
            except Exception:
                pass
        count = len(deleted_msgs)
        embed = liquid_glass_embed("🗑️  Clear", f"**{count}** Nachricht(en) von **{member}** gelöscht.", discord.Color.from_rgb(255, 100, 100))
    else:
        deleted_msgs = await interaction.channel.purge(limit=anzahl)
        count = len(deleted_msgs)
        embed = liquid_glass_embed("🗑️  Clear", f"**{count}** Nachricht(en) gelöscht.", discord.Color.from_rgb(255, 100, 100))

    await interaction.followup.send(embed=embed, ephemeral=True)
    log.info("[Clear] %s löschte %d Nachrichten in #%s", interaction.user, count, interaction.channel)

# ══════════════════════════════════════════════════════════════════
#  /userinfo
# ══════════════════════════════════════════════════════════════════

@tree.command(name="userinfo", description="Zeigt detaillierte Infos über einen User")
@app_commands.describe(member="Der User (leer = du selbst)")
async def userinfo(interaction: discord.Interaction, member: discord.Member = None):
    member  = member or interaction.user
    roles   = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
    joined  = member.joined_at.strftime("%d.%m.%Y %H:%M") if member.joined_at else "Unbekannt"
    created = member.created_at.strftime("%d.%m.%Y %H:%M")

    status_map = {
        discord.Status.online:  "🟢 Online",
        discord.Status.idle:    "🟡 Abwesend",
        discord.Status.dnd:     "🔴 Bitte nicht stören",
        discord.Status.offline: "⚫ Offline",
    }
    status = status_map.get(member.status, "⚫ Offline")

    embed = liquid_glass_embed(
        f"👤  Userinfo – {member.display_name}",
        color=discord.Color.from_rgb(130, 200, 240),
        thumbnail=member.display_avatar.url,
    )
    embed.add_field(name="🆔  ID",               value=f"`{member.id}`",                      inline=True)
    embed.add_field(name="📛  Name",              value=str(member),                            inline=True)
    embed.add_field(name="💬  Status",            value=status,                                 inline=True)
    embed.add_field(name="🤖  Bot",               value="Ja" if member.bot else "Nein",         inline=True)
    embed.add_field(name="📅  Account erstellt",  value=f"`{created}`",                         inline=True)
    embed.add_field(name="📥  Beigetreten",       value=f"`{joined}`",                          inline=True)
    embed.add_field(name="🎭  Rollen",            value=", ".join(roles) if roles else "Keine", inline=False)
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /status  (Owner only)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="status", description="Setzt den Bot-Status (nur Owner)")
@app_commands.describe(text="Status-Text", typ="Typ: playing / watching / listening / streaming")
@app_commands.choices(typ=[
    app_commands.Choice(name="playing",   value="playing"),
    app_commands.Choice(name="watching",  value="watching"),
    app_commands.Choice(name="listening", value="listening"),
    app_commands.Choice(name="streaming", value="streaming"),
])
async def status_cmd(interaction: discord.Interaction, text: str, typ: str = "playing"):
    if not await _require_owner(interaction):
        return
    activity_map = {
        "playing":   discord.Game(name=text),
        "watching":  discord.Activity(type=discord.ActivityType.watching,  name=text),
        "listening": discord.Activity(type=discord.ActivityType.listening, name=text),
        "streaming": discord.Streaming(name=text, url="https://twitch.tv/placeholder"),
    }
    await bot.change_presence(activity=activity_map.get(typ, discord.Game(name=text)))
    embed = _ok("✅  Status geändert", f"**Typ:** {typ}\n**Text:** `{text}`")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log.info("[Status] Owner änderte Status: %s – %s", typ, text)

# ══════════════════════════════════════════════════════════════════
#  /ankündigung  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="ankündigung", description="Schickt eine Ankündigung in einen bestimmten Kanal")
@app_commands.describe(kanal="Ziel-Kanal", titel="Titel", nachricht="Inhalt der Ankündigung")
async def ankuendigung(interaction: discord.Interaction, kanal: discord.TextChannel, titel: str, nachricht: str):
    if not await _require_admin(interaction):
        return
    embed = liquid_glass_embed(
        titel, nachricht,
        color=discord.Color.from_rgb(255, 200, 60),
        author_name=interaction.user.display_name,
        author_icon=interaction.user.display_avatar.url,
        footer_text="📢  GermanyRP • Ankündigung",
    )
    await kanal.send(embed=embed)
    await interaction.response.send_message(embed=_ok("✅  Gesendet", f"Ankündigung wurde in {kanal.mention} gesendet."), ephemeral=True)
    log.info("[Ankündigung] %s → #%s: %s", interaction.user, kanal, titel)

# ══════════════════════════════════════════════════════════════════
#  Musik – Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════

music_queues : dict = defaultdict(list)   # guild_id -> [(url, title), ...]
music_playing: dict = {}                  # guild_id -> bool

YDL_OPTIONS: dict = {
    "format":         "bestaudio/best",
    "noplaylist":     True,
    "quiet":          True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["web_creator", "tv_embedded"]}},
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    },
}
FFMPEG_OPTIONS: dict = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options":        "-vn",
}


def get_audio_source(url: str):
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        stream_url = info["url"]
        title      = info.get("title", "Unbekannt")
        duration   = info.get("duration", 0)
        return discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS), title, duration


async def play_next(guild_id: int, voice_client: discord.VoiceClient) -> None:
    queue = music_queues[guild_id]
    if not queue:
        music_playing[guild_id] = False
        return
    url, title = queue.pop(0)
    try:
        source, fetched_title, _ = await asyncio.get_event_loop().run_in_executor(
            None, get_audio_source, url
        )
        music_playing[guild_id] = True
        voice_client.play(
            discord.PCMVolumeTransformer(source, volume=0.5),
            after=lambda e: asyncio.run_coroutine_threadsafe(
                play_next(guild_id, voice_client), bot.loop
            ),
        )
    except Exception as e:
        music_playing[guild_id] = False
        log.error("[Musik] play_next Fehler: %s", e)

# ══════════════════════════════════════════════════════════════════
#  /play
# ══════════════════════════════════════════════════════════════════

@tree.command(name="play", description="Spielt Musik aus einem YouTube-Link oder Suchbegriff ab")
@app_commands.describe(link="YouTube-Link oder Suchbegriff")
async def play(interaction: discord.Interaction, link: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            embed=_warn("Kein Voice-Channel", "Du musst in einem Voice-Channel sein!"),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    voice_channel = interaction.user.voice.channel
    guild_id      = interaction.guild.id
    vc            = interaction.guild.voice_client

    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    try:
        source, title, duration = await asyncio.get_event_loop().run_in_executor(
            None, get_audio_source, link
        )
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"

        if vc.is_playing() or vc.is_paused():
            music_queues[guild_id].append((link, title))
            embed = liquid_glass_embed(
                "🎵  Zur Warteschlange hinzugefügt",
                f"**{title}**\n⏱️ Dauer: `{dur_str}`\n📋 Position: `{len(music_queues[guild_id])}`",
                discord.Color.from_rgb(130, 200, 240),
            )
            await interaction.followup.send(embed=embed)
        else:
            music_playing[guild_id] = True
            vc.play(
                discord.PCMVolumeTransformer(source, volume=0.5),
                after=lambda e: asyncio.run_coroutine_threadsafe(
                    play_next(guild_id, vc), bot.loop
                ),
            )
            embed = liquid_glass_embed(
                "▶️  Spielt jetzt",
                f"**{title}**\n⏱️ Dauer: `{dur_str}`",
                discord.Color.from_rgb(100, 220, 150),
            )
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(embed=_err("Fehler", f"`{e}`"), ephemeral=True)
        log.error("[Musik] Abspielfehler: %s", e)

# ══════════════════════════════════════════════════════════════════
#  /skip
# ══════════════════════════════════════════════════════════════════

@tree.command(name="skip", description="Überspringt den aktuellen Song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message(
            embed=_warn("Nichts aktiv", "Es wird gerade nichts gespielt."),
            ephemeral=True,
        )
        return
    vc.stop()
    remaining = len(music_queues[interaction.guild.id])
    embed = liquid_glass_embed(
        "⏭️  Übersprungen",
        f"Song übersprungen.\n📋 Noch in der Warteschlange: `{remaining}`",
        discord.Color.from_rgb(130, 200, 240),
    )
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  /stop
# ══════════════════════════════════════════════════════════════════

@tree.command(name="stop", description="Stoppt die Musik und verlässt den Voice-Channel")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message(
            embed=_warn("Nicht verbunden", "Der Bot ist in keinem Voice-Channel."),
            ephemeral=True,
        )
        return
    music_queues[interaction.guild.id].clear()
    music_playing[interaction.guild.id] = False
    await vc.disconnect()
    embed = liquid_glass_embed(
        "⏹️  Gestoppt",
        "Musik gestoppt und Voice-Channel verlassen.",
        discord.Color.from_rgb(220, 100, 100),
    )
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════
#  Anti-Link – Helfer
# ══════════════════════════════════════════════════════════════════

LINK_PATTERN = re.compile(r"https?://|discord\.gg/|www\.", re.IGNORECASE)


def get_antilink(guild_id: int) -> dict:
    cfg = load_config()
    return cfg.get("antilink", {}).get(str(guild_id), {
        "enabled":         False,
        "timeout_minutes": 5,
        "delete_message":  True,
        "ignored_users":   [],
        "ignored_roles":   [],
    })


def save_antilink(guild_id: int, data: dict) -> None:
    cfg = load_config()
    cfg.setdefault("antilink", {})[str(guild_id)] = data
    save_config(cfg)

# ══════════════════════════════════════════════════════════════════
#  on_message  (Anti-Link)
# ══════════════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    settings = get_antilink(message.guild.id)

    if settings.get("enabled"):
        is_ignored_user = message.author.id in settings.get("ignored_users", [])
        member_role_ids = {r.id for r in message.author.roles}
        is_ignored_role = bool(member_role_ids & set(settings.get("ignored_roles", [])))

        if not is_ignored_user and not is_ignored_role:
            if LINK_PATTERN.search(message.content):
                if settings.get("delete_message", True):
                    try:
                        await message.delete()
                    except Exception:
                        pass

                minutes = settings.get("timeout_minutes", 5)
                if minutes > 0:
                    try:
                        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                        await message.author.timeout(until, reason="[Anti-Link] Link gesendet")
                    except Exception:
                        pass

                embed = _err(
                    "🔗  Anti-Link",
                    f"{message.author.mention} Links sind auf diesem Server nicht erlaubt!\n"
                    f"**Timeout:** {minutes} Minuten",
                )
                try:
                    warn_msg = await message.channel.send(embed=embed)
                    await asyncio.sleep(6)
                    await warn_msg.delete()
                except Exception:
                    pass

    await bot.process_commands(message)

# ══════════════════════════════════════════════════════════════════
#  /antilink  (Admin)
# ══════════════════════════════════════════════════════════════════

@tree.command(name="antilink", description="Anti-Link System konfigurieren")
@app_commands.describe(
    aktiv="Anti-Link aktivieren oder deaktivieren",
    timeout_minuten="Timeout-Dauer in Minuten (0 = kein Timeout)",
    nachricht_loeschen="Nachricht mit Link automatisch löschen?",
)
async def antilink_cmd(
    interaction: discord.Interaction,
    aktiv: bool,
    timeout_minuten: int = 5,
    nachricht_loeschen: bool = True,
):
    if not await _require_admin(interaction):
        return

    settings = get_antilink(interaction.guild.id)
    settings["enabled"]         = aktiv
    settings["timeout_minutes"] = timeout_minuten
    settings["delete_message"]  = nachricht_loeschen
    save_antilink(interaction.guild.id, settings)

    status = "✅ Aktiviert" if aktiv else "❌ Deaktiviert"
    embed  = liquid_glass_embed(
        "🔗  Anti-Link Einstellungen",
        f"**Status:** {status}\n**Timeout:** {timeout_minuten} Minuten\n**Nachricht löschen:** {'Ja' if nachricht_loeschen else 'Nein'}",
        discord.Color.from_rgb(100, 220, 150) if aktiv else discord.Color.from_rgb(255, 80, 80),
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="antilink-ignore-user", description="User vom Anti-Link System ausschließen / hinzufügen")
@app_commands.describe(member="Der User", aktion="Hinzufügen oder entfernen")
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen",  value="remove"),
])
async def antilink_ignore_user(interaction: discord.Interaction, member: discord.Member, aktion: str):
    if not await _require_admin(interaction):
        return

    settings = get_antilink(interaction.guild.id)
    ignored  = settings.get("ignored_users", [])

    if aktion == "add":
        if member.id not in ignored:
            ignored.append(member.id)
        msg = f"**{member}** wird jetzt ignoriert."
    else:
        ignored = [u for u in ignored if u != member.id]
        msg = f"**{member}** wird nicht mehr ignoriert."

    settings["ignored_users"] = ignored
    save_antilink(interaction.guild.id, settings)
    await interaction.response.send_message(embed=_info("Anti-Link • User", msg))

@tree.command(name="antilink-ignore-rolle", description="Rolle vom Anti-Link System ausschließen / hinzufügen")
@app_commands.describe(rolle="Die Rolle", aktion="Hinzufügen oder entfernen")
@app_commands.choices(aktion=[
    app_commands.Choice(name="hinzufügen", value="add"),
    app_commands.Choice(name="entfernen",  value="remove"),
])
async def antilink_ignore_rolle(interaction: discord.Interaction, rolle: discord.Role, aktion: str):
    if not await _require_admin(interaction):
        return

    settings = get_antilink(interaction.guild.id)
    ignored  = settings.get("ignored_roles", [])

    if aktion == "add":
        if rolle.id not in ignored:
            ignored.append(rolle.id)
        msg = f"**{rolle.name}** wird jetzt ignoriert."
    else:
        ignored = [r for r in ignored if r != rolle.id]
        msg = f"**{rolle.name}** wird nicht mehr ignoriert."

    settings["ignored_roles"] = ignored
    save_antilink(interaction.guild.id, settings)
    await interaction.response.send_message(embed=_info("Anti-Link • Rolle", msg))

@tree.command(name="antilink-status", description="Zeigt die aktuellen Anti-Link Einstellungen")
async def antilink_status(interaction: discord.Interaction):
    if not await _require_admin(interaction):
        return

    settings      = get_antilink(interaction.guild.id)
    enabled       = settings.get("enabled", False)
    timeout       = settings.get("timeout_minutes", 5)
    delete        = settings.get("delete_message", True)
    ignored_users = settings.get("ignored_users", [])
    ignored_roles = settings.get("ignored_roles", [])
    users_str     = ", ".join(f"<@{u}>"  for u in ignored_users) if ignored_users else "Keine"
    roles_str     = ", ".join(f"<@&{r}>" for r in ignored_roles) if ignored_roles else "Keine"

    embed = liquid_glass_embed(
        "🔗  Anti-Link Status",
        (
            f"**Status:** {'✅ Aktiv' if enabled else '❌ Inaktiv'}\n"
            f"**Timeout:** {timeout} Minuten\n"
            f"**Nachrichten löschen:** {'Ja' if delete else 'Nein'}\n"
            f"**Ignorierte User:** {users_str}\n"
            f"**Ignorierte Rollen:** {roles_str}"
        ),
        discord.Color.from_rgb(130, 200, 240),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════════════
#  Start
# ══════════════════════════════════════════════════════════════════

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN Umgebungsvariable ist nicht gesetzt!")

bot.run(token)