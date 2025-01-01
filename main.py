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
intents.messages = True  # Required for reading message content
intents.guilds = True    # Required for guild-related events
intents.message_content = True  # Explicitly enable message content access

# Define your bot's prefix
client = commands.Bot(command_prefix="!", intents=intents)

# Initialize database
async def init_db():
    async with aiosqlite.connect("client.db") as db:
        # Create twitch_channels table with a live_status column if it doesn't exist
        await db.execute("""
            CREATE TABLE IF NOT EXISTS twitch_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_name TEXT UNIQUE NOT NULL,
                live_status INTEGER DEFAULT 0  -- 0 for offline, 1 for live
            )
        """)

        # Create settings table if it doesn't exist
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
    print(f"We have logged in as {client.user}")

# Simple command to echo the user's message
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def echo(ctx, *, message):
    await ctx.send(message)

# Command to add a Twitch channel notification
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def addnotif(ctx, channel: str):
    async with aiosqlite.connect("client.db") as db:
        try:
            await db.execute("INSERT INTO twitch_channels (channel_name) VALUES (?)", (channel,))
            await db.commit()
            await ctx.send(f"Added Twitch channel: {channel} to the notification list.")
        except aiosqlite.IntegrityError:
            await ctx.send(f"The Twitch channel `{channel}` is already in the notification list.")

# Command to remove a Twitch channel notification
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def removenotif(ctx, channel: str):
    async with aiosqlite.connect("client.db") as db:
        cursor = await db.execute("DELETE FROM twitch_channels WHERE channel_name = ?", (channel,))
        if cursor.rowcount > 0:
            await ctx.send(f"Removed Twitch channel: {channel} from the notification list.")
        else:
            await ctx.send(f"The Twitch channel `{channel}` was not found in the notification list.")

# Command to set the Discord channel for notifications
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def setchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("client.db") as db:
        await db.execute("""
            INSERT INTO settings (guild_id, notification_channel)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET notification_channel = excluded.notification_channel
        """, (ctx.guild.id, channel.id))
        await db.commit()
    await ctx.send(f"Notifications will be sent to: {channel.mention}")

# Command to set the role to ping
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def setrole(ctx, role: discord.Role):
    async with aiosqlite.connect("client.db") as db:
        await db.execute("""
            INSERT INTO settings (guild_id, ping_role)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET ping_role = excluded.ping_role
        """, (ctx.guild.id, role.id))
        await db.commit()
    await ctx.send(f"Role {role.mention} will be pinged for notifications.")

# Command to list all tracked Twitch channels
@client.command()
@commands.has_permissions(administrator=True)  # Admin only
async def notiflist(ctx):
    async with aiosqlite.connect("client.db") as db:
        async with db.execute("SELECT channel_name FROM twitch_channels") as cursor:
            channels = await cursor.fetchall()

    if channels:
        # Format the list of channels
        channel_list = "\n".join(f"- {channel[0]}" for channel in channels)
        await ctx.send(f"Here are the tracked Twitch channels:\n{channel_list}")
    else:
        await ctx.send("No Twitch channels are currently being tracked.")

# Function to get OAuth token from Twitch
def get_oauth_token(client_id, client_secret):
    url = "https://id.twitch.tv/oauth2/token"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }
    response = requests.post(url, data=payload)
    response.raise_for_status()
    return response.json()["access_token"]

# Function to get user ID from channel name
def get_user_id(channel_name, client_id, token):
    url = f"https://api.twitch.tv/helix/users?login={channel_name}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    if data["data"]:
        return data["data"][0]["id"]
    else:
        raise ValueError(f"Channel '{channel_name}' not found.")

# Function to check if the channel is live
def is_channel_live(user_id, client_id, token):
    url = f"https://api.twitch.tv/helix/streams?user_id={user_id}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}"
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    return data["data"]  # Returns an empty list if the channel is not live

# Loop that checks every minute if a tracked Twitch channel goes live
@tasks.loop(minutes=1)  # Check every minute
async def check_live_channels():
    async with aiosqlite.connect("client.db") as db:
        # Get all Twitch channels and their current live status
        async with db.execute("SELECT channel_name, live_status FROM twitch_channels") as cursor:
            channels = await cursor.fetchall()

        for channel_name, live_status in channels:
            try:
                # Get OAuth token for Twitch API
                token = get_oauth_token(client_id, client_secret)

                # Get user ID from channel name
                user_id = get_user_id(channel_name, client_id, token)

                # Check if the channel is live
                live_data = is_channel_live(user_id, client_id, token)

                if live_data and live_status == 0:  # If the channel is live and it was previously offline
                    # Send notification
                    async with db.execute("SELECT guild_id, notification_channel, ping_role FROM settings") as settings_cursor:
                        settings = await settings_cursor.fetchall()

                    for guild_id, notif_channel_id, ping_role_id in settings:
                        guild = client.get_guild(guild_id)
                        if guild:
                            notif_channel = guild.get_channel(notif_channel_id)
                            ping_role = guild.get_role(ping_role_id)

                            if notif_channel:
                                mention = ping_role.mention if ping_role else ""
                                await notif_channel.send(f"{mention} The Twitch channel `{channel_name}` is now live! Watch here: https://twitch.tv/{channel_name}")

                    # Update the channel's live status to reflect that it's now live
                    await db.execute("UPDATE twitch_channels SET live_status = 1 WHERE channel_name = ?", (channel_name,))
                    await db.commit()

                elif not live_data and live_status == 1:  # If the channel is not live and it was previously live
                    # Update the status to offline
                    await db.execute("UPDATE twitch_channels SET live_status = 0 WHERE channel_name = ?", (channel_name,))
                    await db.commit()
            except Exception as e:
                print(f"Error checking channel {channel_name}: {e}")

# Run the bot with your bot token
client.run(token)
