# -*- coding: utf-8 -*-
# FULL main.py — Lyft RP Bot (with /blacklist added)
#
# Features kept intact:
# - Guild restricted (GUILD_ID)
# - /request ride  -> posts request, pings driver roles, creates thread, Claim / End buttons
#       * On End: posts Ride Log in RIDE_LOG_CHANNEL_ID (PING DRIVER ONLY)
#       * Posts 1–5 rating prompt in the ride thread; selecting a rating updates the Ride Log "Rating" field
# - /ride start (in-game) -> dashboard message in INGAME_RIDES_CHANNEL_ID with End button
#       * On End: logs to INGAME_RIDE_LOG_CHANNEL_ID (PING DRIVER ONLY) and deletes the dashboard if last ongoing
# - /allocation and /permission -> create requests in their channels with Accept/Deny buttons (reviewers only click)
# - /promote and /infract -> reviewer-only, orange/red embeds with logo thumbnail, white separators, DM the target + ping on top
# - /log-ride -> TEMP DISABLED (kept as stub)
# - /suggest -> citizen-only suggestions with Up/Down/List Voters buttons and auto thread
# - NEW: /blacklist -> posts a blacklist announcement embed to BLACKLIST_CHANNEL_ID (role-gated)
# - Tiny HTTP server for Render health + serving LYFT.png as thumbnail

import os, json, asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Set

import discord
from discord import app_commands
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ------------------- CONFIG -------------------
GUILD_ID = 1416057930381262880

# Drivers
ROLE_ID_1 = 1416068902609223749
ROLE_ID_2 = 1416063969965248594
DRIVER_ROLES = {ROLE_ID_1, ROLE_ID_2}

# Reviewers (can approve/deny + use promote/infract)
REVIEW_ROLE_1 = 1416069791495622707
REVIEW_ROLE_2 = 1416069983942869113

# Citizen role for /suggest
CITIZEN_ROLE_ID = 1416066285216727072

# Channels
TARGET_CHANNEL_ID             = 1416334665958166560  # /request ride posts + status
RIDE_LOG_CHANNEL_ID           = 1416342987893375007  # auto log for /request ride End
RATING_LOG_CHANNEL_ID         = 1416772722981339206  # ratings audit
AUDIT_LOG_CHANNEL_ID          = 1416392593222270976  # general audit

ALLOCATION_CHANNEL_ID         = 1416425017406914662
PERMISSION_CHANNEL_ID         = 1416388268894720020
PROMOTE_CHANNEL_ID            = 1416423535550791730
INFRACT_CHANNEL_ID            = 1416423631474655304

INGAME_RIDES_CHANNEL_ID       = 1416777579905683557  # in-game dashboards
INGAME_RIDE_LOG_CHANNEL_ID    = 1416342987893375007  # in-game logs

SUGGESTIONS_CHANNEL_ID        = 1417470220276207636  # /suggest

# Blacklist
BLACKLIST_CHANNEL_ID          = 1419171827435049053
BLACKLISTER_ROLE_ID           = 1416069983942869113  # only this role can use /blacklist

# Render web server
PORT = int(os.getenv("PORT", "10000"))
LOGO_ROUTE = "/logo.png"
LOGO_URL: Optional[str] = None

SEPARATOR = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

# ------------------- BOT -------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ------------------- UTIL -------------------
def now_utc():
    return datetime.now(timezone.utc)

def today_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def has_driver_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in DRIVER_ROLES for r in getattr(member, "roles", []))

def is_reviewer(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {REVIEW_ROLE_1, REVIEW_ROLE_2} for r in getattr(member, "roles", []))

def has_citizen_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) == CITIZEN_ROLE_ID for r in getattr(member, "roles", []))

async def send_embed(
    channel_id: int,
    embed: discord.Embed,
    content: Optional[str] = None,
    allow_roles=False,
    allow_users=False,
    view: Optional[discord.ui.View] = None
):
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        try:
            ch = await bot.fetch_channel(channel_id)  # type: ignore
        except Exception:
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
    emb = discord.Embed(title=title, color=color, timestamp=now_utc())
    for name, value, inline in fields:
        emb.add_field(name=name, value=value, inline=inline)
    await send_embed(AUDIT_LOG_CHANNEL_ID, emb)

