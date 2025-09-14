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

# Ride posting + driver roles
TARGET_CHANNEL_ID = 1416334665958166560
ROLE_ID_1 = 1416068902609223749   # driver role 1 (can claim; can submit allocation/permission)
ROLE_ID_2 = 1416063969965248594   # driver role 2 (can claim)

# Logs
AUDIT_LOG_CHANNEL_ID = 1416392593222270976
RIDE_LOG_CHANNEL_ID  = 1416342987893375007

# Admin reviewers (can Accept/Deny allocation/permission)
PING_ROLE_ADMIN_1 = 1416069791495622707
PING_ROLE_ADMIN_2 = 1416069983942869113

# Allocation / Permission destination channels
ALLOCATION_CHANNEL_ID = 1416425017406914662
PERMISSION_CHANNEL_ID = 1416388268894720020

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

# =========================
# BOT
# =========================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =========================
# JSON "DB"
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

def user_has_allowed_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {ROLE_ID_1, ROLE_ID_2} for r in getattr(member, "roles", []))

def is_reviewer(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in {PING_ROLE_ADMIN_1, PING_ROLE_ADMIN_2} for r in getattr(member, "roles", []))

def safe_float(v: str) -> Optional[float]:
    try: return float(v)
    except: return None

def safe_int(v: str) -> Optional[int]:
    try: return int(v)
    except: return None

async def send_embed(channel_id: int, embed: discord.Embed, content: Optional[str] = None, allow_roles=False, allow_users=False, view: Optional[discord.ui.View]=None):
    ch = bot.get_channel(channel_id)
    if not isinstance(ch, discord.TextChannel): return None
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

# =========================
# BUTTON VIEWS
# =========================
class ClaimView(discord.ui.View):
    """Claim / End Ride; updates the request embed + posts embeds for driver assigned & end."""
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
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send("You are not authorized to claim rides.", ephemeral=True)
            if self.claimed_by is not None:
                return await interaction.followup.send("This ride has already been claimed.", ephemeral=True)

            self.claimed_by = interaction.user.id
            button.disabled = True

            # Update the original embed
            msg = interaction.message
            if msg.embeds:
                base = msg.embeds[0]
                new = discord.Embed(
                    title=base.title,
                    description=base.description,
                    color=base.color,
                    timestamp=datetime.now(timezone.utc),
                )
                # Keep existing fields except Driver/Status, then add them fresh
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

            # Post "Driver Assigned" as an embed and ping the rider on top
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

        # Update original embed to "Completed"
        msg = interaction.message
        if msg.embeds:
            base = msg.embeds[0]
            new = discord.Embed(
                title=base.title,
                description=base.description,
                color=discord.Color.dark_grey(),
                timestamp=datetime.now(timezone.utc),
            )
            # Copy fields but replace Status
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

        # Send "Ride Ended" embed to main channel
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

class ApproveDenyView(discord.ui.View):
    """Accept/Deny for allocation/permission. Only reviewers can click and it updates Status with emoji."""
    def __init__(self, kind: str, requester_id: int):
        super().__init__(timeout=None)
        self.kind = kind  # 'allocation' | 'permission'
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

        # Update original embed with Status (with emoji)
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
            if base.thumbnail and base.thumbnail.url:
                new.set_thumbnail(url=base.thumbnail.url)

            try:
                await interaction.response.edit_message(embed=new, view=self)
            except discord.InteractionResponded:
                await interaction.followup.edit_message(message_id=msg.id, embed=new, view=self)

        # Post a decision embed pinging requester
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

