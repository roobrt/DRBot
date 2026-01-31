import requests
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import os
import asyncio
import datetime
from keep_alive import keep_alive
from zoneinfo import ZoneInfo
import logging

# Logger setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# --- CONFIGURATION: SET YOUR CHANNEL IDS HERE ---
CHANNEL_ID_6MANS = int(os.getenv("CHANNEL_ID_6MANS", 0))
CHANNEL_ID_4MANS = int(os.getenv("CHANNEL_ID_4MANS", 0))
CHANNEL_ID_2MANS = int(os.getenv("CHANNEL_ID_2MANS", 0))

keep_alive()

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True 
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Dictionary to track active queue messages
# Format: { queue_channel_id: (alert_message_id, alert_channel_id) }
active_queue_messages = {}

def fetch_tournaments(region):
    logger.info(f"Fetching tournaments for region, inside time range for: {region}")
    url = f"https://rocket-league1.p.rapidapi.com/tournaments/{region}"
    
    
    # DO NOT EDIT, these headers are required for the API to work
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY, # Your RapidAPI key, set it in the .env file
        "x-rapidapi-host": "rocket-league1.p.rapidapi.com",
        "User-Agent": "RapidAPI Playground",
        "Accept-Encoding": "identity"
    }
    
    response = requests.get(url, headers=headers)
    return response.json()

def find_dropshot_tournament(data):
    for tournament in data.get('tournaments', []):
        if "dropshot" in tournament.get('mode', '').lower():
            return tournament
    return None

@tasks.loop(minutes=1)
async def check_active_queues():
    """
    Scans for 'queue-' channels in ALL GUILDS, determines the queue type,
    sends an embed to the specific channel, and cleans up when finished.
    """
    await bot.wait_until_ready()

    # 1. SCAN FOR CURRENT QUEUES IN ALL GUILDS
    current_queue_channels = []
    
    for guild in bot.guilds:
        # Explicitly fetch channels (safer than cache sometimes)
        for channel in guild.text_channels:
            if channel.name.startswith('queue-'):
                current_queue_channels.append(channel)

    # 2. HANDLE NEW QUEUES
    for channel in current_queue_channels:
        # Check if we are already tracking this queue
        if channel.id in active_queue_messages:
            continue

        try:
            # Fetch the first 2 messages. 
            # oldest_first=True means index 0 is oldest, index 1 is 2nd oldest.
            messages = [msg async for msg in channel.history(limit=2, oldest_first=True)]

            if len(messages) >= 2:
                target_message = messages[1]
                mentions = target_message.mentions
                player_count = len(mentions)

                destination_channel_id = None
                queue_type_name = "Queue"

                if player_count == 6:
                    queue_type_name = "6mans"
                    destination_channel_id = CHANNEL_ID_6MANS
                elif player_count == 4:
                    queue_type_name = "4mans"
                    destination_channel_id = CHANNEL_ID_4MANS
                elif player_count == 2:
                    queue_type_name = "2mans"
                    destination_channel_id = CHANNEL_ID_2MANS

                # If matches a valid queue size
                if destination_channel_id:
                    # Robust Channel Fetching (Cache -> API Fallback)
                    dest_channel = bot.get_channel(destination_channel_id)
                    if dest_channel is None:
                        try:
                            dest_channel = await bot.fetch_channel(destination_channel_id)
                        except discord.Forbidden:
                            logger.error(f"❌ Bot cannot access destination channel {destination_channel_id}")
                            continue
                        except discord.NotFound:
                            logger.error(f"❌ Destination channel {destination_channel_id} not found!")
                            continue
                        except Exception:
                            continue # Skip silently if other errors

                    if dest_channel:
                        # Build the Embed
                        player_names = "\n".join([f"• {m.display_name}" for m in mentions])
                        
                        embed = discord.Embed(
                            title=f"Active {queue_type_name} Ongoing!",
                            description=f"**Lobby:** {channel.name}", 
                            color=discord.Color.green(),
                        )
                        embed.add_field(name="Players", value=player_names, inline=False)
                        
                        # Send and Track
                        sent_msg = await dest_channel.send(embed=embed)
                        
                        active_queue_messages[channel.id] = (sent_msg.id, destination_channel_id)
                        logger.info(f"Started tracking {queue_type_name} in {channel.name}")

        except discord.HTTPException as e:
            if e.status == 429:
                logger.critical(f"🛑 RATE LIMIT HIT! Discord says wait {e.retry_after}s. Headers: {e.response.headers}")
                # Optional: Stop the loop to save your bot
                break
        except discord.Forbidden:
            logger.warning(f"⚠️ Skipped {channel.name}: Missing Permissions to read messages.")
            continue 
        except Exception as e:
            logger.error(f"❌ Error processing {channel.name}: {e}")

    # 3. CLEANUP ENDED QUEUES
    current_channel_ids = [c.id for c in current_queue_channels]
    ended_queues = [cid for cid in active_queue_messages if cid not in current_channel_ids]

    for q_id in ended_queues:
        msg_id, dest_channel_id = active_queue_messages[q_id]
        
        try:
            dest_channel = bot.get_channel(dest_channel_id)
            if dest_channel is None:
                 dest_channel = await bot.fetch_channel(dest_channel_id)
            
            if dest_channel:
                msg_to_delete = await dest_channel.fetch_message(msg_id)
                await msg_to_delete.delete()
                logger.info(f"Deleted alert for finished queue (Origin ID: {q_id})")
        except discord.NotFound:
            pass # Message already deleted
        except Exception as e:
            logger.error(f"Error deleting alert for queue {q_id}: {e}")
        
        del active_queue_messages[q_id]

@tasks.loop(minutes=6)
async def periodic_checks():
    await us_east_dropshot_check()
    await europe_dropshot_check()

already_started = False

@bot.event
async def on_ready():
    global already_started
    if not already_started:
        logger.info(f"✅ Logged in as {bot.user.name}")
        periodic_checks.start()
        check_active_queues.start()
        already_started = True

@bot.command()
async def ping(ctx):
    """Replies with Pong! and the bot's latency in ms."""
    latency = round(bot.latency * 1000)  # latency is in seconds, convert to ms
    await ctx.send(f'Pong! ({latency} ms)')
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

announced_tournaments = set()

async def check_dropshot_for_region(region: str, display_name: str):
    try:
        data = fetch_tournaments(region)
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

if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set in the environment variables.")
bot.run(DISCORD_BOT_TOKEN)