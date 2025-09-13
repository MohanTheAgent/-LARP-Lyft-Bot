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
TARGET_CHANNEL_ID = 1416334665958166560        # ride request posts
ROLE_ID_1 = 1416068902609223749                # driver role 1
ROLE_ID_2 = 1416063969965248594                # driver role 2
LOG_CHANNEL_ID = 1416342987893375007           # ride logs (/log-ride)
AUDIT_LOG_CHANNEL_ID = 1416392593222270976     # audit / activity log
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
        except Exception:
            _db = {"riders": {}, "drivers": {}}
        _db.setdefault("riders", {})
        _db.setdefault("drivers", {})

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

def named(user: discord.abc.User) -> str:
    if hasattr(user, "mention"):
        return f"{user.mention} (`{getattr(user, 'id', 'unknown')}`)"
    return f"`{getattr(user, 'id', 'unknown')}`"

async def send_audit_embed(
    title: str,
    fields: List[tuple],
    color: discord.Color = discord.Color.blurple(),
    description: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
):
    ch = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    emb = discord.Embed(title=title, description=description or "", color=color, timestamp=datetime.now(timezone.utc))
    for name, value, inline in fields:
        emb.add_field(name=name, value=value, inline=inline)
    if thumbnail_url:
        emb.set_thumbnail(url=thumbnail_url)
    try:
        await ch.send(embed=emb)
    except Exception:
        pass

