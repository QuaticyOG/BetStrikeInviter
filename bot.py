# bot.py
required_vars = ["DISCORD_TOKEN", "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVER"]
missing_vars = [v for v in required_vars if not os.getenv(v)]
if missing_vars:
    raise SystemExit(f"Missing environment variables: {missing_vars}")
import os
from datetime import datetime, timezone, timedelta
import calendar
import asyncio
import smtplib
from email.message import EmailMessage
import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands

# -------------------- LOAD TOKEN & EMAIL CONFIG (RAILWAY ENV) -------------------
TOKEN = os.getenv("DISCORD_TOKEN")
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER")

# Roles that give points
MEMBERS_ROLE_NAME = "Members"   # +1 point
STRIKER_ROLE_NAME = "Striker"   # +2 points

# Minimum account age for points
ACCOUNT_MIN_AGE_DAYS = 30

# --- Intents --
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True
intents.presences = False
intents.messages = False
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

DB_PATH = "/data/db/betstrike.db"
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
        );
    """)
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
        await db.execute("DELETE FROM inviters")
        await db.commit()

async def top_n_inviters(n=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, points FROM inviters ORDER BY points DESC LIMIT ?", (n,))
        rows = await cur.fetchall()
        return [(int(r[0]), r[1]) for r in rows]

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
    try:
        await tree.sync()
    except Exception as e:
        print("Command sync failed:", e)

# -------------------- COMMANDS --------------------
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

@tree.command(name="points", description="Check how many points you or another user have")
@app_commands.describe(member="The user you want to check (optional)")
async def points(interaction: discord.Interaction, member: discord.Member | None = None):
    await interaction.response.defer(ephemeral=True)
    target = member or interaction.user
    user_id = str(target.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT points FROM inviters WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
    points = row[0] if row else 0
    embed = discord.Embed(
        title="💜 BETSTRIKE POINTS 💜",
        description=f"🏆 **{target.name}** currently has **{points} points!** 💸\n\n👑 Keep inviting friends to climb the leaderboard and earn rewards!",
        color=discord.Color.from_str("#a16bff"),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Use /leaderboard to view the top inviters 🏆")
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="leaderboard", description="Show top 10 inviters")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await top_n_inviters(10)
    if not rows:
        await interaction.followup.send("No points yet.")
        return

    embed = discord.Embed(
        title="🏆 Invite Leaderboard",
        color=discord.Color.from_str("#a16bff"),
        timestamp=datetime.now(timezone.utc)
    )
    rank_emojis = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    prize_map = ["350","250","200","150","100","50","50","25","25","25"]

    for i in range(10):
        prize = prize_map[i]
        rank = rank_emojis[i]
        if i < len(rows):
            user_id, points = rows[i]
            name = f"<@{user_id}>"
        else:
            name = "— No one yet —"
            points = 0
        line = f"{rank} ⠀ {name} ⠀ **POINTS:** {points} ⠀💵 ⠀**${prize} Prize**"
        embed.add_field(name="‎", value=line, inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="reset", description="Reset all inviter points (Moderators only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await clear_all_points()
    await interaction.followup.send("All inviter points reset to 0.", ephemeral=True)
    
@tree.command(name="testreset", description="(Admin only) Test the monthly reset and email now")
@app_commands.checks.has_permissions(administrator=True)
async def testreset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    top10 = await top_n_inviters(10)
    await send_leaderboard_email(top10)
    await full_monthly_reset()
    await interaction.followup.send("✅ Test monthly reset complete. Email sent and leaderboard cleared.", ephemeral=True)

# -------------------- MONTHLY RESET --------------------
async def send_leaderboard_email(top10):
    msg = EmailMessage()
    msg["Subject"] = "🏆 Monthly BetStrike Leaderboard Results"
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    if not top10:
        content = "No leaderboard data this month."
    else:
        lines = [f"{i+1}. <@{uid}> — {pts} pts" for i, (uid, pts) in enumerate(top10)]
        content = "\n".join(lines)
    msg.set_content(content)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print("[EMAIL] Leaderboard email sent.")
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

async def full_monthly_reset():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inviters;")
        await db.execute("DELETE FROM invite_links;")
        await db.execute("DELETE FROM invite_map;")
        await db.execute("DELETE FROM invite_history;")
        await db.commit()
    print("[DB] Full leaderboard and invite data reset.")

@tasks.loop(minutes=1)
async def monthly_reset_check():
    now = datetime.now(timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    if now.day == last_day and now.hour == 23 and now.minute == 59:
        print("[SCHEDULE] Running monthly leaderboard reset...")
        top10 = await top_n_inviters(10)
        await send_leaderboard_email(top10)
        await full_monthly_reset()
        await asyncio.sleep(70)

@monthly_reset_check.before_loop
async def before_monthly_reset_check():
    await bot.wait_until_ready()
    print("[SCHEDULE] Monthly reset task started.")

monthly_reset_check.start()

# -------------------- RUN BOT --------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set in environment variables.")
    print("Starting bot...")
    bot.run(TOKEN)
