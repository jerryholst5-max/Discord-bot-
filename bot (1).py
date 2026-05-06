import discord
from discord.ext import commands
from discord import app_commands
import os
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# ──────────────────────────────────────────────

# Keep-Alive Webserver (für UptimeRobot)

# ──────────────────────────────────────────────

class KeepAlive(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b”Bot is alive!”)

```
def log_message(self, format, *args):
    pass
```

Thread(target=lambda: HTTPServer((“0.0.0.0”, 443), KeepAlive).serve_forever(), daemon=True).start()

# ──────────────────────────────────────────────

# Eigentümer-Immunität

# ──────────────────────────────────────────────

OWNER_ID = 1408144132966322407

def is_owner(user: discord.Member) -> bool:
return user.id == OWNER_ID

# ──────────────────────────────────────────────

# Bot Setup

# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.bans = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix=”!”, intents=intents, help_command=None)
tree = bot.tree

WARNINGS_FILE = os.path.join(os.path.dirname(**file**), “warnings.json”)
CONFIG_FILE = os.path.join(os.path.dirname(**file**), “config.json”)

def load_warnings():
if os.path.exists(WARNINGS_FILE):
with open(WARNINGS_FILE, “r”) as f:
return json.load(f)
return {}

def save_warnings(data):
with open(WARNINGS_FILE, “w”) as f:
json.dump(data, f, indent=2)

def load_config():
if os.path.exists(CONFIG_FILE):
with open(CONFIG_FILE, “r”) as f:
return json.load(f)
return {“alert_users”: []}

def save_config(data):
with open(CONFIG_FILE, “w”) as f:
json.dump(data, f, indent=2)

warnings_data = load_warnings()
config_data = load_config()

# ──────────────────────────────────────────────

# Anti-Nuke

# ──────────────────────────────────────────────

NUKE_WINDOW = 10
NUKE_THRESHOLD = 3
nuke_tracker: dict[int, dict[int, list[datetime]]] = defaultdict(
lambda: defaultdict(list)
)

async def get_audit_executor(guild: discord.Guild, action: discord.AuditLogAction):
try:
async for entry in guild.audit_logs(limit=1, action=action):
if (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 5:
return entry.user
except discord.Forbidden:
pass
return None

async def check_nuke(guild: discord.Guild, user, action_label: str):
if user is None or user.bot:
return
now = datetime.now(timezone.utc)
cutoff = now - timedelta(seconds=NUKE_WINDOW)
tracker = nuke_tracker[guild.id][user.id]
tracker.append(now)
nuke_tracker[guild.id][user.id] = [t for t in tracker if t > cutoff]
count = len(nuke_tracker[guild.id][user.id])

```
if count >= NUKE_THRESHOLD:
    nuke_tracker[guild.id][user.id] = []
    log_channel = guild.system_channel
    actions_taken = []
    try:
        member = guild.get_member(user.id)
        if member:
            bot_top_role = guild.me.top_role
            for role in member.roles:
                if role.name == "@everyone":
                    continue
                if role.permissions.administrator and role < bot_top_role:
                    try:
                        new_perms = discord.Permissions(role.permissions.value)
                        new_perms.update(administrator=False)
                        await role.edit(permissions=new_perms, reason="[Anti-Nuke] Admin-Recht entzogen")
                        actions_taken.append(f'🔐 Admin-Recht aus Rolle "{role.name}" entfernt')
                    except discord.Forbidden:
                        pass
            removable = [r for r in member.roles if r.name != "@everyone" and r < bot_top_role]
            if removable:
                await member.remove_roles(*removable, reason="[Anti-Nuke] Alle Rollen entfernt")
                actions_taken.append(f"🗑️ {len(removable)} Rolle(n) entfernt")
            try:
                await guild.ban(user, reason=f"[Anti-Nuke] {count}x {action_label} in {NUKE_WINDOW}s")
                actions_taken.append("🔨 User gebannt")
            except discord.Forbidden:
                actions_taken.append("❌ Ban fehlgeschlagen")
        msg = (
            f"🚨 **Anti-Nuke ausgelöst!**\n"
            f"**User:** {user.mention} (`{user}`)\n"
            f"**Aktion:** {count}x {action_label} in {NUKE_WINDOW} Sekunden\n"
            f"**Maßnahmen:** {chr(10).join(actions_taken) if actions_taken else 'keine'}"
        )
    except discord.Forbidden:
        msg = f"🚨 **Anti-Nuke:** {user} — {count}x {action_label} — ❌ Fehlende Berechtigung."
    if log_channel:
        await log_channel.send(msg)
```

@bot.event
async def on_member_ban(guild, user):
executor = await get_audit_executor(guild, discord.AuditLogAction.ban)
await check_nuke(guild, executor, “Ban”)

@bot.event
async def on_guild_channel_delete(channel):
executor = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete)
await check_nuke(channel.guild, executor, “Channel-Löschung”)

