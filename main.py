import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from dotenv import load_dotenv

# Tiny HTTP server for Render Free Web Service
from aiohttp import web

load_dotenv()

# -----------------------------
# CONFIG
# -----------------------------
GUILD_ID = 1416057930381262880

# Ride request posting channel + roles that are allowed to claim
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749
ROLE_ID_2 = 1416063969965248594

# Ride log channel
LOG_CHANNEL_ID = 1416342987893375007

TOKEN = os.getenv("DISCORD_TOKEN")

# -----------------------------
# BOT SETUP
# -----------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# Utilities & "DB"
# -----------------------------
ALLOWED_ROLES = {ROLE_ID_1, ROLE_ID_2}

def user_has_allowed_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in ALLOWED_ROLES for r in getattr(member, "roles", []))

def safe_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None

def safe_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None

# In-memory Rider DB (resets on restart/redeploy — good enough for now)
# Structure:
# RIDER_DB[user_id] = {
#   "name": str,
#   "rides": [
#       {
#         "ts": int (unix),
#         "driver_id": int,
#         "driver_name": str,
#         "income": float|None,
#         "rating": float|None,
#         "rides_this_week": int|None,
#         "ride_link": str,
#         "comment": str|None
#       }, ...
#   ],
#   "avg_rating": float|None,
#   "total_rides": int
# }
RIDER_DB: Dict[int, Dict[str, Any]] = {}
RIDER_DB_LOCK = asyncio.Lock()

async def update_rider_db(
    rider: discord.abc.User,
    driver: discord.abc.User,
    ride_link: str,
    income_val: Optional[float],
    rating_val: Optional[float],
    rides_this_week_val: Optional[int],
    comment: Optional[str],
):
    async with RIDER_DB_LOCK:
        entry = RIDER_DB.get(rider.id)
        if entry is None:
            entry = {"name": rider.name, "rides": [], "avg_rating": None, "total_rides": 0}
            RIDER_DB[rider.id] = entry
        # keep latest display name seen
        entry["name"] = rider.name

        entry["rides"].append({
            "ts": int(datetime.now(timezone.utc).timestamp()),
            "driver_id": driver.id,
            "driver_name": getattr(driver, "display_name", driver.name),
            "income": income_val,
            "rating": rating_val,
            "rides_this_week": rides_this_week_val,
            "ride_link": ride_link,
            "comment": (comment or "").strip() or None,
        })
        entry["total_rides"] = len(entry["rides"])
        # recompute average rating
        ratings = [r["rating"] for r in entry["rides"] if isinstance(r.get("rating"), (int, float))]
        entry["avg_rating"] = (sum(ratings) / len(ratings)) if ratings else None

def format_currency(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"${val:,.2f}"

def format_rating(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.2f}"

def format_time(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # ISO-like without seconds for neatness
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# -----------------------------
# CLAIM / END BUTTON VIEW
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, channel_id: int, thread_id: int | None = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.channel_id = channel_id
        self.thread_id = thread_id
        self.claimed = False
        self.claimed_by_user_id: Optional[int] = None
        self._lock = asyncio.Lock()  # prevent race on claim

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="rideclaim:button")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Immediate ack to avoid timeouts
        await interaction.response.defer()

        async with self._lock:
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send(
                    "You are not authorized to claim ride requests.", ephemeral=True
                )

            if self.claimed:
                return await interaction.followup.send(
                    "This request has already been claimed.", ephemeral=True
                )

            # Mark claimed
            self.claimed = True
            self.claimed_by_user_id = interaction.user.id
            button.disabled = True  # disable Claim; keep End Ride clickable

            # Update the embed: add a "Driver" field and keep rest
            msg = interaction.message
            embed = None
            if msg.embeds:
                base = msg.embeds[0]
                new_embed = discord.Embed(
                    title=base.title,
                    description=base.description,
                    color=base.color,
                    timestamp=datetime.now(timezone.utc)
                )
                for f in base.fields:
                    if f.name.strip().lower() == "driver":
                        continue
                    new_embed.add_field(name=f.name, value=f.value, inline=f.inline)

                new_embed.add_field(name="Driver", value=interaction.user.mention, inline=False)

                if base.thumbnail and base.thumbnail.url:
                    new_embed.set_thumbnail(url=base.thumbnail.url)
                new_embed.set_footer(text="Ride has been claimed")
                embed = new_embed

            await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

            # Announce to main channel (outside thread)
            channel = interaction.client.get_channel(self.channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(f"<@{self.requester_id}> Your driver is {interaction.user.mention}")

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="rideend:button")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not self.claimed or self.claimed_by_user_id is None:
            return await interaction.followup.send("No driver has claimed this ride yet.", ephemeral=True)

        if interaction.user.id != self.claimed_by_user_id:
            return await interaction.followup.send(
                "Only the driver who claimed this ride can end it.", ephemeral=True
            )

        button.disabled = True  # one-time action

        # Update embed footer to indicate ride ended
        msg = interaction.message
        embed = None
        if msg.embeds:
            base = msg.embeds[0]
            end_embed = discord.Embed(
                title=base.title,
                description=base.description,
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc)
            )
            for f in base.fields:
                end_embed.add_field(name=f.name, value=f.value, inline=f.inline)
            if base.thumbnail and base.thumbnail.url:
                end_embed.set_thumbnail(url=base.thumbnail.url)
            end_embed.set_footer(text="Ride has ended")
            embed = end_embed

        await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

        # Send termination notice in main channel (outside thread)
        channel = interaction.client.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"-# 🚗 | Ride ended by {interaction.user.mention}")

