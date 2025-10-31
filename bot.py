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
    monthly_reset_check
