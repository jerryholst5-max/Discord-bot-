"""
EINMAL-SKRIPT v2: Löscht ALLE Slash-Commands bei Discord - sowohl global
als auch pro-Server (Guild-Commands) - und zeigt danach, was noch übrig ist.

Der Unterschied zu clear_commands.py: Das alte Skript hat nur GLOBALE
Commands geleert (guild=None). Falls /actionaccess & Co. irgendwann mal
als serverspezifische Commands registriert wurden (z.B. durch einen alten
Testlauf mit tree.sync(guild=irgendein_server)), bleiben die davon
unberührt. Dieses Skript räumt zusätzlich JEDEN Server auf, auf dem der
Bot aktuell ist.

VERWENDUNG:
1. In denselben Ordner wie bot.py legen.
2. DISCORD_TOKEN als Umgebungsvariable setzen (denselben wie bot.py).
3. Einmal ausführen: python3 clear_commands_v2.py
4. Nach dem Lauf: bot.py normal neu deployen (Redeploy), damit sie ihre
   aktuellen Commands frisch registriert.
5. Diese Datei danach wieder löschen.
"""

import os
import discord
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN ist nicht gesetzt! Bitte als Umgebungsvariable setzen.")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"[INFO] Eingeloggt als {bot.user} ({bot.user.id})")
    print(f"[INFO] Bot ist auf {len(bot.guilds)} Server(n).")

    # 1. Globale Commands leeren
    print("\n[SCHRITT 1] Leere globale Commands...")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)
    global_cmds = await bot.tree.fetch_commands()
    print(f"[OK] Globale Commands jetzt: {len(global_cmds)} (sollte 0 sein)")

    # 2. Auf JEDEM Server, auf dem der Bot ist, auch die serverspezifischen
    #    (Guild-)Commands leeren - das erfasst /actionaccess & Co. falls sie
    #    dort und nicht global registriert wurden.
    print(f"\n[SCHRITT 2] Leere serverspezifische Commands auf allen {len(bot.guilds)} Servern...")
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            guild_cmds = await bot.tree.fetch_commands(guild=guild)
            status = "OK (0 Commands)" if len(guild_cmds) == 0 else f"⚠️ {len(guild_cmds)} übrig: {[c.name for c in guild_cmds]}"
            print(f"  - {guild.name} ({guild.id}): {status}")
        except discord.Forbidden:
            print(f"  - {guild.name} ({guild.id}): ❌ Keine Berechtigung (applications.commands fehlt?)")
        except Exception as e:
            print(f"  - {guild.name} ({guild.id}): ❌ Fehler: {e}")

    print("\n[FERTIG] Falls oben irgendwo noch Commands übrig sind, sind das")
    print("die gesuchten Phantom-Commands - deren Namen stehen jeweils dabei.")
    print("Starte danach deine normale bot.py neu (Redeploy).")
    await bot.close()


bot.run(TOKEN)
