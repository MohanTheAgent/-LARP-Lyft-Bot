# -*- coding: utf-8 -*-
import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# -----------------------------
# CONFIG
# -----------------------------
GUILD_ID = 1416057930381262880
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749
ROLE_ID_2 = 1416063969965248594
LOG_CHANNEL_ID = 1416342987893375007
AUDIT_LOG_CHANNEL_ID = 1416392593222270976
ADMIN_ROLE_ID = 1416069791495622707  # for /profile_admin

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

# -----------------------------
# BOT
# -----------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# DB (persistent JSON)
# -----------------------------
_db_lock = asyncio.Lock()
_db: Dict[str, Any] = {"riders": {}, "drivers": {}}

def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

async def load_db():
    global _db
    async with _db_lock:
        if not os.path.exists(DATA_FILE):
            _db = {"riders": {}, "drivers": {}}
            await save_db()
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _db = json.load(f)
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

# -----------------------------
# Utils
# -----------------------------
def user_has_allowed_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {ROLE_ID_1, ROLE_ID_2} for r in getattr(member, "roles", []))

def user_is_admin(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(member, "roles", []))

def safe_float(v: str) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None

def safe_int(v: str) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None

def avg(values: List[float]) -> Optional[float]:
    vals = [x for x in values if isinstance(x, (int, float))]
    return (sum(vals) / len(vals)) if vals else None

async def audit_log(text: str):
    ch = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text)
        except Exception:
            pass

# -----------------------------
# Rating buttons (DM)
# -----------------------------
class RatingView(discord.ui.View):
    def __init__(self, driver_id: int, from_user_id: int):
        super().__init__(timeout=300)
        self.driver_id = driver_id
        self.from_user_id = from_user_id

    async def _record(self, interaction: discord.Interaction, value: int):
        async with _db_lock:
            drivers = _db.setdefault("drivers", {})
            drec = drivers.setdefault(str(self.driver_id), {"name": "", "ratings": [], "admin_notes": [], "flag": None})
            drec["ratings"].append({"from": self.from_user_id, "rating": int(value), "date": today_iso()})
        await save_db()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Thank you for your feedback!", view=self)
        await audit_log(f"Rating submitted: user {self.from_user_id} rated driver {self.driver_id} = {value}")

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary)
    async def r1(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary)
    async def r2(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary)
    async def r3(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary)
    async def r4(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary)
    async def r5(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 5)

