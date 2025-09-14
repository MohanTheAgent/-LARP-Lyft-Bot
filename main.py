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

# Ride request posts + roles that are allowed to claim
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749  # driver role 1 (also allowed to use new commands)
ROLE_ID_2 = 1416063969965248594  # driver role 2

# Global audit/activity log
AUDIT_LOG_CHANNEL_ID = 1416392593222270976

# Heads-up roles to ping for allocation/permission requests
PING_ROLE_ADMIN_1 = 1416069791495622707
PING_ROLE_ADMIN_2 = 1416069983942869113

# Destination channels for new commands
ALLOCATION_CHANNEL_ID = 1416425017406914662
PERMISSION_CHANNEL_ID = 1416388268894720020

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

def is_approver(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {PING_ROLE_ADMIN_1, PING_ROLE_ADMIN_2} for r in getattr(member, "roles", []))

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
# Rating buttons (1–5) — thread-based; only rider can press
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
                ("Rating", f"{value}/5", True),
                ("Date", today_iso(), True),
            ],
            color=discord.Color.green(),
        )

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, row=0, custom_id="rate_1")
    async def r1(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, row=0, custom_id="rate_2")
    async def r2(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, row=0, custom_id="rate_3")
    async def r3(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, row=0, custom_id="rate_4")
    async def r4(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.primary, row=0, custom_id="rate_5")
    async def r5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._record(interaction, 5)

