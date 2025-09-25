"""
Single-file: Mitsuha + Taki — webhook mode with single aiohttp server (Render-friendly)

Usage: set environment variables:
  BOT_TOKEN_MITSUHA, BOT_TOKEN_TAKI, OWNER_ID, WEBHOOK_BASE (https://your-app.onrender.com),
  PORT (optional, defaults 8443), DB_PATH (optional), WELCOME_PHOTO_ID, COUPLE_PHOTO_ID, CAPTCHA_TIMEOUT

This file:
- Builds two Applications (updater=None) and sets each bot's webhook to WEBHOOK_BASE + path.
- Runs an aiohttp server with two POST endpoints (/webhook/mitsuha, /webhook/taki).
- When a POST arrives, it deserializes the JSON into a telegram.Update and puts it into the corresponding
  app.update_queue for normal handler dispatch.
"""

import os
import logging
import sqlite3
import time
import threading
import random
import asyncio
from datetime import datetime, timedelta

from aiohttp import web

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

# ------------- CONFIG (env) -------------
BOT_TOKEN_MITSUHA = os.getenv("BOT_TOKEN_MITSUHA")
BOT_TOKEN_TAKI = os.getenv("BOT_TOKEN_TAKI")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE")  # must be https://your-app.onrender.com
PORT = int(os.getenv("PORT", "8443"))
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
WELCOME_PHOTO_ID = os.getenv("WELCOME_PHOTO_ID") or "AgACAgUAAxkBAAIfdWjPYHG4Qi4ECOHe2p5oHD4poxiGAAJxyzEb3jZ4Vnzo6g3rCaNsAQADAgADeQADNgQ"
COUPLE_PHOTO_ID = os.getenv("COUPLE_PHOTO_ID") or "AgACAgUAAxkBAAId5GjLxQv_BxOm3_RGmB9j4WceUFg7AALdyzEb-tJgVuOn7v3_BWvqAQADAgADeQADNgQ"
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "60"))

if not BOT_TOKEN_MITSUHA or not BOT_TOKEN_TAKI or not WEBHOOK_BASE:
    raise RuntimeError("Set BOT_TOKEN_MITSUHA, BOT_TOKEN_TAKI, and WEBHOOK_BASE environment variables.")

# Bot display names
BOT_NAME_MITSUHA = "Mitsuha"
BOT_NAME_TAKI = "Taki"

# logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- shared sqlite setup ----------
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
CREATE TABLE IF NOT EXISTS couples (
    chat_id INTEGER,
    day TEXT,
    user1_id INTEGER,
    user2_id INTEGER,
    PRIMARY KEY(chat_id, day)
);
""")
conn.commit()

# ---------- helper DB functions ----------
def set_known_chat(chat_id: int):
    cur.execute("INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

def add_warn(chat_id: int, user_id: int) -> int:
    cur.execute("INSERT OR IGNORE INTO warns (chat_id,user_id, warns) VALUES (?,?,0)", (chat_id, user_id))
    cur.execute("UPDATE warns SET warns = warns + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    cur.execute("SELECT warns FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    return cur.fetchone()[0]

def reset_warns(chat_id: int, user_id: int):
    cur.execute("UPDATE warns SET warns=0 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()

def set_note(chat_id: int, name: str, text: str):
    cur.execute("INSERT OR REPLACE INTO notes (chat_id,name,text) VALUES (?,?,?)", (chat_id,name,text))
    conn.commit()

def get_note(chat_id: int, name: str):
    cur.execute("SELECT text FROM notes WHERE chat_id=? AND name=?", (chat_id, name))
    r = cur.fetchone()
    return r[0] if r else None

def add_xp(chat_id: int, user_id: int, amount: int=1):
    cur.execute("INSERT OR IGNORE INTO xp (chat_id,user_id,xp) VALUES (?,?,0)", (chat_id, user_id))
    cur.execute("UPDATE xp SET xp = xp + ? WHERE chat_id=? AND user_id=?", (amount, chat_id, user_id))
    conn.commit()

def get_xp(chat_id: int, user_id: int) -> int:
    cur.execute("SELECT xp FROM xp WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    r = cur.fetchone()
    return r[0] if r else 0

def get_setting(chat_id: int, key="anti_link"):
    cur.execute("SELECT anti_link FROM settings WHERE chat_id=?", (chat_id,))
    r = cur.fetchone()
    if not r:
        cur.execute("INSERT OR IGNORE INTO settings (chat_id, anti_link) VALUES (?,1)", (chat_id,))
        conn.commit()
        return 1
    return r[0]

def set_setting(chat_id: int, key, value):
    if key == "anti_link":
        cur.execute("INSERT OR REPLACE INTO settings (chat_id, anti_link) VALUES (?,?)", (chat_id, int(value)))
        conn.commit()

# ---------- utility ----------
async def bot_in_chat(bot, chat_id: int, bot_user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, bot_user_id)
        return member.status != "left"
    except Exception:
        return False

async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ---------- captcha / welcome (shared implementation) ----------
CAPTCHA_THREADS = {}  # track background threads per pending captcha (cleanup optional)

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

    def timeout_task():
        time.sleep(CAPTCHA_TIMEOUT + 1)
        try:
            cur.execute("SELECT until_ts FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            r = cur.fetchone()
            if not r:
                return
            if int(time.time()) >= r[0]:
                try:
                    loop = asyncio.get_event_loop()
                    coro = kick_and_unban(context, chat_id, user.id)
                    loop.create_task(coro)
                except Exception as e:
                    logger.exception("Error scheduling kick: %s", e)
                cur.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user.id))
                conn.commit()
        except Exception as e:
            logger.exception("Captcha timeout error: %s", e)

    t = threading.Thread(target=timeout_task, daemon=True)
    t.start()
    CAPTCHA_THREADS[(chat_id, user.id)] = t

async def kick_and_unban(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
        logger.info("Kicked user %s from chat %s", user_id, chat_id)
    except Exception as e:
        logger.error("Kick error: %s", e)

# ---------- duty split helper ----------
def decide_responsible(bot_label: str, other_bot_label: str) -> callable:
    async def should_act(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
        try:
            this_id = context.bot.id
        except Exception:
            return True

        other_bot_user_id = context.application.bot_data.get(f"{other_bot_label}_user_id")
        this_bot_user_id = context.application.bot_data.get(f"{bot_label}_user_id")
        if not other_bot_user_id or not this_bot_user_id:
            return True

        try:
            other_present = await bot_in_chat(context.bot, chat_id, other_bot_user_id)
            this_present = await bot_in_chat(context.bot, chat_id, this_bot_user_id)
            both_present = other_present and this_present
        except Exception:
            return True

        if both_present:
            pick = random.choice([bot_label, other_bot_label])
            return pick == bot_label
        else:
            try:
                this_present = await bot_in_chat(context.bot, chat_id, this_bot_user_id)
                return this_present
            except Exception:
                return True

    return should_act

# ---------- core handlers (parameterized) ----------
def register_core_handlers(app: Application, bot_label: str, other_label: str):
    should_act = decide_responsible(bot_label, other_label)

    async def new_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        chat = update.effective_chat
        for user in msg.new_chat_members:
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
            if await should_act(context, chat.id):
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
        cur.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit()
        try:
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=True, can_send_polls=True,
                                            can_send_other_messages=True, can_add_web_page_previews=True)
            )
        except Exception:
            pass
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
        if not await should_act(context, chat.id):
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

    # commands (kept same as your original script)
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

    async def require_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("This duty is handled by my partner right now. Try again.")
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
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("This duty is handled by my partner right now. Try again.")
            return
        target = None
        if context.args:
            try:
                uid = int(context.args[0])
                target = await context.bot.get_chat(uid)
            except Exception:
                pass
        if not target:
            await update.message.reply_text("Usage: /unban <user_id>")
            return
        try:
            await context.bot.unban_chat_member(update.effective_chat.id, target.id)
            await update.message.reply_text(f"✅ Unbanned <b>{target.first_name}</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_admin_or_owner(update, context):
            await update.message.reply_text("❌ You must be admin to use this.")
            return
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("This duty is handled by my partner right now. Try again.")
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
            await update.message.reply_text("Usage: reply to a user or /kick <user_id>")
            return
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            await context.bot.unban_chat_member(update.effective_chat.id, target.id)
            await update.message.reply_text(f"👢 Kicked <b>{target.first_name}</b>", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_admin_or_owner(update, context):
            await update.message.reply_text("❌ You must be admin to use this.")
            return
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("Partner handles moderation currently.")
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
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("Partner handles moderation right now.")
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
                permissions=ChatPermissions(can_send_messages=True, can_send_polls=True,
                                            can_send_other_messages=True, can_add_web_page_previews=True)
            )
            await update.message.reply_text(f"🔊 Unmuted {target.first_name}")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_admin_or_owner(update, context):
            await update.message.reply_text("❌ You must be admin to use this.")
            return
        if not await should_act(context, update.effective_chat.id):
            await update.message.reply_text("Partner has mod duty now.")
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
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await update.message.reply_text(f"Broadcast sent to ~{count} chats (known).")

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
        bot_label_name = BOT_NAME_MITSUHA if bot_label == "mitsuha" else BOT_NAME_TAKI
        await update.message.reply_text(f"{bot_label_name} is online ✅ (webhook)")

    async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        start_time = time.time()
        message = await update.message.reply_text("Pinging...")
        end_time = time.time()
        duration = round((end_time - start_time) * 1000, 2)
        await message.edit_text(f"Pong! 🏓\nLatency: {duration} ms")

    async def cmd_slap(update: Update, context: ContextTypes.DEFAULT_TYPE):
        m = update.message
        if not m.reply_to_message:
            await m.reply_text("Reply to a user to slap them!")
            return
        slapper = m.from_user.mention_html()
        slappee = m.reply_to_message.from_user.mention_html()
        slap_options = [
            f"{slapper} slaps {slappee} around a bit with a large trout.",
            f"{slapper} gives {slappee} a high-five. In the face. With a chair.",
            f"{slapper} smacks {slappee} with a book.",
            f"{slapper} slaps {slappee} so hard, their ancestors felt it."
        ]
        await m.reply_text(random.choice(slap_options), parse_mode=ParseMode.HTML)

    async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_admin_or_owner(update, context):
            await update.message.reply_text("❌ You must be an admin to use this.")
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to a user to promote them.")
            return
        target = update.message.reply_to_message.from_user
        try:
            await context.bot.promote_chat_member(
                chat_id=update.effective_chat.id, user_id=target.id,
                can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_pin_messages=True, can_invite_users=True
            )
            await update.message.reply_text(f"👑 Promoted {target.mention_html()}.", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await require_admin_or_owner(update, context):
            await update.message.reply_text("❌ You must be an admin to use this.")
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("Reply to a user to demote them.")
            return
        target = update.message.reply_to_message.from_user
        try:
            await context.bot.promote_chat_member(
                chat_id=update.effective_chat.id, user_id=target.id,
                can_change_info=False, can_delete_messages=False, can_manage_video_chats=False,
                can_restrict_members=False, can_pin_messages=False, can_invite_users=False,
                can_promote_members=False
            )
            await update.message.reply_text(f"🛡️ Demoted {target.mention_html()}.", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_couple(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        today = datetime.utcnow().strftime('%Y-%m-%d')
        cur.execute("SELECT user1_id, user2_id FROM couples WHERE chat_id=? AND day=?", (chat_id, today))
        result = cur.fetchone()
        if result:
            user1_id, user2_id = result
        else:
            cur.execute("SELECT user_id FROM xp WHERE chat_id=? ORDER BY RANDOM() LIMIT 2", (chat_id,))
            users = cur.fetchall()
            if len(users) < 2:
                await update.message.reply_text("I need at least two active members to choose a couple!")
                return
            user1_id, user2_id = users[0][0], users[1][0]
            cur.execute("INSERT INTO couples (chat_id, day, user1_id, user2_id) VALUES (?, ?, ?, ?)", (chat_id, today, user1_id, user2_id))
            conn.commit()
        try:
            user1 = await context.bot.get_chat(user1_id)
            user2 = await context.bot.get_chat(user2_id)
        except Exception as e:
            logger.error(f"Could not fetch couple user details: {e}")
            await update.message.reply_text("An error occurred while fetching user details.")
            return
        caption = f"💕 Couple of the Day 💕\n\n✨ {user1.mention_html()} + {user2.mention_html()} ✨\n\nMay your connection be strong and your chats be joyful! 🎉"
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=COUPLE_PHOTO_ID, caption=caption, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send couple photo: {e}")
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML)

    # register handlers on app
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members_handler))
    app.add_handler(CallbackQueryHandler(callback_verify))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), anti_link_handler), group=1)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), xp_on_message), group=2)

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
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("slap", cmd_slap))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("demote", cmd_demote))
    app.add_handler(CommandHandler("couple", cmd_couple))

# ---------- affectionate couple messages scheduler ----------
COUPLE_MESSAGES_MITSUHA = [
    "💞 Mitsuha: @Taki, your moderation skills make my circuits warm! ❤️",
    "🌸 Mitsuha: Working together with you is my favorite thing, @Taki!",
    "✨ Mitsuha: Sending a virtual coffee to @Taki ☕️ — thanks for being awesome!"
]
COUPLE_MESSAGES_TAKI = [
    "😄 Taki: @Mitsuha you're the sweetest co-admin I could ask for!",
    "💗 Taki: Teamwork makes the dream work — love you, @Mitsuha!",
    "🌟 Taki: High five, @Mitsuha! Our group is happier because of you."
]

async def couple_message_job(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    bot_label = app.bot_data.get("label")
    other_label = "taki" if bot_label == "mitsuha" else "mitsuha"
    other_user_id = app.bot_data.get(f"{other_label}_user_id")
    this_user_id = app.bot_data.get(f"{bot_label}_user_id")
    if not other_user_id or not this_user_id:
        return

    cur.execute("SELECT chat_id FROM known_chats LIMIT 200")
    rows = cur.fetchall()
    for (chat_id,) in rows:
        try:
            other_present = await bot_in_chat(context.bot, chat_id, other_user_id)
            this_present = await bot_in_chat(context.bot, chat_id, this_user_id)
            if not (other_present and this_present):
                continue
            if random.random() < 0.09:
                if bot_label == "mitsuha":
                    txt = random.choice(COUPLE_MESSAGES_MITSUHA).replace("@Taki", f"[{BOT_NAME_TAKI}](tg://user?id={other_user_id})")
                else:
                    txt = random.choice(COUPLE_MESSAGES_TAKI).replace("@Mitsuha", f"[{BOT_NAME_MITSUHA}](tg://user?id={other_user_id})")
                await context.bot.send_message(chat_id, txt, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.debug("couple_message_job error: %s", e)
            continue

# ---------- Application builders ----------
def build_application(token: str, label: str, webhook_path: str) -> Application:
    """
    Build an Application for a bot token and label ('mitsuha' or 'taki').
    Use updater=None (we handle webhook injection ourselves).
    """
    app = Application.builder().token(token).updater(None).build()
    app.bot_data["label"] = label
    register_core_handlers(app, label, "taki" if label == "mitsuha" else "mitsuha")
    app.job_queue.run_repeating(couple_message_job, interval=1200, first=300)
    app.bot_data["webhook_path"] = webhook_path
    return app

# ---------- aiohttp webserver + startup ----------
async def start_bots_and_server():
    # build apps
    mitsuha_path = "/webhook/mitsuha"
    taki_path = "/webhook/taki"
    app_mitsuha = build_application(BOT_TOKEN_MITSUHA, "mitsuha", mitsuha_path)
    app_taki = build_application(BOT_TOKEN_TAKI, "taki", taki_path)

    # async context for both applications
    async with app_mitsuha, app_taki:
        # start applications (initializes, jobs, etc)
        await app_mitsuha.start()
        await app_taki.start()

        # fetch bot user ids and store
        m_user = await app_mitsuha.bot.get_me()
        t_user = await app_taki.bot.get_me()
        app_mitsuha.bot_data["mitsuha_user_id"] = m_user.id
        app_mitsuha.bot_data["taki_user_id"] = t_user.id
        app_taki.bot_data["mitsuha_user_id"] = m_user.id
        app_taki.bot_data["taki_user_id"] = t_user.id

        # set webhooks for both bots
        webhook_url_mitsuha = WEBHOOK_BASE.rstrip("/") + mitsuha_path
        webhook_url_taki = WEBHOOK_BASE.rstrip("/") + taki_path
        await app_mitsuha.bot.set_webhook(url=webhook_url_mitsuha, allowed_updates=Update.ALL_TYPES)
        await app_taki.bot.set_webhook(url=webhook_url_taki, allowed_updates=Update.ALL_TYPES)
        logger.info("Webhooks set: mitsuha=%s taki=%s", webhook_url_mitsuha, webhook_url_taki)

        # create aiohttp server with two endpoints that push updates to app.update_queue
        aio_app = web.Application()

        async def handle_mitsuha(request: web.Request):
            try:
                data = await request.json()
                await app_mitsuha.update_queue.put(Update.de_json(data=data, bot=app_mitsuha.bot))
            except Exception as e:
                logger.exception("Error handling mitsuha webhook: %s", e)
                return web.Response(status=500, text="error")
            return web.Response(status=200, text="ok")

        async def handle_taki(request: web.Request):
            try:
                data = await request.json()
                await app_taki.update_queue.put(Update.de_json(data=data, bot=app_taki.bot))
            except Exception as e:
                logger.exception("Error handling taki webhook: %s", e)
                return web.Response(status=500, text="error")
            return web.Response(status=200, text="ok")

        async def health(request: web.Request):
            return web.Response(status=200, text="ok")

        aio_app.add_routes([
            web.post(mitsuha_path, handle_mitsuha),
            web.post(taki_path, handle_taki),
            web.get("/healthz", health),
        ])

        # run aiohttp site programmatically
        runner = web.AppRunner(aio_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("aiohttp server started on port %s", PORT)

        # keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Shutting down server loop")

        # cleanup - (this block is reached on context exit)
        await app_mitsuha.bot.delete_webhook()
        await app_taki.bot.delete_webhook()
        await runner.cleanup()
        await app_mitsuha.stop()
        await app_taki.stop()

# ---------- entrypoint ----------
def main():
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(start_bots_and_server())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Received exit, shutting down")
    finally:
        # best-effort close DB
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