# ------------------- RATINGS (1..5) -------------------
class RatingView(discord.ui.View):
    def __init__(self, rider_id: int, driver_id: int, log_channel_id: Optional[int], log_message_id: Optional[int]):
        super().__init__(timeout=600)
        self.rider_id = rider_id
        self.driver_id = driver_id
        self.log_channel_id = log_channel_id
        self.log_message_id = log_message_id
        self.submitted = False

    async def _update_log_rating(self, score_str: str):
        if not (self.log_channel_id and self.log_message_id):
            return
        ch = bot.get_channel(self.log_channel_id)
        if not isinstance(ch, discord.TextChannel):
            try:
                ch = await bot.fetch_channel(self.log_channel_id)  # type: ignore
            except Exception:
                return
        try:
            msg = await ch.fetch_message(self.log_message_id)  # type: ignore
        except Exception:
            return
        if not msg.embeds:
            return
        base = msg.embeds[0]
        new = discord.Embed(
            title=base.title, description=base.description,
            color=discord.Color.green(), timestamp=now_utc()
        )
        replaced = False
        for f in base.fields:
            if f.name.strip().lower() == "rating":
                new.add_field(name="Rating", value=score_str, inline=True)
                replaced = True
            else:
                new.add_field(name=f.name, value=f.value, inline=f.inline)
        if not replaced:
            new.add_field(name="Rating", value=score_str, inline=True)
        if base.thumbnail and base.thumbnail.url:
            new.set_thumbnail(url=base.thumbnail.url)
        if base.footer and base.footer.text:
            new.set_footer(text=base.footer.text)
        await msg.edit(embed=new)

    async def _submit(self, interaction: discord.Interaction, score: int):
        if interaction.user.id != self.rider_id:
            return await interaction.response.send_message("Only the rider can submit this rating.", ephemeral=True)
        if self.submitted:
            return await interaction.response.send_message("Rating already submitted. Thank you.", ephemeral=True)

        self.submitted = True
        for c in self.children: c.disabled = True

        base = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        new = discord.Embed(
            title="Thanks for your feedback!",
            description=f"You rated your driver {score}/5.",
            color=discord.Color.green(), timestamp=now_utc()
        )
        if base:
            for f in base.fields:
                new.add_field(name=f.name, value=f.value, inline=f.inline)
        await interaction.response.edit_message(embed=new, view=self)

        await self._update_log_rating(f"{score}/5")
        log = discord.Embed(title="Ride Rating Submitted", color=discord.Color.green(), timestamp=now_utc())
        log.add_field(name="Rider", value=f"<@{self.rider_id}>", inline=True)
        log.add_field(name="Driver", value=f"<@{self.driver_id}>", inline=True)
        log.add_field(name="Score", value=f"{score}/5", inline=True)
        log.add_field(name="Date", value=today_iso(), inline=True)
        await send_embed(RATING_LOG_CHANNEL_ID, log)

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, custom_id="rate_1")
    async def b1(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 1)
    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, custom_id="rate_2")
    async def b2(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 2)
    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, custom_id="rate_3")
    async def b3(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 3)
    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, custom_id="rate_4")
    async def b4(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 4)
    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, custom_id="rate_5")
    async def b5(self, i: discord.Interaction, _: discord.ui.Button): await self._submit(i, 5)

