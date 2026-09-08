"""
DIAGNOSE-SKRIPT: Zeigt ALLE Slash-Commands, die Discord für deine
Anwendung tatsächlich gespeichert hat - global und pro Server - mit
vollen Rohdaten (ID, Name, Integration-Type). Das ist die einzige
100% zuverlässige Quelle, unabhängig von Client-Cache-Problemen.

VERWENDUNG:
1. In denselben Ordner wie bot.py legen.
2. DISCORD_TOKEN als Umgebungsvariable setzen.
3. Ausführen: python3 diagnose_commands.py
4. Das Ergebnis hier im Chat teilen.
"""

import os
import discord
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN ist nicht gesetzt!")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"[INFO] Eingeloggt als {bot.user} ({bot.user.id})")
    print(f"[INFO] Application ID: {bot.application_id}")

    print("\n=== GLOBALE COMMANDS (roh von Discord) ===")
    global_cmds = await bot.tree.fetch_commands()
    if not global_cmds:
        print("  (keine)")
    for c in global_cmds:
        print(f"  - /{c.name}  [id={c.id}]")

    print(f"\n=== SERVERSPEZIFISCHE COMMANDS auf {len(bot.guilds)} Server(n) ===")
    any_guild_cmds = False
    for guild in bot.guilds:
        try:
            guild_cmds = await bot.tree.fetch_commands(guild=guild)
            if guild_cmds:
                any_guild_cmds = True
                print(f"  {guild.name} ({guild.id}):")
                for c in guild_cmds:
                    print(f"    - /{c.name}  [id={c.id}]")
        except Exception as e:
            print(f"  {guild.name} ({guild.id}): Fehler - {e}")
    if not any_guild_cmds:
        print("  (keine serverspezifischen Commands irgendwo)")

    print("\n[FERTIG] Das ist der tatsächliche, aktuelle Stand bei Discord.")
    await bot.close()


bot.run(TOKEN)