# =========================
# /REQUEST ride
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

    separator = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"
    color = discord.Color.orange() if service_level.value == "Premium" else discord.Color.blue()
    e = discord.Embed(
        title=f"{service_level.value} Ride Request",
        description=f"A new ride is waiting to be claimed.\n{separator}",
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
            description=f"{interaction.user.mention} This thread is for coordinating your ride.",
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
# /LOG-RIDE  --> posts to RIDE_LOG_CHANNEL_ID
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
    if not user_has_allowed_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
    rides_val = safe_int(rides_this_week)

    # Save
    async with _db_lock:
        riders = _db.setdefault("riders", {})
        rec = riders.setdefault(str(rider.id), {"name": rider.name, "rides": []})
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

    # Build log embed
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
    emb.add_field(name="Comment", value=(comment or "—"), inline=False)
    emb.set_thumbnail(url=rider.display_avatar.url)
    emb.set_footer(text=f"Date: {today_iso()}")

    await send_embed(RIDE_LOG_CHANNEL_ID, emb)
    await interaction.edit_original_response(content="Ride logged successfully.")
    await audit("Ride Logged", [("Rider", rider.mention, True), ("Driver", interaction.user.mention, True)], color=discord.Color.dark_grey())

# =========================
# /ALLOCATION  (reviewed with Accept/Deny; status shows 🟡/🟢/🔴)
# =========================
@tree.command(name="allocation", description="Submit an allocation request")
@app_commands.describe(
    role_recipient="User who will receive role changes",
    roles_to_give="Role(s) to give (names/IDs, comma separated)",
    roles_to_remove="Role(s) to remove (names/IDs, comma separated)",
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
        description="A driver has submitted an allocation request for review.",
        color=discord.Color.dark_teal(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Requested By", value=interaction.user.mention, inline=True)
    emb.add_field(name="Recipient", value=role_recipient.mention, inline=True)
    emb.add_field(name="Roles to Give", value=roles_to_give or "—", inline=False)
    emb.add_field(name="Roles to Remove", value=roles_to_remove or "—", inline=False)
    emb.add_field(name="Proof", value=proof or "—", inline=False)
    emb.add_field(name="Status", value="🟡 Pending", inline=True)
    emb.add_field(name="Date", value=today_iso(), inline=True)
    emb.set_thumbnail(url=role_recipient.display_avatar.url)

    content = f"<@&{PING_ROLE_ADMIN_1}> <@&{PING_ROLE_ADMIN_2}>"
    view = ApproveDenyView(kind="allocation", requester_id=interaction.user.id)

    # Send one message with embed+view so the buttons can edit the same message
    await send_embed(
        ALLOCATION_CHANNEL_ID,
        emb,
        content=content,
        allow_roles=True,
        view=view
    )

    await audit(
        "Allocation Request Logged",
        [("By", interaction.user.mention, True), ("Recipient", role_recipient.mention, True)],
        color=discord.Color.dark_teal()
    )
    await interaction.followup.send("Allocation request sent.", ephemeral=True)

# =========================
# /PERMISSION  (reviewed with Accept/Deny; status shows 🟡/🟢/🔴)
# =========================
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
        description="A driver has submitted a permission request for approval.",
        color=discord.Color.dark_gold(),
        timestamp=datetime.now(timezone.utc)
    )
    emb.add_field(name="Requested By", value=interaction.user.mention, inline=True)
    emb.add_field(name="Permission", value=permission or "—", inline=False)
    emb.add_field(name="Duration", value=duration or "—", inline=True)
    emb.add_field(name="Reason", value=reason or "—", inline=False)
    emb.add_field(name="Signed", value=signed or "—", inline=True)
    emb.add_field(name="Status", value="🟡 Pending", inline=True)
    emb.add_field(name="Date", value=today_iso(), inline=True)
    emb.set_thumbnail(url=interaction.user.display_avatar.url)

    content = f"<@&{PING_ROLE_ADMIN_1}> <@&{PING_ROLE_ADMIN_2}>"
    view = ApproveDenyView(kind="permission", requester_id=interaction.user.id)

    await send_embed(
        PERMISSION_CHANNEL_ID,
        emb,
        content=content,
        allow_roles=True,
        view=view
    )

    await audit(
        "Permission Request Logged",
        [("By", interaction.user.mention, True), ("Permission", permission, True), ("Duration", duration, True)],
        color=discord.Color.dark_gold()
    )
    await interaction.followup.send("Permission request sent.", ephemeral=True)

# =========================
# READY + SYNC + WEB
# =========================
@bot.event
async def on_ready():
    await load_db()
    guild = discord.Object(id=GUILD_ID)
    tree.add_command(request_group, guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    started = discord.Embed(
        title="Lyft Bot Online",
        description="Bot is up and ready.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await send_embed(AUDIT_LOG_CHANNEL_ID, started)

async def health(_): return web.Response(text="OK")
async def webserver():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    if not TOKEN: raise RuntimeError("Missing DISCORD_TOKEN")
    await webserver()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