# -----------------------------
# Generic Approvals View (Accept / Deny) for allocation/permission posts
# -----------------------------
class ApproveDenyView(discord.ui.View):
    def __init__(self, kind: str, requester_id: int):
        super().__init__(timeout=None)
        self.kind = kind  # 'allocation' or 'permission'
        self.requester_id = requester_id
        self.finalized = False

    async def _guard(self, interaction: discord.Interaction) -> Optional[bool]:
        if not is_approver(interaction.user):
            await interaction.response.send_message("You are not allowed to act on this request.", ephemeral=True)
            return False
        if self.finalized:
            await interaction.response.send_message("This request has already been processed.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, status_text: str):
        self.finalized = True
        for c in self.children:
            c.disabled = True

        # Update embed with Status
        msg = interaction.message
        new_embed = None
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=base.description,
                color=(discord.Color.green() if status_text == "Accepted" else discord.Color.red()),
                timestamp=datetime.now(timezone.utc),
            )
            has_status = False
            for f in base.fields:
                if f.name.strip().lower() == "status":
                    has_status = True
                    new.add_field(name="Status", value=status_text, inline=True)
                else:
                    new.add_field(name=f.name, value=f.value, inline=f.inline)
            if not has_status:
                new.add_field(name="Status", value=status_text, inline=True)
            if base.thumbnail and base.thumbnail.url:
                new.set_thumbnail(url=base.thumbnail.url)
            new_embed = new

        try:
            await interaction.response.edit_message(embed=new_embed, view=self)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(message_id=msg.id, embed=new_embed, view=self)

        # Notify requester in-channel
        await msg.channel.send(f"<@{self.requester_id}> Your {self.kind} request was {status_text.lower()} by {interaction.user.mention}")

        # Audit
        await send_audit_embed(
            f"{self.kind.capitalize()} Request {status_text}",
            fields=[
                ("Action By", named(interaction.user), True),
                ("Requester", f"<@{self.requester_id}>", True),
                ("Channel", f"<#{msg.channel.id}>", True),
                ("Message ID", str(msg.id), True),
                ("Date", today_iso(), True),
            ],
            color=(discord.Color.green() if status_text == "Accepted" else discord.Color.red()),
            thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
        )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="approve_btn")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok = await self._guard(interaction)
        if ok:
            await self._finish(interaction, "Accepted")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="deny_btn")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        ok = await self._guard(interaction)
        if ok:
            await self._finish(interaction, "Denied")

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
                        embed=discord.Embed(
                            title="Driver Feedback",
                            description="Please reply in this thread with any comments about your driver.",
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
# /request ride
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
# /log-ride (store to JSON + log to AUDIT channel only)
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride")
@app_commands.describe(
    rider="Rider user",
    ride_link="Ride link or reference",
    income="Income for this ride (number)",
    rating="Your rating for this ride (number 1–5)",
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

    await send_audit_embed(
        "Ride Logged",
        fields=[
            ("Rider", named(rider), True),
            ("Driver", named(interaction.user), True),
            ("Ride Link", ride_link, False),
            ("Income", f"${income_val:,.2f}" if isinstance(income_val, (int, float)) else str(income), True),
            ("Driver Rating (1–5)", f"{rating_val:.1f}" if isinstance(rating_val, (int, float)) else str(rating), True),
            ("Rides This Week", str(rides_val) if rides_val is not None else rides_this_week, True),
            ("Comment", comment[:1024] if comment else "-", False),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.dark_grey(),
        thumbnail_url=getattr(rider.display_avatar, "url", discord.Embed.Empty),
    )

    await interaction.edit_original_response(content="Ride logged.")

# -----------------------------
# /allocation (with Accept/Deny buttons for approver roles)
# -----------------------------
@tree.command(name="allocation", description="Submit an allocation request")
@app_commands.describe(
    role_recipient="User who will receive role changes",
    roles_to_give="Role(s) to be given (names or IDs, comma separated)",
    roles_to_remove="Role(s) to be removed (names or IDs, comma separated)",
    proof="Proof (URL or description)"
)
async def allocation_request(
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

    ch = bot.get_channel(ALLOCATION_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return await interaction.followup.send("Allocation channel not found.", ephemeral=True)

    emb = discord.Embed(
        title="Allocation Request",
        color=discord.Color.dark_teal(),
        timestamp=datetime.now(timezone.utc),
        description="A driver has submitted an allocation request for review."
    )
    emb.add_field(name="Requested By", value=named(interaction.user), inline=True)
    emb.add_field(name="Recipient", value=named(role_recipient), inline=True)
    emb.add_field(name="Roles to Give", value=roles_to_give or "-", inline=False)
    emb.add_field(name="Roles to Remove", value=roles_to_remove or "-", inline=False)
    emb.add_field(name="Proof", value=proof or "-", inline=False)
    emb.add_field(name="Date", value=today_iso(), inline=True)
    emb.add_field(name="Status", value="Pending", inline=True)
    try:
        emb.set_thumbnail(url=role_recipient.display_avatar.url)
    except Exception:
        pass

    content = f"<@&{PING_ROLE_ADMIN_1}> <@&{PING_ROLE_ADMIN_2}>"
    view = ApproveDenyView(kind="allocation", requester_id=interaction.user.id)
    await ch.send(
        content=content,
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False)
    )

    await send_audit_embed(
        "Allocation Request Logged",
        fields=[
            ("By", named(interaction.user), True),
            ("Recipient", named(role_recipient), True),
            ("Give", roles_to_give or "-", False),
            ("Remove", roles_to_remove or "-", False),
            ("Proof", (proof or "-")[:512], False),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.dark_teal(),
        thumbnail_url=getattr(role_recipient.display_avatar, "url", discord.Embed.Empty),
    )

    await interaction.followup.send("Allocation request sent.", ephemeral=True)

# -----------------------------
# /permission (with Accept/Deny buttons for approver roles)
# -----------------------------
@tree.command(name="permission", description="Submit a permission request")
@app_commands.describe(
    permission="Permission requested",
    duration="Requested duration",
    reason="Reason for the permission",
    signed="Your signature (name/ID)"
)
async def permission_request(
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

    ch = bot.get_channel(PERMISSION_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        return await interaction.followup.send("Permission channel not found.", ephemeral=True)

    emb = discord.Embed(
        title="Permission Request",
        color=discord.Color.dark_gold(),
        timestamp=datetime.now(timezone.utc),
        description="A driver has submitted a permission request for approval."
    )
    emb.add_field(name="Requested By", value=named(interaction.user), inline=True)
    emb.add_field(name="Permission", value=permission or "-", inline=False)
    emb.add_field(name="Duration", value=duration or "-", inline=True)
    emb.add_field(name="Reason", value=reason or "-", inline=False)
    emb.add_field(name="Signed", value=signed or "-", inline=True)
    emb.add_field(name="Date", value=today_iso(), inline=True)
    emb.add_field(name="Status", value="Pending", inline=True)
    try:
        emb.set_thumbnail(url=interaction.user.display_avatar.url)
    except Exception:
        pass

    content = f"<@&{PING_ROLE_ADMIN_1}> <@&{PING_ROLE_ADMIN_2}>"
    view = ApproveDenyView(kind="permission", requester_id=interaction.user.id)
    await ch.send(
        content=content,
        embed=emb,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False)
    )

    await send_audit_embed(
        "Permission Request Logged",
        fields=[
            ("By", named(interaction.user), True),
            ("Permission", permission or "-", False),
            ("Duration", duration or "-", True),
            ("Reason", (reason or "-")[:512], False),
            ("Signed", signed or "-", True),
            ("Date", today_iso(), True),
        ],
        color=discord.Color.dark_gold(),
        thumbnail_url=getattr(interaction.user.display_avatar, "url", discord.Embed.Empty),
    )

    await interaction.followup.send("Permission request sent.", ephemeral=True)

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
