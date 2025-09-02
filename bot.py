import asyncio
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button
from dotenv import load_dotenv

load_dotenv()

# ---------- config helpers ----------
def parse_channel(value: Optional[str], default: int = 0) -> int:
    if not value:
        return default
    v = value.strip()
    if v.isdigit():
        return int(v)
    m = re.search(r"/channels/\d+/(\d+)", v)
    if m:
        return int(m.group(1))
    return default

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = os.getenv("GUILD_ID", "")

LFT_CHANNEL_ID = parse_channel(os.getenv("LFT_CHANNEL_ID", "0"))
LFP_CHANNEL_ID = parse_channel(os.getenv("LFP_CHANNEL_ID", "0"))
MOD_QUEUE_CHANNEL_ID = parse_channel(os.getenv("MOD_QUEUE_CHANNEL_ID", "0"))  # 0 = post immediately

COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "10"))  # 10-minute cooldown
POST_EXPIRY_DAYS = int(os.getenv("POST_EXPIRY_DAYS", "7"))

# ---------- client ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ---------- in-memory state ----------
last_post_at: Dict[int, datetime] = {}     # cooldown tracker
active_message_ids: Dict[int, int] = {}    # last post per user (for cleanup)

URL_TO_DM = "https://discordapp.com/users/{user_id}"

# ---------- utils ----------
def clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return (text[: limit - 1] + "…") if len(text) > limit else text

def cooldown_ok(user_id: int):
    now = datetime.utcnow()
    last = last_post_at.get(user_id)
    if last and now - last < timedelta(minutes=COOLDOWN_MINUTES):
        remaining = timedelta(minutes=COOLDOWN_MINUTES) - (now - last)
        secs = int(remaining.total_seconds())
        mins = max(1, secs // 60)
        return False, "Cooldown active. Try again in about {} minutes.".format(mins)
    return True, ""

def mark_post(user_id: int, message_id: int):
    last_post_at[user_id] = datetime.utcnow()
    active_message_ids[user_id] = message_id

def clear_cooldown(user_id: int):
    last_post_at.pop(user_id, None)

async def maybe_delete_previous(user_id: int):
    mid = active_message_ids.get(user_id)
    if not mid:
        return
    for cid in (LFT_CHANNEL_ID, LFP_CHANNEL_ID):
        if not cid:
            continue
        ch = bot.get_channel(cid)
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(mid)
            await msg.delete()
            break
        except Exception:
            continue

def build_title_and_fields(kind: str, data: dict):
    if kind == "lft":
        # EXACT heading: LFT {Riot ID (with #)}
        title = clean("LFT {}".format(data["Riot ID (with #)"]))
        fields = {
            "Riot ID (with #)": data["Riot ID (with #)"],
            "Current/Peak Rank": data["Current/Peak Rank"],
            "Roles": data["Roles"],
            "Recent Teams": data["Recent Teams"],
            "Details": data["Details"],
        }
        color = 0x5865F2  # blue/blurple
    else:
        # EXACT heading: LFP {Team Name}
        title = clean("LFP {}".format(data["Team Name"]))
        fields = {
            "Team Name": data["Team Name"],
            "Roles Needed": data["Roles Needed"],
            "Peak Rank": data["Peak Rank"],
            "Current Rank": data["Current Rank"],
            "Benefits/Details": data["Benefits or details"],
        }
        color = 0x57F287  # green (success)
    return title, fields, color

async def post_embed(kind: str, author_id: int, data: dict) -> discord.Message:
    channel_id = LFT_CHANNEL_ID if kind == "lft" else LFP_CHANNEL_ID
    channel = bot.get_channel(channel_id)
    if not channel:
        raise RuntimeError("Target channel not configured. Use /setup or set env IDs.")

    title, fields, color = build_title_and_fields(kind, data)
    long_key = "Details" if kind == "lft" else "Benefits/Details"
    summary = clean(fields.get(long_key, ""), 400)

    embed = discord.Embed(
        title=title,
        description=summary if summary else None,
        color=color,
        timestamp=datetime.utcnow(),
    )
    for k, v in fields.items():
        if k == long_key:
            continue
        if v:
            embed.add_field(name=k, value=v, inline=False)

    view = View()
    view.add_item(Button(
        label="DM Player" if kind == "lft" else "DM Contact",
        style=discord.ButtonStyle.link,
        url=URL_TO_DM.format(user_id=author_id),
    ))

    msg = await channel.send(embed=embed, view=view)
    try:
        await msg.create_thread(name="{}".format(title[:80]))
    except Exception:
        pass
    return msg

# ---------- presence ----------
async def update_presence():
    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="players and teams connect!"
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)

