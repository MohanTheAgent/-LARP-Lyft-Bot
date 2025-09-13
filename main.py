import os
import json
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

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

# -----------------------------
# BOT SETUP
# -----------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# PERSISTENT JSON "DB"
# -----------------------------
_db_lock = asyncio.Lock()
_db: Dict[str, Any] = {"riders": {}, "drivers": {}}

def _today_iso() -> str:
    # date only (UTC) — requested: no time
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

async def load_db():
    global _db
    async with _db_lock:
        if not os.path.exists(DATA_FILE):
            _db = {"riders": {}, "drivers": {}}
            await save_db()  # create file
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _db = json.load(f)
            # sanity defaults
            _db.setdefault("riders", {})
            _db.setdefault("drivers", {})
        except Exception:
            _db = {"riders": {}, "drivers": {}}

async def save_db():
    async with _db_lock:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_db, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)

def user_has_allowed_role(member: discord.abc.User) -> bool:
    allowed = {ROLE_ID_1, ROLE_ID_2}
    return any(getattr(r, "id", None) in allowed for r in getattr(member, "roles", []))

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

def avg(values: List[float]) -> Optional[float]:
    values = [v for v in values if isinstance(v, (int, float))]
    return (sum(values) / len(values)) if values else None

def fmt_money(v: Optional[float]) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"

def fmt_rating(v: Optional[float]) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"

# -----------------------------
# RATING UI (DM after End Ride)
# -----------------------------
class RatingView(discord.ui.View):
    def __init__(self, driver_id: int, from_user_id: int):
        super().__init__(timeout=300)  # 5 minutes timeout
        self.driver_id = driver_id
        self.from_user_id = from_user_id

    async def _record(self, interaction: discord.Interaction, value: int):
        await interaction.response.defer(ephemeral=True)
        # Save to drivers[driver_id].ratings[]
        async with _db_lock:
            drivers = _db.setdefault("drivers", {})
            drec = drivers.setdefault(str(self.driver_id), {"name": "", "ratings": []})
            # Keep latest driver name (if we can)
            try:
                member = interaction.client.get_user(self.driver_id) or await interaction.client.fetch_user(self.driver_id)
                if member:
                    drec["name"] = getattr(member, "name", drec.get("name", ""))
            except Exception:
                pass
            drec["ratings"].append({
                "from": self.from_user_id,
                "rating": int(value),
                "date": _today_iso()
            })
        await save_db()
        await interaction.followup.send("Thanks! Your rating has been recorded.", ephemeral=True)

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary)
    async def rate1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary)
    async def rate2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary)
    async def rate3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary)
    async def rate4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary)
    async def rate5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 5)

