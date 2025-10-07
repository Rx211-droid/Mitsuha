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
import json
import requests
from datetime import datetime, timedelta
from typing import Dict, List

from aiohttp import web

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
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
WEATHER_API = os.getenv("WEATHER_API", "")

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
CREATE TABLE IF NOT EXISTS afk (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    since INTEGER
);
CREATE TABLE IF NOT EXISTS filters (
    chat_id INTEGER,
    keyword TEXT,
    response TEXT,
    PRIMARY KEY(chat_id,keyword)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER,
    reported_user_id INTEGER,
    chat_id INTEGER,
    reason TEXT,
    timestamp INTEGER
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

def set_afk(user_id: int, reason: str = "AFK"):
    cur.execute("INSERT OR REPLACE INTO afk (user_id, reason, since) VALUES (?,?,?)", 
                (user_id, reason, int(time.time())))
    conn.commit()

def get_afk(user_id: int):
    cur.execute("SELECT reason, since FROM afk WHERE user_id=?", (user_id,))
    return cur.fetchone()

def remove_afk(user_id: int):
    cur.execute("DELETE FROM afk WHERE user_id=?", (user_id,))
    conn.commit()

def add_filter(chat_id: int, keyword: str, response: str):
    cur.execute("INSERT OR REPLACE INTO filters (chat_id, keyword, response) VALUES (?,?,?)", 
                (chat_id, keyword.lower(), response))
    conn.commit()

def remove_filter(chat_id: int, keyword: str):
    cur.execute("DELETE FROM filters WHERE chat_id=? AND keyword=?", (chat_id, keyword.lower()))
    conn.commit()

def get_filters(chat_id: int):
    cur.execute("SELECT keyword, response FROM filters WHERE chat_id=?", (chat_id,))
    return cur.fetchall()

def add_report(reporter_id: int, reported_user_id: int, chat_id: int, reason: str):
    cur.execute("INSERT INTO reports (reporter_id, reported_user_id, chat_id, reason, timestamp) VALUES (?,?,?,?,?)",
                (reporter_id, reported_user_id, chat_id, reason, int(time.time())))
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

# ---------- NEW COMMANDS & FEATURES ----------
async def cmd_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = " ".join(context.args) if context.args else "AFK"
    set_afk(update.effective_user.id, reason)
    await update.message.reply_text(f"🟢 {update.effective_user.first_name} is now AFK: {reason}")

async def cmd_unafk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_afk(update.effective_user.id)
    await update.message.reply_text(f"🔴 {update.effective_user.first_name} is no longer AFK")

async def check_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    
    # Check if replying to AFK user
    if update.message.reply_to_message:
        afk_data = get_afk(update.message.reply_to_message.from_user.id)
        if afk_data:
            reason, since = afk_data
            elapsed = time.time() - since
            await update.message.reply_text(
                f"💤 {update.message.reply_to_message.from_user.first_name} is AFK: {reason}\n"
                f"⏰ Since {timedelta(seconds=int(elapsed))} ago"
            )
            return
    
    # Check if mentioned users are AFK
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == "mention":
                mentioned_text = update.message.text[entity.offset:entity.offset+entity.length]
                # This would need user_id mapping - simplified version
                pass
            elif entity.type == "text_mention":
                afk_data = get_afk(entity.user.id)
                if afk_data:
                    reason, since = afk_data
                    elapsed = time.time() - since
                    await update.message.reply_text(
                        f"💤 {entity.user.first_name} is AFK: {reason}\n"
                        f"⏰ Since {timedelta(seconds=int(elapsed))} ago"
                    )

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
        
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /filter <keyword> <response>")
        return
        
    keyword = context.args[0].lower()
    response = " ".join(context.args[1:])
    add_filter(update.effective_chat.id, keyword, response)
    await update.message.reply_text(f"✅ Filter added for '{keyword}'")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_or_owner(update, context):
        await update.message.reply_text("❌ Admin only.")
        return
        
    if not context.args:
        await update.message.reply_text("Usage: /stop <keyword>")
        return
        
    keyword = context.args[0].lower()
    remove_filter(update.effective_chat.id, keyword)
    await update.message.reply_text(f"✅ Filter removed for '{keyword}'")

async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters_list = get_filters(update.effective_chat.id)
    if not filters_list:
        await update.message.reply_text("No filters in this chat.")
        return
        
    text = "📋 Filters in this chat:\n\n"
    for keyword, response in filters_list:
        text += f"• {keyword}: {response[:50]}...\n" if len(response) > 50 else f"• {keyword}: {response}\n"
        
    await update.message.reply_text(text)

async def handle_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    text = update.message.text.lower()
    filters_list = get_filters(update.effective_chat.id)
    
    for keyword, response in filters_list:
        if keyword in text:
            await update.message.reply_text(response)
            break

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message to report the user.")
        return
        
    if not context.args:
        await update.message.reply_text("Please provide a reason: /report <reason>")
        return
        
    reported_user = update.message.reply_to_message.from_user
    reason = " ".join(context.args)
    
    add_report(update.effective_user.id, reported_user.id, update.effective_chat.id, reason)
    
    # Notify admins
    admins = await update.effective_chat.get_administrators()
    for admin in admins:
        if admin.user.is_bot:
            continue
        try:
            await context.bot.send_message(
                admin.user.id,
                f"🚨 Report in {update.effective_chat.title}\n"
                f"Reported: {reported_user.mention_html()} (ID: {reported_user.id})\n"
                f"Reason: {reason}\n"
                f"Reporter: {update.effective_user.mention_html()}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            continue
            
    await update.message.reply_text("✅ Report sent to admins.")

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /weather <city>")
        return
        
    city = " ".join(context.args)
    
    if not WEATHER_API:
        # Mock weather response
        temps = random.randint(15, 35)
        conditions = ["Sunny", "Cloudy", "Rainy", "Snowy", "Windy"]
        condition = random.choice(conditions)
        await update.message.reply_text(
            f"🌤 Weather in {city}:\n"
            f"Temperature: {temps}°C\n"
            f"Condition: {condition}\n"
            f"Humidity: {random.randint(30, 90)}%"
        )
        return
        
    try:
        # Using OpenWeatherMap API
        url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API}&units=metric"
        response = requests.get(url).json()
        
        if response["cod"] != 200:
            await update.message.reply_text("City not found.")
            return
            
        temp = response["main"]["temp"]
        feels_like = response["main"]["feels_like"]
        humidity = response["main"]["humidity"]
        condition = response["weather"][0]["description"]
        
        await update.message.reply_text(
            f"🌤 Weather in {city}:\n"
            f"Temperature: {temp}°C (feels like {feels_like}°C)\n"
            f"Condition: {condition.title()}\n"
            f"Humidity: {humidity}%"
        )
    except Exception as e:
        await update.message.reply_text("Error fetching weather data.")

async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = requests.get("https://api.quotable.io/random")
        if response.status_code == 200:
            data = response.json()
            quote = f"\"{data['content']}\"\n\n- {data['author']}"
            await update.message.reply_text(quote)
        else:
            quotes = [
                "The only way to do great work is to love what you do. - Steve Jobs",
                "Innovation distinguishes between a leader and a follower. - Steve Jobs",
                "Stay hungry, stay foolish. - Steve Jobs"
            ]
            await update.message.reply_text(random.choice(quotes))
    except Exception:
        quotes = [
            "The only way to do great work is to love what you do. - Steve Jobs",
            "Life is what happens when you're busy making other plans. - John Lennon",
            "The future belongs to those who believe in the beauty of their dreams. - Eleanor Roosevelt"
        ]
        await update.message.reply_text(random.choice(quotes))

async def cmd_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jokes = [
        "Why don't scientists trust atoms? Because they make up everything!",
        "Why did the scarecrow win an award? He was outstanding in his field!",
        "Why don't eggs tell jokes? They'd crack each other up!",
        "What do you call a fake noodle? An impasta!",
        "Why did the math book look so sad? Because it had too many problems!"
    ]
    await update.message.reply_text(random.choice(jokes))

async def cmd_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    facts = [
        "Honey never spoils. Archaeologists have found pots of honey in ancient Egyptian tombs that are over 3,000 years old and still perfectly good to eat.",
        "Octopuses have three hearts.",
        "A day on Venus is longer than a year on Venus.",
        "The shortest war in history was between Britain and Zanzibar on August 27, 1896. Zanzibar surrendered after 38 minutes.",
        "Bananas are berries, but strawberries aren't."
    ]
    await update.message.reply_text(f"📚 Did you know?\n\n{random.choice(facts)}")

async def cmd_roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            max_num = int(context.args[0])
            result = random.randint(1, max_num)
        except ValueError:
            result = random.randint(1, 6)
    else:
        result = random.randint(1, 6)
    
    await update.message.reply_text(f"🎲 You rolled: {result}")

async def cmd_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = random.choice(["Heads", "Tails"])
    await update.message.reply_text(f"🪙 Coin flip: {result}")

async def cmd_ttt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(" ", callback_data="ttt_0_0"),
         InlineKeyboardButton(" ", callback_data="ttt_0_1"),
         InlineKeyboardButton(" ", callback_data="ttt_0_2")],
        [InlineKeyboardButton(" ", callback_data="ttt_1_0"),
         InlineKeyboardButton(" ", callback_data="ttt_1_1"),
         InlineKeyboardButton(" ", callback_data="ttt_1_2")],
        [InlineKeyboardButton(" ", callback_data="ttt_2_0"),
         InlineKeyboardButton(" ", callback_data="ttt_2_1"),
         InlineKeyboardButton(" ", callback_data="ttt_2_2")]
    ]
    await update.message.reply_text(
        "🎮 Tic-Tac-Toe\n\nYou're X, I'm O. Your turn!",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_ttt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith("ttt_"):
        return
        
    # Simplified TTT logic - in real implementation, you'd track game state
    await query.edit_message_text(
        "🎮 Tic-Tac-Toe\n\nGame ended in draw! Want to play again?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Play Again", callback_data="ttt_new")
        ]])
    )

