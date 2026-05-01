# import requests
from pyexpat.errors import messages

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncio
import datetime
from keep_alive import keep_alive
from zoneinfo import ZoneInfo
import logging

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ENVIRONMENT VARIABLES ---
load_dotenv()
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0)) 

if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set.")

# --- CHANNEL CONFIGURATION ---
CHANNEL_ID_6MANS = int(os.getenv("CHANNEL_ID_6MANS", 0))
CHANNEL_ID_4MANS = int(os.getenv("CHANNEL_ID_4MANS", 0))
CHANNEL_ID_2MANS = int(os.getenv("CHANNEL_ID_2MANS", 0))

# --- GLOBAL STATE ---
# Tracks active match alerts to delete them later
# Format: { match_channel_id: (alert_message_id, alert_channel_id) }
active_alerts = {}

# Tracks announced tournaments to prevent duplicates
announced_tournaments = set()

# --- BOT SETUP ---
intents = discord.Intents.default()
intents.message_content = True 
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Start the web server for 24/7 hosting
keep_alive()

# ==============================================================================
# 🎯 EVENT LISTENERS (to fix the discord rate limits, hopefully...)
# ==============================================================================

@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user.name}")
    # Start the tournament checker loop
    if not periodic_checks.is_running():
        periodic_checks.start()

@bot.event
async def on_guild_channel_create(channel):
    """
    Fires when a new channel is created
    If it's a match channel, we check it for players and send an alert
    """
    # 1. FILTER: Check if the new channel look like a match channel
    # UPDATE THIS LIST based on what your 6mans bot names the channels, for neatqueue its only "queue-"
    target_prefixes = ["queue-"]
    
    # If the channel name doesn't start with any of the prefixes, ignore it
    if not any(channel.name.startswith(p) for p in target_prefixes):
        return

    logger.info(f"🆕 Potential match channel detected: {channel.name} (ID: {channel.id})")

    # 2. Wait 5s for the other bot to post the teams (neatqueue is sometimes slow)
    await asyncio.sleep(5) 

    try:
        # 3. FETCH: Read the first few messages to find the bot's team post
        messages = [msg async for msg in channel.history(limit=5, oldest_first=True)]
        logger.info(f"📨 Found {len(messages)} message(s) in {channel.name}")
        for i, msg in enumerate(messages):
            logger.info(f"  msg[{i}] author={msg.author} | mentions={[m.display_name for m in msg.mentions]} | content={msg.content[:80]!r}")
        
        if len(messages) < 2:  # covers both the empty case and the 1-message case
            logger.warning(f"⚠️ {channel.name} has fewer than 2 messages after 5 seconds.")
            return

        # Usually the second message contains the mentions (kinda broken if its not ig)
        target_msg = messages[1] 
        mentions = target_msg.mentions

        # 4. determine queue type based on player count
        queue_type = None
        dest_channel_id = None
        
        if len(mentions) == 6:
            queue_type = "6mans"
            dest_channel_id = CHANNEL_ID_6MANS
        elif len(mentions) == 4:
            queue_type = "4mans"
            dest_channel_id = CHANNEL_ID_4MANS
        elif len(mentions) == 2:
            queue_type = "2mans"
            dest_channel_id = CHANNEL_ID_2MANS
        
        # 5. send alert
        if dest_channel_id:
            await send_alert(queue_type, channel, mentions, dest_channel_id)

    except discord.Forbidden:
        logger.warning(f"❌ Missing permissions to read {channel.name}")
    except Exception as e:
        logger.error(f"❌ Error processing new channel {channel.name}: {e}")

@bot.event
async def on_guild_channel_delete(channel):
    """
    Fires when a channel is deleted (Match Finished).
    We check if we have an active alert for this channel and delete it.
    """
    if channel.id in active_alerts:
        msg_id, dest_channel_id = active_alerts[channel.id]
        
        try:
            dest_channel = bot.get_channel(dest_channel_id)
            if dest_channel:
                msg_to_delete = await dest_channel.fetch_message(msg_id)
                await msg_to_delete.delete()
                logger.info(f"🗑️ Deleted alert for ended match {channel.name}")
        except discord.NotFound:
            pass # Message was already deleted manually
        except Exception as e:
            logger.error(f"❌ Error deleting alert: {e}")
        
        # Remove from our tracker
        del active_alerts[channel.id]

# --- HELPER FUNCTIONS ---

async def send_alert(queue_type, origin_channel, mentions, dest_id):
    dest_channel = bot.get_channel(dest_id)
    if not dest_channel:
        logger.error(f"❌ Destination channel {dest_id} not found.")
        return

    player_names = "\n".join([f"• {m.display_name}" for m in mentions])
    
    embed = discord.Embed(
        title=f"🚨 Active {queue_type} Started!",
        description=f"**Lobby:** {origin_channel.name}", # Clickable link to channel
        color=discord.Color.green(),
    )
    embed.add_field(name="Players", value=player_names, inline=False)
    
    try:
        sent_msg = await dest_channel.send(embed=embed)
        # Save the IDs so we can delete this message later
        active_alerts[origin_channel.id] = (sent_msg.id, dest_id)
        logger.info(f"📢 Alert sent for {queue_type} in {origin_channel.name}")
    except Exception as e:
        logger.error(f"❌ Failed to send alert: {e}")