# ---------- LFT modal (5 inputs, with Riot ID + placeholders) ----------
class LFTModal(Modal, title="LFT Form"):
    def __init__(self):
        super(LFTModal, self).__init__(title="LFT Form")
        self.riot = TextInput(
            label="Riot ID (with #)",
            placeholder="Username#TAG (e.g., PlayerOne#EUW)"
        )
        self.rank = TextInput(
            label="Current/Peak Rank",
            placeholder="Current / Peak (e.g., Ascendant 2 / Immortal 1)"
        )
        self.roles = TextInput(
            label="Roles",
            placeholder="e.g., Duelist, Initiator, Controller, Sentinel"
        )
        self.recent = TextInput(
            label="Recent Teams",
            placeholder="Team names/events (optional)",
            required=False
        )
        self.details = TextInput(
            label="Details",
            placeholder="Availability, languages, scrim times, goals (optional)",
            style=discord.TextStyle.paragraph,
            required=False
        )
        for item in (self.riot, self.rank, self.roles, self.recent, self.details):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ok, msg = cooldown_ok(interaction.user.id)
            if not ok:
                await interaction.response.send_message(msg, ephemeral=True)
                return

            data = {
                "Riot ID (with #)": self.riot.value,
                "Current/Peak Rank": self.rank.value,
                "Roles": self.roles.value,
                "Recent Teams": self.recent.value or "",
                "Details": self.details.value or "",
            }

            if MOD_QUEUE_CHANNEL_ID:
                qch = bot.get_channel(MOD_QUEUE_CHANNEL_ID)
                if not qch:
                    await interaction.response.send_message("Moderation queue channel is not accessible.", ephemeral=True)
                    return
                title, fields, _ = build_title_and_fields("lft", data)
                embed = discord.Embed(title="[Queue] " + title, color=discord.Color.orange())
                for k, v in fields.items():
                    if k == "Details":
                        continue
                    if v:
                        embed.add_field(name=k, value=v, inline=False)
                if data.get("Details"):
                    embed.description = clean(data["Details"], 400)
                view = ApprovalView(kind="lft", author_id=interaction.user.id, payload=data,
                                    queue_channel_id=qch.id, queue_message_id=None)
                message = await qch.send(embed=embed, view=view)
                view.queue_message_id = message.id
                await interaction.response.send_message("Submitted for review. Mods will approve soon.", ephemeral=True)
            else:
                await maybe_delete_previous(interaction.user.id)
                posted = await post_embed("lft", interaction.user.id, data)
                mark_post(interaction.user.id, posted.id)
                await interaction.response.send_message("Posted to LFT channel.", ephemeral=True)

        except Exception as e:
            print("LFT submit error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Error: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Error: {}".format(e), ephemeral=True)

# ---------- LFP modal (5 inputs + placeholders) ----------
class LFPModal(Modal, title="LFP Form"):
    def __init__(self):
        super(LFPModal, self).__init__(title="LFP Form")
        self.team = TextInput(
            label="Team Name",
            placeholder="Your team/org name"
        )
        self.roles_needed = TextInput(
            label="Roles Needed",
            placeholder="e.g., Controller, IGL, Initiator"
        )
        self.peak = TextInput(
            label="Peak Rank",
            placeholder="e.g., Immortal 2"
        )
        self.current = TextInput(
            label="Current Rank",
            placeholder="e.g., Ascendant 3+"
        )
        self.benefits = TextInput(
            label="Benefits or details",
            placeholder="Schedule, staff, compensation, region, expectations (optional)",
            style=discord.TextStyle.paragraph,
            required=False
        )
        for item in (self.team, self.roles_needed, self.peak, self.current, self.benefits):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            ok, msg = cooldown_ok(interaction.user.id)
            if not ok:
                await interaction.response.send_message(msg, ephemeral=True)
                return

            data = {
                "Team Name": self.team.value,
                "Roles Needed": self.roles_needed.value,
                "Peak Rank": self.peak.value,
                "Current Rank": self.current.value,
                "Benefits or details": self.benefits.value or "",
            }

            if MOD_QUEUE_CHANNEL_ID:
                qch = bot.get_channel(MOD_QUEUE_CHANNEL_ID)
                if not qch:
                    await interaction.response.send_message("Moderation queue channel is not accessible.", ephemeral=True)
                    return
                title, fields, _ = build_title_and_fields("lfp", data)
                embed = discord.Embed(title="[Queue] " + title, color=discord.Color.orange())
                for k, v in fields.items():
                    if k == "Benefits/Details":
                        continue
                    if v:
                        embed.add_field(name=k, value=v, inline=False)
                if data.get("Benefits or details"):
                    embed.description = clean(data["Benefits or details"], 400)
                view = ApprovalView(kind="lfp", author_id=interaction.user.id, payload=data,
                                    queue_channel_id=qch.id, queue_message_id=None)
                message = await qch.send(embed=embed, view=view)
                view.queue_message_id = message.id
                await interaction.response.send_message("Submitted for review. Mods will approve soon.", ephemeral=True)
            else:
                await maybe_delete_previous(interaction.user.id)
                posted = await post_embed("lfp", interaction.user.id, data)
                mark_post(interaction.user.id, posted.id)
                await interaction.response.send_message("Posted to LFP channel.", ephemeral=True)

        except Exception as e:
            print("LFP submit error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Error: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Error: {}".format(e), ephemeral=True)