# ------------------- REQUEST RIDE VIEW -------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, thread_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.thread_id = thread_id
        self.claimed_by: Optional[int] = None
        self._lock = asyncio.Lock()
        self._log_message_id: Optional[int] = None

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
                    title=base.title, description=base.description,
                    color=base.color, timestamp=now_utc()
                )
                for f in base.fields:
                    nm = f.name.strip().lower()
                    if nm in {"driver", "status"}: continue
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
                color=discord.Color.green(), timestamp=now_utc()
            )
            assigned.add_field(name="Rider", value=f"<@{self.requester_id}>", inline=True)
            assigned.add_field(name="Driver", value=interaction.user.mention, inline=True)
            await send_embed(TARGET_CHANNEL_ID, assigned, content=f"<@{self.requester_id}>", allow_users=True)

            await audit("Ride Claimed",
                        [("Rider", f"<@{self.requester_id}>", True),
                         ("Driver", interaction.user.mention, True)],
                        color=discord.Color.orange())

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="ride_end")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.claimed_by is None:
            return await interaction.followup.send("This ride has not been claimed yet.", ephemeral=True)
        if interaction.user.id != self.claimed_by:
            return await interaction.followup.send("Only the driver who claimed this ride can end it.", ephemeral=True)

        button.disabled = True

        msg = interaction.message
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title, description=base.description,
                color=discord.Color.dark_grey(), timestamp=now_utc()
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

        orig = msg.embeds[0] if msg and msg.embeds else None
        pickup = destination = service = "N/A"
        rider_mention = f"<@{self.requester_id}>"
        if orig:
            for f in orig.fields:
                nm = f.name.strip().lower()
                if nm == "pickup": pickup = f.value
                elif nm == "destination": destination = f.value
                elif nm == "service": service = f.value

        log_embed = discord.Embed(
            title="Ride Log",
            description="Ride completed and logged automatically.",
            color=discord.Color.dark_grey(), timestamp=now_utc()
        )
        log_embed.add_field(name="Rider", value=rider_mention, inline=True)
        log_embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Pickup", value=pickup, inline=True)
        log_embed.add_field(name="Destination", value=destination, inline=True)
        log_embed.add_field(name="Service", value=service, inline=True)
        log_embed.add_field(name="Rating", value="N/A", inline=True)
        log_embed.set_footer(text=f"Date: {today_iso()}")

        log_msg = await send_embed(
            RIDE_LOG_CHANNEL_ID, log_embed,
            content=interaction.user.mention,  # ping DRIVER only
            allow_users=True
        )
        self._log_message_id = getattr(log_msg, "id", None)

        done = discord.Embed(
            title="Ride Completed",
            description=f"Ride ended by {interaction.user.mention}.",
            color=discord.Color.dark_grey(), timestamp=now_utc()
        )
        done.add_field(name="Rider", value=rider_mention, inline=True)
        done.add_field(name="Driver", value=interaction.user.mention, inline=True)
        await send_embed(TARGET_CHANNEL_ID, done)

        await audit("Ride Ended",
                    [("Rider", rider_mention, True),
                     ("Driver", interaction.user.mention, True)],
                    color=discord.Color.dark_grey())

        rating_embed = discord.Embed(
            title="Rate Your Driver",
            description="How much do you rate your driver?",
            color=discord.Color.blurple(), timestamp=now_utc()
        )
        rating_embed.add_field(name="\u200b", value=SEPARATOR, inline=False)
        rating_view = RatingView(
            rider_id=self.requester_id,
            driver_id=interaction.user.id,
            log_channel_id=RIDE_LOG_CHANNEL_ID,
            log_message_id=self._log_message_id
        )

        thread_chan = None
        if self.thread_id:
            thread_chan = bot.get_channel(self.thread_id) or await bot.fetch_channel(self.thread_id)
        if isinstance(thread_chan, discord.Thread):
            await thread_chan.send(
                content=rider_mention,
                embed=rating_embed,
                view=rating_view,
                allowed_mentions=discord.AllowedMentions(users=True)
            )
        else:
            await send_embed(
                TARGET_CHANNEL_ID, rating_embed,
                content=rider_mention, allow_users=True, view=rating_view
            )