# -----------------------------
# Rating buttons (1–10) — thread-based; only rider can press
# -----------------------------
class RatingView(discord.ui.View):
    def __init__(self, driver_id: int, from_user_id: int):
        super().__init__(timeout=None)
        self.driver_id = driver_id
        self.from_user_id = from_user_id

    async def _record(self, interaction: discord.Interaction, value: int):
        if interaction.user.id != self.from_user_id:
            return await interaction.response.send_message("Only the rider can rate this ride.", ephemeral=True)

        async with _db_lock:
            drivers = _db.setdefault("drivers", {})
            drec = drivers.setdefault(str(self.driver_id), {"name": "", "ratings": []})
            try:
                u = interaction.client.get_user(self.driver_id) or await interaction.client.fetch_user(self.driver_id)
                if u:
                    drec["name"] = getattr(u, "name", drec.get("name", ""))
            except Exception:
                pass
            drec["ratings"].append({"from": self.from_user_id, "rating": int(value), "date": today_iso()})
        await save_db()

        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(content="Thanks for your feedback!", view=self)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(interaction.message.id, content="Thanks for your feedback!", view=self)

        await send_audit_embed(
            "Rating Submitted",
            fields=[
                ("Driver", f"<@{self.driver_id}>", True),
                ("From", f"<@{self.from_user_id}>", True),
                ("Rating", f"{value}/10", True),
                ("Date", today_iso(), True),
            ],
            color=discord.Color.green(),
        )

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def r1(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def r2(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def r3(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def r4(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, custom_id="rate_5")
    async def r5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 5)

    @discord.ui.button(label="6", style=discord.ButtonStyle.secondary, custom_id="rate_6")
    async def r6(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 6)

    @discord.ui.button(label="7", style=discord.ButtonStyle.secondary, custom_id="rate_7")
    async def r7(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 7)

    @discord.ui.button(label="8", style=discord.ButtonStyle.secondary, custom_id="rate_8")
    async def r8(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 8)

    @discord.ui.button(label="9", style=discord.ButtonStyle.secondary, custom_id="rate_9")
    async def r9(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 9)

    @discord.ui.button(label="10", style=discord.ButtonStyle.primary, custom_id="rate_10")
    async def r10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 10)

# -----------------------------
# Claim / End view (stores thread_id to post rating there)
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, thread_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.claimed_by: Optional[int] = None
        self._lock = asyncio.Lock()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="claim_btn")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self._lock:
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send("You are not authorized to claim rides.", ephemeral=True)
            if self.claimed_by is not None:
                return await interaction.followup.send("This ride has already been claimed.", ephemeral=True)

            self.claimed_by = interaction.user.id
            button.disabled = True

            # Update embed: set Status=Claimed/Ongoing and Driver
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
                # Copy/replace fields, inject Status + Driver
                has_status = False
                for f in base.fields:
                    name_lower = f.name.strip().lower()
                    if name_lower == "status":
                        has_status = True
                        new.add_field(name="Status", value="Claimed / Ongoing", inline=True)
                    elif name_lower == "driver":
                        continue
                    else:
                        new.add_field(name=f.name, value=f.value, inline=f.inline)
                if not has_status:
                    new.add_field(name="Status", value="Claimed / Ongoing", inline=True)
                new.add_field(name="Driver", value=interaction.user.mention, inline=False)

                if base.thumbnail and base.thumbnail.url:
                    new.set_thumbnail(url=base.thumbnail.url)
                new.set_footer(text="Ride claimed")
                embed = new

            await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

            ch = bot.get_channel(TARGET_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(f"<@{self.requester_id}> Your driver is {interaction.user.mention}")

            await send_audit_embed(
                "Ride Claimed",
                fields=[
                    ("Driver", named(interaction.user), True),
                    ("Rider", f"<@{self.requester_id}>", True),
                    ("Date", today_iso(), True),
                ],
                color=discord.Color.orange(),
                thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
            )

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="end_btn")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.claimed_by is None:
            return await interaction.followup.send("This ride has not been claimed yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

        button.disabled = True

        # Update embed: set Status=Completed
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
            has_status = False
            for f in base.fields:
                if f.name.strip().lower() == "status":
                    has_status = True
                    new.add_field(name="Status", value="Completed", inline=True)
                else:
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
            if not has_status:
                new.add_field(name="Status", value="Completed", inline=True)

            new.set_footer(text="Ride ended")
            embed = new
        await interaction.followup.edit_message(message_id=msg.id, embed=embed, view=self)

        ch = bot.get_channel(TARGET_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            await ch.send(f"-# Ride ended by {interaction.user.mention}")

        await send_audit_embed(
            "Ride Ended",
            fields=[
                ("Driver", named(interaction.user), True),
                ("Rider", f"<@{self.requester_id}>", True),
                ("Date", today_iso(), True),
            ],
            color=discord.Color.dark_grey(),
            thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
        )

        # Rating UI + comment prompt in ride thread
        if self.thread_id:
            thread = bot.get_channel(self.thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    rating_embed = discord.Embed(
                        title="Rate Your Driver",
                        description="Please choose a rating from **1 to 10**.",
                        color=discord.Color.blurple()
                    )
                    await thread.send(
                        content=f"<@{self.requester_id}>",
                        embed=rating_embed,
                        view=RatingView(driver_id=self.claimed_by, from_user_id=self.requester_id),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                    )
                    await thread.send(
                        embed=discord.Embed(
                            title="Driver Feedback",
                            description="💬 Please reply in this thread with any comments about your driver.",
                            color=discord.Color.grayple()
                        )
                    )
                    await send_audit_embed(
                        "Rating Form Posted",
                        fields=[
                            ("Thread", f"<#{self.thread_id}>", True),
                            ("Rider", f"<@{self.requester_id}>", True),
                            ("Driver", f"<@{self.claimed_by}>", True),
                        ],
                        color=discord.Color.blurple(),
                    )
                except Exception as e:
                    await send_audit_embed(
                        "Error",
                        fields=[("Posting rating form failed", str(e), False)],
                        color=discord.Color.red()
                    )

# -----------------------------
# /request ride  (adds Status field; rating 1–10 later)
# -----------------------------
request_group = app_commands.Group(name="request", description="Create ride requests")

@app_commands.choices(
    service_level=[app_commands.Choice(name="Premium", value="Premium"),
                   app_commands.Choice(name="Standard", value="Standard")]
)
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
    separator = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

    e = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description=f"A new ride is waiting to be claimed.\n{separator}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Pickup", value=starting_location, inline=True)
    e.add_field(name="Destination", value=destination, inline=True)
    e.add_field(name="Status", value="Unclaimed", inline=True)
    e.add_field(name="Requested By", value=interaction.user.mention, inline=False)
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Click Claim to accept this ride")

    view = ClaimView(requester_id=interaction.user.id, thread_id=None)

    ch = bot.get_channel(TARGET_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(TARGET_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response(content="Ride channel not found.")

    content = f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>"
    msg = await ch.send(content=content, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))

    # Create the ride thread and store it
    try:
        t = await msg.create_thread(name=f"Ride - {interaction.user.display_name}", auto_archive_duration=1440)
        view.thread_id = t.id
        await t.send(
            embed=discord.Embed(
                title="Ride Thread",
                description=f"{interaction.user.mention} This thread is for coordinating your ride.",
                color=discord.Color.dark_theme()
            )
        )
    except Exception:
        pass

    await interaction.edit_original_response(content="Your ride has been posted.")
    await send_audit_embed(
        "Ride Requested",
        fields=[
            ("Rider", named(interaction.user), True),
            ("Pickup", starting_location, True),
            ("Destination", destination, True),
            ("Service", service_level.value, True),
            ("Status", "Unclaimed", True),
            ("Date", today_iso(), True),
        ],
        color=color,
        thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
    )

# -----------------------------
# /log-ride  (driver-side numeric rating 1–10 for the ride)
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride")
@app_commands.describe(
    rider="Rider user",
    ride_link="Ride link or reference",
    income="Income for this ride (number)",
    rating="Your rating for this ride (number 1–10)",
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
        rrec = riders.setdefault(str(rider.id), {"name": rider.name, "rides": []})
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
    e.add_field(name="Rider", value=named(rider), inline=False)
    e.add_field(name="Ride Link", value=ride_link, inline=False)
    e.add_field(
        name="Income",
        value=(f"${income_val:,.2f}" if isinstance(income_val, (int, float)) else str(income)),
        inline=True,
    )
    e.add_field(
        name="Driver's Rating (1–10)",
        value=(f"{rating_val:.1f}" if isinstance(rating_val, (int, float)) else str(rating)),
        inline=True,
    )
    e.add_field(
        name="Rides This Week",
        value=(str(rides_val) if rides_val is not None else rides_this_week),
        inline=True,
    )
    if comment:
        e.add_field(name="Comment", value=comment[:1024], inline=False)
    e.add_field(name="Driver", value=named(interaction.user), inline=True)
    e.set_thumbnail(url=rider.display_avatar.url)

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if isinstance(log_ch, discord.TextChannel):
        await log_ch.send(embed=e)

    await interaction.edit_original_response(content="Ride logged.")
    await send_audit_embed(
        "Ride Logged (Internal)",
        fields=[
            ("Rider", named(rider), True),
            ("Driver", named(interaction.user), True),
            ("Income", e.fields[2].value, True),
            ("Driver Rating", e.fields[3].value, True),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.dark_grey(),
        thumbnail_url=getattr(rider.display_avatar, "url", discord.Embed.Empty),
    )

# -----------------------------
# /search (with ephemeral toggle)
# -----------------------------
@tree.command(name="search", description="Search a user's rider/driver profile")
@app_commands.describe(
    user="User to search",
    ephemeral="If true, only you see the result (default: true)"
)
async def search_cmd(interaction: discord.Interaction, user: discord.User, ephemeral: Optional[bool] = True):
    await interaction.response.defer(ephemeral=bool(ephemeral))
    if interaction.guild_id != GUILD_ID:
        return await interaction.followup.send("This command is not available in this server.", ephemeral=True)
    if not user_has_allowed_role(interaction.user):
        return await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)

    async with _db_lock:
        riders = _db.get("riders", {})
        drivers = _db.get("drivers", {})
        rrec = riders.get(str(user.id))
        drec = drivers.get(str(user.id))

    emb = discord.Embed(
        title="Member Profile",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="User", value=named(user), inline=False)

    if rrec and rrec.get("rides"):
        rides = rrec["rides"]
        rider_ratings: List[float] = []
        for r in rides:
            try:
                rider_ratings.append(float(r["rating"]))
            except Exception:
                pass
        rider_avg = avg(rider_ratings)
        emb.add_field(
            name="Rider Rides",
            value=f"Total: {len(rides)} | Avg: {(f'{rider_avg:.2f}' if rider_avg is not None else '-')}",
            inline=False
        )
        comments = [f"- {x['date']}: {x['comment']}" for x in rides if x.get("comment")] or ["-"]
        emb.add_field(name="Recent Rider Comments", value="\n".join(comments[:5]), inline=False)
    else:
        emb.add_field(name="Rider Rides", value="No history", inline=False)

    if drec and drec.get("ratings"):
        ratings = [int(x["rating"]) for x in drec["ratings"] if isinstance(x.get("rating"), int)]
        if ratings:
            d_avg = avg(ratings)
            emb.add_field(name="Driver Ratings", value=f"{len(ratings)} | Avg {d_avg:.2f} (1–10)", inline=False)

    emb.set_thumbnail(url=user.display_avatar.url)
    await interaction.followup.send(embed=emb, ephemeral=bool(ephemeral))

    await send_audit_embed(
        "Profile Searched",
        fields=[
            ("Queried", named(user), True),
            ("By", named(interaction.user), True),
            ("Ephemeral", str(bool(ephemeral)), True),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.teal(),
        thumbnail_url=getattr(user.display_avatar, "url", discord.Embed.Empty),
    )

# -----------------------------
# Ready / sync
# -----------------------------
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    g = bot.get_guild(GUILD_ID)
    member_count = g.member_count if g else "—"
    await send_audit_embed(
        "Lyft Bot Online",
        fields=[
            ("Bot", named(bot.user), True),
            ("Guild", f"{getattr(g, 'name', 'Unknown')} (`{GUILD_ID}`)", True),
            ("Members", str(member_count), True),
            ("Commands Synced", "Yes", True),
        ],
        color=discord.Color.green(),
        thumbnail_url=getattr(bot.user.display_avatar, "url", discord.Embed.Empty),
    )

# -----------------------------
# Minimal HTTP server (Render)
# -----------------------------
async def handle_health(_):
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
