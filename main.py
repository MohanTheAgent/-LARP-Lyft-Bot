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

GUILD_ID = 1416057930381262880
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749
ROLE_ID_2 = 1416063969965248594
LOG_CHANNEL_ID = 1416342987893375007
AUDIT_LOG_CHANNEL_ID = 1416392593222270976
ADMIN_ROLE_ID = 1416069791495622707
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

_db_lock = asyncio.Lock()
_db: Dict[str, Any] = {"riders": {}, "drivers": {}}

def _today_iso():
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

async def save_db():
    async with _db_lock:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_db, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)

def user_has_allowed_role(member):
    return any(getattr(r, "id", None) in {ROLE_ID_1, ROLE_ID_2} for r in getattr(member, "roles", []))

def user_is_admin(member):
    return any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(member, "roles", []))

def avg(values: List[float]) -> Optional[float]:
    vals = [v for v in values if isinstance(v, (int, float))]
    return (sum(vals) / len(vals)) if vals else None

async def log_action(client: discord.Client, text: str):
    ch = client.get_channel(AUDIT_LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        await ch.send(f"?? {text}")

# -----------------------------
# RATING BUTTONS (DM)
# -----------------------------
class RatingView(discord.ui.View):
    def __init__(self, driver_id: int, from_user_id: int):
        super().__init__(timeout=300)
        self.driver_id = driver_id
        self.from_user_id = from_user_id

    async def _record(self, interaction: discord.Interaction, value: int):
        async with _db_lock:
            d = _db.setdefault("drivers", {}).setdefault(str(self.driver_id), {"name": "", "ratings": []})
            d["ratings"].append({"from": self.from_user_id, "rating": int(value), "date": _today_iso()})
        await save_db()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="? Thank you for your feedback!", view=self)
        await log_action(interaction.client, f"{interaction.user} rated driver <@{self.driver_id}> {value}/5")

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
# CLAIM + END RIDE VIEW
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.claimed_by: Optional[int] = None

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success)
    async def claim(self, i: discord.Interaction, b: discord.ui.Button):
        if not user_has_allowed_role(i.user):
            return await i.response.send_message("? You are not authorized to claim rides.", ephemeral=True)
        if self.claimed_by:
            return await i.response.send_message("?? Already claimed.", ephemeral=True)
        self.claimed_by = i.user.id
        b.disabled = True
        e = i.message.embeds[0]
        new = discord.Embed(title=e.title, description=e.description, color=e.color, timestamp=datetime.now(timezone.utc))
        for f in e.fields:
            if f.name != "Driver": new.add_field(name=f.name, value=f.value, inline=f.inline)
        new.add_field(name="Driver", value=i.user.mention, inline=False)
        new.set_thumbnail(url=e.thumbnail.url if e.thumbnail else i.user.display_avatar.url)
        await i.response.edit_message(embed=new, view=self)
        await log_action(i.client, f"{i.user} claimed a ride from <@{self.requester_id}>")

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger)
    async def end(self, i: discord.Interaction, b: discord.ui.Button):
        if not self.claimed_by:
            return await i.response.send_message("?? This ride has not been claimed yet.", ephemeral=True)
        if i.user.id != self.claimed_by:
            return await i.response.send_message("? Only the driver who claimed this ride can end it.", ephemeral=True)
        b.disabled = True
        e = i.message.embeds[0]
        new = discord.Embed(title=e.title, description=e.description, color=discord.Color.dark_grey(), timestamp=datetime.now(timezone.utc))
        for f in e.fields: new.add_field(name=f.name, value=f.value, inline=f.inline)
        new.set_footer(text="Ride ended")
        await i.response.edit_message(embed=new, view=self)
        await log_action(i.client, f"{i.user} ended ride from <@{self.requester_id}>")

        # DM client
        try:
            user = i.client.get_user(self.requester_id) or await i.client.fetch_user(self.requester_id)
            if user:
                embed = discord.Embed(title="Rate Your Driver", description="How was your ride? Please rate 1–5.", color=discord.Color.blurple())
                await user.send(embed=embed, view=RatingView(driver_id=self.claimed_by, from_user_id=self.requester_id))
                await log_action(i.client, f"DM rating prompt sent to {user}")
        except discord.Forbidden:
            await i.followup.send("?? Couldn't DM the rider (their DMs are closed).", ephemeral=True)

# -----------------------------
# /request ride
# -----------------------------
request_group = app_commands.Group(name="request", description="Create ride requests")