# ------------------- /request ride -------------------
request_group = app_commands.Group(name="request", description="Create service requests")

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
        color=color, timestamp=now_utc()
    )
    e.add_field(name="Pickup", value=starting_location, inline=True)
    e.add_field(name="Destination", value=destination, inline=True)
    e.add_field(name="Service", value=service_level.value, inline=True)
    e.add_field(name="Status", value="🟡 Unclaimed", inline=True)
    e.add_field(name="Requested By", value=interaction.user.mention, inline=False)
    e.set_thumbnail(url=interaction.user.display_avatar.url)
    e.set_footer(text="Click Claim to accept this ride")

    view = ClaimView(requester_id=interaction.user.id)

    ch = bot.get_channel(TARGET_CHANNEL_ID) or await bot.fetch_channel(TARGET_CHANNEL_ID)
    msg = await ch.send(
        content=f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>",
        embed=e, view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

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
    await audit("Ride Requested",
                [("Rider", interaction.user.mention, True),
                 ("Pickup", starting_location, True),
                 ("Destination", destination, True),
                 ("Service", service_level.value, True)], color=color)

# ------------------- /log-ride (disabled) -------------------
@tree.command(name="log-ride", description="Log a completed ride (temporarily disabled)")
async def log_ride_disabled(interaction: discord.Interaction):
    return await interaction.response.send_message(
        "This command is temporarily disabled. Ride logs are posted automatically when the driver ends a ride.",
        ephemeral=True
    )

# ------------------- Approve / Deny view -------------------
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
                title=base.title, description=base.description,
                color=color, timestamp=now_utc()
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
            color=color, timestamp=now_utc()
        )
        dec.add_field(name="Requester", value=f"<@{self.requester_id}>", inline=True)
        dec.add_field(name="Reviewed By", value=interaction.user.mention, inline=True)
        await msg.channel.send(
            content=f"<@{self.requester_id}>",
            embed=dec,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await audit(f"{self.kind.capitalize()} Request {decision}",
                    [("Requester", f"<@{self.requester_id}>", True),
                     ("Reviewed By", interaction.user.mention, True)], color=color)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="approve_accept")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self._finish(interaction, "Accepted", "🟢", discord.Color.green())

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="approve_deny")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        if await self._guard(interaction):
            await self._finish(interaction, "Denied", "🔴", discord.Color.red())

# ------------------- /allocation -------------------
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
        color=discord.Color.dark_teal(), timestamp=now_utc()
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
    await interaction.followup.send("Allocation request sent.", ephemeral=True)

# ------------------- /permission -------------------
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
        color=discord.Color.dark_gold(), timestamp=now_utc()
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
    await interaction.followup.send("Permission request sent.", ephemeral=True)

# ------------------- /promote (reviewers only) -------------------
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
    emb = discord.Embed(description=desc, color=discord.Color.green(), timestamp=now_utc())
    if LOGO_URL:
        emb.set_thumbnail(url=LOGO_URL)

    await send_embed(PROMOTE_CHANNEL_ID, emb, content=employee.mention, allow_users=True)
    try:
        await employee.send(embed=emb)
    except discord.Forbidden:
        await audit("Promotion DM Failed", [("Employee", employee.mention, True)], color=discord.Color.red())

# ------------------- /infract (reviewers only) -------------------
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
    emb = discord.Embed(description=desc, color=discord.Color.red(), timestamp=now_utc())
    if LOGO_URL:
        emb.set_thumbnail(url=LOGO_URL)

    await send_embed(INFRACT_CHANNEL_ID, emb, content=employee.mention, allow_users=True)
    try:
        await employee.send(embed=emb)
    except discord.Forbidden:
        await audit("Infraction DM Failed", [("Employee", employee.mention, True)], color=discord.Color.red())

# ------------------- In-Game /ride start -------------------
ongoing_message_ids: Set[int] = set()
ongoing_lock = asyncio.Lock()

