# -*- coding: utf-8 -*-
import os, json, asyncio
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
RIDE_LOG_CHANNEL_ID = 1416342987893375007
AUDIT_LOG_CHANNEL_ID = 1416392593222270976
TOKEN = os.getenv("DISCORD_TOKEN")

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

_db_lock = asyncio.Lock()
_db: Dict[str, Any] = {"riders": {}}

# -----------------------------
# DB
# -----------------------------
async def load_db():
    global _db
    async with _db_lock:
        if not os.path.exists(DATA_FILE):
            _db = {"riders": {}}
            await save_db()
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                _db = json.load(f)
        except Exception:
            _db = {"riders": {}}
        _db.setdefault("riders", {})

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
    try: return float(v)
    except: return None

def safe_int(v: str) -> Optional[int]:
    try: return int(v)
    except: return None

def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

async def send_audit_embed(title: str, fields: List[tuple], color=discord.Color.blurple()):
    ch = bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel): return
    emb = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    for name, value, inline in fields:
        emb.add_field(name=name, value=value, inline=inline)
    await ch.send(embed=emb)

# -----------------------------
# Claim / End view
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

            msg = interaction.message
            if msg.embeds:
                base = msg.embeds[0]
                new = discord.Embed(
                    title=base.title,
                    description=base.description,
                    color=base.color,
                    timestamp=datetime.now(timezone.utc)
                )
                for f in base.fields:
                    if f.name.lower() != "driver":
                        new.add_field(name=f.name, value=f.value, inline=f.inline)
                new.add_field(name="Driver", value=interaction.user.mention, inline=False)
                new.add_field(name="Status", value="Claimed / Ongoing", inline=False)
                if base.thumbnail and base.thumbnail.url:
                    new.set_thumbnail(url=base.thumbnail.url)
                new.set_footer(text="Ride claimed")
                await interaction.followup.edit_message(message_id=msg.id, embed=new, view=self)

            # EMBED for "Your driver is..."
            driver_embed = discord.Embed(
                title="Driver Assigned",
                description=f"Your driver is {interaction.user.mention}",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            driver_embed.add_field(name="Rider", value=f"<@{self.requester_id}>", inline=True)
            driver_embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
            ch = bot.get_channel(TARGET_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(
                    content=f"<@{self.requester_id}>",
                    embed=driver_embed,
                    allowed_mentions=discord.AllowedMentions(users=True)
                )
            await send_audit_embed("Ride Claimed", [("Rider", f"<@{self.requester_id}>", True), ("Driver", interaction.user.mention, True)], discord.Color.orange())

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="end_btn")
    async def end(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.claimed_by is None:
            return await interaction.followup.send("No driver has claimed this ride yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

        button.disabled = True
        msg = interaction.message
        if msg.embeds:
            base = msg.embeds[0]
            ended = discord.Embed(
                title=base.title,
                description=base.description,
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc)
            )
            for f in base.fields:
                ended.add_field(name=f.name, value=f.value, inline=f.inline)
            ended.set_footer(text="Ride ended")
            await interaction.followup.edit_message(message_id=msg.id, embed=ended, view=self)

        await send_audit_embed("Ride Ended", [("Rider", f"<@{self.requester_id}>", True), ("Driver", interaction.user.mention, True)], discord.Color.dark_grey())

# -----------------------------
# /request ride
# -----------------------------
request_group = app_commands.Group(name="request", description="Create ride requests")

@app_commands.choices(
    service_level=[app_commands.Choice(name="Premium", value="Premium"), app_commands.Choice(name="Standard", value="Standard")]
)
@request_group.command(name="ride", description="Request a ride")
@app_commands.describe(starting_location="Pickup location", destination="Destination", service_level="Premium or Standard")
async def request_ride(interaction: discord.Interaction, starting_location: str, destination: str, service_level: app_commands.Choice[str]):
    await interaction.response.send_message("Posting your ride...", ephemeral=True)

    separator = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    e = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description=f"A new ride is waiting to be claimed.\n{separator}",
        color=discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Pickup", value=starting_location, inline=True)
    e.add_field(name="Destination", value=destination, inline=True)
    e.add_field(name="Requested By", value=interaction.user.mention, inline=False)
    e.set_thumbnail(url=interaction.user.display_avatar.url)

    v = ClaimView(requester_id=interaction.user.id)
    ch = bot.get_channel(TARGET_CHANNEL_ID)
    msg = await ch.send(
        content=f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>",
        embed=e,
        view=v,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    thread = await msg.create_thread(name=f"Ride - {interaction.user.display_name}", auto_archive_duration=1440)
    v.thread_id = thread.id
    await thread.send(embed=discord.Embed(description=f"{interaction.user.mention} This thread is for this ride.", color=discord.Color.dark_grey()))
    await interaction.edit_original_response(content="Ride posted successfully.")
    await send_audit_embed("Ride Requested", [("Rider", interaction.user.mention, True), ("Pickup", starting_location, True), ("Destination", destination, True)])

# -----------------------------
# /log-ride
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride")
@app_commands.describe(
    rider="Rider user",
    ride_link="Ride link or reference",
    income="Income for this ride (number)",
    rides_this_week="Number of rides you completed this week (number)",
    comment="Optional rider comment"
)
async def log_ride(
    interaction: discord.Interaction,
    rider: discord.User,
    ride_link: str,
    income: str,
    rides_this_week: str,
    comment: Optional[str] = None
):
    await interaction.response.send_message("Logging ride...", ephemeral=True)
    if not user_has_allowed_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
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
            "rides_this_week": rides_val if rides_val is not None else rides_this_week,
            "comment": (comment or "").strip() or None,
            "ride_link": ride_link
        })
    await save_db()

    log_embed = discord.Embed(
        title="📋 Ride Logged",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(timezone.utc)
    )
    log_embed.add_field(name="Rider", value=rider.mention, inline=True)
    log_embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
    log_embed.add_field(name="Ride Link", value=ride_link, inline=False)
    log_embed.add_field(name="Income", value=f"${income_val:,.2f}" if income_val else income, inline=True)
    log_embed.add_field(name="Rides This Week", value=str(rides_val) if rides_val else rides_this_week, inline=True)
    log_embed.add_field(name="Comment", value=comment or "No comment provided", inline=False)
    log_embed.set_thumbnail(url=rider.display_avatar.url)
    log_embed.set_footer(text=f"Logged on {today_iso()}")

    ch = bot.get_channel(RIDE_LOG_CHANNEL_ID)
    if isinstance(ch, discord.TextChannel):
        await ch.send(embed=log_embed)

    await interaction.edit_original_response(content="Ride logged successfully.")
    await send_audit_embed("Ride Logged", [("Rider", rider.mention, True), ("Driver", interaction.user.mention, True)], discord.Color.dark_grey())

# -----------------------------
# READY + webserver
# -----------------------------
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await send_audit_embed("Lyft Bot Online", [("Bot", bot.user.mention, True)], discord.Color.green())

async def health(_): return web.Response(text="OK")
async def webserver():
    a = web.Application(); a.router.add_get("/", health); a.router.add_get("/health", health)
    r = web.AppRunner(a); await r.setup()
    await web.TCPSite(r, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()

async def main():
    await webserver()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
