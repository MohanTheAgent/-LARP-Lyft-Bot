import os
import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from dotenv import load_dotenv

# --- tiny HTTP server for Render Free Web Service ---
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

# -----------------------------
# BOT SETUP
# -----------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# Utilities
# -----------------------------
ALLOWED_ROLES = {ROLE_ID_1, ROLE_ID_2}

def user_has_allowed_role(member: discord.abc.User) -> bool:
    return any(getattr(r, "id", None) in ALLOWED_ROLES for r in getattr(member, "roles", []))

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

# -----------------------------
# CLAIM / END BUTTON VIEW
# -----------------------------
class ClaimView(discord.ui.View):
    def __init__(self, requester_id: int, channel_id: int, thread_id: int | None = None):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.channel_id = channel_id
        self.thread_id = thread_id
        self.claimed = False
        self.claimed_by_user_id: Optional[int] = None
        self._lock = asyncio.Lock()  # prevent race on claim

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="rideclaim:button")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Immediate ack to avoid timeouts
        await interaction.response.defer()

        async with self._lock:
            if not user_has_allowed_role(interaction.user):
                return await interaction.followup.send(
                    "You are not authorized to claim ride requests.", ephemeral=True
                )

            if self.claimed:
                return await interaction.followup.send(
                    "This request has already been claimed.", ephemeral=True
                )

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

            # Announce to main channel (outside thread)
            channel = interaction.client.get_channel(self.channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(f"<@{self.requester_id}> Your driver is {interaction.user.mention}")

    @discord.ui.button(label="End Ride", style=discord.ButtonStyle.danger, custom_id="rideend:button")
    async def end_ride(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not self.claimed or self.claimed_by_user_id is None:
            return await interaction.followup.send("No driver has claimed this ride yet.", ephemeral=True)

        if interaction.user.id != self.claimed_by_user_id:
            return await interaction.followup.send(
                "Only the driver who claimed this ride can end it.", ephemeral=True
            )

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
# /log-ride COMMAND (top-level, role-locked)
# -----------------------------
@tree.command(name="log-ride", description="Log a completed ride to the log channel.")
@app_commands.describe(
    ride_link="Link to the ride (URL or reference)",
    income="Income for this ride (e.g., 25.50)",
    rating="Your rating for this ride (e.g., 4.8)",
    rides_this_week="Number of rides you've completed this week (integer)"
)
async def log_ride(
    interaction: discord.Interaction,
    ride_link: str,
    income: str,
    rating: str,
    rides_this_week: str
):
    await interaction.response.send_message("Logging your ride...", ephemeral=True)

    if interaction.guild_id != GUILD_ID:
        return await interaction.edit_original_response(content="This command isn't available in this server.")

    if not user_has_allowed_role(interaction.user):
        return await interaction.edit_original_response(content="You are not authorized to use this command.")

    income_val = safe_float(income)
    rating_val = safe_float(rating)
    rides_val = safe_int(rides_this_week)

    embed = discord.Embed(
        title="Ride Log",
        description="A ride has been logged.",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Ride Link", value=ride_link, inline=False)
    embed.add_field(name="Income", value=(f"${income_val:,.2f}" if income_val is not None else income), inline=True)
    embed.add_field(name="Rating", value=(f"{rating_val:.2f}" if rating_val is not None else rating), inline=True)
    embed.add_field(name="Rides This Week", value=(str(rides_val) if rides_val is not None else rides_this_week), inline=True)
    embed.add_field(name="Logged By", value=interaction.user.mention, inline=False)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Ride log entry")

    log_channel = interaction.client.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await interaction.client.fetch_channel(LOG_CHANNEL_ID)
        except discord.NotFound:
            return await interaction.edit_original_response(
                content="I couldn't find the log channel. Please check my configuration."
            )

    await log_channel.send(embed=embed)
    await interaction.edit_original_response(content="Your ride has been logged.")

# -----------------------------
# ON READY (guild-scoped sync)
# -----------------------------
@bot.event
async def on_ready():
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

    # Run web server and bot concurrently
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