class IngameRideView(discord.ui.View):
    def __init__(self, driver_id: int, rider_name: str, pickup: str, destination: str, username: str, price_estimate: str, notes: str):
        super().__init__(timeout=None)
        self.driver_id = driver_id
        self.rider_name = rider_name
        self.pickup = pickup
        self.destination = destination
        self.username = username
        self.price_estimate = price_estimate
        self.notes = notes
        self._lock = asyncio.Lock()

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="ingame:end_ride")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if interaction.user.id != self.driver_id:
            return await interaction.followup.send("Only the driver who started this in-game ride can end it.", ephemeral=True)

        async with self._lock:
            message = interaction.message
            if message is None:
                return

            log_channel = interaction.client.get_channel(INGAME_RIDE_LOG_CHANNEL_ID) or await interaction.client.fetch_channel(INGAME_RIDE_LOG_CHANNEL_ID)
            log_embed = discord.Embed(title="In-Game Ride Log", color=discord.Color.dark_grey(), timestamp=now_utc())
            log_embed.add_field(name="Driver", value=interaction.user.mention, inline=True)
            log_embed.add_field(name="Rider Name", value=self.rider_name, inline=True)
            log_embed.add_field(name="Username", value=self.username, inline=True)
            log_embed.add_field(name="Pick-Up", value=self.pickup, inline=False)
            log_embed.add_field(name="Destination", value=self.destination, inline=False)
            if self.price_estimate:
                log_embed.add_field(name="Price Estimate", value=self.price_estimate, inline=True)
            if self.notes:
                log_embed.add_field(name="Notes", value=self.notes, inline=False)
            log_embed.set_footer(text=f"Date: {today_iso()}")

            if isinstance(log_channel, (discord.TextChannel, discord.Thread)):
                await log_channel.send(
                    content=interaction.user.mention,
                    embed=log_embed,
                    allowed_mentions=discord.AllowedMentions(roles=False, users=True, everyone=False, replied_user=False)
                )

            async with ongoing_lock:
                ongoing_message_ids.discard(message.id)
                no_other_ongoing = len(ongoing_message_ids) == 0

            if no_other_ongoing:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            else:
                button.disabled = True
                if message.embeds:
                    base = message.embeds[0]
                    new = discord.Embed(
                        title=base.title or "In-Game Ride",
                        description=base.description or "",
                        color=discord.Color.dark_grey(),
                        timestamp=now_utc()
                    )
                    had_status = False
                    for f in base.fields:
                        if f.name.strip().lower() == "status":
                            had_status = True
                            new.add_field(name="Status", value="Completed", inline=True)
                        else:
                            new.add_field(name=f.name, value=f.value, inline=f.inline)
                    if not had_status:
                        new.add_field(name="Status", value="Completed", inline=True)
                    if base.thumbnail and base.thumbnail.url:
                        new.set_thumbnail(url=base.thumbnail.url)
                    if base.footer and base.footer.text:
                        new.set_footer(text=base.footer.text)
                    try:
                        await interaction.followup.edit_message(message_id=message.id, embed=new, view=self)
                    except discord.HTTPException:
                        pass

ride_group = app_commands.Group(name="ride", description="Driver in-game ride actions")