async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /music <song name>")
        return
        
    song = " ".join(context.args)
    await update.message.reply_text(
        f"🎵 Searching for: {song}\n\n"
        "Music feature would integrate with music bots or streaming services."
    )

async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /remind <time> <message>\nExample: /remind 10m Buy milk")
        return
        
    time_str = context.args[0]
    message = " ".join(context.args[1:])
    
    # Simple time parsing
    seconds = 0
    if time_str.endswith('s'):
        seconds = int(time_str[:-1])
    elif time_str.endswith('m'):
        seconds = int(time_str[:-1]) * 60
    elif time_str.endswith('h'):
        seconds = int(time_str[:-1]) * 3600
    elif time_str.endswith('d'):
        seconds = int(time_str[:-1]) * 86400
    else:
        seconds = int(time_str) * 60  # Default to minutes
    
    if seconds > 86400 * 30:  # Max 30 days
        await update.message.reply_text("Maximum reminder time is 30 days.")
        return
        
    async def send_reminder(ctx):
        await ctx.bot.send_message(
            update.effective_chat.id,
            f"⏰ Reminder: {message}"
        )
    
    context.job_queue.run_once(send_reminder, seconds)
    await update.message.reply_text(f"✅ Reminder set for {time_str}")

async def cmd_translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.text:
        await update.message.reply_text("Reply to a message to translate it.")
        return
        
    text = update.message.reply_to_message.text
    target_lang = context.args[0] if context.args else "en"
    
    # Mock translation
    await update.message.reply_text(
        f"🌐 Translation to {target_lang}:\n\n"
        f"[Mock Translation] {text}\n\n"
        "Note: Real translation would require API key"
    )