# -----------------------------
# /request ride COMMAND (group)
# -----------------------------
request_group = app_commands.Group(name="request", description="Create service requests")

@app_commands.choices(
    service_level=[
        app_commands.Choice(name="Premium", value="Premium"),
        app_commands.Choice(name="Standard", value="Standard")
    ]
)
@request_group.command(name="ride", description="Request a ride for a pickup and destination.")
@app_commands.describe(
    starting_location="Where the driver should pick you up",
    destination="Where you want to go",
    service_level="Premium or Standard"
)
async def ride(
    interaction: discord.Interaction,
    starting_location: str,
    destination: str,
    service_level: app_commands.Choice[str]
):
    # Instant ack (prevents 'Unknown interaction')
    await interaction.response.send_message("Posting your ride...", ephemeral=True)

    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command isn't available in this server.")

    color = discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue()
    embed = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description="A new ride has been requested and is waiting for a driver to claim it.",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Pickup Location", value=starting_location, inline=True)
    embed.add_field(name="Destination", value=destination, inline=True)
    embed.add_field(name="Requested By", value=interaction.user.mention, inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Click Claim when you are taking this ride")

    view = ClaimView(requester_id=interaction.user.id, channel_id=TARGET_CHANNEL_ID)

    channel = interaction.client.get_channel(TARGET_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(TARGET_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response(content="I couldn't find the ride request channel.")

    content = f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>"
    allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False, replied_user=False)

    message = await channel.send(content=content, embed=embed, view=view, allowed_mentions=allowed_mentions)

    # Create thread for chat
    try:
        thread = await message.create_thread(
            name=f"Ride - {interaction.user.display_name}",
            auto_archive_duration=1440
        )
        view.thread_id = thread.id
        await thread.send(f"{interaction.user.mention} This thread is for discussing your ride.")
    except discord.HTTPException:
        pass

    await interaction.edit_original_response(content="Your ride request has been posted.")

# -----------------------------
# /log-ride COMMAND (enhanced, role-locked)
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride to the log channel (and update rider record).")
@app_commands.describe(
    rider="The rider (user) this ride was for",
    ride_link="Link to the ride (URL or reference)",
    income="Income for this ride (e.g., 25.50)",
    rating="Your rating for this ride (e.g., 4.8)",
    rides_this_week="Number of rides you've completed this week (integer)",
    comment="Optional note about the rider to help other drivers"
)
async def log_ride(
    interaction: discord.Interaction,
    rider: discord.User,
    ride_link: str,
    income: str,
    rating: str,
    rides_this_week: str,
    comment: Optional[str] = None
):
    await interaction.response.send_message("Logging your ride...", ephemeral=True)

    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command isn't available in this server.")

    if not user_has_allowed_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
    rating_val = safe_float(rating)
    rides_val = safe_int(rides_this_week)

    # Update the in-memory rider DB
    await update_rider_db(
        rider=rider,
        driver=interaction.user,
        ride_link=ride_link,
        income_val=income_val,
        rating_val=rating_val,
        rides_this_week_val=rides_val,
        comment=comment
    )

    # Build a clear log embed for the log channel
    embed = discord.Embed(
        title="Ride Log",
        description="A ride has been logged.",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Rider", value=f"{rider.mention} (`{rider.id}`)", inline=False)
    embed.add_field(name="Ride Link", value=ride_link, inline=False)
    embed.add_field(name="Income", value=(f"${income_val:,.2f}" if income_val is not None else income), inline=True)
    embed.add_field(name="Rating", value=(f"{rating_val:.2f}" if rating_val is not None else rating), inline=True)
    embed.add_field(name="Rides This Week", value=(str(rides_val) if rides_val is not None else rides_this_week), inline=True)
    embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
    if comment:
        embed.add_field(name="Comment", value=comment[:1024], inline=False)
    embed.set_thumbnail(url=rider.display_avatar.url)
    embed.set_footer(text="Ride log entry")

    # Send to the dedicated log channel
    log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await interaction.client.fetch_channel(LOG_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response(
                content="I couldn't find the log channel. Please check my configuration."
            )

    await log_channel.send(embed=embed)
    await interaction.edit_original_response(content="Your ride has been logged and the rider record updated.")

# -----------------------------
# /search COMMAND (role-locked, ephemeral)
# -----------------------------
@tree.command(name="search", description="Look up a rider's history, rating, and comments.")
@app_commands.describe(
    user="The rider to look up"
)
async def search_rider(interaction: discord.Interaction, user: discord.User):
    # Private, fast response
    await interaction.response.defer(ephemeral=True)

    if interaction.guild_id != GUILD_ID:
        return await interaction.followup.send("This command isn't available in this server.", ephemeral=True)
    if not user_has_allowed_role(interaction.user):
        return await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)

    async with RIDER_DB_LOCK:
        entry = RIDER_DB.get(user.id)

        if not entry:
            embed = discord.Embed(
                title="Rider Lookup",
                description="No records found for this rider.",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Rider", value=f"{user.mention} (`{user.id}`)", inline=False)
            embed.set_thumbnail(url=user.display_avatar.url)
            return await interaction.followup.send(embed=embed, ephemeral=True)

        total = entry.get("total_rides", 0)
        avg = entry.get("avg_rating", None)
        rides: List[Dict[str, Any]] = entry.get("rides", [])

        # Last 5 rides summary
        recent = sorted(rides, key=lambda r: r["ts"], reverse=True)[:5]
        recent_lines = []
        for r in recent:
            line = f"- {format_time(r['ts'])} | Driver: <@{r['driver_id']}> | Income: {format_currency(r.get('income'))} | Rating: {format_rating(r.get('rating'))}"
            recent_lines.append(line)
        recent_block = "\n".join(recent_lines) if recent_lines else "—"

        # Latest comments (up to 5)
        comments = [r for r in reversed(rides) if r.get("comment")]
        comments = comments[:5]
        if comments:
            comment_lines = []
            for r in comments:
                who = r.get("driver_name", "Driver")
                when = format_time(r["ts"])
                text = r["comment"]
                comment_lines.append(f"- {when} by **{who}**: {text}")
            comment_block = "\n".join(comment_lines)
        else:
            comment_block = "—"

        embed = discord.Embed(
            title="Rider Lookup",
            description="Internal rider profile for driver safety.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Rider", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Total Rides Logged", value=str(total), inline=True)
        embed.add_field(name="Average Rating", value=(f"{avg:.2f}" if avg is not None else "—"), inline=True)
        embed.add_field(name="Recent Rides (last 5)", value=recent_block[:1024] or "—", inline=False)
        embed.add_field(name="Recent Comments", value=comment_block[:1024] or "—", inline=False)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="Data is session-based and resets if the bot restarts.")

    await interaction.followup.send(embed=embed, ephemeral=True)

# -----------------------------
# ON READY (guild-scoped sync)
# -----------------------------
@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    # group command to guild
    tree.add_command(request_group, guild=guild)
    # make top-level commands appear instantly in the guild
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced to allowed guild.")

# -----------------------------
# Minimal HTTP server for Render
# -----------------------------
async def handle_health(request):
    user = bot.user
    name = f"{user} (ID: {user.id})" if user else "starting"
    return web.Response(text=f"OK - {name}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"HTTP server listening on 0.0.0.0:{port}")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    if not TOKEN:
        raise RuntimeError("Please set your Discord bot token in the DISCORD_TOKEN environment variable.")

    # Run web server and bot concurrently
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