@ride_group.command(name="start", description="Start an in-game ride (for non-Discord riders)")
@app_commands.describe(
    rider_name="Passenger name/callsign",
    pickup="Pick-Up location",
    destination="Destination",
    username="Rider's username (in-game)",
    price_estimate="Quoted price / estimate",
    notes="Extra info"
)
async def ride_start(
    interaction: discord.Interaction,
    rider_name: str,
    pickup: str,
    destination: str,
    username: str,
    price_estimate: str = "",
    notes: str = ""
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command isn't available here.", ephemeral=True)
    if not has_driver_role(interaction.user):
        return await interaction.response.send_message("Drivers only.", ephemeral=True)

    await interaction.response.send_message("In-game ride started.", ephemeral=True)

    emb = discord.Embed(
        title="In-Game Ride",
        description="Ride created for a rider outside Discord.",
        color=discord.Color.teal(), timestamp=now_utc()
    )
    emb.add_field(name="Rider Name", value=rider_name, inline=True)
    emb.add_field(name="Username", value=username, inline=True)
    emb.add_field(name="Status", value="In Progress", inline=True)
    emb.add_field(name="Pick-Up", value=pickup, inline=False)
    emb.add_field(name="Destination", value=destination, inline=False)
    if price_estimate:
        emb.add_field(name="Price Estimate", value=price_estimate, inline=True)
    if notes:
        emb.add_field(name="Notes", value=notes, inline=False)
    emb.add_field(name="Driver", value=interaction.user.mention, inline=True)
    emb.set_footer(text=f"Date: {today_iso()}")

    view = IngameRideView(
        driver_id=interaction.user.id,
        rider_name=rider_name, pickup=pickup, destination=destination,
        username=username, price_estimate=price_estimate, notes=notes
    )

    channel = bot.get_channel(INGAME_RIDES_CHANNEL_ID) or await bot.fetch_channel(INGAME_RIDES_CHANNEL_ID)
    msg = await channel.send(
        content=interaction.user.mention,  # ping driver at top of dashboard
        embed=emb, view=view,
        allowed_mentions=discord.AllowedMentions(roles=False, users=True, everyone=False, replied_user=False)
    )

    async with ongoing_lock:
        ongoing_message_ids.add(msg.id)

# ------------------- /suggest -------------------
_votes: Dict[int, Dict[str, Set[int]]]= {}
_votes_lock = asyncio.Lock()

def short_preview(text: str, maxlen: int = 40) -> str:
    s = text.strip().replace("\n"," ")
    return (s[:maxlen-1]+"…") if len(s)>maxlen else s

class SuggestionView(discord.ui.View):
    def __init__(self, message_id: int, up_count: int, down_count: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.up.label = f"⬆ {up_count}"
        self.down.label = f"⬇ {down_count}"

    async def _ensure(self):
        async with _votes_lock:
            _votes.setdefault(self.message_id, {"up": set(), "down": set()})

    async def _toggle(self, interaction: discord.Interaction, side: str):
        await self._ensure()
        uid = interaction.user.id
        async with _votes_lock:
            rec = _votes[self.message_id]
            other = "down" if side=="up" else "up"
            rec[other].discard(uid)
            if uid in rec[side]:
                rec[side].remove(uid)
                action = "removed"
            else:
                rec[side].add(uid)
                action = "added"
            upc, dnc = len(rec["up"]), len(rec["down"])
        self.up.label = f"⬆ {upc}"
        self.down.label = f"⬇ {dnc}"
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)
        await interaction.followup.send(f"Vote {action}. (Up: {upc} / Down: {dnc})", ephemeral=True)

    @discord.ui.button(label="⬆ 0", style=discord.ButtonStyle.success, custom_id="suggest:up")
    async def up(self, i: discord.Interaction, _: discord.ui.Button): await self._toggle(i,"up")

    @discord.ui.button(label="⬇ 0", style=discord.ButtonStyle.danger, custom_id="suggest:down")
    async def down(self, i: discord.Interaction, _: discord.ui.Button): await self._toggle(i,"down")

    @discord.ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="suggest:list")
    async def lst(self, i: discord.Interaction, _: discord.ui.Button):
        await self._ensure()
        async with _votes_lock:
            rec = _votes[self.message_id]
            ups = ", ".join(f"<@{u}>" for u in rec["up"]) or "—"
            dns = ", ".join(f"<@{u}>" for u in rec["down"]) or "—"
        e = discord.Embed(title="Suggestion Voters", color=discord.Color.orange(), timestamp=now_utc())
        e.add_field(name="Upvoters", value=ups, inline=False)
        e.add_field(name="Downvoters", value=dns, inline=False)
        await i.response.send_message(embed=e, ephemeral=True)

async def build_suggest_view(mid: int)->SuggestionView:
    async with _votes_lock:
        rec = _votes.get(mid, {"up": set(), "down": set()})
        return SuggestionView(mid, len(rec["up"]), len(rec["down"]))