# ---------- Rejection Reason Modal (with placeholder) ----------
class RejectReasonModal(Modal, title="Rejection Reason"):
    def __init__(self, *, author_id: int, kind: str, payload: dict, queue_channel_id: int, queue_message_id: int):
        super(RejectReasonModal, self).__init__(title="Rejection Reason")
        self.reason = TextInput(
            label="Reason for rejection",
            placeholder="e.g., missing info, wrong channel, not enough details",
            style=discord.TextStyle.paragraph,
            required=False
        )
        self.add_item(self.reason)
        self.author_id = author_id
        self.kind = kind
        self.payload = payload
        self.queue_channel_id = queue_channel_id
        self.queue_message_id = queue_message_id

    async def on_submit(self, interaction: discord.Interaction):
        clear_cooldown(self.author_id)
        # DM user
        try:
            user = await bot.fetch_user(self.author_id)
            r = self.reason.value or "No reason provided."
            await user.send("Your {} submission was rejected.\nReason: {}".format(self.kind.upper(), r))
        except Exception:
            pass

        # Ping in queue channel and disable buttons
        try:
            ch = bot.get_channel(self.queue_channel_id)
            if ch:
                r = self.reason.value or "No reason provided."
                await ch.send("<@{}> your {} submission was **rejected**.\nReason: {}".format(self.author_id, self.kind.upper(), r))
            if ch and self.queue_message_id:
                try:
                    msg = await ch.fetch_message(self.queue_message_id)
                    await msg.edit(view=None)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            await interaction.response.send_message("Rejected and user notified (cooldown cleared).", ephemeral=True)
        except Exception:
            pass

