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
AUDIT_LOG_CHANNEL_ID = 1416392593222270976     # all actions
ADMIN_ROLE_ID = 1416069791495622707            # manage-rating

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

def named(user: discord.abc.User) -> str:
    if hasattr(user, "mention"):
        return f"{user.mention} (`{getattr(user, 'id', 'unknown')}`)"
    return f"`{getattr(user, 'id', 'unknown')}`"

async def send_audit_embed(
    title: str,
    description: Optional[str] = None,
    fields: Optional[List[tuple]] = None,
    color: discord.Color = discord.Color.blurple(),
    thumbnail_url: Optional[str] = None,
):
    ch = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return
    emb = discord.Embed(title=title, description=description or "", color=color, timestamp=datetime.now(timezone.utc))
    if fields:
        for name, value, inline in fields:
            emb.add_field(name=name, value=value, inline=inline)
    if thumbnail_url:
        emb.set_thumbnail(url=thumbnail_url)
    try:
        await ch.send(embed=emb)
    except Exception:
        pass

# -----------------------------
# Rating buttons (thread-based)
# Only the rider (from_user_id) can press
# -----------------------------
class RatingView(discord.ui.View):
    def __init__(self, driver_id: int, from_user_id: int):
        super().__init__(timeout=300)
        self.driver_id = driver_id
        self.from_user_id = from_user_id

    async def _record(self, interaction: discord.Interaction, value: int):
        if interaction.user.id != self.from_user_id:
            return await interaction.response.send_message(
                "Only the requesting rider can submit a rating for this ride.", ephemeral=True
            )

        async with _db_lock:
            drivers = _db.setdefault("drivers", {})
            drec = drivers.setdefault(str(self.driver_id), {"name": "", "ratings": []})
            # keep latest name if possible
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
            await interaction.response.edit_message(content="Thank you for your feedback!", view=self)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(interaction.message.id, content="Thank you for your feedback!", view=self)
        await send_audit_embed(
            "Rating Submitted",
            fields=[
                ("Driver", f"<@{self.driver_id}>", True),
                ("From", f"<@{self.from_user_id}>", True),
                ("Rating", f"{value}/5", True),
                ("Date", today_iso(), True),
            ],
            color=discord.Color.green(),
        )

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def r1(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 1)
    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def r2(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 2)
    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def r3(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 3)
    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def r4(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 4)
    @discord.ui.button(label="5", style=discord.ButtonStyle.primary, custom_id="rate_5")
    async def r5(self, i: discord.Interaction, b: discord.ui.Button): await self._record(i, 5)

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

        # Post rating UI + comment prompt in the thread
        if self.thread_id:
            thread = bot.get_channel(self.thread_id)
            if isinstance(thread, discord.Thread):
                try:
                    rating_embed = discord.Embed(
                        title="Rate Your Driver",
                        description="Please choose a rating from 1 to 5.",
                        color=discord.Color.blurple()
                    )
                    await thread.send(
                        content=f"<@{self.requester_id}>",
                        embed=rating_embed,
                        view=RatingView(driver_id=self.claimed_by, from_user_id=self.requester_id),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                    )
                    await thread.send(
                        content=f"<@{self.requester_id}>",
                        embed=discord.Embed(
                            title="Driver Feedback",
                            description="💬 Please reply in this thread with any comments about your driver.",
                            color=discord.Color.grayple()
                        ),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
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
                    await send_audit_embed("Error", description=f"Failed posting rating form in thread: {e}", color=discord.Color.red())
        # If no thread exists, do nothing further (we keep everything thread-based now)

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
            ("Date", today_iso(), True),
        ],
        color=color,
        thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
    )

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

    # Public log embed
    e = discord.Embed(title="Ride Logged", color=discord.Color.dark_grey(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Rider", value=named(rider), inline=False)
    e.add_field(name="Ride Link", value=ride_link, inline=False)
    e.add_field(name="Income", value=(f"${income_val:,.2f}" if isinstance(income_val, (int, float)) else str(income)), inline=True)
    e.add_field(name="Rating", value=(f"{rating_val:.2f}" if isinstance(rating_val, (int, float)) else str(rating)), inline=True)
    e.add_field(name="Rides This Week", value=(str(rides_val) if rides_val is not None else rides_this_week), inline=True)
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
            ("Rating", e.fields[3].value, True),
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
        title=f"Member Profile",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Member", value=named(user), inline=False)

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
            emb.add_field(name="Driver Rating", value=f"Average: {d_avg:.2f} from {len(ratings)} ratings", inline=False)

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
# /manage-rating (admin: edit/remove driver ratings)
# -----------------------------
@tree.command(name="manage-rating", description="Admin: edit or remove a driver's rating by index")
@app_commands.describe(
    driver="Driver to modify",
    action="Choose Edit or Remove",
    index="Which rating number to target (1 = oldest rating)",
    new_value="New rating 1–5 (required for Edit)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Edit", value="edit"),
    app_commands.Choice(name="Remove", value="remove"),
])
async def manage_rating(
    interaction: discord.Interaction,
    driver: discord.User,
    action: app_commands.Choice[str],
    index: int,
    new_value: Optional[int] = None
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not user_is_admin(interaction.user):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    async with _db_lock:
        d = _db.setdefault("drivers", {}).setdefault(str(driver.id), {"name": driver.name, "ratings": []})
        ratings = d.get("ratings", [])
        if index < 1 or index > len(ratings):
            return await interaction.followup.send(f"Invalid index. This driver has {len(ratings)} rating(s).", ephemeral=True)

        target = ratings[index - 1]
        if action.value == "edit":
            if new_value is None or not (1 <= int(new_value) <= 5):
                return await interaction.followup.send("For Edit, provide new_value between 1 and 5.", ephemeral=True)
            old = target.get("rating")
            target["rating"] = int(new_value)
            result_text = f"Edited rating #{index}: {old} → {new_value}"
        else:
            removed = ratings.pop(index - 1)
            result_text = f"Removed rating #{index}: {removed.get('rating')} (from <@{removed.get('from', 'unknown')}>)"

    await save_db()
    await interaction.followup.send(f"Done. {result_text}", ephemeral=True)

    await send_audit_embed(
        "Manage Rating",
        fields=[
            ("Action", action.name, True),
            ("Driver", named(driver), True),
            ("By", named(interaction.user), True),
            ("Index", str(index), True),
            ("Result", result_text, False),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.gold(),
        thumbnail_url=getattr(driver.display_avatar, "url", discord.Embed.Empty),
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

    # Bot started embed
    g = bot.get_guild(GUILD_ID)
    member_count = g.member_count if g else "—"
    started = discord.Embed(
        title="Lyft Bot Online",
        description="All systems ready.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    started.add_field(name="Bot", value=named(bot.user), inline=True)
    started.add_field(name="Guild", value=f"{getattr(g, 'name', 'Unknown')} (`{GUILD_ID}`)", inline=True)
    started.add_field(name="Members", value=str(member_count), inline=True)
    started.add_field(name="Commands Synced", value="Yes", inline=True)
    await send_audit_embed("Lyft Bot Online", fields=[
        ("Bot", named(bot.user), True),
        ("Guild", f"{getattr(g, 'name', 'Unknown')} (`{GUILD_ID}`)", True),
        ("Members", str(member_count), True),
        ("Commands Synced", "Yes", True),
    ], color=discord.Color.green(), thumbnail_url=getattr(bot.user.display_avatar, "url", discord.Embed.Empty))

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