# -----------------------------
# CLAIM / END BUTTON VIEW
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, channel_id: int, thread_id: int | None = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id          # rider (client)
        self.channel_id = channel_id              # main channel for announcements
        self.thread_id = thread_id
        self.claimed = False
        self.claimed_by_user_id: Optional[int] = None
        self._lock = asyncio.Lock()               # prevent race on claim

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="rideclaim:button")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Immediate ack to avoid timeouts
        await interaction.response.defer()

        async with self._lock:
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send("You are not authorized to claim ride requests.", ephemeral=True)
            if self.claimed:
                return await interaction.followup.send("This request has already been claimed.", ephemeral=True)

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

            # Announce to the main channel (outside the thread)
            channel = interaction.client.get_channel(self.channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(f"<@{self.requester_id}> Your driver is {interaction.user.mention}")

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="rideend:button")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not self.claimed or self.claimed_by_user_id is None:
            return await interaction.followup.send("No driver has claimed this ride yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by_user_id:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

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

        # DM the rider (requester) for rating (1-5)
        try:
            rider_user = interaction.client.get_user(self.requester_id) or await interaction.client.fetch_user(self.requester_id)
            if rider_user is not None:
                view = RatingView(driver_id=self.claimed_by_user_id, from_user_id=self.requester_id)
                dm = await rider_user.create_dm()
                await dm.send(
                    "Please rate your driver (1–5). Your feedback helps keep rides safe and high quality.",
                    view=view
                )
        except discord.Forbidden:
            # Rider has DMs disabled — ignore silently
            pass
        except Exception:
            pass

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
# /log-ride COMMAND (role-locked + persists to JSON)
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
    date_str = _today_iso()

    # Update persistent JSON DB for the rider
    async with _db_lock:
        riders = _db.setdefault("riders", {})
        rrec = riders.setdefault(str(rider.id), {"name": rider.name, "rides": []})
        rrec["name"] = rider.name  # keep recent
        rrec["rides"].append({
            "date": date_str,
            "driver_id": interaction.user.id,
            "driver_name": getattr(interaction.user, "display_name", interaction.user.name),
            "income": income_val if income_val is not None else income,
            "rating": rating_val if rating_val is not None else rating,
            "comment": (comment or "").strip() or None,
            "ride_link": ride_link
        })
    await save_db()

    # Build a clear log embed for the log channel
    embed = discord.Embed(
        title="Ride Log",
        description="A ride has been logged.",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Rider", value=f"{rider.mention} (`{rider.id}`)", inline=False)
    embed.add_field(name="Ride Link", value=ride_link, inline=False)
    embed.add_field(name="Income", value=fmt_money(income_val), inline=True)
    embed.add_field(name="Rating", value=fmt_rating(rating_val), inline=True)
    embed.add_field(name="Rides This Week", value=str(rides_val) if rides_val is not None else rides_this_week, inline=True)
    if comment:
        embed.add_field(name="Comment", value=comment[:1024], inline=False)
    embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
    embed.set_thumbnail(url=rider.display_avatar.url)
    embed.set_footer(text="Ride log entry")

    # Send to the dedicated log channel
    log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await interaction.client.fetch_channel(LOG_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response("I couldn't find the log channel. Please check my configuration.")

    await log_channel.send(embed=embed)
    await interaction.edit_original_response(content="Your ride has been logged and the rider record updated.")

# -----------------------------
# /search COMMAND (role-locked, simple, date-only)
# -----------------------------
@tree.command(name="search", description="Look up a member as rider & driver: ratings, rides, comments.")
@app_commands.describe(user="The member to look up")
async def search(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild_id != GUILD_ID:
        return await interaction.followup.send("This command isn't available in this server.", ephemeral=True)
    if not user_has_allowed_role(interaction.user):
        return await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)

    async with _db_lock:
        riders = _db.get("riders", {})
        drivers = _db.get("drivers", {})
        rrec = riders.get(str(user.id)) or {"name": user.name, "rides": []}
        drec = drivers.get(str(user.id)) or {"name": user.name, "ratings": []}

        # Rider summary
        rides = rrec.get("rides", [])
        rider_count = len(rides)
        rider_avg = avg([
            (r.get("rating") if isinstance(r.get("rating"), (int, float)) else None)
            for r in rides
        ])

        # Recent comments (date only)
        comments_lines = []
        for r in reversed(rides):
            if r.get("comment"):
                comments_lines.append(f"- {r['date']}: {r['comment']}")
            if len(comments_lines) >= 5:
                break
        comments_block = "\n".join(comments_lines) if comments_lines else "—"

        # Driver rating summary (from client DMs)
        ratings = [rr.get("rating") for rr in drec.get("ratings", []) if isinstance(rr.get("rating"), int)]
        driver_avg = avg(ratings)
        driver_count = len(ratings)

    # Build a clean, minimal embed
    embed = discord.Embed(
        title=f"Member Profile",
        description="Concise safety summary.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Member", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="Driver Rating (avg / count)", value=f"{fmt_rating(driver_avg)} / {driver_count}", inline=True)
    embed.add_field(name="Rider Rides (avg / count)", value=f"{fmt_rating(rider_avg)} / {rider_count}", inline=True)
    embed.add_field(name="Recent Rider Comments", value=comments_block[:1024] or "—", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Dates shown as YYYY-MM-DD")

    await interaction.followup.send(embed=embed, ephemeral=True)

# -----------------------------
# ON READY (guild-scoped sync)
# -----------------------------
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
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
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