# ---------- moderation approval view ----------
class ApprovalView(View):
    def __init__(self, kind: str, author_id: int, payload: dict, queue_channel_id: int, queue_message_id: Optional[int], timeout: float = 3600):
        super(ApprovalView, self).__init__(timeout=timeout)
        self.kind = kind
        self.author_id = author_id
        self.payload = payload
        self.queue_channel_id = queue_channel_id
        self.queue_message_id = queue_message_id or 0

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("Moderator permission needed.", ephemeral=True)
            return
        try:
            await maybe_delete_previous(self.author_id)
            msg = await post_embed(self.kind, self.author_id, self.payload)
            mark_post(self.author_id, msg.id)

            # DM user with approval and link
            try:
                user = await bot.fetch_user(self.author_id)
                await user.send("Your {} submission was **approved**! Link: {}".format(self.kind.upper(), msg.jump_url))
            except Exception:
                pass

            # Ping user in queue channel & disable buttons
            try:
                ch = bot.get_channel(self.queue_channel_id)
                if ch:
                    await ch.send("<@{}> your {} submission was **approved**! {}".format(self.author_id, self.kind.upper(), msg.jump_url))
                if ch and self.queue_message_id:
                    try:
                        qmsg = await ch.fetch_message(self.queue_message_id)
                        await qmsg.edit(view=None)
                    except Exception:
                        pass
            except Exception:
                pass

            await interaction.response.send_message("Approved and posted; user notified.", ephemeral=True)
        except Exception as e:
            print("Approve error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Error: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Error: {}".format(e), ephemeral=True)

    @discord.ui.button(label="Reject (add reason)", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("Moderator permission needed.", ephemeral=True)
            return
        try:
            await interaction.response.send_modal(
                RejectReasonModal(
                    author_id=self.author_id,
                    kind=self.kind,
                    payload=self.payload,
                    queue_channel_id=self.queue_channel_id,
                    queue_message_id=self.queue_message_id
                )
            )
        except Exception as e:
            print("Reject open modal error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Could not open reason modal: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Could not open reason modal: {}".format(e), ephemeral=True)

# ---------- DM helper buttons (contrasting colors) ----------
class ChoiceView(View):
    @discord.ui.button(label="LFT", style=discord.ButtonStyle.primary)   # blue/blurple
    async def btn_lft(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(LFTModal())
        except Exception as e:
            print("btn_lft error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Could not open LFT: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Could not open LFT: {}".format(e), ephemeral=True)

    @discord.ui.button(label="LFP", style=discord.ButtonStyle.success)   # green
    async def btn_lfp(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(LFPModal())
        except Exception as e:
            print("btn_lfp error:", repr(e))
            if not interaction.response.is_done():
                await interaction.response.send_message("Could not open LFP: {}".format(e), ephemeral=True)
            else:
                await interaction.followup.send("Could not open LFP: {}".format(e), ephemeral=True)

# ---------- slash commands (optional entry) ----------
@tree.command(name="lft", description="Open the LFT form")
async def lft_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(LFTModal())

@tree.command(name="lfp", description="Open the LFP form")
async def lfp_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(LFPModal())

@tree.command(name="setup", description="Bind channels and settings (admin only)")
@app_commands.describe(
    lft_channel="Target channel for LFT posts",
    lfp_channel="Target channel for LFP posts",
    mod_queue_channel="Queue channel (optional, set to 0 to disable)",
    cooldown_minutes="Submission cooldown in minutes",
    expire_days="Auto-expire days"
)
async def setup_cmd(
    interaction: discord.Interaction,
    lft_channel: discord.TextChannel,
    lfp_channel: discord.TextChannel,
    mod_queue_channel: Optional[discord.TextChannel],
    cooldown_minutes: app_commands.Range[int, 1, 1440] = COOLDOWN_MINUTES,
    expire_days: app_commands.Range[int, 1, 30] = POST_EXPIRY_DAYS,
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Manage Server permission needed.", ephemeral=True)
        return
    global LFT_CHANNEL_ID, LFP_CHANNEL_ID, MOD_QUEUE_CHANNEL_ID, COOLDOWN_MINUTES, POST_EXPIRY_DAYS
    LFT_CHANNEL_ID = lft_channel.id
    LFP_CHANNEL_ID = lfp_channel.id
    MOD_QUEUE_CHANNEL_ID = mod_queue_channel.id if mod_queue_channel else 0
    COOLDOWN_MINUTES = cooldown_minutes
    POST_EXPIRY_DAYS = expire_days
    await interaction.response.send_message("Setup updated.", ephemeral=True)

# ---------- message handler: DM welcome text + buttons ----------
WELCOME_DM = (
    "🎮 Ready to level up your journey?\n"
    "Tap **LFT** if you're a player hunting for a team.\n"
    "Tap **LFP** if you're a team looking for talent."
)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await message.channel.send(WELCOME_DM, view=ChoiceView())

# ---------- expire old posts ----------
async def expire_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = datetime.utcnow()
            to_delete = []
            for uid, mid in list(active_message_ids.items()):
                for cid in (LFT_CHANNEL_ID, LFP_CHANNEL_ID):
                    if not cid:
                        continue
                    ch = bot.get_channel(cid)
                    if not ch:
                        continue
                    try:
                        msg = await ch.fetch_message(mid)
                        age = now - msg.created_at.replace(tzinfo=None)
                        if age > timedelta(days=POST_EXPIRY_DAYS):
                            await msg.delete()
                            to_delete.append(uid)
                            break
                    except Exception:
                        continue
            for uid in to_delete:
                active_message_ids.pop(uid, None)
        except Exception:
            pass
        await asyncio.sleep(3600)

# ---------- ready ----------
@bot.event
async def on_ready():
    try:
        if GUILD_ID and GUILD_ID.isdigit():
            guild = discord.Object(id=int(GUILD_ID))
            synced = await tree.sync(guild=guild)
            print("Synced {} commands (guild) to {}".format(len(synced), GUILD_ID))
        else:
            synced = await tree.sync()
            print("Synced {} commands (global)".format(len(synced)))
        print("discord.py version:", discord.__version__)
        print("Logged in as {} (id: {})".format(bot.user, bot.user.id))
        await update_presence()
    except Exception as e:
        print("Command sync/presence failed:", e)
    bot.loop.create_task(expire_loop())

# ---------- main ----------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Put it in your .env.")
    bot.run(TOKEN)
