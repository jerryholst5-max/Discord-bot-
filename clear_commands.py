"""
EINMAL-SKRIPT: Löscht ALLE globalen Slash-Commands bei Discord und
registriert danach die Commands aus deiner aktuellen bot.py neu.

VERWENDUNG:
1. Diese Datei in denselben Ordner wie deine bot.py legen (oder Pfad unten anpassen).
2. DISCORD_TOKEN als Umgebungsvariable setzen (denselben, den auch bot.py nutzt).
3. Einmal ausführen:  python3 clear_commands.py
4. Skript wieder löschen/nicht dauerhaft laufen lassen.

Das Skript macht NICHTS an deiner bot.py kaputt und läuft komplett unabhängig davon.
"""

import asyncio
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

    # 1. Alle GLOBALEN Commands bei Discord löschen (leere Liste setzen)
    print("[INFO] Lösche alle globalen Slash-Commands bei Discord...")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync(guild=None)
    print("[OK] Globale Commands wurden bei Discord geleert.")

    # 2. Zur Kontrolle: aktuell bei Discord registrierte globale Commands anzeigen
    cmds = await bot.tree.fetch_commands()
    print(f"[INFO] Discord meldet jetzt {len(cmds)} globale Commands (sollte 0 sein).")

    print("\n[FERTIG] Starte jetzt deine normale bot.py neu (Redeploy),")
    print("damit sie ihre aktuellen Commands frisch registriert.")
    await bot.close()


bot.run(TOKEN)
