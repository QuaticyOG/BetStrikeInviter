# bot.py
import os
from datetime import datetime, timezone, timedelta
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load token from .env or environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Roles that give points
MEMBERS_ROLE_NAME = "Members"   # +1 point
STRIKER_ROLE_NAME = "Striker"   # +2 points

# Minimum account age for points
ACCOUNT_MIN_AGE_DAYS = 30

# --- Intents ---
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True
intents.presences = False
intents.messages = False
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

DB_PATH = "invites.db"
guild_invites_cache = {}

# -------------------- DATABASE --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS inviters (
            user_id TEXT PRIMARY KEY,
            points INTEGER DEFAULT 0
        );""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS invite_links (
            code TEXT PRIMARY KEY,
            creator_id TEXT,
            created_at TEXT
        );""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS invite_map (
            invitee_id TEXT PRIMARY KEY,
            inviter_id TEXT,
            members_awarded INTEGER DEFAULT 0,
            striker_awarded INTEGER DEFAULT 0,
            valid_account INTEGER DEFAULT 0,
            used_code TEXT
        );""")
        await db.commit()

# -------------------- HELPER FUNCTIONS --------------------
async def add_points(user_id: int, amount: int):
    uid = str(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM inviters WHERE user_id = ?", (uid,))
        row = await cur.fetchone()
        if row is None:
            await db.execute("INSERT INTO inviters (user_id, points) VALUES (?, ?)", (uid, amount))
        else:
            await db.execute("UPDATE inviters SET points = ? WHERE user_id = ?", (row[0]+amount, uid))
        await db.commit()

async def set_invite_map(invitee_id: int, inviter_id: int, valid_account: bool, used_code: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO invite_map (invitee_id, inviter_id, valid_account, used_code) VALUES (?, ?, ?, ?)",
            (str(invitee_id), str(inviter_id), 1 if valid_account else 0, used_code)
        )
        await db.commit()

async def get_inviter_for_invitee(invitee_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT inviter_id, members_awarded, striker_awarded, valid_account FROM invite_map WHERE invitee_id = ?",
            (str(invitee_id),),
        )
        row = await cur.fetchone()
        if row:
            return {
                "inviter_id": int(row[0]),
                "members_awarded": bool(row[1]),
                "striker_awarded": bool(row[2]),
                "valid_account": bool(row[3]),
            }
        return None

async def set_awarded_flags(invitee_id: int, members_awarded: bool = None, striker_awarded: bool = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if members_awarded is not None:
            await db.execute("UPDATE invite_map SET members_awarded = ? WHERE invitee_id = ?", (1 if members_awarded else 0, str(invitee_id)))
        if striker_awarded is not None:
            await db.execute("UPDATE invite_map SET striker_awarded = ? WHERE invitee_id = ?", (1 if striker_awarded else 0, str(invitee_id)))
        await db.commit()

async def save_invite_link(code: str, creator_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO invite_links (code, creator_id, created_at) VALUES (?, ?, ?)", (code, str(creator_id), datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_creator_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT creator_id FROM invite_links WHERE code = ?", (code,))
        row = await cur.fetchone()
        if row:
            return int(row[0])
        return None

async def clear_all_points():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE inviters SET points = 0")
        await db.commit()

async def top_n_inviters(n=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, points FROM inviters ORDER BY points DESC LIMIT ?",
            (n,)
        )
        rows = await cur.fetchall()
        return [(int(r[0]), r[1]) for r in rows]

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    await init_db()
    guild_invites_cache.clear()

    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            guild_invites_cache[guild.id] = {invite.code: invite.uses for invite in invites}
        except Exception as e:
            print(f"[WARN] Could not fetch invites for {guild.name}: {e}")
            guild_invites_cache[guild.id] = {}

    print(f"✅ Bot ready: {bot.user}")
    try:
        await tree.sync()
    except Exception as e:
        print("Command sync failed:", e)

# -------------------- INVITE COMMAND --------------------
@tree.command(name="getinvite", description="Generate your personal server invite link")
async def getinvite(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    try:
        invite = await channel.create_invite(max_age=0, max_uses=0, unique=True, reason=f"Invite for {interaction.user}")
        await save_invite_link(invite.code, interaction.user.id)
        await interaction.followup.send(f"Your invite link: {invite.url}", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to create invites in this channel.", ephemeral=True)

# -------------------- MEMBER JOIN --------------------
@bot.event
async def on_member_join(member):
    guild = member.guild
    account_age = datetime.now(timezone.utc) - member.created_at
    valid_account = account_age >= timedelta(days=ACCOUNT_MIN_AGE_DAYS)

    try:
        invites_after = await guild.invites()
    except Exception:
        invites_after = []

    used_inviter = None
    used_code = None
    before_cache = guild_invites_cache.get(guild.id, {})

    # find used invite
    for inv in invites_after:
        before_uses = before_cache.get(inv.code, 0)
        if inv.uses > before_uses:
            used_code = inv.code
            creator_id = await get_creator_by_code(inv.code)
            used_inviter = creator_id or (inv.inviter.id if inv.inviter else None)
            break

    # update cache
    guild_invites_cache[guild.id] = {invite.code: invite.uses for invite in invites_after}

    print(f"[DEBUG] on_member_join: {member} | used_inviter={used_inviter} | used_code={used_code}")

    if used_inviter:
        await set_invite_map(member.id, used_inviter, valid_account, used_code)

        # ✅ award points immediately if they already have roles
        members_role = discord.utils.get(guild.roles, name=MEMBERS_ROLE_NAME)
        striker_role = discord.utils.get(guild.roles, name=STRIKER_ROLE_NAME)

        if members_role and members_role in member.roles:
            await add_points(used_inviter, 1)
            await set_awarded_flags(member.id, members_awarded=True)
        if striker_role and striker_role in member.roles:
            await add_points(used_inviter, 2)
            await set_awarded_flags(member.id, striker_awarded=True)

# -------------------- MEMBER UPDATE --------------------
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    print(f"[DEBUG] on_member_update fired for {after}.")

    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)

    added = after_roles - before_roles
    removed = before_roles - after_roles

    guild = after.guild
    members_role = discord.utils.get(guild.roles, name=MEMBERS_ROLE_NAME)
    striker_role = discord.utils.get(guild.roles, name=STRIKER_ROLE_NAME)

    inviter_record = await get_inviter_for_invitee(after.id)
    if not inviter_record or inviter_record["inviter_id"] == 0 or not inviter_record["valid_account"]:
        return

    inviter_id = inviter_record["inviter_id"]

    # Members role +1
    if members_role:
        if members_role.id in added and not inviter_record["members_awarded"]:
            await add_points(inviter_id, 1)
            await set_awarded_flags(after.id, members_awarded=True)
            print(f"[POINTS] +1 for inviter {inviter_id} (Members role).")
        elif members_role.id in removed and inviter_record["members_awarded"]:
            await add_points(inviter_id, -1)
            await set_awarded_flags(after.id, members_awarded=False)
            print(f"[POINTS] -1 for inviter {inviter_id} (Members role removed).")

    # Striker role +2
    if striker_role:
        if striker_role.id in added and not inviter_record["striker_awarded"]:
            await add_points(inviter_id, 2)
            await set_awarded_flags(after.id, striker_awarded=True)
            print(f"[POINTS] +2 for inviter {inviter_id} (Striker role).")
        elif striker_role.id in removed and inviter_record["striker_awarded"]:
            await add_points(inviter_id, -2)
            await set_awarded_flags(after.id, striker_awarded=False)
            print(f"[POINTS] -2 for inviter {inviter_id} (Striker role removed).")

# -------------------- LEADERBOARD --------------------
@tree.command(name="leaderboard", description="Show top 10 inviters")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await top_n_inviters(10)

    embed = discord.Embed(
        title="🏆  **Top 10 Inviters Leaderboard**",
        description="Here are the legends bringing in new members!",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc)
    )

    if not rows:
        embed.add_field(
            name="No inviters yet!",
            value="Be the first to invite someone and earn points!",
            inline=False
        )
    else:
        for i, (user_id, points) in enumerate(rows, start=1):
            try:
                user = await bot.fetch_user(user_id)
                name = f"{user.name}#{user.discriminator}"
            except:
                name = str(user_id)
            embed.add_field(
                name=f"#{i} — {name}",
                value=f"Points: {points}",
                inline=False
            )

    await interaction.followup.send(embed=embed)

# -------------------- RESET --------------------
@tree.command(name="reset", description="Reset all inviter points (Moderators only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await clear_all_points()
    await interaction.followup.send("All inviter points reset to 0.", ephemeral=True)

@reset.error
async def reset_error(interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("You need Manage Server permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Error: {error}", ephemeral=True)

# -------------------- INVITE CACHE UPDATES --------------------
@bot.event
async def on_invite_create(invite):
    guild_invites_cache.setdefault(invite.guild.id, {})
    guild_invites_cache[invite.guild.id][invite.code] = invite.uses

@bot.event
async def on_invite_delete(invite):
    guild_cache = guild_invites_cache.get(invite.guild.id, {})
    if invite.code in guild_cache:
        del guild_cache[invite.code]

# -------------------- RUN BOT --------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set")
    print("Starting bot...")
    bot.run(TOKEN)
