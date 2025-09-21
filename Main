"""
Mitsuha - Full-advanced Telegram Group Management Bot (webhook-ready)

Usage:
- Provide BOT_TOKEN, OWNER_ID, WEBHOOK_URL, DB_PATH (optional) environment variables.
- Deploy as Render Web Service (port provided by Render in PORT env var).
- Make the bot an admin in groups for moderation features.

Features:
- Welcome photo (uses provided file_id)
- Captcha verification on join (button) with timeout (kick if not verify)
- /rules, /id, /info
- /ban, /unban, /kick, /promote, /demote (admin/owner)
- /mute, /unmute (with optional duration)
- /warn (3 warns => auto ban)
- Anti-link & anti-spam basics
- Notes system (/note add/get)
- Broadcast (owner only)
- XP system & /rank
- Per-chat settings (anti_link toggle)
- SQLite persistence (simple for Render free)
"""

import os
import logging
import sqlite3
import time
import threading
from datetime import datetime, timedelta

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# ------------- CONFIG -------------
BOT_NAME = "Mitsuha"
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com/webhook
PORT = int(os.getenv("PORT", "8443"))
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
WELCOME_PHOTO_ID = "AgACAgUAAxkBAAIfdWjPYHG4Qi4ECOHe2p5oHD4poxiGAAJxyzEb3jZ4Vnzo6g3rCaNsAQADAgADeQADNgQ"
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "60"))
# ----------------------------------

if not BOT_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("BOT_TOKEN and WEBHOOK_URL environment variables are required")

# logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# sqlite setup
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER,
    user_id INTEGER,
    warns INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS notes (
    chat_id INTEGER,
    name TEXT,
    text TEXT,
    PRIMARY KEY(chat_id,name)
);
CREATE TABLE IF NOT EXISTS xp (
    chat_id INTEGER,
    user_id INTEGER,
    xp INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS pending_captcha (
    chat_id INTEGER,
    user_id INTEGER,
    until_ts INTEGER,
    PRIMARY KEY(chat_id,user_id)
);
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER PRIMARY KEY,
    anti_link INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS known_chats (chat_id INTEGER PRIMARY KEY);
""")
conn.commit()

captcha_timers = {}

# ---------- helper DB functions ----------
def set_known_chat(chat_id):
    cur.execute("INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

def add_warn(chat_id, user_id):
    cur.execute("INSERT OR IGNORE INTO warns (chat_id,user_id, warns) VALUES (?,?,0)", (chat_id, user_id))
    cur.execute("UPDATE warns SET warns = warns + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    cur.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return cur.fetchone()[0]

def reset_warns(chat_id, user_id):
    cur.execute("UPDATE warns SET warns=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()

def set_note(chat_id, name, text):
    cur.execute("INSERT OR REPLACE INTO notes (chat_id,name,text) VALUES (?,?,?)", (chat_id,name,text))
    conn.commit()

def get_note(chat_id, name):
    cur.execute("SELECT text FROM notes WHERE chat_id=? AND name=?", (chat_id, name))
    r = cur.fetchone()
    return r[0] if r else None

def add_xp(chat_id, user_id, amount=1):
    cur.execute("INSERT OR IGNORE INTO xp (chat_id,user_id,xp) VALUES (?,?,0)", (chat_id, user_id))
    cur.execute("UPDATE xp SET xp = xp + ? WHERE chat_id=? AND user_id=?", (amount, chat_id, user_id))
    conn.commit()

def get_xp(chat_id, user_id):
    cur.execute("SELECT xp FROM xp WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone()
    return r[0] if r else 0

def get_setting(chat_id, key="anti_link"):
    cur.execute("SELECT anti_link FROM settings WHERE chat_id=?", (chat_id,))
    r = cur.fetchone()
    if not r:
        # default enabled
        cur.execute("INSERT OR IGNORE INTO settings (chat_id, anti_link) VALUES (?,1)", (chat_id,))
        conn.commit()
        return 1
    return r[0]

def set_setting(chat_id, key, value):
    if key == "anti_link":
        cur.execute("INSERT OR REPLACE INTO settings (chat_id, anti_link) VALUES (?,?)", (chat_id, int(value)))
        conn.commit()

# ---------- utility ----------
async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ---------- captcha / welcome ----------
async def send_welcome_and_captcha(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user):
    txt = (f"👋 Welcome, {user.mention_html()}!\n\n"
           "To prevent spam please verify by pressing the button below within "
           f"{CAPTCHA_TIMEOUT} seconds. If you don't verify you'll be removed.")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("I'm human ✅", callback_data=f"verify:{chat_id}:{user.id}")]])
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=WELCOME_PHOTO_ID,
            caption=txt,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.warning("Failed to send welcome photo: %s", e)
        try:
            await context.bot.send_message(chat_id, txt, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    until = int(time.time()) + CAPTCHA_TIMEOUT
    cur.execute("INSERT OR REPLACE INTO pending_captcha (chat_id,user_id,until_ts) VALUES (?,?,?)", (chat_id, user.id, until))
    conn.commit()

    # start timeout thread
    def timeout_task():
        time.sleep(CAPTCHA_TIMEOUT + 1)
        try:
            cur.execute("SELECT until_ts FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            r = cur.fetchone()
            if not r:
                return
            if int(time.time()) >= r[0]:
                # still pending => kick (ban then unban)
                try:
                    # use asyncio task via app (will be scheduled below)
                    app = context.application
                    app.create_task(kick_and_unban(context, chat_id, user.id))
                except Exception as e:
                    logger.exception("Error scheduling kick: %s", e)
                cur.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user.id))
                conn.commit()
        except Exception as e:
            logger.exception("Captcha timeout error: %s", e)

    t = threading.Thread(target=timeout_task, daemon=True)
    t.start()
    captcha_timers[(chat_id, user.id)] = t

async def kick_and_unban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
        logger.info("Kicked user %s from chat %s", user_id, chat_id)
    except Exception as e:
        logger.error("Kick error: %s", e)

# ---------- handlers ----------
async def new_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    for user in msg.new_chat_members:
        # restrict first
        try:
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions(can_send_messages=False, can_send_media_messages=False,
                                            can_send_polls=False, can_send_other_messages=False,
                                            can_add_web_page_previews=False)
            )
        except Exception:
            pass
        set_known_chat(chat.id)
        await send_welcome_and_captcha(context, chat.id, user)

async def callback_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("verify:"):
        return
    _, chat_id_s, user_id_s = data.split(":")
    chat_id = int(chat_id_s); user_id = int(user_id_s)
    if query.from_user.id != user_id:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("🔒 Please press the button using the same account that joined.")
        return
    # remove pending
    cur.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    # lift restrictions
    try:
        await context.bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                        can_send_polls=True, can_send_other_messages=True,
                                        can_add_web_page_previews=True)
        )
    except Exception:
        pass
    # cancel timer thread (no op; thread ends soon automatically)
    try:
        await query.edit_message_caption((query.message.caption or "") + "\n\n✅ Verified — enjoy!", parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await query.message.reply_text("✅ Verified — enjoy!")
        except Exception:
            pass

async def anti_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat = update.effective_chat
    user = update.effective_user
    anti = get_setting(chat.id, "anti_link")
    if not anti:
        return
    text = msg.text.lower()
    if ("http://" in text) or ("https://" in text) or ("t.me/" in text) or ("discord.gg/" in text):
        if not await is_user_admin(context, chat.id, user.id):
            try:
                await msg.delete()
                await context.bot.send_message(chat.id, f"🚫 {user.mention_html()}, links are not allowed here.", parse_mode=ParseMode.HTML)
            except Exception:
                pass

async def xp_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.from_user or msg.from_user.is_bot:
        return
    add_xp(update.effective_chat.id, msg.from_user.id, 1)
    set_known_chat(update.effective_chat.id)

# ---------- commands ----------
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = ("📜 Group Rules\n"
             "1. Be respectful.\n2. No spamming or links unless allowed.\n3. No NSFW.\n4. Follow admins.")
    await update.message.reply_text(rules)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if m.reply_to_message:
        target = m.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0])
            target = (await context.bot.get_chat_member(update.effective_chat.id, uid)).user
        except Exception:
            target = m.from_user
    else:
        target = m.from_user
    await m.reply_text(f"🔎 <b>{target.first_name}</b>\nID: <code>{target.id}</code>", parse_mode=ParseMode.HTML)

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if m.reply_to_message:
        u = m.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0])
            u = await context.bot.get_chat(uid)
        except Exception:
            u = m.from_user
    else:
        u = m.from_user
    txt = f"<b>{u.first_name}</b>\nID: <code>{u.id}</code>"
    await m.reply_text(txt, parse_mode=ParseMode.HTML)

async def require_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        return True
    try:
        m = await update.effective_chat.get_member(user.id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0])
            target = await context.bot.get_chat(uid)
        except Exception:
            pass
    if not target:
        await update.message.reply_text("Usage: reply to a user or /ban <user_id>")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"🚫 Banned <b>{target.first_name}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text("✅ Unbanned.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            uid = int(context.args[0]); target = await context.bot.get_chat(uid)
        except Exception:
            pass
    if not target:
        await update.message.reply_text("Usage: reply to a user or /kick <user_id>")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(f"👢 Kicked {target.first_name}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            target = await context.bot.get_chat(int(context.args[0]))
        except Exception:
            target = None
    else:
        await update.message.reply_text("Reply to user or /mute <user_id> [seconds]")
        return
    seconds = None
    if len(context.args) >= 2:
        try: seconds = int(context.args[1])
        except: seconds = None
    until = (datetime.utcnow() + timedelta(seconds=seconds)) if seconds else None
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        await update.message.reply_text(f"🔇 Muted {target.first_name}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif context.args:
        try:
            target = await context.bot.get_chat(int(context.args[0]))
        except Exception:
            target = None
    else:
        await update.message.reply_text("Reply to user or /unmute <user_id>")
        return
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                        can_send_polls=True, can_send_other_messages=True,
                                        can_add_web_page_previews=True)
        )
        await update.message.reply_text(f"🔊 Unmuted {target.first_name}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ You must be admin to use this.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a user to warn.")
        return
    target = update.message.reply_to_message.from_user
    warns = add_warn(update.effective_chat.id, target.id)
    await update.message.reply_text(f"⚠️ {target.first_name} now has {warns} warn(s).")
    if warns >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            reset_warns(update.effective_chat.id, target.id)
            await update.message.reply_text(f"🚫 {target.first_name} has been banned for 3 warns.")
        except Exception as e:
            await update.message.reply_text(f"Error when auto-banning: {e}")

async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /note add <name> <text>  or /note get <name>")
        return
    mode = context.args[0].lower()
    if mode == "add" and len(context.args) >= 3:
        name = context.args[1]
        text = " ".join(context.args[2:])
        set_note(update.effective_chat.id, name, text)
        await update.message.reply_text("✅ Note saved.")
    elif mode == "get" and len(context.args) == 2:
        name = context.args[1]
        text = get_note(update.effective_chat.id, name)
        if text:
            await update.message.reply_text(f"📌 {name}: {text}")
        else:
            await update.message.reply_text("Note not found.")
    else:
        await update.message.reply_text("Invalid usage.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only owner can broadcast.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    txt = " ".join(context.args)
    cur.execute("SELECT chat_id FROM known_chats")
    rows = cur.fetchall()
    count = 0
    for (chat_id,) in rows:
        try:
            await context.bot.send_message(chat_id, f"📢 Broadcast:\n\n{txt}")
            count += 1
            # small sleep to avoid flood
            await context.application.create_task(asyncio_sleep(0.05))
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to ~{count} chats (known).")

# tiny async sleep helper without importing asyncio everywhere
async def asyncio_sleep(sec):
    import asyncio
    await asyncio.sleep(sec)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT COUNT(DISTINCT chat_id) FROM known_chats")
    chats = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM xp")
    users = cur.fetchone()[0] or 0
    await update.message.reply_text(f"📊 Stats\nKnown groups: {chats}\nKnown members recorded: {users}")

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    xp = get_xp(update.effective_chat.id, uid)
    await update.message.reply_text(f"🏅 {update.effective_user.first_name}, your XP: {xp}")

async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = await update.effective_chat.get_administrators()
    text = "👮 Admins:\n" + "\n".join([f"- {a.user.first_name} ({a.user.id})" for a in admins])
    await update.message.reply_text(text)

async def cmd_toggle_antilink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
    cur_val = get_setting(update.effective_chat.id, "anti_link")
    new = 0 if cur_val else 1
    set_setting(update.effective_chat.id, "anti_link", new)
    await update.message.reply_text(f"Anti-link set to: {'ON' if new else 'OFF'}")

async def cmd_alive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"{BOT_NAME} is online ✅ (webhook)")

# ------------- init application & handlers -------------
def build_app():
    return ApplicationBuilder().token(BOT_TOKEN).build()

app = build_app()

# message handlers
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members_handler))
app.add_handler(CallbackQueryHandler(callback_verify))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), anti_link_handler))
app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), xp_on_message))

# commands
app.add_handler(CommandHandler("rules", cmd_rules))
app.add_handler(CommandHandler("id", cmd_id))
app.add_handler(CommandHandler("info", cmd_info))
app.add_handler(CommandHandler("ban", cmd_ban))
app.add_handler(CommandHandler("unban", cmd_unban))
app.add_handler(CommandHandler("kick", cmd_kick))
app.add_handler(CommandHandler("mute", cmd_mute))
app.add_handler(CommandHandler("unmute", cmd_unmute))
app.add_handler(CommandHandler("warn", cmd_warn))
app.add_handler(CommandHandler("note", cmd_note))
app.add_handler(CommandHandler("broadcast", cmd_broadcast))
app.add_handler(CommandHandler("stats", cmd_stats))
app.add_handler(CommandHandler("rank", cmd_rank))
app.add_handler(CommandHandler("admins", cmd_admins))
app.add_handler(CommandHandler("toggle_antilink", cmd_toggle_antilink))
app.add_handler(CommandHandler("alive", cmd_alive))

# ------------- start webhook -------------
def main():
    logger.info("Starting Mitsuha (webhook mode)...")
    # Will serve on PORT, and set webhook to WEBHOOK_URL
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    main()
