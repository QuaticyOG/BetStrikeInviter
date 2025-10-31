# bot.py
import os
import calendar
from datetime import datetime, timezone, timedelta
import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands
from email.message import EmailMessage
import aiosmtplib
from dotenv import load_dotenv

# -------------------- LOAD ENV --------------------
load_dotenv()
REQUIRED_VARS = ["DISCORD_TOKEN", "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER"]
missing_vars = [v for v in REQUIRED_VARS if not os.getenv(v)]
if missing_vars:
    raise SystemExit(f"Missing environment variables: {missing_vars}")

TOKEN = os.getenv("DISCORD_TOKEN")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

# -------------------- CONFIG --------------------
MEMBERS_ROLE_NAME = "Members"   # +1 point
STRIKER_ROLE_NAME = "Striker"   # +2 points
ACCOUNT_MIN_AGE_DAYS = 30
DB_PATH = "/data/db/betstrike.db"

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
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
        CREATE TABLE IF NOT EXISTS invite_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitee_id TEXT,
            inviter_id TEXT,
            join_time TEXT
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
        await db.execute("""
        CREATE TABLE IF NOT EXISTS log_channel (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT
        );""")
        await db.commit()

# -------------------- HELPER FUNCTIONS --------------------
async def get_inviter_points(user_id: int) -> int:
    """Return total points an inviter currently has."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM inviters WHERE user_id = ?", (str(user_id),))
        row = await cur.fetchone()
        return row[0] if row else 0

async def add_points(user_id: int, amount: int, reason: str = None, guild_id: int = None, invitee_id: int = None):
    """Add or subtract points and log the change."""
    uid = str(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM inviters WHERE user_id = ?", (uid,))
        row = await cur.fetchone()
        new_points = (row[0] + amount) if row else amount
        if row is None:
            await db.execute("INSERT INTO inviters (user_id, points) VALUES (?, ?)", (uid, amount))
        else:
            await db.execute("UPDATE inviters SET points = ? WHERE user_id = ?", (new_points, uid))
        await db.commit()

    if guild_id and reason and invitee_id:
        # send log to log channel
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT channel_id FROM log_channel WHERE guild_id = ?", (str(guild_id),))
            row = await cur.fetchone()
        if row:
            channel = bot.get_channel(int(row[0]))
            if channel:
                await channel.send(
                    f"✅ <@{user_id}> {'gained' if amount>0 else 'lost'} {abs(amount)} points because <@{invitee_id}> {reason}.\n"
                    f"💎 Total points: {new_points}"
                )

async def set_invite_map(invitee_id: int, inviter_id: int, valid_account: bool, used_code: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT members_awarded, striker_awarded FROM invite_map WHERE invitee_id = ?", (str(invitee_id),))
        existing = await cur.fetchone()
        if existing:
            await db.execute("""
                UPDATE invite_map
                SET inviter_id = ?, valid_account = ?, used_code = ?
                WHERE invitee_id = ?
            """, (str(inviter_id), 1 if valid_account else 0, used_code, str(invitee_id)))
        else:
            await db.execute("""
                INSERT INTO invite_map (invitee_id, inviter_id, valid_account, used_code)
                VALUES (?, ?, ?, ?)
            """, (str(invitee_id), str(inviter_id), 1 if valid_account else 0, used_code))
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
        await db.execute("INSERT OR REPLACE INTO invite_links (code, creator_id, created_at) VALUES (?, ?, ?)", 
                         (code, str(creator_id), datetime.now(timezone.utc).isoformat()))
        await db.commit()

async def get_creator_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT creator_id FROM invite_links WHERE code = ?", (code,))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def clear_all_points():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inviters")
        await db.commit()

async def top_n_inviters(n=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, points FROM inviters ORDER BY points DESC LIMIT ?", (n,))
        rows = await cur.fetchall()
        return [(int(r[0]), r[1]) for r in rows]

# -------------------- LOGGING --------------------
async def set_log_channel(guild_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO log_channel (guild_id, channel_id) VALUES (?, ?)", (str(guild_id), str(channel_id)))
        await db.commit()

async def get_log_channel(guild_id: int) -> discord.TextChannel | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT channel_id FROM log_channel WHERE guild_id = ?", (str(guild_id),))
        row = await cur.fetchone()
        if row:
            return bot.get_channel(int(row[0]))
    return None

# -------------------- EMAIL --------------------
async def send_leaderboard_email(top10):
    msg = EmailMessage()
    msg["Subject"] = "🏆 Monthly BetStrike Leaderboard Results"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    if not top10:
        content = "No leaderboard data this month."
    else:
        content = "\n".join([f"{i+1}. <@{uid}> — {pts} pts" for i, (uid, pts) in enumerate(top10)])
    msg.set_content(content)
    try:
        await aiosmtplib.send(
            msg,
            hostname="smtp.gmail.com",
            port=465,
            username=EMAIL_SENDER,
            password=EMAIL_PASSWORD,
            use_tls=True
        )
        print("[EMAIL] Leaderboard email sent.")
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

# -------------------- MONTHLY RESET --------------------
async def full_monthly_reset():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inviters;")
        await db.execute("DELETE FROM invite_links;")
        await db.execute("DELETE FROM invite_map;")
        await db.execute("DELETE FROM invite_history;")
        await db.commit()
    print("[DB] Full leaderboard and invite data reset.")

monthly_reset_done_today = False
@tasks.loop(minutes=1)
async def monthly_reset_check():
    global monthly_reset_done_today
    now = datetime.now(timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day == last_day and now.hour == 23 and now.minute == 59:
        if not monthly_reset_done_today:
            print("[SCHEDULE] Running monthly leaderboard reset...")
            top10 = await top_n_inviters(10)
            await send_leaderboard_email(top10)
            await full_monthly_reset()
            monthly_reset_done_today = True
    else:
        monthly_reset_done_today = False

@monthly_reset_check.before_loop
async def before_monthly_reset_check():
    await bot.wait_until_ready()
    print("[SCHEDULE] Monthly reset task started.")

# -------------------- DISCORD EVENTS --------------------
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
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="the leaderboard 👀"),
        status=discord.Status.online
    )
    guild = discord.Object(id=1433510848382370016)
    try:
        synced = await tree.sync(guild=guild)
        print(f"[SYNC] Synced {len(synced)} commands to guild {guild.id}.")
    except Exception as e:
        print(f"[SYNC ERROR] {e}")
    monthly_reset_check.start()

# -------------------- COMMANDS --------------------
# /getinvite
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

# /points
@tree.command(name="points", description="Check your or another user's points")
@app_commands.describe(member="Optional: user to check")
async def points(interaction: discord.Interaction, member: discord.Member | None = None):
    await interaction.response.defer(ephemeral=True)
    target = member or interaction.user
    pts = await get_inviter_points(target.id)
    embed = discord.Embed(
        title="💜 BETSTRIKE POINTS 💜",
        description=f"🏆 **{target.name}** has **{pts} points!**",
        color=discord.Color.from_str("#a16bff"),
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# /leaderboard
@tree.command(name="leaderboard", description="Show top 10 inviters")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await top_n_inviters(10)
    if not rows:
        await interaction.followup.send("No points yet.")
        return
    embed = discord.Embed(
        title="⠀⠀⠀⠀⠀⠀🏆 BetStrike Monthly Invite Leaderboard 🏆",
        description=(
            "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀💰 $1,000 Monthly Prize Pool! 💰\n"
            "💸 Invite your friends and earn points to climb the leaderboard! 💸\n\n"
            "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀✨ Top 10 inviters get amazing rewards! ✨"
        ),
        color=discord.Color.from_str("#a16bff"),
        timestamp=datetime.now(timezone.utc)
    )
    rank_emojis = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    prize_map = ["350","250","200","150","100","50","50","25","25","25"]
    for i in range(10):
        if i < len(rows):
            uid, pts = rows[i]
            name = f"<@{uid}>"
        else:
            name = "— No one yet —"
            pts = 0
        # 4 extra spaces for centering
        embed.add_field(name="‎", value=f"⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀{rank_emojis[i]} {name} {pts} pts 💵 ${prize_map[i]}", inline=False)
    await interaction.followup.send(embed=embed)

# -------------------- ADMIN COMMANDS --------------------
# /removepoints
@tree.command(name="removepoints", description="Remove points from a user (admin only)")
@app_commands.describe(member="User to remove points from", points="Number of points to remove")
@app_commands.checks.has_permissions(administrator=True)
async def removepoints(interaction: discord.Interaction, member: discord.Member, points: int):
    await interaction.response.defer(ephemeral=True)
    inviter_record = await get_inviter_for_invitee(member.id)
    guild_id = interaction.guild.id
    if inviter_record:
        await add_points(inviter_record["inviter_id"], -points, f"admin removed {points} points", guild_id=guild_id, invitee_id=member.id)
        await interaction.followup.send(f"✅ Removed {points} points from <@{member.id}>'s inviter.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Could not find inviter for this user.", ephemeral=True)

# /adjustpoints
@tree.command(name="adjustpoints", description="Admin: Adjust points for a user")
@app_commands.describe(user="Target user", amount="Points to add/subtract", reason="Reason for adjustment")
@app_commands.checks.has_permissions(administrator=True)
async def adjustpoints(interaction: discord.Interaction, user: discord.Member, amount: int, reason: str):
    await interaction.response.defer(ephemeral=True)
    await add_points(user.id, amount, reason, guild_id=interaction.guild.id, invitee_id=user.id)
    await interaction.followup.send(f"✅ Adjusted {amount} points for {user.mention}. Reason: {reason}", ephemeral=True)

# /setuplog
@tree.command(name="setuplog", description="Admin: Set the log channel for point events")
@app_commands.describe(channel="Channel to send point logs")
@app_commands.checks.has_permissions(administrator=True)
async def setuplog(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_log_channel(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"✅ Log channel set to {channel.mention}", ephemeral=True)

# /reset
@tree.command(name="reset", description="Reset all inviter points (Moderators only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reset(interaction: discord.Interaction):
    await clear_all_points()
    await interaction.response.send_message("All inviter points reset to 0.", ephemeral=True)

# /testreset
@tree.command(name="testreset", description="(Admin only) Test monthly reset + email")
@app_commands.checks.has_permissions(administrator=True)
async def testreset(interaction: discord.Interaction):
    top10 = await top_n_inviters(10)
    await send_leaderboard_email(top10)
    await full_monthly_reset()
    await interaction.response.send_message("✅ Test monthly reset complete.", ephemeral=True)

# -------------------- MEMBER EVENTS --------------------
@bot.event
async def on_member_join(member: discord.Member):
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

    guild_invites_cache[guild.id] = {invite.code: invite.uses for invite in invites_after}

    if used_inviter:
        await set_invite_map(member.id, used_inviter, valid_account, used_code)

        members_role = discord.utils.get(guild.roles, name=MEMBERS_ROLE_NAME)
        striker_role = discord.utils.get(guild.roles, name=STRIKER_ROLE_NAME)

        if members_role and members_role in member.roles:
            await add_points(used_inviter, 1, guild_id=guild.id, invitee_id=member.id, reason="joined with Members role")
            await set_awarded_flags(member.id, members_awarded=True)

        if striker_role and striker_role in member.roles:
            await add_points(used_inviter, 2, guild_id=guild.id, invitee_id=member.id, reason="joined with Striker role")
            await set_awarded_flags(member.id, striker_awarded=True)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    guild = after.guild
    inviter_record = await get_inviter_for_invitee(after.id)
    if not inviter_record or not inviter_record["valid_account"]:
        return

    inviter_id = inviter_record["inviter_id"]
    before_roles = set(r.id for r in before.roles)
    after_roles = set(r.id for r in after.roles)
    added = after_roles - before_roles
    removed = before_roles - after_roles

    members_role = discord.utils.get(guild.roles, name=MEMBERS_ROLE_NAME)
    striker_role = discord.utils.get(guild.roles, name=STRIKER_ROLE_NAME)

    if members_role:
        if members_role.id in added and not inviter_record["members_awarded"]:
            await add_points(inviter_id, 1, guild_id=guild.id, invitee_id=after.id, reason="got Members role")
            await set_awarded_flags(after.id, members_awarded=True)
        elif members_role.id in removed and inviter_record["members_awarded"]:
            await add_points(inviter_id, -1, guild_id=guild.id, invitee_id=after.id, reason="lost Members role")
            await set_awarded_flags(after.id, members_awarded=False)

    if striker_role:
        if striker_role.id in added and not inviter_record["striker_awarded"]:
            await add_points(inviter_id, 2, guild_id=guild.id, invitee_id=after.id, reason="got Striker role")
            await set_awarded_flags(after.id, striker_awarded=True)
        elif striker_role.id in removed and inviter_record["striker_awarded"]:
            await add_points(inviter_id, -2, guild_id=guild.id, invitee_id=after.id, reason="lost Striker role")
            await set_awarded_flags(after.id, striker_awarded=False)


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    inviter_record = await get_inviter_for_invitee(member.id)
    if not inviter_record or not inviter_record["valid_account"]:
        return

    inviter_id = inviter_record["inviter_id"]

    if inviter_record["members_awarded"]:
        await add_points(inviter_id, -1, guild_id=guild.id, invitee_id=member.id, reason="invitee left with Members role")
        await set_awarded_fl


# -------------------- INVITE CREATE/DELETE --------------------
@bot.event
async def on_invite_create(invite):
    guild_invites_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses

@bot.event
async def on_invite_delete(invite):
    guild_cache = guild_invites_cache.get(invite.guild.id, {})
    guild_cache.pop(invite.code, None)

# -------------------- RUN BOT --------------------
if __name__ == "__main__":
    print("Starting bot...")
    bot.run(TOKEN)