@tree.command(name="suggest", description="Create a suggestion with voting buttons")
@app_commands.describe(suggestion="Your suggestion", notes="Optional notes")
async def suggest(interaction: discord.Interaction, suggestion: str, notes: Optional[str]=None):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not has_citizen_role(interaction.user):
        return await interaction.response.send_message("You need the Los Angeles Citizen role to use this command.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    e = discord.Embed(title="New Suggestion", color=discord.Color.orange(), timestamp=now_utc())
    e.add_field(name="Suggestion", value=suggestion, inline=False)
    if notes: e.add_field(name="Notes", value=notes, inline=False)
    e.add_field(name="Submitted by", value=interaction.user.mention, inline=False)

    channel = bot.get_channel(SUGGESTIONS_CHANNEL_ID) or await bot.fetch_channel(SUGGESTIONS_CHANNEL_ID)
    temp_view = SuggestionView(0,0,0)
    msg = await channel.send(embed=e, view=temp_view)

    async with _votes_lock:
        _votes[msg.id] = {"up": set(), "down": set()}
    await msg.edit(view=await build_suggest_view(msg.id))

    try:
        thread = await msg.create_thread(name=f"Suggestion – {short_preview(suggestion)}", auto_archive_duration=1440)
        await thread.send(
            content=interaction.user.mention,
            embed=discord.Embed(description="Discuss this suggestion here.", color=discord.Color.dark_grey(), timestamp=now_utc()),
            allowed_mentions=discord.AllowedMentions(users=True)
        )
    except discord.HTTPException:
        pass

    await interaction.followup.send("Your suggestion has been posted.", ephemeral=True)

# ------------------- /blacklist -------------------
@tree.command(name="blacklist", description="Post a Lyft Blacklist announcement.")
@app_commands.describe(
    citizen="Person being blacklisted (type a name; not a member picker)",
    blacklist="Select the blacklist (Lyft Blacklist)",
    reason="Reason for blacklist",
    duration="e.g., Permanent, or 2025-01-01 to 2025-03-01"
)
@app_commands.choices(
    blacklist=[app_commands.Choice(name="Lyft Blacklist", value="Lyft Blacklist")]
)
async def blacklist(
    interaction: discord.Interaction,
    citizen: str,
    blacklist: app_commands.Choice[str],
    reason: str,
    duration: str
):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)
    if not any(getattr(r, "id", None) == BLACKLISTER_ROLE_ID for r in getattr(interaction.user, "roles", [])):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    title = "<:Lyft:1416424004092428370> Lyft Blacklist Announcement <:Lyft:1416424004092428370>"
    desc_lines = [
        f"**Citizen:** {citizen}",
        "",
        f"**Blacklist:** {blacklist.value}",
        "",
        f"**Reason:** {reason}",
        "",
        f"**Duration:** `{duration}`",
        "",
        f"**Signed:** {interaction.user.mention}",
    ]
    emb = discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=discord.Color.red(),
        timestamp=now_utc(),
    )
    await send_embed(BLACKLIST_CHANNEL_ID, emb)
    await interaction.followup.send("Blacklist announcement posted.", ephemeral=True)

# ------------------- READY + SYNC + PERSISTENT ROUTERS -------------------
@bot.event
async def on_ready():
    class _SuggestRouter(discord.ui.View):
        def __init__(self): super().__init__(timeout=None)
        @discord.ui.button(label="⬆ 0", style=discord.ButtonStyle.success, custom_id="suggest:up")
        async def r_up(self, i: discord.Interaction, b: discord.ui.Button):
            v = await build_suggest_view(i.message.id); await v.up.callback(i)  # type: ignore
        @discord.ui.button(label="⬇ 0", style=discord.ButtonStyle.danger, custom_id="suggest:down")
        async def r_dn(self, i: discord.Interaction, b: discord.ui.Button):
            v = await build_suggest_view(i.message.id); await v.down.callback(i)  # type: ignore
        @discord.ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="suggest:list")
        async def r_ls(self, i: discord.Interaction, b: discord.ui.Button):
            v = await build_suggest_view(i.message.id); await v.lst.callback(i)  # type: ignore
    bot.add_view(_SuggestRouter())

    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.add_command(ride_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) — commands synced")

# ------------------- WEB SERVER (health + serve logo) -------------------
async def handle_logo(_):
    path = os.path.join(os.path.dirname(__file__), "LYFT.png")
    return web.FileResponse(path) if os.path.exists(path) else web.Response(status=404)

async def handle_health(_):
    return web.Response(text="OK")

async def start_web_server():
    global LOGO_URL
    app = web.Application()
    app.router.add_get(LOGO_ROUTE, handle_logo)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    host = os.getenv("RENDER_EXTERNAL_URL")
    if host:
        if not host.startswith("http"): host = "https://" + host
        LOGO_URL = host.rstrip("/") + LOGO_ROUTE
    else:
        LOGO_URL = f"http://localhost:{PORT}{LOGO_ROUTE}"
    print(f"HTTP server listening on 0.0.0.0:{PORT} | LOGO_URL={LOGO_URL}")

# ------------------- MAIN -------------------
async def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN")
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