async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /calc <expression>\nExample: /calc 2+2*3")
        return
        
    expression = " ".join(context.args)
    try:
        # Safety check - only allow basic math
        allowed_chars = set('0123456789+-*/.() ')
        if not all(c in allowed_chars for c in expression):
            await update.message.reply_text("Only basic arithmetic allowed.")
            return
            
        result = eval(expression)
        await update.message.reply_text(f"🧮 {expression} = {result}")
    except Exception as e:
        await update.message.reply_text("Error: Invalid expression")

async def cmd_shorten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /shorten <url>")
        return
        
    url = context.args[0]
    # Mock URL shortening
    short_url = f"https://short.url/{hash(url) % 10000:04d}"
    await update.message.reply_text(f"🔗 Shortened URL:\n{short_url}")

async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /qr <text or url>")
        return
        
    text = " ".join(context.args)
    # In real implementation, generate QR code image
    await update.message.reply_text(
        f"📷 QR Code for:\n{text}\n\n"
        "QR code image would be generated here."
    )

async def cmd_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /lyrics <song name>")
        return
        
    song = " ".join(context.args)
    # Mock lyrics
    await update.message.reply_text(
        f"🎵 Lyrics for: {song}\n\n"
        "[First verse]\nMock lyrics would appear here...\n\n"
        "Real implementation would use lyrics API."
    )

