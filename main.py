# -*- coding: utf-8 -*-
import os, json, asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

# =========================
# CONFIG
# =========================
GUILD_ID = 1416057930381262880

# Ride request channel + roles allowed to claim
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749   # driver role 1
ROLE_ID_2 = 1416063969965248594   # driver role 2

# Logs
AUDIT_LOG_CHANNEL_ID = 1416392593222270976         # general audit (kept)
RIDE_LOG_CHANNEL_ID  = 1416342987893375007         # /log-ride posts here
RATING_LOG_CHANNEL_ID = 1416772722981339206        # <— ratings go here

# Admin reviewers (can approve allocation/permission & use /promote, /infract)
REVIEW_ROLE_1 = 1416069791495622707
REVIEW_ROLE_2 = 1416069983942869113

# Allocation / Permission request channels
ALLOCATION_CHANNEL_ID = 1416425017406914662
PERMISSION_CHANNEL_ID = 1416388268894720020

# Promotion / Infraction channels
PROMOTE_CHANNEL_ID   = 1416423535550791730
INFRACT_CHANNEL_ID   = 1416423631474655304

# Static file (logo) served by built-in web server
LOGO_ROUTE = "/logo.png"
TOKEN = os.getenv("DISCORD_TOKEN")

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
SEPARATOR = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"  # exact line you asked for

# =========================
# BOT
# =========================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =========================
# JSON "DB" (riders ratings/logs)
# =========================
_db_lock = asyncio.Lock()
_db: Dict[str, Any] = {"riders": {}}

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

# =========================
# UTILS
# =========================
def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def has_driver_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {ROLE_ID_1, ROLE_ID_2} for r in getattr(member, "roles", []))

def is_reviewer(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {REVIEW_ROLE_1, REVIEW_ROLE_2} for r in getattr(member, "roles", []))

def safe_float(v: str) -> Optional[float]:
    try: return float(v)
    except: return None

def safe_int(v: str) -> Optional[int]:
    try: return int(v)
    except: return None

async def send_embed(
    channel_id: int,
    embed: discord.Embed,
    content: Optional[str] = None,
    allow_roles=False,
    allow_users=False,
    view: Optional[discord.ui.View]=None
):
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return None
    return await ch.send(
        content=content or None,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(
            roles=allow_roles, users=allow_users, everyone=False, replied_user=False
        )
    )

async def audit(title: str, fields: List[tuple], color: discord.Color = discord.Color.blurple()):
    emb = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    for name, value, inline in fields:
        emb.add_field(name=name, value=value, inline=inline)
    await send_embed(AUDIT_LOG_CHANNEL_ID, emb)

# will be filled after web server starts
LOGO_URL: Optional[str] = None

# =========================
# RATING VIEW (1..5) — logs to RATING_LOG_CHANNEL_ID
# =========================
class RatingView(discord.ui.View):
    def __init__(self, rider_id: int, driver_id: int):
        super().__init__(timeout=600)
        self.rider_id = rider_id
        self.driver_id = driver_id
        self.submitted = False

    async def _submit(self, interaction: discord.Interaction, score: int):
        if interaction.user.id != self.rider_id:
            return await interaction.response.send_message("Only the rider can submit a rating for this ride.", ephemeral=True)
        if self.submitted:
            return await interaction.response.send_message("You already submitted a rating. Thank you.", ephemeral=True)

        self.submitted = True
        for c in self.children:
            c.disabled = True

        # Save rating
        async with _db_lock:
            riders = _db.setdefault("riders", {})
            rec = riders.setdefault(str(self.rider_id), {"name": interaction.user.name, "rides": [], "ratings": []})
            rec["name"] = interaction.user.name
            rec.setdefault("ratings", []).append({
                "date": today_iso(),
                "driver_id": self.driver_id,
                "score": score
            })
        await save_db()

        # Edit message
        msg = interaction.message
        if msg and msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=f"Thanks. You rated your driver {score}/5.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            for f in base.fields:
                new.add_field(name=f.name, value=f.value, inline=f.inline)
            await interaction.response.edit_message(embed=new, view=self)
        else:
            await interaction.response.edit_message(view=self)

        # Post rating log (to rating log channel)
        log = discord.Embed(
            title="Ride Rating Submitted",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        log.add_field(name="Rider", value=f"<@{self.rider_id}>", inline=True)
        log.add_field(name="Driver", value=f"<@{self.driver_id}>", inline=True)
        log.add_field(name="Score", value=f"{score}/5", inline=True)
        log.add_field(name="Date", value=today_iso(), inline=True)
        await send_embed(RATING_LOG_CHANNEL_ID, log)

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def r1(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 1)
    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def r2(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 2)
    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def r3(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 3)
    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def r4(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 4)
    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, custom_id="rate_5")
    async def r5(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 5)

