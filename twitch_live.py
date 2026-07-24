"""
Twitch Live-Ankündigung Cog
----------------------------
Postet automatisch eine Nachricht in einen Discord-Kanal, sobald du auf
Twitch live gehst. Prüft alle 60 Sekunden den Status über die Twitch API.

EINRICHTUNG:
1. Diese Datei in deinen Cogs-Ordner legen (z. B. cogs/twitch_live.py)
2. In deiner main.py / bot.py laden mit:
       await bot.load_extension("cogs.twitch_live")
3. Umgebungsvariablen bei Railway setzen (Variables-Tab im Service):
       TWITCH_CLIENT_ID     -> deine Client ID von dev.twitch.tv/console/apps
       TWITCH_CLIENT_SECRET -> dein Client Secret
       TWITCH_USERNAME      -> dein Twitch-Benutzername (klein geschrieben, ohne @)
       LIVE_CHANNEL_ID      -> die Discord-Kanal-ID, in der gepostet werden soll
       LIVE_ROLE_ID         -> (optional) Rollen-ID, die gepingt werden soll, z. B. "Live-Ping"
                                Leer lassen, wenn kein Ping gewünscht ist.

Falls du mehrere Server (Guilds) mit demselben Bot bedienst und in beiden
posten willst, trag einfach eine zweite Kanal-ID/Rollen-ID ein und dupliziere
den Post-Block unten (siehe Kommentar "ZWEITER SERVER").
"""

import os
import time
import discord
from discord.ext import commands, tasks
import aiohttp


class TwitchLive(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client_id = os.getenv("TWITCH_CLIENT_ID")
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        self.twitch_username = os.getenv("TWITCH_USERNAME", "").lower()
        self.channel_id = int(os.getenv("LIVE_CHANNEL_ID", "0") or 0)
        self.role_id = os.getenv("LIVE_ROLE_ID", "")

        self.access_token = None
        self.token_expires_at = 0
        self.is_live = False  # verhindert Mehrfach-Posts während desselben Streams

        self.session: aiohttp.ClientSession | None = None
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()
        if self.session:
            self.bot.loop.create_task(self.session.close())

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _ensure_token(self):
        """Holt ein neues App-Access-Token, falls keins vorhanden oder abgelaufen."""
        if self.access_token and time.time() < self.token_expires_at - 60:
            return

        session = await self._get_session()
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with session.post(url, params=params) as resp:
            if resp.status != 200:
                print(f"[TwitchLive] Token-Fehler: {resp.status} {await resp.text()}")
                return
            data = await resp.json()
            self.access_token = data["access_token"]
            self.token_expires_at = time.time() + data.get("expires_in", 3600)

    async def _fetch_stream_status(self) -> dict | None:
        """Gibt die Stream-Daten zurück, falls live, sonst None."""
        await self._ensure_token()
        if not self.access_token:
            return None

        session = await self._get_session()
        url = "https://api.twitch.tv/helix/streams"
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self.access_token}",
        }
        params = {"user_login": self.twitch_username}

        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                print(f"[TwitchLive] API-Fehler: {resp.status} {await resp.text()}")
                return None
            data = await resp.json()
            streams = data.get("data", [])
            return streams[0] if streams else None

    @tasks.loop(seconds=60)
    async def check_loop(self):
        if not (self.client_id and self.client_secret and self.twitch_username and self.channel_id):
            return  # Konfiguration unvollständig, still überspringen

        stream = await self._fetch_stream_status()

        if stream and not self.is_live:
            # Stream hat gerade begonnen
            self.is_live = True
            await self._post_announcement(stream)

        elif not stream and self.is_live:
            # Stream ist beendet, Status zurücksetzen
            self.is_live = False

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    async def _post_announcement(self, stream: dict):
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            print(f"[TwitchLive] Kanal {self.channel_id} nicht gefunden.")
            return

        title = stream.get("title", "Live auf Twitch!")
        game = stream.get("game_name", "")
        thumbnail = stream.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")

        embed = discord.Embed(
            title=f"🔴 {self.twitch_username} ist jetzt LIVE!",
            description=title,
            url=f"https://twitch.tv/{self.twitch_username}",
            color=discord.Color.purple(),
        )
        if game:
            embed.add_field(name="Kategorie", value=game)
        if thumbnail:
            embed.set_image(url=thumbnail)
        embed.set_footer(text="Klick auf den Titel, um direkt zum Stream zu kommen!")

        content = None
        if self.role_id:
            content = f"<@&{self.role_id}>"

        await channel.send(content=content, embed=embed)

        # ZWEITER SERVER (optional):
        # Falls du auch in einem zweiten Discord posten willst, hol dir
        # den zweiten Kanal genauso über self.bot.get_channel(ANDERE_ID)
        # und sende dieselbe embed dort noch einmal.


async def setup(bot: commands.Bot):
    await bot.add_cog(TwitchLive(bot))