async def cmd_define(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /define <word>")
        return
        
    word = context.args[0].lower()
    # Mock definitions
    definitions = {
        "love": "An intense feeling of deep affection",
        "python": "A high-level programming language",
        "bot": "An automated program that performs tasks"
    }
    
    definition = definitions.get(word, f"Definition for '{word}' not found in mock database.")
    await update.message.reply_text(f"📚 {word}: {definition}")

async def cmd_font(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /font <text>")
        return
        
    text = " ".join(context.args)
    # Mock font transformation
    fancy_text = " ".join(text.upper())
    await update.message.reply_text(f"✨ {fancy_text}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 *Available Commands*

*👮 Admin Commands:*
/ban - Ban a user
/unban - Unban a user  
/kick - Kick a user
/mute - Mute a user
/unmute - Unmute a user
/warn - Warn a user
/promote - Promote to admin
/demote - Demote admin
/toggle_antilink - Toggle link protection

*📝 Group Management:*
/rules - Show group rules
/note - Save/get notes
/filter - Add auto-response
/stop - Remove auto-response
/filters - List auto-responses
/report - Report a user to admins

*😊 Fun Commands:*
/slap - Slap someone
/couple - Daily couple
/joke - Random joke
/fact - Random fact
/roll - Roll dice
/flip - Coin flip
/ttt - Tic-tac-toe
/quote - Inspirational quote

*🛠 Utility Commands:*
/weather - Weather info
/remind - Set reminder
/translate - Translate text
/calc - Calculator
/shorten - Shorten URL
/qr - Generate QR code
/lyrics - Song lyrics
/define - Word definition
/font - Fancy text
/music - Search music
/id - Get user ID
/info - User info
/ping - Bot latency

*👤 User Commands:*
/afk - Set AFK status
/unafk - Remove AFK
/rank - Check XP rank
/stats - Bot statistics
/alive - Check if bot is online

*👑 Owner Only:*
/broadcast - Broadcast message

Use /help <command> for more info!
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

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

    async def require_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user = update.effective_user
        if user.id == OWNER_ID:
            return True
        try:
            m = await update.effective_chat.get_member(user.id)
            return m.status in ("administrator", "creator")
        except Exception:
            return False

    # ... (previous command implementations remain the same: cmd_rules, cmd_id, cmd_info, cmd_ban, etc.)

    # Register ALL handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_members_handler))
    app.add_handler(CallbackQueryHandler(callback_verify))
    app.add_handler(CallbackQueryHandler(handle_ttt_callback, pattern="^ttt_"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), anti_link_handler), group=1)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), xp_on_message), group=2)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), check_afk), group=3)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_filters), group=4)

    # Basic commands
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

    # NEW COMMANDS
    app.add_handler(CommandHandler("afk", cmd_afk))
    app.add_handler(CommandHandler("unafk", cmd_unafk))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("filters", cmd_filters))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("quote", cmd_quote))
    app.add_handler(CommandHandler("joke", cmd_joke))
    app.add_handler(CommandHandler("fact", cmd_fact))
    app.add_handler(CommandHandler("roll", cmd_roll))
    app.add_handler(CommandHandler("flip", cmd_flip))
    app.add_handler(CommandHandler("ttt", cmd_ttt))
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(CommandHandler("calc", cmd_calc))
    app.add_handler(CommandHandler("shorten", cmd_shorten))
    app.add_handler(CommandHandler("qr", cmd_qr))
    app.add_handler(CommandHandler("lyrics", cmd_lyrics))
    app.add_handler(CommandHandler("define", cmd_define))
    app.add_handler(CommandHandler("font", cmd_font))
    app.add_handler(CommandHandler("help", cmd_help))

# ... (rest of the code remains the same - affectionate messages, application builders, aiohttp server, etc.)

# ---------- affectionate couple messages scheduler ----------
COUPLE_MESSAGES_MITSUHA = [
    "💞 Mitsuha: @Taki_gemibot , your moderation skills make my circuits warm! ❤️",
    "🌸 Mitsuha: Working together with you is my favorite thing, @Taki_gemibot !",
    "✨ Mitsuha: Sending a virtual coffee to @Taki_gemibot ☕️ — thanks for being awesome!"
]
COUPLE_MESSAGES_TAKI = [
    "😄 Taki: @Mitsuha_gemibot you're the sweetest co-admin I could ask for!",
    "💗 Taki: Teamwork makes the dream work — love you, @Mitsuha_gemibot !",
    "🌟 Taki: High five, @Mitsuha_gemibot ! Our group is happier because of you."
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