# =========================
# CLAIM / END VIEW
# =========================
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, thread_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.claimed_by: Optional[int] = None
        self._lock = asyncio.Lock()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="ride_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self._lock:
            if not has_driver_role(interaction.user):
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
                    timestamp=datetime.now(timezone.utc),
                )
                for f in base.fields:
                    if f.name.strip().lower() in {"driver", "status"}:
                        continue
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
                new.add_field(name="Driver", value=interaction.user.mention, inline=True)
                new.add_field(name="Status", value="🟢 Claimed / Ongoing", inline=True)
                if base.thumbnail and base.thumbnail.url:
                    new.set_thumbnail(url=base.thumbnail.url)
                new.set_footer(text="Ride claimed")
                await interaction.followup.edit_message(message_id=msg.id, embed=new, view=self)

            assigned = discord.Embed(
                title="Driver Assigned",
                description=f"Your driver is {interaction.user.mention}.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            assigned.add_field(name="Rider", value=f"<@{self.requester_id}>", inline=True)
            assigned.add_field(name="Driver", value=interaction.user.mention, inline=True)
            await send_embed(
                TARGET_CHANNEL_ID,
                assigned,
                content=f"<@{self.requester_id}>",
                allow_users=True
            )

            await audit(
                "Ride Claimed",
                [("Rider", f"<@{self.requester_id}>", True), ("Driver", interaction.user.mention, True)],
                color=discord.Color.orange()
            )

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="ride_end")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.claimed_by is None:
            return await interaction.followup.send("This ride has not been claimed yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

        button.disabled = True

        # Update original embed to completed
        msg = interaction.message
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=base.description,
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc),
            )
            status_replaced = False
            for f in base.fields:
                if f.name.strip().lower() == "status":
                    new.add_field(name="Status", value="🔴 Completed", inline=True)
                    status_replaced = True
                else:
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
            if not status_replaced:
                new.add_field(name="Status", value="🔴 Completed", inline=True)
            new.set_footer(text="Ride ended")
            await interaction.followup.edit_message(message_id=msg.id, embed=new, view=self)

        ended = discord.Embed(
            title="Ride Completed",
            description=f"Ride ended by {interaction.user.mention}.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(timezone.utc),
        )
        ended.add_field(name="Rider", value=f"<@{self.requester_id}>", inline=True)
        ended.add_field(name="Driver", value=interaction.user.mention, inline=True)
        await send_embed(TARGET_CHANNEL_ID, ended)

        await audit(
            "Ride Ended",
            [("Rider", f"<@{self.requester_id}>", True), ("Driver", interaction.user.mention, True)],
            color=discord.Color.dark_grey()
        )

        # Rating prompt in thread (or fallback)
        rating_embed = discord.Embed(
            title="Rate Your Driver",
            description="How much do you rate your driver?",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        rating_embed.add_field(name="\u200b", value=SEPARATOR, inline=False)
        rating_view = RatingView(rider_id=self.requester_id, driver_id=interaction.user.id)

        thread_chan = None
        if self.thread_id:
            thread_chan = bot.get_channel(self.thread_id)
            if thread_chan is None:
                try:
                    thread_chan = await bot.fetch_channel(self.thread_id)
                except Exception:
                    thread_chan = None

        if isinstance(thread_chan, discord.Thread):
            await thread_chan.send(
                content=f"<@{self.requester_id}>",
                embed=rating_embed,
                view=rating_view,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
        else:
            await send_embed(
                TARGET_CHANNEL_ID,
                rating_embed,
                content=f"<@{self.requester_id}>",
                allow_users=True,
                view=rating_view
            )

# =========================
# /request ride
# =========================
request_group = app_commands.Group(name="request", description="Create ride requests")

@app_commands.choices(service_level=[
    app_commands.Choice(name="Premium", value="Premium"),
    app_commands.Choice(name="Standard", value="Standard"),
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

    color = discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue()
    e = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description=f"L y f t  R i d e  R e q u e s t\n{SEPARATOR}",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="Pickup", value=starting_location, inline=True)
    e.add_field(name="Destination", value=destination, inline=True)
    e.add_field(name="Status", value="🟡 Unclaimed", inline=True)
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

    msg = await ch.send(
        content=f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>",
        embed=e,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

    # Create a thread for the ride
    try:
        t = await msg.create_thread(name=f"Ride - {interaction.user.display_name}", auto_archive_duration=1440)
        view.thread_id = t.id
        intro = discord.Embed(
            title="Ride Thread",
            description=f"{interaction.user.mention}\nUse this thread to coordinate your ride.",
            color=discord.Color.dark_grey()
        )
        await t.send(embed=intro)
    except Exception:
        pass

    await interaction.edit_original_response(content="Ride posted successfully.")
    await audit(
        "Ride Requested",
        [("Rider", interaction.user.mention, True),
         ("Pickup", starting_location, True),
         ("Destination", destination, True),
         ("Service", service_level.value, True)],
        color=color
    )

# =========================
# /log-ride -> RIDE_LOG_CHANNEL_ID
# =========================
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

    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command is not available in this server.")
    if not has_driver_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
    rides_val = safe_int(rides_this_week)

    async with _db_lock:
        riders = _db.setdefault("riders", {})
        rec = riders.setdefault(str(rider.id), {"name": rider.name, "rides": [], "ratings": []})
        rec["name"] = rider.name
        rec["rides"].append({
            "date": today_iso(),
            "driver_id": interaction.user.id,
            "driver_name": getattr(interaction.user, "display_name", interaction.user.name),
            "income": income_val if income_val is not None else income,
            "rides_this_week": rides_val if rides_val is not None else rides_this_week,
            "comment": (comment or "").strip() or None,
            "ride_link": ride_link
        })
    await save_db()

    emb = discord.Embed(
        title="Ride Logged",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Rider", value=rider.mention, inline=True)
    emb.add_field(name="Driver", value=interaction.user.mention, inline=True)
    emb.add_field(name="Ride Link", value=ride_link, inline=False)
    emb.add_field(name="Income", value=(f"${income_val:,.2f}" if income_val is not None else income), inline=True)
    emb.add_field(name="Rides This Week", value=(str(rides_val) if rides_val is not None else rides_this_week), inline=True)
    emb.add_field(name="Comment", value=(comment or "-"), inline=False)
    emb.set_thumbnail(url=rider.display_avatar.url)
    emb.set_footer(text=f"Date: {today_iso()}")

    await send_embed(RIDE_LOG_CHANNEL_ID, emb)
    await interaction.edit_original_response(content="Ride logged successfully.")
    await audit("Ride Logged", [("Rider", rider.mention, True), ("Driver", interaction.user.mention, True)], color=discord.Color.dark_grey())

# =========================
# ALLOCATION / PERMISSION with Approve/Deny
# =========================
class ApproveDenyView(discord.ui.View):
    def __init__(self, kind: str, requester_id: int):
        super().__init__(timeout=None)
        self.kind = kind
        self.requester_id = requester_id
        self.finalized = False

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not is_reviewer(interaction.user):
            await interaction.response.send_message("You are not allowed to act on this request.", ephemeral=True)
            return False
        if self.finalized:
            await interaction.response.send_message("This request has already been processed.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, decision: str, symbol: str, color: discord.Color):
        self.finalized = True
        for c in self.children: c.disabled = True

        msg = interaction.message
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=base.description,
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            had_status = False
            for f in base.fields:
                if f.name.strip().lower() == "status":
                    had_status = True
                    new.add_field(name="Status", value=f"{symbol} {decision}", inline=True)
                else:
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
            if not had_status:
                new.add_field(name="Status", value=f"{symbol} {decision}", inline=True)
            try:
                await interaction.response.edit_message(embed=new, view=self)
            except discord.InteractionResponded:
                await interaction.followup.edit_message(message_id=msg.id, embed=new, view=self)

        dec = discord.Embed(
            title=f"{self.kind.capitalize()} Request {decision}",
            description=f"{self.kind.capitalize()} request was {decision.lower()} by {interaction.user.mention}.",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        dec.add_field(name="Requester", value=f"<@{self.requester_id}>", inline=True)
        dec.add_field(name="Reviewed By", value=interaction.user.mention, inline=True)
        await msg.channel.send(
            content=f"<@{self.requester_id}>",
            embed=dec,
            allowed_mentions=discord.AllowedMentions(users=True)
        )

        await audit(
            f"{self.kind.capitalize()} Request {decision}",
            [("Requester", f"<@{self.requester_id}>", True), ("Reviewed By", interaction.user.mention, True)],
            color=color
        )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="approve_accept")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self._finish(interaction, "Accepted", "🟢", discord.Color.green())

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="approve_deny")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self._finish(interaction, "Denied", "🔴", discord.Color.red())

@tree.command(name="allocation", description="Submit an allocation request")
@app_commands.describe(
    role_recipient="User who will receive role changes",
    roles_to_give="Roles to give (names/IDs, comma separated)",
    roles_to_remove="Roles to remove (names/IDs, comma separated)",
    proof="Proof (URL or description)"
)
async def allocation(
    interaction: discord.Interaction,
    role_recipient: discord.User,
    roles_to_give: str,
    roles_to_remove: str,
    proof: str
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not any(getattr(r, "id", None) == ROLE_ID_1 for r in getattr(interaction.user, "roles", [])):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.send_message("Submitting allocation request...", ephemeral=True)

    emb = discord.Embed(
        title="Allocation Request",
        description=f"{SEPARATOR}\nA driver has submitted an allocation request for review.\n{SEPARATOR}",
        color=discord.Color.dark_teal(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Requested By", value=interaction.user.mention, inline=True)
    emb.add_field(name="Recipient", value=role_recipient.mention, inline=True)
    emb.add_field(name="Roles to Give", value=roles_to_give or "-", inline=False)
    emb.add_field(name="Roles to Remove", value=roles_to_remove or "-", inline=False)
    emb.add_field(name="Proof", value=proof or "-", inline=False)
    emb.add_field(name="Status", value="🟡 Pending", inline=True)
    emb.add_field(name="Date", value=today_iso(), inline=True)

    content = f"<@&{REVIEW_ROLE_1}> <@&{REVIEW_ROLE_2}>"
    view = ApproveDenyView(kind="allocation", requester_id=interaction.user.id)
    await send_embed(ALLOCATION_CHANNEL_ID, emb, content=content, allow_roles=True, view=view)

    await audit(
        "Allocation Request Logged",
        [("By", interaction.user.mention, True), ("Recipient", role_recipient.mention, True)],
        color=discord.Color.dark_teal()
    )
    await interaction.followup.send("Allocation request sent.", ephemeral=True)

@tree.command(name="permission", description="Submit a permission request")
@app_commands.describe(
    permission="Permission requested",
    duration="Requested duration",
    reason="Reason",
    signed="Signature (name/ID)"
)
async def permission(
    interaction: discord.Interaction,
    permission: str,
    duration: str,
    reason: str,
    signed: str
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not any(getattr(r, "id", None) == ROLE_ID_1 for r in getattr(interaction.user, "roles", [])):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.send_message("Submitting permission request...", ephemeral=True)

    emb = discord.Embed(
        title="Permission Request",
        description=f"{SEPARATOR}\nA driver has submitted a permission request for approval.\n{SEPARATOR}",
        color=discord.Color.dark_gold(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Requested By", value=interaction.user.mention, inline=True)
    emb.add_field(name="Permission", value=permission or "-", inline=False)
    emb.add_field(name="Duration", value=duration or "-", inline=True)
    emb.add_field(name="Reason", value=reason or "-", inline=False)
    emb.add_field(name="Signed", value=signed or "-", inline=True)
    emb.add_field(name="Status", value="🟡 Pending", inline=True)
    emb.add_field(name="Date", value=today_iso(), inline=True)

    content = f"<@&{REVIEW_ROLE_1}> <@&{REVIEW_ROLE_2}>"
    view = ApproveDenyView(kind="permission", requester_id=interaction.user.id)
    await send_embed(PERMISSION_CHANNEL_ID, emb, content=content, allow_roles=True, view=view)

    await audit(
        "Permission Request Logged",
        [("By", interaction.user.mention, True), ("Permission", permission, True), ("Duration", duration, True)],
        color=discord.Color.dark_gold()
    )
    await interaction.followup.send("Permission request sent.", ephemeral=True)

# =========================
# /promote (reviewers only) with exact lines + logo + DM + ping on top
# =========================
@tree.command(name="promote", description="Lyft Promotion record (reviewers only)")
@app_commands.describe(
    employee="User being promoted",
    old_rank="Previous rank",
    new_rank="New rank",
    reason="Reason for promotion",
    notes="Optional notes"
)
async def promote(
    interaction: discord.Interaction,
    employee: discord.User,
    old_rank: str,
    new_rank: str,
    reason: str,
    notes: Optional[str] = None
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not is_reviewer(interaction.user):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.send_message("Promotion logged.", ephemeral=True)

    desc = (
        "Lyft Promotion Log!\n"
        f"{SEPARATOR}\n\n"
        f"Employee: {employee.mention}\n\n"
        f"Old Rank: {old_rank}\n\n"
        f"New Rank: {new_rank}\n"
        f"{SEPARATOR}\n"
        f"Reason: {reason}\n"
        f"Notes: {notes or 'N/A'}\n"
        f"{SEPARATOR}\n"
        f"Processed by: {interaction.user.mention}"
    )
    emb = discord.Embed(description=desc, color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    if LOGO_URL:
        emb.set_thumbnail(url=LOGO_URL)

    await send_embed(PROMOTE_CHANNEL_ID, emb, content=employee.mention, allow_users=True)

    try:
        await employee.send(embed=emb)
    except discord.Forbidden:
        await audit("Promotion DM Failed", [("Employee", employee.mention, True)], color=discord.Color.red())

# =========================
# /infract (reviewers only) with exact lines + logo + DM + ping on top
# =========================
INFRACTION_CHOICES = [
    app_commands.Choice(name="Notice", value="Notice"),
    app_commands.Choice(name="Warning", value="Warning"),
    app_commands.Choice(name="Strike", value="Strike"),
    app_commands.Choice(name="Demotion", value="Demotion"),
    app_commands.Choice(name="Suspension", value="Suspension"),
    app_commands.Choice(name="Termination", value="Termination"),
    app_commands.Choice(name="Blacklist", value="Blacklist"),
]

@tree.command(name="infract", description="Lyft Infraction record (reviewers only)")
@app_commands.describe(
    employee="User receiving infraction",
    infraction_type="Type of infraction",
    reason="Reason",
    proof="Proof links or description",
    notes="Optional notes",
    appealable="Appealable? yes/no (optional)"
)
@app_commands.choices(infraction_type=INFRACTION_CHOICES)
async def infract(
    interaction: discord.Interaction,
    employee: discord.User,
    infraction_type: app_commands.Choice[str],
    reason: str,
    proof: Optional[str] = None,
    notes: Optional[str] = None,
    appealable: Optional[str] = None
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not is_reviewer(interaction.user):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.send_message("Infraction logged.", ephemeral=True)

    appeal_display = "Yes" if (appealable or "").lower().startswith("y") else "No"

    desc = (
        "Lyft Infraction Log!\n"
        f"{SEPARATOR}\n\n"
        f"Officer: {interaction.user.mention}\n\n"
        f"Reason: {reason}\n\n"
        f"Infraction Type: {infraction_type.value}\n\n"
        f"Appealable: {appeal_display}\n\n"
        f"Proof: {proof or 'N/A'}\n"
        f"Notes: {notes or 'N/A'}\n"
        f"{SEPARATOR}\n"
        f"Issued to: {employee.mention}"
    )
    emb = discord.Embed(description=desc, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
    if LOGO_URL:
        emb.set_thumbnail(url=LOGO_URL)

    await send_embed(INFRACT_CHANNEL_ID, emb, content=employee.mention, allow_users=True)

    try:
        await employee.send(embed=emb)
    except discord.Forbidden:
        await audit("Infraction DM Failed", [("Employee", employee.mention, True)], color=discord.Color.red())

# =========================
# READY + SYNC + WEB (serve LYFT.png at /logo.png)
# =========================
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# Web server to serve logo and health
async def handle_logo(request):
    filepath = os.path.join(os.path.dirname(__file__), "LYFT.png")
    return web.FileResponse(filepath)

async def handle_health(_):
    return web.Response(text="OK")

async def start_web_server():
    global LOGO_URL
    app = web.Application()
    app.router.add_get(LOGO_ROUTE, handle_logo)
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    host = os.getenv("RENDER_EXTERNAL_URL")
    if host:
        if not host.startswith("http"):
            host = "https://" + host
        LOGO_URL = host.rstrip("/") + LOGO_ROUTE
    else:
        LOGO_URL = f"http://localhost:{port}{LOGO_ROUTE}"
    print(f"HTTP server listening on 0.0.0.0:{port} | LOGO_URL={LOGO_URL}")

async def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN")
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
