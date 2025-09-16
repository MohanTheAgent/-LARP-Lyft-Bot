# -*- coding: utf-8 -*-
# main.py — /suggest command with vote buttons + auto-thread
#
# What it does
# ------------
# - Restricts ALL slash commands to a single guild (GUILD_ID)
# - /suggest [suggestion] [notes]
#     * Only users with role `CITIZEN_ROLE_ID` can use it
#     * If the user lacks the role: ephemeral error telling them they need the Los Angeles Citizen role
#     * Posts an orange embed in SUGGESTIONS_CHANNEL_ID
#     * Auto-creates a thread for discussion
#     * Adds Upvote / Downvote buttons that track unique voters
#     * “List Voters” shows current up/down voters (ephemeral)
# - Keeps a tiny in-memory vote store (resets on restart; easy to swap to JSON/DB if needed)
# - Starts a tiny HTTP server (for Render health checks)

import os
import asyncio
from typing import Optional, Dict, Set
from datetime import datetime, timezone

import discord
from discord import app_commands
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ------------- CONFIG -------------
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1416057930381262880

# Role gate:
# The same role is used both as the “allowed to use /suggest” and the
# “Los Angeles Citizen” requirement per your instruction.
CITIZEN_ROLE_ID = 1416066285216727072

# Where suggestions are posted
SUGGESTIONS_CHANNEL_ID = 1417470220276207636

# Web server port (for Render)
PORT = int(os.getenv("PORT", "10000"))

# ------------- BOT SETUP -------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# In-memory vote store: { message_id: {"up": set(user_ids), "down": set(user_ids)} }
_votes: Dict[int, Dict[str, Set[int]]] = {}
_votes_lock = asyncio.Lock()

# ------------- HELPERS -------------
def has_citizen_role(member: discord.abc.User) -> bool:
    """Check if a member has the Los Angeles Citizen role."""
    return any(getattr(r, "id", None) == CITIZEN_ROLE_ID for r in getattr(member, "roles", []))

def now_utc():
    return datetime.now(timezone.utc)

def short_preview(text: str, maxlen: int = 40) -> str:
    s = text.strip().replace("\n", " ")
    return (s[: maxlen - 1] + "…") if len(s) > maxlen else s