# -----------------------------
# Claim/End view
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.claimed_by: Optional[int] = None
        self._lock = asyncio.Lock()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="claim_btn")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self._lock:
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send("You are not authorized to claim rides.", ephemeral=True)
            if self.claimed_by is not None:
                return await interaction.followup.send("This ride is already claimed.", ephemeral=True)

            self.claimed_by = interaction.user.id
            button.disabled = True

            msg = interaction.message
            embed = None
            if msg.embeds:
                base = msg.embeds[0]
                new = discord.Embed(
                    title=base.title,
                    description=base.description,
                    color=base.color,
                    timestamp=datetime.now(timezone.utc)
                )
                for f in base.fields:
                    if f.name.strip().lower() == "driver":
                        continue
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
                new.add_field(name="Driver", value=interaction.user.mention, inline=False)
                if base.thumbnail and base.thumbnail.url:
                    new.set_thumbnail(url=base.thumbnail.url)
                new.set_footer(text="Ride claimed")
                embed = new

            await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

            ch = bot.get_channel(TARGET_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(f"<@{self.requester_id}> Your driver is {interaction.user.mention}")
            await audit_log(f"Ride claimed by {interaction.user.id} for requester {self.requester_id}")

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="end_btn")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.claimed_by is None:
            return await interaction.followup.send("This ride has not been claimed yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

        button.disabled = True

        # Update message embed
        msg = interaction.message
        embed = None
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=base.description,
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc)
            )
            for f in base.fields:
                new.add_field(name=f.name, value=f.value, inline=f.inline)
            new.set_footer(text="Ride ended")
            embed = new
        await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

        ch = bot.get_channel(TARGET_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(f"-# Ride ended by {interaction.user.mention}")
        await audit_log(f"Ride ended by driver {interaction.user.id} for requester {self.requester_id}")

        # DM the requester for rating (embed + buttons)
        try:
            user = bot.get_user(self.requester_id) or await bot.fetch_user(self.requester_id)
            if user:
                rating_embed = discord.Embed(
                    title="Rate Your Driver",
                    description="How was your ride? Please choose a rating from 1 to 5.",
                    color=discord.Color.blurple()
                )
                view = RatingView(driver_id=self.claimed_by, from_user_id=self.requester_id)
                await user.send(embed=rating_embed, view=view)
                await audit_log(f"Sent rating DM to {self.requester_id} for driver {self.claimed_by}")
        except discord.Forbidden:
            await interaction.followup.send("Could not DM the rider for rating (DMs may be closed).", ephemeral=True)
            await audit_log(f"Failed to DM rider {self.requester_id} for rating (Forbidden)")
        except Exception as e:
            await audit_log(f"Failed to DM rider {self.requester_id} for rating: {e}")

# -----------------------------
# /request ride
# -----------------------------
request_group = app_commands.Group(name="request", description="Create ride requests")

@app_commands.choices(service_level=[
    app_commands.Choice(name="Premium", value="Premium"),
    app_commands.Choice(name="Standard", value="Standard")
])
@request_group.command(name="ride", description="Request a ride")
@app_commands.describe(
    starting_location="Pickup location",
    destination="Destination",
    service_level="Premium or Standard"
)
async def request_ride(
    interaction: discord.Interaction,
    starting_location: str,
    destination: str,
    service_level: app_commands.Choice[str]
):
    await interaction.response.send_message("Posting your ride...", ephemeral=True)
    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command is not available in this server.")

    color = discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue()
    e = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description="A new ride is waiting to be claimed.",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Pickup", value=starting_location, inline=True)
    e.add_field(name="Destination", value=destination, inline=True)
    e.add_field(name="Requested By", value=interaction.user.mention, inline=False)
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Click Claim to accept this ride")

    view = ClaimView(requester_id=interaction.user.id)

    ch = bot.get_channel(TARGET_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response(content="Ride channel not found.")

    content = f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>"
    msg = await ch.send(content=content, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))

    try:
        t = await msg.create_thread(name=f"Ride - {interaction.user.display_name}", auto_archive_duration=1440)
        await t.send(f"{interaction.user.mention} Use this thread to coordinate your ride.")
    except Exception:
        pass

    await interaction.edit_original_response(content="Your ride has been posted.")
    await audit_log(f"Ride requested by {interaction.user.id}")

# -----------------------------
# /log-ride
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride")
@app_commands.describe(
    rider="Rider user",
    ride_link="Ride link or reference",
    income="Income for this ride (number)",
    rating="Your rating for this ride (number)",
    rides_this_week="Number of rides you completed this week (number)",
    comment="Optional rider comment"
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
    await interaction.response.send_message("Logging ride...", ephemeral=True)
    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command is not available in this server.")
    if not user_has_allowed_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
    rating_val = safe_float(rating)
    rides_val = safe_int(rides_this_week)

    async with _db_lock:
        riders = _db.setdefault("riders", {})
        rrec = riders.setdefault(str(rider.id), {"name": rider.name, "rides": [], "admin_notes": [], "flag": None})
        rrec["name"] = rider.name
        rrec["rides"].append({
            "date": today_iso(),
            "driver_id": interaction.user.id,
            "driver_name": getattr(interaction.user, "display_name", interaction.user.name),
            "income": income_val if income_val is not None else income,
            "rating": rating_val if rating_val is not None else rating,
            "comment": (comment or "").strip() or None,
            "ride_link": ride_link
        })
    await save_db()

    e = discord.Embed(title="Ride Logged", color=discord.Color.dark_grey(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Rider", value=f"{rider.mention} ({rider.id})", inline=False)
    e.add_field(name="Ride Link", value=ride_link, inline=False)
    e.add_field(name="Income", value=(f"${income_val:,.2f}" if isinstance(income_val, (int, float)) else str(income)), inline=True)
    e.add_field(name="Rating", value=(f"{rating_val:.2f}" if isinstance(rating_val, (int, float)) else str(rating)), inline=True)
    e.add_field(name="Rides This Week", value=(str(rides_val) if rides_val is not None else rides_this_week), inline=True)
    if comment:
        e.add_field(name="Comment", value=comment[:1024], inline=False)
    e.add_field(name="Driver", value=interaction.user.mention, inline=True)
    e.set_thumbnail(url=rider.display_avatar.url)

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if isinstance(log_ch, discord.TextChannel):
        await log_ch.send(embed=e)

    await interaction.edit_original_response(content="Ride logged.")
    await audit_log(f"Ride logged by {interaction.user.id} for rider {rider.id}")

# -----------------------------
# /search (ephemeral, role-locked)
# -----------------------------
@tree.command(name="search", description="Search a user's rider/driver profile")
@app_commands.describe(user="User to search")
async def search_cmd(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id != GUILD_ID:
        return await interaction.followup.send("This command is not available in this server.", ephemeral=True)
    if not user_has_allowed_role(interaction.user):
        return await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)

    async with _db_lock:
        riders = _db.get("riders", {})
        drivers = _db.get("drivers", {})
        rrec = riders.get(str(user.id))
        drec = drivers.get(str(user.id))

    embed = discord.Embed(
        title=f"Member Profile",
        description="Summary view",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Member", value=f"{user.mention} ({user.id})", inline=False)

    # Rider section
    if rrec and rrec.get("rides"):
        rides = rrec["rides"]
        r_avg_vals: List[float] = []
        for r in rides:
            try:
                r_avg_vals.append(float(r["rating"]))
            except Exception:
                pass
        rider_avg = avg(r_avg_vals)
        embed.add_field(
            name="Rider Rides",
            value=f"Total: {len(rides)} | Avg: {(f'{rider_avg:.2f}' if rider_avg is not None else '-')}",
            inline=False
        )
        comments = [f"- {x['date']}: {x['comment']}" for x in rides if x.get("comment")] or ["-"]
        embed.add_field(name="Recent Rider Comments", value="\n".join(comments[:5]), inline=False)
    else:
        embed.add_field(name="Rider Rides", value="No history", inline=False)

    # Driver section (only if any ratings exist)
    if drec and drec.get("ratings"):
        ratings = [int(x["rating"]) for x in drec["ratings"] if isinstance(x.get("rating"), int)]
        d_avg = avg(ratings)
        embed.add_field(name="Driver Rating", value=f"Average: {d_avg:.2f} from {len(ratings)} ratings", inline=False)

    embed.set_thumbnail(url=user.display_avatar.url)

    await interaction.followup.send(embed=embed, ephemeral=True)
    await audit_log(f"Search run by {interaction.user.id} for {user.id}")

# -----------------------------
# /profile_admin (optional tools)
# -----------------------------
profile_admin = app_commands.Group(name="profile_admin", description="Admin tools")

def admin_guard(func):
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if interaction.guild_id != GUILD_ID:
            return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
        if not user_is_admin(interaction.user):
            return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return await func(interaction, *args, **kwargs)
    return wrapper

@profile_admin.command(name="set_flag", description="Set a short flag on a rider or driver profile")
@app_commands.describe(user="Member", target="rider or driver", flag="Short label")
@app_commands.choices(target=[app_commands.Choice(name="rider", value="rider"), app_commands.Choice(name="driver", value="driver")])
@admin_guard
async def pa_set_flag(interaction: discord.Interaction, user: discord.User, target: app_commands.Choice[str], flag: str):
    await interaction.response.defer(ephemeral=True)
    async with _db_lock:
        if target.value == "rider":
            rec = _db.setdefault("riders", {}).setdefault(str(user.id), {"name": user.name, "rides": [], "admin_notes": [], "flag": None})
            rec["flag"] = flag[:50]
        else:
            rec = _db.setdefault("drivers", {}).setdefault(str(user.id), {"name": user.name, "ratings": [], "admin_notes": [], "flag": None})
            rec["flag"] = flag[:50]
    await save_db()
    await interaction.followup.send("Flag set.", ephemeral=True)
    await audit_log(f"Admin {interaction.user.id} set flag on {user.id} {target.value}: {flag}")

@profile_admin.command(name="clear_flag", description="Clear flag on a rider or driver profile")
@app_commands.describe(user="Member", target="rider or driver")
@app_commands.choices(target=[app_commands.Choice(name="rider", value="rider"), app_commands.Choice(name="driver", value="driver")])
@admin_guard
async def pa_clear_flag(interaction: discord.Interaction, user: discord.User, target: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    async with _db_lock:
        if target.value == "rider":
            rec = _db.setdefault("riders", {}).setdefault(str(user.id), {"name": user.name, "rides": [], "admin_notes": [], "flag": None})
            rec["flag"] = None
        else:
            rec = _db.setdefault("drivers", {}).setdefault(str(user.id), {"name": user.name, "ratings": [], "admin_notes": [], "flag": None})
            rec["flag"] = None
    await save_db()
    await interaction.followup.send("Flag cleared.", ephemeral=True)
    await audit_log(f"Admin {interaction.user.id} cleared flag on {user.id} {target.value}")

# -----------------------------
# Ready/sync
# -----------------------------
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.add_command(profile_admin, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await audit_log("Bot started")

# -----------------------------
# Minimal HTTP server (Render)
# -----------------------------
async def handle_health(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server listening on 0.0.0.0:{port}")

# -----------------------------
# MAIN
# -----------------------------
async def main():
    if not TOKEN:
        raise RuntimeError("Please set DISCORD_TOKEN env var.")
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