@bot.event
async def on_guild_role_delete(role):
executor = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete)
await check_nuke(role.guild, executor, “Rollen-Löschung”)

@bot.event
async def on_member_remove(member):
executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick)
if executor:
await check_nuke(member.guild, executor, “Kick”)

# ──────────────────────────────────────────────

# Ready

# ──────────────────────────────────────────────

@bot.event
async def on_ready():
await tree.sync()
print(f”Bot ist online als {bot.user}”)
print(“Alle Slash Commands synchronisiert! Anti-Nuke läuft.”)

# ──────────────────────────────────────────────

# Slash Commands

# ──────────────────────────────────────────────

@tree.command(name=“help”, description=“Zeigt alle Befehle”)
async def help_cmd(interaction: discord.Interaction):
embed = discord.Embed(
title=“📋 Bot Befehle”,
description=“Alle Befehle mit `/` Prefix”,
color=discord.Color.blurple(),
)
embed.add_field(name=“👋 Allgemein”, value=”`/hallo` — Begrüßung\n`/help` — Diese Hilfe”, inline=False)
embed.add_field(
name=“🛡️ Moderation”,
value=(
“`/teamkick @User` — Alle Rollen entfernen\n”
“`/tempmute @User [Min] [Grund]` — Stumm schalten\n”
“`/unmute @User` — Mute aufheben\n”
“`/teamwarn @User [Grund]` — Verwarnen (Auto-Ban bei 3)\n”
“`/warnings @User` — Verwarnungen anzeigen\n”
“`/allwarnings` — Alle User mit Warns\n”
“`/clearwarnings @User` — Verwarnungen löschen\n”
“`/bann @User [Grund]` — User bannen\n”
“`/unban [User-ID]` — User entbannen”
),
inline=False,
)
embed.add_field(name=“🔔 DM-Alerts”, value=”`/setalerts` — DM an\n`/removealerts` — DM aus”, inline=False)
embed.add_field(name=“🚨 Anti-Nuke”, value=“Läuft automatisch im Hintergrund.”, inline=False)
await interaction.response.send_message(embed=embed)

@tree.command(name=“hallo”, description=“Begrüßung vom Bot”)
async def hallo(interaction: discord.Interaction):
await interaction.response.send_message(“Hallo! 👋”)