@app_commands.choices(service_level=[app_commands.Choice(name="Premium", value="Premium"), app_commands.Choice(name="Standard", value="Standard")])
@request_group.command(name="ride")
@app_commands.describe(starting_location="Pickup", destination="Destination", service_level="Premium or Standard")
async def ride(i: discord.Interaction, starting_location: str, destination: str, service_level: app_commands.Choice[str]):
    await i.response.send_message("?? Posting your ride...", ephemeral=True)
    if i.guild_id != GUILD_ID: return
    color = discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue()
    e = discord.Embed(title=f"{service_level.value} Ride Request", description="A new ride request is waiting to be claimed.", color=color, timestamp=datetime.now(timezone.utc))
    e.add_field(name="Pickup", value=starting_location)
    e.add_field(name="Destination", value=destination)
    e.add_field(name="Requested By", value=i.user.mention, inline=False)
    e.set_thumbnail(url=i.user.display_avatar.url)
    e.set_footer(text="Click Claim to accept this ride")
    view = ClaimView(requester_id=i.user.id)
    ch = i.client.get_channel(TARGET_CHANNEL_ID)
    content = f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>"
    msg = await ch.send(content=content, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
    try:
        t = await msg.create_thread(name=f"Ride - {i.user.display_name}", auto_archive_duration=1440)
        await t.send(f"{i.user.mention} This thread is for your ride discussion.")
    except: pass
    await i.edit_original_response(content="? Your ride has been posted.")
    await log_action(i.client, f"{i.user} requested a ride")

# -----------------------------
# /log-ride (adds to riders)
# -----------------------------
@tree.command(name="log-ride")
@app_commands.describe(rider="The rider", ride_link="Ride link", income="Income", rating="Your rating", rides_this_week="Rides this week", comment="Optional comment")
async def log_ride(i: discord.Interaction, rider: discord.User, ride_link: str, income: str, rating: str, rides_this_week: str, comment: Optional[str] = None):
    await i.response.send_message("?? Logging ride...", ephemeral=True)
    if i.guild_id != GUILD_ID or not user_has_allowed_role(i.user): return
    async with _db_lock:
        r = _db.setdefault("riders", {}).setdefault(str(rider.id), {"name": rider.name, "rides": []})
        r["rides"].append({"date": _today_iso(),"driver_id": i.user.id,"driver_name": i.user.display_name,"income": income,"rating": rating,"comment": comment,"ride_link": ride_link})
    await save_db()
    e = discord.Embed(title="Ride Logged", color=discord.Color.dark_grey(), timestamp=datetime.now(timezone.utc))
    e.add_field(name="Rider", value=rider.mention)
    e.add_field(name="Income", value=income)
    e.add_field(name="Rating", value=rating)
    e.add_field(name="Driver", value=i.user.mention)
    await i.client.get_channel(LOG_CHANNEL_ID).send(embed=e)
    await log_action(i.client, f"{i.user} logged a ride for {rider}")

# -----------------------------
# /search
# -----------------------------
@tree.command(name="search")
@app_commands.describe(user="The user to search")
async def search(i: discord.Interaction, user: discord.User):
    await i.response.defer(ephemeral=True)
    if i.guild_id != GUILD_ID or not user_has_allowed_role(i.user):
        return await i.followup.send("? Not allowed.", ephemeral=True)
    async with _db_lock:
        r = _db.get("riders", {}).get(str(user.id))
        d = _db.get("drivers", {}).get(str(user.id))
    e = discord.Embed(title=f"Profile for {user}", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if r:
        rides = r.get("rides", [])
        avg_rating = avg([float(x["rating"]) for x in rides if str(x.get("rating")).replace(".","",1).isdigit()])
        e.add_field(name="Rider Rides", value=f"{len(rides)} total\nAvg: {avg_rating:.2f}" if rides else "No rides", inline=False)
        comments = [f"- {x['date']}: {x['comment']}" for x in rides if x.get("comment")] or ["—"]
        e.add_field(name="Recent Comments", value="\n".join(comments[:5]), inline=False)
    else:
        e.add_field(name="Rider", value="No history", inline=False)
    if d and d.get("ratings"):
        ratings = [x["rating"] for x in d["ratings"]]
        e.add_field(name="Driver Rating", value=f"{avg(ratings):.2f} avg from {len(ratings)} ratings", inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    await i.followup.send(embed=e, ephemeral=True)
    await log_action(i.client, f"{i.user} searched {user}")

# -----------------------------
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user}")
    await log_action(bot, "Bot started")

async def handle_health(_): return web.Response(text="OK")
async def start_web_server():
    app = web.Application(); app.router.add_get("/", handle_health); app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT","10000"))); await site.start()

async def main():
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
