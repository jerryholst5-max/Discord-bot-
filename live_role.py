"""
Live-Ping Self-Role Cog
------------------------
Postet ein Panel mit einem Button. Klickt jemand drauf, bekommt er/sie die
"Live-Ping"-Rolle (bzw. verliert sie wieder bei erneutem Klick). Keine
manuelle Rollenvergabe mehr nötig.

EINRICHTUNG:
1. Diese Datei als cogs/live_role.py speichern
2. In main.py / bot.py laden:
       await bot.load_extension("cogs.live_role")
3. Die Umgebungsvariable LIVE_ROLE_ID musst du sowieso schon für das
   twitch_live.py Cog gesetzt haben – wird hier wiederverwendet.
4. Einmalig in einem Kanal deiner Wahl den Slash-Befehl ausführen:
       /liverole-panel
   Das postet das Panel mit Button. Das war's – läuft danach von allein,
   auch nach einem Bot-Neustart (View ist persistent).
"""

import os
import discord
from discord.ext import commands
from discord import app_commands


class LiveRoleButton(discord.ui.View):
    """Persistenter Button, funktioniert auch nach Bot-Neustart."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🔴 Live-Ping an/aus",
        style=discord.ButtonStyle.primary,
        custom_id="live_role_toggle_button",  # feste ID nötig für Persistenz
    )
    async def toggle_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = int(os.getenv("LIVE_ROLE_ID", "0") or 0)
        if not role_id:
            await interaction.response.send_message(
                "Die Live-Ping-Rolle ist noch nicht konfiguriert. Sag dem Serverbesitzer Bescheid.",
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message(
                "Die konfigurierte Rolle wurde auf diesem Server nicht gefunden.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role, reason="Live-Ping per Button entfernt")
            await interaction.response.send_message(
                "Live-Ping wurde entfernt. Du wirst nicht mehr benachrichtigt.",
                ephemeral=True,
            )
        else:
            await member.add_roles(role, reason="Live-Ping per Button hinzugefügt")
            await interaction.response.send_message(
                "Live-Ping aktiviert! Du wirst gepingt, sobald der Stream live geht.",
                ephemeral=True,
            )


class LiveRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # View einmal registrieren, damit der Button auch nach Neustart funktioniert
        self.bot.add_view(LiveRoleButton())

    @app_commands.command(
        name="liverole-panel",
        description="Postet das Panel, mit dem sich Member die Live-Ping-Rolle selbst geben können.",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def liverole_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🔴 Live-Benachrichtigung",
            description=(
                "Klick auf den Button, um benachrichtigt zu werden, sobald der Stream live geht.\n"
                "Nochmal klicken entfernt die Benachrichtigung wieder."
            ),
            color=discord.Color.purple(),
        )
        await interaction.channel.send(embed=embed, view=LiveRoleButton())
        await interaction.response.send_message("Panel wurde gepostet.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveRole(bot))