@tree.command(name=“teamkick”, description=“Entfernt alle Rollen eines Users”)
@app_commands.describe(member=“Der User dem die Rollen entfernt werden”)
async def teamkick(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_roles:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
roles = [r for r in member.roles if r.name != “@everyone”]
if not roles:
await interaction.response.send_message(f”ℹ️ {member.mention} hat keine Rollen.”)
return
await member.remove_roles(*roles)
await interaction.response.send_message(f”✅ Alle Rollen von {member.mention} entfernt!”)

@tree.command(name=“tempmute”, description=“Schaltet einen User stumm”)
@app_commands.describe(member=“Der User”, minuten=“Dauer in Minuten”, grund=“Grund”)
async def tempmute(interaction: discord.Interaction, member: discord.Member, minuten: int, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.moderate_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
if minuten <= 0 or minuten > 40320:
await interaction.response.send_message(“❌ Gültige Dauer: 1–40320 Minuten.”)
return
until = datetime.now(timezone.utc) + timedelta(minutes=minuten)
try:
await member.timeout(until, reason=f”{grund} (von {interaction.user})”)
await interaction.response.send_message(f”🔇 {member.mention} für **{minuten} Min** stummgeschaltet. **Grund:** {grund}”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“unmute”, description=“Hebt den Mute eines Users auf”)
@app_commands.describe(member=“Der User”)
async def unmute(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.moderate_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
try:
await member.timeout(None)
await interaction.response.send_message(f”🔊 Mute von {member.mention} aufgehoben!”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“teamwarn”, description=“Verwarnt einen User”)
@app_commands.describe(member=“Der User”, grund=“Grund der Verwarnung”)
async def teamwarn(interaction: discord.Interaction, member: discord.Member, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
user_id = str(member.id)
if user_id not in warnings_data:
warnings_data[user_id] = []
warnings_data[user_id].append({“reason”: grund, “by”: str(interaction.user), “at”: datetime.utcnow().isoformat()})
save_warnings(warnings_data)
count = len(warnings_data[user_id])
await interaction.response.send_message(f”⚠️ {member.mention} verwarnt! **Grund:** {grund} — Verwarnung **{count}**”)

```
for uid in config_data.get("alert_users", []):
    try:
        u = await bot.fetch_user(int(uid))
        embed = discord.Embed(title="⚠️ Neue Verwarnung", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
        embed.add_field(name="Von", value=str(interaction.user), inline=True)
        embed.add_field(name="Grund", value=grund, inline=False)
        embed.add_field(name="Nr.", value=str(count), inline=True)
        embed.add_field(name="Server", value=interaction.guild.name, inline=True)
        await u.send(embed=embed)
    except (discord.Forbidden, discord.NotFound):
        pass

if count >= 3:
    try:
        await member.ban(reason=f"Auto-Ban nach {count} Verwarnungen")
        warnings_data[user_id] = []
        save_warnings(warnings_data)
        await interaction.followup.send(f"🔨 {member.mention} automatisch gebannt!")
    except discord.Forbidden:
        await interaction.followup.send("❌ Fehlende Berechtigung zum Bannen.")
```

@tree.command(name=“warnings”, description=“Zeigt Verwarnungen eines Users”)
@app_commands.describe(member=“Der User”)
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
user_warnings = warnings_data.get(str(member.id), [])
if not user_warnings:
await interaction.response.send_message(f”✅ {member.mention} hat keine Verwarnungen.”)
return
lines = [f”📋 **Verwarnungen für {member.mention}:** ({len(user_warnings)} gesamt)\n”]
for i, w in enumerate(user_warnings, 1):
lines.append(f”**{i}.** {w[‘reason’]} — von {w[‘by’]} am {w[‘at’][:10]}”)
await interaction.response.send_message(”\n”.join(lines))

@tree.command(name=“allwarnings”, description=“Zeigt alle User mit Verwarnungen”)
async def allwarnings(interaction: discord.Interaction):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
active = {uid: w for uid, w in warnings_data.items() if w}
if not active:
await interaction.response.send_message(“✅ Keine aktiven Verwarnungen.”)
return
embed = discord.Embed(title=“⚠️ Alle Verwarnungen”, description=f”{len(active)} User”, color=discord.Color.orange())
for uid, warns in sorted(active.items(), key=lambda x: len(x[1]), reverse=True):
try:
u = await bot.fetch_user(int(uid))
name = f”{u} (`{uid}`)”
except discord.NotFound:
name = f”Unbekannt (`{uid}`)”
last = warns[-1]
embed.add_field(name=f”{name} — {len(warns)}x”, value=f”Letzte: {last[‘reason’]} am {last[‘at’][:10]}”, inline=False)
await interaction.response.send_message(embed=embed)

@tree.command(name=“clearwarnings”, description=“Löscht Verwarnungen eines Users”)
@app_commands.describe(member=“Der User”)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
user_id = str(member.id)
if not warnings_data.get(user_id):
await interaction.response.send_message(f”ℹ️ {member.mention} hat keine Verwarnungen.”)
return
count = len(warnings_data[user_id])
warnings_data[user_id] = []
save_warnings(warnings_data)
await interaction.response.send_message(f”✅ {count} Verwarnung(en) von {member.mention} gelöscht!”)

@tree.command(name=“bann”, description=“Bannt einen User vom Server”)
@app_commands.describe(member=“Der User”, grund=“Grund des Banns”)
async def bann(interaction: discord.Interaction, member: discord.Member, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.ban_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
try:
await member.ban(reason=f”{grund} (von {interaction.user})”)
await interaction.response.send_message(f”🔨 {member.mention} gebannt! **Grund:** {grund}”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“unban”, description=“Entbannt einen User”)
@app_commands.describe(user_id=“Die Discord User-ID”)
async def unban(interaction: discord.Interaction, user_id: str):
if not interaction.user.guild_permissions.ban_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
try:
user = await bot.fetch_user(int(user_id))
await interaction.guild.unban(user)
await interaction.response.send_message(f”✅ {user} entbannt!”)
except ValueError:
await interaction.response.send_message(“❌ Ungültige User-ID.”)
except discord.NotFound:
await interaction.response.send_message(“❌ User nicht gefunden oder nicht gebannt.”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“setalerts”, description=“DM-Benachrichtigungen bei Verwarnungen aktivieren”)
async def setalerts(interaction: discord.Interaction):
if not interaction.user.guild_permissions.administrator:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
uid = str(interaction.user.id)
if uid in config_data[“alert_users”]:
await interaction.response.send_message(“ℹ️ Du erhältst bereits DM-Benachrichtigungen.”)
return
config_data[“alert_users”].append(uid)
save_config(config_data)
await interaction.response.send_message(“✅ Du bekommst ab jetzt eine DM bei jeder Verwarnung!”)

@tree.command(name=“removealerts”, description=“DM-Benachrichtigungen deaktivieren”)
async def removealerts(interaction: discord.Interaction):
if not interaction.user.guild_permissions.administrator:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
uid = str(interaction.user.id)
if uid not in config_data[“alert_users”]:
await interaction.response.send_message(“ℹ️ Du hast keine aktiven Benachrichtigungen.”)
return
config_data[“alert_users”].remove(uid)
save_config(config_data)
await interaction.response.send_message(“✅ DM-Benachrichtigungen deaktiviert.”)

# ──────────────────────────────────────────────

# Start

# ──────────────────────────────────────────────

token = os.environ.get(“DISCORD_TOKEN”)
if not token:
raise RuntimeError(“DISCORD_TOKEN environment variable is not set”)

bot.run(token)import discord
from discord.ext import commands
from discord import app_commands
import os
import json
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# ──────────────────────────────────────────────

# Keep-Alive Webserver (für UptimeRobot)

# ──────────────────────────────────────────────

class KeepAlive(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b”Bot is alive!”)

```
def log_message(self, format, *args):
    pass
```

Thread(target=lambda: HTTPServer((“0.0.0.0”, 8080), KeepAlive).serve_forever(), daemon=True).start()

# ──────────────────────────────────────────────

# Eigentümer-Immunität

# ──────────────────────────────────────────────

OWNER_ID = 1408144132966322407

def is_owner(user: discord.Member) -> bool:
return user.id == OWNER_ID

# ──────────────────────────────────────────────

# Bot Setup

# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.bans = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix=”!”, intents=intents, help_command=None)
tree = bot.tree

WARNINGS_FILE = os.path.join(os.path.dirname(**file**), “warnings.json”)
CONFIG_FILE = os.path.join(os.path.dirname(**file**), “config.json”)

def load_warnings():
if os.path.exists(WARNINGS_FILE):
with open(WARNINGS_FILE, “r”) as f:
return json.load(f)
return {}

def save_warnings(data):
with open(WARNINGS_FILE, “w”) as f:
json.dump(data, f, indent=2)

def load_config():
if os.path.exists(CONFIG_FILE):
with open(CONFIG_FILE, “r”) as f:
return json.load(f)
return {“alert_users”: []}

def save_config(data):
with open(CONFIG_FILE, “w”) as f:
json.dump(data, f, indent=2)

warnings_data = load_warnings()
config_data = load_config()

# ──────────────────────────────────────────────

# Anti-Nuke

# ──────────────────────────────────────────────

NUKE_WINDOW = 10
NUKE_THRESHOLD = 3
nuke_tracker: dict[int, dict[int, list[datetime]]] = defaultdict(
lambda: defaultdict(list)
)

async def get_audit_executor(guild: discord.Guild, action: discord.AuditLogAction):
try:
async for entry in guild.audit_logs(limit=1, action=action):
if (datetime.now(timezone.utc) - entry.created_at).total_seconds() < 5:
return entry.user
except discord.Forbidden:
pass
return None

async def check_nuke(guild: discord.Guild, user, action_label: str):
if user is None or user.bot:
return
now = datetime.now(timezone.utc)
cutoff = now - timedelta(seconds=NUKE_WINDOW)
tracker = nuke_tracker[guild.id][user.id]
tracker.append(now)
nuke_tracker[guild.id][user.id] = [t for t in tracker if t > cutoff]
count = len(nuke_tracker[guild.id][user.id])

```
if count >= NUKE_THRESHOLD:
    nuke_tracker[guild.id][user.id] = []
    log_channel = guild.system_channel
    actions_taken = []
    try:
        member = guild.get_member(user.id)
        if member:
            bot_top_role = guild.me.top_role
            for role in member.roles:
                if role.name == "@everyone":
                    continue
                if role.permissions.administrator and role < bot_top_role:
                    try:
                        new_perms = discord.Permissions(role.permissions.value)
                        new_perms.update(administrator=False)
                        await role.edit(permissions=new_perms, reason="[Anti-Nuke] Admin-Recht entzogen")
                        actions_taken.append(f'🔐 Admin-Recht aus Rolle "{role.name}" entfernt')
                    except discord.Forbidden:
                        pass
            removable = [r for r in member.roles if r.name != "@everyone" and r < bot_top_role]
            if removable:
                await member.remove_roles(*removable, reason="[Anti-Nuke] Alle Rollen entfernt")
                actions_taken.append(f"🗑️ {len(removable)} Rolle(n) entfernt")
            try:
                await guild.ban(user, reason=f"[Anti-Nuke] {count}x {action_label} in {NUKE_WINDOW}s")
                actions_taken.append("🔨 User gebannt")
            except discord.Forbidden:
                actions_taken.append("❌ Ban fehlgeschlagen")
        msg = (
            f"🚨 **Anti-Nuke ausgelöst!**\n"
            f"**User:** {user.mention} (`{user}`)\n"
            f"**Aktion:** {count}x {action_label} in {NUKE_WINDOW} Sekunden\n"
            f"**Maßnahmen:** {chr(10).join(actions_taken) if actions_taken else 'keine'}"
        )
    except discord.Forbidden:
        msg = f"🚨 **Anti-Nuke:** {user} — {count}x {action_label} — ❌ Fehlende Berechtigung."
    if log_channel:
        await log_channel.send(msg)
```

@bot.event
async def on_member_ban(guild, user):
executor = await get_audit_executor(guild, discord.AuditLogAction.ban)
await check_nuke(guild, executor, “Ban”)

@bot.event
async def on_guild_channel_delete(channel):
executor = await get_audit_executor(channel.guild, discord.AuditLogAction.channel_delete)
await check_nuke(channel.guild, executor, “Channel-Löschung”)

@bot.event
async def on_guild_role_delete(role):
executor = await get_audit_executor(role.guild, discord.AuditLogAction.role_delete)
await check_nuke(role.guild, executor, “Rollen-Löschung”)

@bot.event
async def on_member_remove(member):
executor = await get_audit_executor(member.guild, discord.AuditLogAction.kick)
if executor:
await check_nuke(member.guild, executor, “Kick”)

# ──────────────────────────────────────────────

# Ready

# ──────────────────────────────────────────────

@bot.event
async def on_ready():
await tree.sync()
print(f”Bot ist online als {bot.user}”)
print(“Alle Slash Commands synchronisiert! Anti-Nuke läuft.”)

# ──────────────────────────────────────────────

# Slash Commands

# ──────────────────────────────────────────────

@tree.command(name=“help”, description=“Zeigt alle Befehle”)
async def help_cmd(interaction: discord.Interaction):
embed = discord.Embed(
title=“📋 Bot Befehle”,
description=“Alle Befehle mit `/` Prefix”,
color=discord.Color.blurple(),
)
embed.add_field(name=“👋 Allgemein”, value=”`/hallo` — Begrüßung\n`/help` — Diese Hilfe”, inline=False)
embed.add_field(
name=“🛡️ Moderation”,
value=(
“`/teamkick @User` — Alle Rollen entfernen\n”
“`/tempmute @User [Min] [Grund]` — Stumm schalten\n”
“`/unmute @User` — Mute aufheben\n”
“`/teamwarn @User [Grund]` — Verwarnen (Auto-Ban bei 3)\n”
“`/warnings @User` — Verwarnungen anzeigen\n”
“`/allwarnings` — Alle User mit Warns\n”
“`/clearwarnings @User` — Verwarnungen löschen\n”
“`/bann @User [Grund]` — User bannen\n”
“`/unban [User-ID]` — User entbannen”
),
inline=False,
)
embed.add_field(name=“🔔 DM-Alerts”, value=”`/setalerts` — DM an\n`/removealerts` — DM aus”, inline=False)
embed.add_field(name=“🚨 Anti-Nuke”, value=“Läuft automatisch im Hintergrund.”, inline=False)
await interaction.response.send_message(embed=embed)

@tree.command(name=“hallo”, description=“Begrüßung vom Bot”)
async def hallo(interaction: discord.Interaction):
await interaction.response.send_message(“Hallo! 👋”)

@tree.command(name=“teamkick”, description=“Entfernt alle Rollen eines Users”)
@app_commands.describe(member=“Der User dem die Rollen entfernt werden”)
async def teamkick(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_roles:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
roles = [r for r in member.roles if r.name != “@everyone”]
if not roles:
await interaction.response.send_message(f”ℹ️ {member.mention} hat keine Rollen.”)
return
await member.remove_roles(*roles)
await interaction.response.send_message(f”✅ Alle Rollen von {member.mention} entfernt!”)

@tree.command(name=“tempmute”, description=“Schaltet einen User stumm”)
@app_commands.describe(member=“Der User”, minuten=“Dauer in Minuten”, grund=“Grund”)
async def tempmute(interaction: discord.Interaction, member: discord.Member, minuten: int, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.moderate_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
if minuten <= 0 or minuten > 40320:
await interaction.response.send_message(“❌ Gültige Dauer: 1–40320 Minuten.”)
return
until = datetime.now(timezone.utc) + timedelta(minutes=minuten)
try:
await member.timeout(until, reason=f”{grund} (von {interaction.user})”)
await interaction.response.send_message(f”🔇 {member.mention} für **{minuten} Min** stummgeschaltet. **Grund:** {grund}”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“unmute”, description=“Hebt den Mute eines Users auf”)
@app_commands.describe(member=“Der User”)
async def unmute(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.moderate_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
try:
await member.timeout(None)
await interaction.response.send_message(f”🔊 Mute von {member.mention} aufgehoben!”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“teamwarn”, description=“Verwarnt einen User”)
@app_commands.describe(member=“Der User”, grund=“Grund der Verwarnung”)
async def teamwarn(interaction: discord.Interaction, member: discord.Member, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
user_id = str(member.id)
if user_id not in warnings_data:
warnings_data[user_id] = []
warnings_data[user_id].append({“reason”: grund, “by”: str(interaction.user), “at”: datetime.utcnow().isoformat()})
save_warnings(warnings_data)
count = len(warnings_data[user_id])
await interaction.response.send_message(f”⚠️ {member.mention} verwarnt! **Grund:** {grund} — Verwarnung **{count}**”)

```
for uid in config_data.get("alert_users", []):
    try:
        u = await bot.fetch_user(int(uid))
        embed = discord.Embed(title="⚠️ Neue Verwarnung", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
        embed.add_field(name="Von", value=str(interaction.user), inline=True)
        embed.add_field(name="Grund", value=grund, inline=False)
        embed.add_field(name="Nr.", value=str(count), inline=True)
        embed.add_field(name="Server", value=interaction.guild.name, inline=True)
        await u.send(embed=embed)
    except (discord.Forbidden, discord.NotFound):
        pass

if count >= 3:
    try:
        await member.ban(reason=f"Auto-Ban nach {count} Verwarnungen")
        warnings_data[user_id] = []
        save_warnings(warnings_data)
        await interaction.followup.send(f"🔨 {member.mention} automatisch gebannt!")
    except discord.Forbidden:
        await interaction.followup.send("❌ Fehlende Berechtigung zum Bannen.")
```

@tree.command(name=“warnings”, description=“Zeigt Verwarnungen eines Users”)
@app_commands.describe(member=“Der User”)
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
user_warnings = warnings_data.get(str(member.id), [])
if not user_warnings:
await interaction.response.send_message(f”✅ {member.mention} hat keine Verwarnungen.”)
return
lines = [f”📋 **Verwarnungen für {member.mention}:** ({len(user_warnings)} gesamt)\n”]
for i, w in enumerate(user_warnings, 1):
lines.append(f”**{i}.** {w[‘reason’]} — von {w[‘by’]} am {w[‘at’][:10]}”)
await interaction.response.send_message(”\n”.join(lines))

@tree.command(name=“allwarnings”, description=“Zeigt alle User mit Verwarnungen”)
async def allwarnings(interaction: discord.Interaction):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
active = {uid: w for uid, w in warnings_data.items() if w}
if not active:
await interaction.response.send_message(“✅ Keine aktiven Verwarnungen.”)
return
embed = discord.Embed(title=“⚠️ Alle Verwarnungen”, description=f”{len(active)} User”, color=discord.Color.orange())
for uid, warns in sorted(active.items(), key=lambda x: len(x[1]), reverse=True):
try:
u = await bot.fetch_user(int(uid))
name = f”{u} (`{uid}`)”
except discord.NotFound:
name = f”Unbekannt (`{uid}`)”
last = warns[-1]
embed.add_field(name=f”{name} — {len(warns)}x”, value=f”Letzte: {last[‘reason’]} am {last[‘at’][:10]}”, inline=False)
await interaction.response.send_message(embed=embed)

@tree.command(name=“clearwarnings”, description=“Löscht Verwarnungen eines Users”)
@app_commands.describe(member=“Der User”)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
if not interaction.user.guild_permissions.manage_messages:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
user_id = str(member.id)
if not warnings_data.get(user_id):
await interaction.response.send_message(f”ℹ️ {member.mention} hat keine Verwarnungen.”)
return
count = len(warnings_data[user_id])
warnings_data[user_id] = []
save_warnings(warnings_data)
await interaction.response.send_message(f”✅ {count} Verwarnung(en) von {member.mention} gelöscht!”)

@tree.command(name=“bann”, description=“Bannt einen User vom Server”)
@app_commands.describe(member=“Der User”, grund=“Grund des Banns”)
async def bann(interaction: discord.Interaction, member: discord.Member, grund: str = “Kein Grund angegeben”):
if not interaction.user.guild_permissions.ban_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
if is_owner(member):
await interaction.response.send_message(“🛡️ Der Eigentümer ist immun gegen diesen Befehl!”, ephemeral=True)
return
try:
await member.ban(reason=f”{grund} (von {interaction.user})”)
await interaction.response.send_message(f”🔨 {member.mention} gebannt! **Grund:** {grund}”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“unban”, description=“Entbannt einen User”)
@app_commands.describe(user_id=“Die Discord User-ID”)
async def unban(interaction: discord.Interaction, user_id: str):
if not interaction.user.guild_permissions.ban_members:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
try:
user = await bot.fetch_user(int(user_id))
await interaction.guild.unban(user)
await interaction.response.send_message(f”✅ {user} entbannt!”)
except ValueError:
await interaction.response.send_message(“❌ Ungültige User-ID.”)
except discord.NotFound:
await interaction.response.send_message(“❌ User nicht gefunden oder nicht gebannt.”)
except discord.Forbidden:
await interaction.response.send_message(“❌ Fehlende Berechtigung.”)

@tree.command(name=“setalerts”, description=“DM-Benachrichtigungen bei Verwarnungen aktivieren”)
async def setalerts(interaction: discord.Interaction):
if not interaction.user.guild_permissions.administrator:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
uid = str(interaction.user.id)
if uid in config_data[“alert_users”]:
await interaction.response.send_message(“ℹ️ Du erhältst bereits DM-Benachrichtigungen.”)
return
config_data[“alert_users”].append(uid)
save_config(config_data)
await interaction.response.send_message(“✅ Du bekommst ab jetzt eine DM bei jeder Verwarnung!”)

@tree.command(name=“removealerts”, description=“DM-Benachrichtigungen deaktivieren”)
async def removealerts(interaction: discord.Interaction):
if not interaction.user.guild_permissions.administrator:
await interaction.response.send_message(“❌ Du hast keine Berechtigung!”, ephemeral=True)
return
uid = str(interaction.user.id)
if uid not in config_data[“alert_users”]:
await interaction.response.send_message(“ℹ️ Du hast keine aktiven Benachrichtigungen.”)
return
config_data[“alert_users”].remove(uid)
save_config(config_data)
await interaction.response.send_message(“✅ DM-Benachrichtigungen deaktiviert.”)

# ──────────────────────────────────────────────

# Start

# ──────────────────────────────────────────────

token = os.environ.get(“DISCORD_TOKEN”)
if not token:
raise RuntimeError(“DISCORD_TOKEN environment variable is not set”)

bot.run(toke