# ==============================================================================
# 🏆 TOURNAMENT DETECTION/ALERT LOGIC
# ==============================================================================

async def fetch_tournaments(region):
    logger.info(f"Fetching tournaments for region, inside time range for: {region}")
    url = f"https://rocket-league1.p.rapidapi.com/tournaments/{region}"
    
    
    # DO NOT EDIT, these headers are required for the API to work
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY, # Your RapidAPI key, set it in the .env file
        "x-rapidapi-host": "rocket-league1.p.rapidapi.com",
        "User-Agent": "RapidAPI Playground",
        "Accept-Encoding": "identity"
    }
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            return await response.json()

def find_dropshot_tournament(data):
    for tournament in data.get('tournaments', []):
        if "dropshot" in tournament.get('mode', '').lower():
            return tournament
    return None

@tasks.loop(minutes=6)
async def periodic_checks():
    await us_east_dropshot_check()
    await europe_dropshot_check()

async def us_east_dropshot_check():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Check window: 18:55–19:05 UTC or 19:25–19:35 UTC (1 hour before both cases)
    if (now.hour == 18 and now.minute >= 55) or (now.hour == 19 and now.minute <= 5):
        await check_dropshot_for_region("us-east", display_name="US-EAST")
        logger.info("US-EAST check completed")
    else:
        # print("Not in US-EAST check window")
        return

async def europe_dropshot_check():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Check window: 11:55–12:05 UTC or 11:25–11:35 UTC (1 hour before both cases)
    if (now.hour == 11 and now.minute >= 55) or (now.hour == 12 and now.minute <= 5):
        await check_dropshot_for_region("europe", display_name="EUROPE")
        logger.info("EUROPE check completed")
    else:
        # print("Not in EUROPE check window")
        return



async def check_dropshot_for_region(region: str, display_name: str):
    try:
        data = await fetch_tournaments(region)
        tournament = find_dropshot_tournament(data)

        if tournament:
            start_str = tournament.get('starts')
            if not start_str:
                return

            start_dt = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            key = f"{tournament.get('mode')}|{region}|{start_dt.isoformat()}"

            if key not in announced_tournaments:
                announced_tournaments.add(key)
                unix_timestamp = int(start_dt.timestamp())
                formatted_time = f"<t:{unix_timestamp}:R>" # Discord's relative time format

                for guild in bot.guilds:
                    for channel in guild.text_channels:
                        if channel.name == 'tournament-alerts':
                            try:
                                await channel.send(
                                    f"# :alarm_clock: **IN-GAME TOURNAMENT ALERT!**\n"
                                    f"**Dropshot** Tournament in **{display_name}** starts {formatted_time}\n"
                                    f"<@&1377509538311176213>"
                                )
                                logger.info(f"[{region.upper()}] ✅ Alert sent to {guild.name} in #{channel.name} for tournament at {start_dt.isoformat()}")
                            except Exception as e:
                                logger.error(f"❌ ERROR sending to {guild.name} in #{channel.name}: {e}")
            else:
                logger.info(f"[{region.upper()}] Tournament at {start_dt.isoformat()} already announced.")
        else:
            logger.info(f"[{region.upper()}] No Dropshot tournament found in API response.")
    except Exception as e:
        logger.error(f"❌ Error during check_dropshot_for_region({region}): {e}")

# ==============================================================================
# 🛠️ UTILITY COMMANDS
# ==============================================================================

@bot.command()
async def cleanup(ctx):
    """
    Manually deletes all active queue alerts the bot is currently tracking.
    Only works for the ID specified by ADMIN_USER_ID in .env file
    """
    if ctx.author.id != ADMIN_USER_ID:
        await ctx.send("⛔ You are not authorized to use this command.", delete_after=5)
        return

    if not active_alerts:
        await ctx.send("🧹 No active alerts to clean up.", delete_after=5)
        return

    count = 0
    # Copy items to list because we modify the dictionary during iteration
    for origin_id, (msg_id, dest_channel_id) in list(active_alerts.items()):
        try:
            channel = bot.get_channel(dest_channel_id)
            if channel:
                msg = await channel.fetch_message(msg_id)
                await msg.delete()
                count += 1
        except:
            pass # Already deleted
    
    active_alerts.clear()
    await ctx.send(f"✅ Manually cleared {count} active alerts.", delete_after=5)
    
    # Try to delete the command message itself to keep chat clean
    try:
        await ctx.message.delete()
    except:
        pass


@bot.command()
async def ping(ctx):
    """Replies with Pong! and the bot's latency in ms."""
    latency = round(bot.latency * 1000)
    await ctx.send(f'Pong! ({latency} ms)')

# --- RUN ---
bot.run(DISCORD_BOT_TOKEN)
