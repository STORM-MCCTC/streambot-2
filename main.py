import discord
from discord.ext import commands, tasks
import aiosqlite
import requests
import os

# Load configuration from config.txt
def load_config(file_path):
    config = {}
    with open(file_path, "r") as f:
        for line in f:
            key, value = line.strip().split("=")
            config[key] = value
    return config

# Load config from config.txt
config = load_config("config.txt")
token = config["DISCORD_BOT_TOKEN"]
client_id = config["TWITCH_CLIENT_ID"]
client_secret = config["TWITCH_CLIENT_SECRET"]

# Enable the intents your bot requires
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True

# Define your bot's prefix
client = commands.Bot(command_prefix="!", intents=intents)

# Initialize database
async def init_db():
    async with aiosqlite.connect("client.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS twitch_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_name TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                live_status INTEGER DEFAULT 0,
                UNIQUE(channel_name, guild_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER UNIQUE NOT NULL,
                notification_channel INTEGER,
                ping_role INTEGER
            )
        """)
        await db.commit()

# Event listener for when the bot is ready
@client.event
async def on_ready():
    await init_db()
    check_live_channels.start()
    print(f"Logged in as {client.user}")

# Command to add a Twitch channel notification
@client.command()
@commands.has_permissions(administrator=True)
async def addnotif(ctx, channel: str):
    async with aiosqlite.connect("client.db") as db:
        try:
            await db.execute("INSERT INTO twitch_channels (channel_name, guild_id) VALUES (?, ?)", (channel, ctx.guild.id))
            await db.commit()
            await ctx.send(f"Added Twitch channel: {channel} to the notification list.")
        except aiosqlite.IntegrityError:
            await ctx.send(f"Twitch channel `{channel}` is already being tracked.")

# Command to remove a Twitch channel notification
@client.command()
@commands.has_permissions(administrator=True)
async def removenotif(ctx, channel: str):
    async with aiosqlite.connect("client.db") as db:
        await db.execute("DELETE FROM twitch_channels WHERE channel_name = ? AND guild_id = ?", (channel, ctx.guild.id))
        await db.commit()
        await ctx.send(f"Removed Twitch channel: {channel} from the notification list.")

# Command to set the Discord channel for notifications
@client.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("client.db") as db:
        await db.execute("""
            INSERT INTO settings (guild_id, notification_channel)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET notification_channel = excluded.notification_channel
        """, (ctx.guild.id, channel.id))
        await db.commit()
    await ctx.send(f"Notifications will be sent to {channel.mention}.")

# Command to set the role to ping
@client.command()
@commands.has_permissions(administrator=True)
async def setrole(ctx, role: discord.Role):
    async with aiosqlite.connect("client.db") as db:
        await db.execute("""
            INSERT INTO settings (guild_id, ping_role)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET ping_role = excluded.ping_role
        """, (ctx.guild.id, role.id))
        await db.commit()
    await ctx.send(f"Set {role.mention} to be pinged for notifications.")

# Command to list tracked Twitch channels
@client.command()
@commands.has_permissions(administrator=True)
async def notiflist(ctx):
    async with aiosqlite.connect("client.db") as db:
        async with db.execute("SELECT channel_name FROM twitch_channels WHERE guild_id = ?", (ctx.guild.id,)) as cursor:
            channels = await cursor.fetchall()
    if channels:
        channel_list = "\n".join(f"- {channel[0]}" for channel in channels)
        await ctx.send(f"Tracked Twitch channels:\n{channel_list}")
    else:
        await ctx.send("No Twitch channels are being tracked for this server.")

# Function to get OAuth token
def get_oauth_token(client_id, client_secret):
    url = "https://id.twitch.tv/oauth2/token"
    payload = {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    response = requests.post(url, data=payload)
    response.raise_for_status()
    return response.json()["access_token"]

# Function to get user ID from channel name
def get_user_id(channel_name, client_id, token):
    url = f"https://api.twitch.tv/helix/users?login={channel_name}"
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data["data"][0]["id"] if data["data"] else None

# Function to check if a channel is live
def is_channel_live(user_id, client_id, token):
    url = f"https://api.twitch.tv/helix/streams?user_id={user_id}"
    headers = {"Client-ID": client_id, "Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return bool(response.json()["data"])

# Loop to check live status of channels
@tasks.loop(minutes=1)
async def check_live_channels():
    async with aiosqlite.connect("client.db") as db:
        async with db.execute("""
            SELECT twitch_channels.channel_name, twitch_channels.live_status, settings.guild_id, settings.notification_channel, settings.ping_role
            FROM twitch_channels
            INNER JOIN settings ON twitch_channels.guild_id = settings.guild_id
        """) as cursor:
            channels = await cursor.fetchall()
        for channel_name, live_status, guild_id, notif_channel_id, ping_role_id in channels:
            try:
                token = get_oauth_token(client_id, client_secret)
                user_id = get_user_id(channel_name, client_id, token)
                live = is_channel_live(user_id, client_id, token)
                if live and live_status == 0:
                    guild = client.get_guild(guild_id)
                    notif_channel = guild.get_channel(notif_channel_id) if guild else None
                    ping_role = guild.get_role(ping_role_id) if guild else None
                    if notif_channel:
                        mention = ping_role.mention if ping_role else ""
                        await notif_channel.send(f"{mention} `{channel_name}` is now live! Watch here: https://twitch.tv/{channel_name}")
                    await db.execute("UPDATE twitch_channels SET live_status = 1 WHERE channel_name = ? AND guild_id = ?", (channel_name, guild_id))
                elif not live and live_status == 1:
                    await db.execute("UPDATE twitch_channels SET live_status = 0 WHERE channel_name = ? AND guild_id = ?", (channel_name, guild_id))
                await db.commit()
            except Exception as e:
                print(f"Error with channel {channel_name}: {e}")

# Run the bot
client.run(token)