# ------------- VOTE VIEW -------------
class SuggestionView(discord.ui.View):
    """Upvote/Downvote/List Voters. Recreated each click so labels show current counts."""
    def __init__(self, message_id: int, up_count: int, down_count: int):
        super().__init__(timeout=None)
        self.message_id = message_id

        # We set labels dynamically to show counts (e.g., "⬆ 4" / "⬇ 1")
        # Colors mimic the screenshot (green for up, red for down, grey secondary for list).
        self.up_button.label = f"⬆ {up_count}"
        self.down_button.label = f"⬇ {down_count}"

    async def _ensure_store(self):
        async with _votes_lock:
            _votes.setdefault(self.message_id, {"up": set(), "down": set()})

    async def _refresh_counts(self):
        async with _votes_lock:
            rec = _votes.get(self.message_id) or {"up": set(), "down": set()}
            return len(rec["up"]), len(rec["down"])

    async def _toggle_vote(self, interaction: discord.Interaction, direction: str):
        """direction is 'up' or 'down'."""
        await self._ensure_store()
        uid = interaction.user.id
        async with _votes_lock:
            rec = _votes[self.message_id]
            other = "down" if direction == "up" else "up"
            # Remove from other side if present
            rec[other].discard(uid)
            # Toggle on/off for chosen side
            if uid in rec[direction]:
                rec[direction].remove(uid)
                action = "removed"
            else:
                rec[direction].add(uid)
                action = "added"

            up_count, down_count = len(rec["up"]), len(rec["down"])

        # Rebuild the view with updated counts and edit message
        self.up_button.label = f"⬆ {up_count}"
        self.down_button.label = f"⬇ {down_count}"
        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(message_id=interaction.message.id, view=self)

        # Quiet ephemeral confirmation
        await interaction.followup.send(f"Vote {action}. (Up: {up_count} / Down: {down_count})", ephemeral=True)

    @discord.ui.button(
        label="⬆ 0",
        style=discord.ButtonStyle.success,
        custom_id="suggest:up",
        row=0,
    )
    async def up_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle_vote(interaction, "up")

    @discord.ui.button(
        label="⬇ 0",
        style=discord.ButtonStyle.danger,
        custom_id="suggest:down",
        row=0,
    )
    async def down_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._toggle_vote(interaction, "down")

    @discord.ui.button(
        label="List Voters",
        style=discord.ButtonStyle.secondary,
        custom_id="suggest:list",
        row=0,
    )
    async def list_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._ensure_store()
        async with _votes_lock:
            rec = _votes[self.message_id]
            up_ids = list(rec["up"])
            down_ids = list(rec["down"])

        def fmt(ids):
            return ", ".join(f"<@{i}>" for i in ids) if ids else "—"

        embed = discord.Embed(
            title="Suggestion Voters",
            color=discord.Color.orange(),
            timestamp=now_utc(),
        )
        embed.add_field(name="Upvoters", value=fmt(up_ids), inline=False)
        embed.add_field(name="Downvoters", value=fmt(down_ids), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Helper to build a fresh view from store
async def build_view_for(message_id: int) -> SuggestionView:
    async with _votes_lock:
        rec = _votes.get(message_id, {"up": set(), "down": set()})
        up, down = len(rec["up"]), len(rec["down"])
    return SuggestionView(message_id, up, down)

# ------------- /suggest COMMAND -------------
@tree.command(name="suggest", description="Create a suggestion with voting buttons")
@app_commands.describe(
    suggestion="Your suggestion",
    notes="Optional notes or context"
)
async def suggest(
    interaction: discord.Interaction,
    suggestion: str,
    notes: Optional[str] = None
):
    # Guild lock
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message("This command is not available in this server.", ephemeral=True)

    # Role gate
    if not has_citizen_role(interaction.user):
        return await interaction.response.send_message(
            "You need the **Los Angeles Citizen** role to use this command.",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    # Build the embed
    embed = discord.Embed(
        title="New Suggestion",
        color=discord.Color.orange(),
        timestamp=now_utc(),
    )
    embed.add_field(name="Suggestion", value=suggestion, inline=False)
    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Submitted by", value=interaction.user.mention, inline=False)

    # Send to suggestions channel
    channel = bot.get_channel(SUGGESTIONS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SUGGESTIONS_CHANNEL_ID)  # type: ignore
        except discord.NotFound:
            return await interaction.followup.send("Suggestions channel not found.", ephemeral=True)

    # Prepare store and view
    async with _votes_lock:
        # Placeholder entry so counts start at 0
        # (we'll replace the key after we have the message ID)
        temp_key = -1
        _votes[temp_key] = {"up": set(), "down": set()}

    # Send message with a temporary view (we'll rebuild right after with real message id)
    temp_view = SuggestionView(message_id=0, up_count=0, down_count=0)
    msg = await channel.send(embed=embed, view=temp_view)

    # Swap the store key to the real message id and rebuild the view
    async with _votes_lock:
        rec = _votes.pop(temp_key, {"up": set(), "down": set()})
        _votes[msg.id] = rec

    real_view = await build_view_for(msg.id)
    await msg.edit(view=real_view)

    # Auto-create a thread for discussion
    try:
        thread_name = f"Suggestion – {short_preview(suggestion)}"
        thread = await msg.create_thread(name=thread_name, auto_archive_duration=1440)
        await thread.send(
            content=interaction.user.mention,
            embed=discord.Embed(
                description="This thread is for discussing the suggestion above.",
                color=discord.Color.dark_grey(),
                timestamp=now_utc(),
            ),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except discord.HTTPException:
        pass

    await interaction.followup.send("Your suggestion has been posted.", ephemeral=True)

# ------------- READY + VIEW REGISTRATION -------------
@bot.event
async def on_ready():
    # Register a persistent view so buttons keep working after restarts.
    # (We attach a "blank" view that will accept interactions by custom_id,
    #  then we rebuild the real labels dynamically on each click.)
    class _Persistent(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="⬆ 0", style=discord.ButtonStyle.success, custom_id="suggest:up")
        async def _p_up(self, interaction: discord.Interaction, button: discord.ui.Button):
            mid = interaction.message.id
            view = await build_view_for(mid)
            await view.up_button.callback(interaction)  # reuse logic

        @discord.ui.button(label="⬇ 0", style=discord.ButtonStyle.danger, custom_id="suggest:down")
        async def _p_down(self, interaction: discord.Interaction, button: discord.ui.Button):
            mid = interaction.message.id
            view = await build_view_for(mid)
            await view.down_button.callback(interaction)  # reuse logic

        @discord.ui.button(label="List Voters", style=discord.ButtonStyle.secondary, custom_id="suggest:list")
        async def _p_list(self, interaction: discord.Interaction, button: discord.ui.Button):
            mid = interaction.message.id
            view = await build_view_for(mid)
            await view.list_button.callback(interaction)  # reuse logic

    bot.add_view(_Persistent())

    # Guild-only sync
    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Logged in as {bot.user} (ID: {bot.user.id}) — commands synced")

# ------------- TINY HTTP SERVER (for Render) -------------
async def handle_health(_):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server listening on 0.0.0.0:{PORT}")

# ------------- MAIN -------------
async def main():
    if not TOKEN:
        raise RuntimeError("Please set DISCORD_TOKEN in your environment.")
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
