# -*- coding: utf-8 -*-
import os, json, asyncio
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

def today_iso(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def user_has_allowed_role(m): return any(getattr(r,"id",None) in {ROLE_ID_1,ROLE_ID_2} for r in getattr(m,"roles",[]))
def user_is_admin(m): return any(getattr(r,"id",None)==ADMIN_ROLE_ID for r in getattr(m,"roles",[]))
def safe_float(v): 
    try:return float(v)
    except: return None
def safe_int(v):
    try:return int(v)
    except: return None
def avg(v): 
    a=[x for x in v if isinstance(x,(int,float))]
    return sum(a)/len(a) if a else None
def named(u): return f"{u.mention} (`{getattr(u,'id','?')}`)"
async def load_db():
    global _db
    async with _db_lock:
        if not os.path.exists(DATA_FILE):
            _db={"riders":{},"drivers":{}}
            await save_db();return
        try: _db=json.load(open(DATA_FILE,"r",encoding="utf-8"))
        except: _db={"riders":{},"drivers":{}}
        _db.setdefault("riders",{});_db.setdefault("drivers",{})
async def save_db():
    async with _db_lock:
        json.dump(_db,open(DATA_FILE,"w",encoding="utf-8"),indent=2,ensure_ascii=False)
async def send_audit(title,fields,color=discord.Color.blurple()):
    c=bot.get_channel(AUDIT_LOG_CHANNEL_ID)
    if not isinstance(c,discord.TextChannel):return
    e=discord.Embed(title=title,color=color,timestamp=datetime.now(timezone.utc))
    for n,v,i in fields:e.add_field(name=n,value=v,inline=i)
    await c.send(embed=e)

class RatingView(discord.ui.View):
    def __init__(self,driver_id:int,from_id:int):
        super().__init__(timeout=None)
        self.driver_id=driver_id;self.from_id=from_id
    async def _rate(self,i,v):
        if i.user.id!=self.from_id:
            return await i.response.send_message("Only the rider can rate.",ephemeral=True)
        async with _db_lock:
            d=_db.setdefault("drivers",{}).setdefault(str(self.driver_id),{"name":"","ratings":[]})
            d["ratings"].append({"from":self.from_id,"rating":v,"date":today_iso()})
        await save_db()
        for b in self.children:b.disabled=True
        await i.response.edit_message(content="Thanks for your feedback!",view=self)
        await send_audit("Rating Submitted",[("Driver",f"<@{self.driver_id}>",True),("From",f"<@{self.from_id}>",True),("Rating",str(v),True)],discord.Color.green())
    @discord.ui.button(label="1",style=discord.ButtonStyle.secondary) async def r1(self,i,_): await self._rate(i,1)
    @discord.ui.button(label="2",style=discord.ButtonStyle.secondary) async def r2(self,i,_): await self._rate(i,2)
    @discord.ui.button(label="3",style=discord.ButtonStyle.secondary) async def r3(self,i,_): await self._rate(i,3)
    @discord.ui.button(label="4",style=discord.ButtonStyle.secondary) async def r4(self,i,_): await self._rate(i,4)
    @discord.ui.button(label="5",style=discord.ButtonStyle.secondary) async def r5(self,i,_): await self._rate(i,5)
    @discord.ui.button(label="6",style=discord.ButtonStyle.secondary) async def r6(self,i,_): await self._rate(i,6)
    @discord.ui.button(label="7",style=discord.ButtonStyle.secondary) async def r7(self,i,_): await self._rate(i,7)
    @discord.ui.button(label="8",style=discord.ButtonStyle.secondary) async def r8(self,i,_): await self._rate(i,8)
    @discord.ui.button(label="9",style=discord.ButtonStyle.secondary) async def r9(self,i,_): await self._rate(i,9)
    @discord.ui.button(label="10",style=discord.ButtonStyle.primary) async def r10(self,i,_): await self._rate(i,10)

class ClaimView(discord.ui.View):
    def __init__(self,rid:int,tid:int):
        super().__init__(timeout=None)
        self.rid=rid;self.tid=tid;self.driver=None
    @discord.ui.button(label="Claim",style=discord.ButtonStyle.success)
    async def claim(self,i,b):
        await i.response.defer()
        if not user_has_allowed_role(i.user):return await i.followup.send("Not allowed.",ephemeral=True)
        if self.driver:return await i.followup.send("Already claimed.",ephemeral=True)
        self.driver=i.user.id;b.disabled=True
        e=i.message.embeds[0]
        n=discord.Embed(title=e.title,description=e.description,color=e.color,timestamp=datetime.now(timezone.utc))
        for f in e.fields:n.add_field(name=f.name,value=f.value,inline=f.inline)
        n.add_field(name="Driver",value=i.user.mention,inline=False);n.set_footer(text="Ride claimed")
        await i.followup.edit_message(message_id=i.message.id,embed=n,view=self)
        await send_audit("Ride Claimed",[("Driver",named(i.user),True),("Rider",f"<@{self.rid}>",True)],discord.Color.orange())
    @discord.ui.button(label="End Ride",style=discord.ButtonStyle.danger)
    async def end(self,i,b):
        await i.response.defer()
        if not self.driver:return await i.followup.send("No driver yet.",ephemeral=True)
        if i.user.id!=self.driver:return await i.followup.send("Only the driver can end.",ephemeral=True)
        b.disabled=True
        e=i.message.embeds[0]
        n=discord.Embed(title=e.title,description=e.description,color=discord.Color.dark_grey(),timestamp=datetime.now(timezone.utc))
        for f in e.fields:n.add_field(name=f.name,value=f.value,inline=f.inline)
        n.set_footer(text="Ride ended")
        await i.followup.edit_message(message_id=i.message.id,embed=n,view=self)
        t=bot.get_channel(self.tid)
        if isinstance(t,discord.Thread):
            await t.send(content=f"<@{self.rid}>",embed=discord.Embed(title="Rate Your Driver",description="1-10",color=discord.Color.blurple()),view=RatingView(self.driver,self.rid))
            await t.send(embed=discord.Embed(title="Comments",description="💬 Please reply here with comments.",color=discord.Color.grayple()))
        await send_audit("Ride Ended",[("Driver",named(i.user),True),("Rider",f"<@{self.rid}>",True)],discord.Color.dark_grey())

request_group=app_commands.Group(name="request",description="Request rides")
@app_commands.choices(service_level=[app_commands.Choice(name="Premium",value="Premium"),app_commands.Choice(name="Standard",value="Standard")])
@request_group.command(name="ride")
@app_commands.describe(starting_location="Pickup",destination="Destination",service_level="Service level")
async def ride(i:discord.Interaction,starting_location:str,destination:str,service_level:app_commands.Choice[str]):
    await i.response.send_message("Posting...",ephemeral=True)
    c=discord.Color.orange() if service_level.value=="Premium" else discord.Color.blue()
    e=discord.Embed(title=f"{service_level.value} Ride Request",
                    description="A new ride is waiting for a driver to claim.\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬",
                    color=c,timestamp=datetime.now(timezone.utc))
    e.add_field(name="Pickup",value=starting_location,inline=True)
    e.add_field(name="Destination",value=destination,inline=True)
    e.add_field(name="Requested By",value=i.user.mention,inline=False)
    e.set_thumbnail(url=i.user.display_avatar.url)
    v=ClaimView(i.user.id,0)
    ch=bot.get_channel(TARGET_CHANNEL_ID)
    m=await ch.send(content=f"<@&{ROLE_ID_1}> <@&{ROLE_ID_2}>",embed=e,view=v,allowed_mentions=discord.AllowedMentions(roles=True))
    t=await m.create_thread(name=f"Ride - {i.user.display_name}",auto_archive_duration=1440)
    v.tid=t.id
    await t.send(embed=discord.Embed(description=f"{i.user.mention} This thread is for this ride.",color=discord.Color.dark_theme()))
    await i.edit_original_response(content="Ride posted.")
    await send_audit("Ride Requested",[("Rider",named(i.user),True),("Pickup",starting_location,True),("Destination",destination,True)],c)

@tree.command(name="search")
@app_commands.describe(user="User",ephemeral="Only you can see")
async def search(i:discord.Interaction,user:discord.User,ephemeral:Optional[bool]=True):
    await i.response.defer(ephemeral=bool(ephemeral))
    if not user_has_allowed_role(i.user):return await i.followup.send("Not allowed",ephemeral=True)
    async with _db_lock:
        rr=_db.get("riders",{}).get(str(user.id))
        dr=_db.get("drivers",{}).get(str(user.id))
    e=discord.Embed(title="Profile",color=discord.Color.blurple(),timestamp=datetime.now(timezone.utc))
    e.add_field(name="User",value=named(user),inline=False)
    if rr and rr.get("rides"):
        ratings=[float(r["rating"]) for r in rr["rides"] if r.get("rating")]
        e.add_field(name="Rider Rides",value=f"{len(rr['rides'])} | Avg {avg(ratings) or '-'}",inline=False)
    if dr and dr.get("ratings"):
        rates=[int(r["rating"]) for r in dr["ratings"]]
        e.add_field(name="Driver Ratings",value=f"{len(rates)} | Avg {avg(rates):.2f}",inline=False)
    e.set_thumbnail(url=user.display_avatar.url)
    await i.followup.send(embed=e,ephemeral=bool(ephemeral))
    await send_audit("Search",[("By",named(i.user),True),("Target",named(user),True)],discord.Color.teal())

@bot.event
async def on_ready():
    await load_db()
    guild=discord.Object(id=GUILD_ID)
    tree.add_command(request_group,guild=guild)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    await send_audit("Lyft Bot Online",[("Bot",named(bot.user),True)],discord.Color.green())

async def health(_):return web.Response(text="OK")
async def webserver():
    a=web.Application();a.router.add_get("/",health);a.router.add_get("/health",health)
    r=web.AppRunner(a);await r.setup()
    await web.TCPSite(r,"0.0.0.0",int(os.getenv("PORT","10000"))).start()

async def main():
    await webserver()
    await bot.start(TOKEN)
if __name__=="__main__":asyncio.run(main())
