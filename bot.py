import asyncio
import re
import os
import sqlite3
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient, events

# ---------- CONFIG ----------
API_ID = int(os.environ.get('API_ID', 12345))
API_HASH = os.environ.get('API_HASH', 'your_api_hash')
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'your_bot_token')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '0').split(',')))

SESSION_DIR = Path('sessions')
SESSION_DIR.mkdir(exist_ok=True)
DB_PATH = Path('sessions.db')

# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            phone TEXT NOT NULL,
            password TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            status TEXT DEFAULT 'available'
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def get_session(name):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE name = ? AND is_active = 1', (name,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_sessions():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('SELECT name, phone, status, created_at FROM sessions WHERE is_active = 1')
    rows = c.fetchall()
    conn.close()
    return rows

def add_session(name, phone, password, created_by):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute(
        'INSERT OR IGNORE INTO sessions (name, phone, password, created_by) VALUES (?, ?, ?, ?)',
        (name, phone, password, created_by)
    )
    conn.commit()
    conn.close()

def update_status(name, status):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('UPDATE sessions SET status = ? WHERE name = ?', (status, name))
    conn.commit()
    conn.close()

def add_claim(session_name, user_id):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('INSERT INTO claims (session_name, user_id) VALUES (?, ?)', (session_name, user_id))
    conn.commit()
    conn.close()
    update_status(session_name, 'claimed')

def delete_session(name):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('DELETE FROM sessions WHERE name = ?', (name,))
    conn.commit()
    conn.close()
    session_file = SESSION_DIR / f"{name}.session"
    if session_file.exists():
        session_file.unlink()

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ---------- GLOBALS ----------
clients = {}
claim_map = {}
listener_tasks = {}

# ---------- HELPERS ----------
def extract_otp(text):
    match = re.search(r'\b(\d{5,6})\b', text)
    return match.group(1) if match else None

async def forward_otp(session_name, otp):
    user_id = claim_map.get(session_name)
    if user_id:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=f"🔑 **OTP for {session_name}**\n\n`{otp}`\n\n⏳ Expires in 2 minutes.",
                parse_mode='Markdown'
            )
            return True
        except:
            pass
    return False

async def send_login_instruction(user_id, session_name, phone):
    await app.bot.send_message(
        chat_id=user_id,
        text=f"📱 **Login Required**\n\n"
             f"Please login to Telegram using this number:\n`{phone}`\n\n"
             f"⚠️ Once you login, I'll receive the OTP and forward it here.\n⏳ Waiting for OTP...",
        parse_mode='Markdown'
    )

# ---------- OTP LISTENER ----------
async def start_listener(session_name, password=None):
    session_path = SESSION_DIR / f"{session_name}.session"
    if not session_path.exists():
        return

    client = TelegramClient(str(session_path), API_ID, API_HASH)
    try:
        if password:
            await client.start(password=password)
        else:
            await client.start()
        clients[session_name] = client
        print(f"✅ Listener started: {session_name}")

        @client.on(events.NewMessage(incoming=True))
        async def handler(event):
            text = event.message.text or ""
            otp = extract_otp(text)
            if otp:
                print(f"📩 OTP on {session_name}: {otp}")
                await forward_otp(session_name, otp)

        await client.run_until_disconnected()
    except Exception as e:
        print(f"❌ Listener error {session_name}: {e}")

def run_listener(session_name, password=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_listener(session_name, password))

# ---------- BOT ----------
app = Application.builder().token(BOT_TOKEN).build()

# ---------- CONVERSATION STATES ----------
WAITING_PHONE, WAITING_OTP, WAITING_2FA = range(3)

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin_flag = is_admin(user_id)
    msg = (
        "🤖 **Session Bot v2.2**\n\n"
        "**Commands:**\n"
        "/create - Create new session\n"
        "/list - Show all available sessions\n"
        "/claim <name> - Claim a session (OTP comes here)\n"
        "/unclaim <name> - Release session\n"
        "/delete <name> - Delete session (admin only)\n\n"
        "📂 **How it works:**\n"
        "1. Send `.session` file to bot\n"
        "2. Bot checks if session exists in database\n"
        "3. If YES → Bot asks you to login to Telegram\n"
        "4. You login → OTP arrives here automatically\n\n"
        "⚠️ Only registered sessions work."
    )
    if is_admin_flag:
        msg += "\n👑 **Admin:** /delete <name>  /stats"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ---------- /create ----------
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 Enter phone number with country code:\nExample: `+919876543210`",
        parse_mode='Markdown'
    )
    return WAITING_PHONE

async def create_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    context.user_data['session_name'] = phone.replace('+', '').replace(' ', '')
    context.user_data['otp_attempts'] = 0

    try:
        client = TelegramClient(str(SESSION_DIR / f"{context.user_data['session_name']}.session"), API_ID, API_HASH)
        await client.start(phone=phone)
        context.user_data['client'] = client

        keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📲 OTP sent to `{phone}`.\nEnter the OTP (5-6 digits):\n⚠️ You have 3 attempts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return WAITING_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send OTP: {e}")
        return ConversationHandler.END

async def create_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    otp = update.message.text.strip()
    attempts = context.user_data.get('otp_attempts', 0) + 1
    context.user_data['otp_attempts'] = attempts
    client = context.user_data.get('client')

    if not client:
        await update.message.reply_text("❌ Session expired. Please /create again.")
        return ConversationHandler.END

    try:
        await client.sign_in(code=otp)
        me = await client.get_me()
        await client.disconnect()

        user_id = update.effective_user.id
        session_name = context.user_data['session_name']
        phone = context.user_data['phone']
        add_session(session_name, me.phone, None, user_id)

        await update.message.reply_text(
            f"✅ **Session created!**\n\n📱 Phone: `{me.phone}`\n📁 Name: `{session_name}`\n\nNow send this `.session` file to verify.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    except Exception as e:
        error_msg = str(e)
        if '2FA' in error_msg or 'password' in error_msg.lower():
            await update.message.reply_text("🔐 Account has 2FA. Enter your 2FA password:")
            return WAITING_2FA

        elif 'Invalid code' in error_msg:
            keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if attempts >= 3:
                await update.message.reply_text(
                    "❌ **3 failed attempts.** Use /create to restart.",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return ConversationHandler.END
            else:
                await update.message.reply_text(
                    f"❌ Invalid OTP. Attempt {attempts}/3.\nEnter correct OTP or click below:",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                return WAITING_OTP
        else:
            await update.message.reply_text(f"❌ Error: {error_msg}")
            return ConversationHandler.END

async def create_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client = context.user_data.get('client')
    if not client:
        await update.message.reply_text("❌ Session expired. Please /create again.")
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        await client.disconnect()

        user_id = update.effective_user.id
        session_name = context.user_data['session_name']
        phone = context.user_data['phone']
        add_session(session_name, me.phone, password, user_id)

        await update.message.reply_text(
            f"✅ **Session created with 2FA!**\n\n📱 Phone: `{me.phone}`\n📁 Name: `{session_name}`\n\nSend `.session` file to verify.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    except Exception as e:
        await update.message.reply_text(f"❌ 2FA error: {e}")
        return WAITING_2FA

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ---------- RESEND OTP ----------
async def resend_otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = context.user_data.get('phone')
    if not phone:
        await update.message.reply_text("❌ No active session. Use /create first.")
        return

    session_name = context.user_data.get('session_name')
    if not session_name:
        await update.message.reply_text("❌ Session name missing. Use /create again.")
        return

    if 'client' in context.user_data:
        try:
            await context.user_data['client'].disconnect()
        except:
            pass

    try:
        client = TelegramClient(str(SESSION_DIR / f"{session_name}.session"), API_ID, API_HASH)
        await client.start(phone=phone)
        context.user_data['client'] = client
        context.user_data['otp_attempts'] = 0

        keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📲 New OTP sent to `{phone}`.\nEnter the OTP (5-6 digits):\n⚠️ You have 3 attempts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        return WAITING_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to resend OTP: {e}")
        return ConversationHandler.END

async def resend_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    phone = context.user_data.get('phone')
    session_name = context.user_data.get('session_name')

    if not phone or not session_name:
        await query.edit_message_text("❌ No active session. Use /create first.")
        return

    if 'client' in context.user_data:
        try:
            await context.user_data['client'].disconnect()
        except:
            pass

    try:
        client = TelegramClient(str(SESSION_DIR / f"{session_name}.session"), API_ID, API_HASH)
        await client.start(phone=phone)
        context.user_data['client'] = client
        context.user_data['otp_attempts'] = 0

        keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"📲 New OTP sent to `{phone}`.\nEnter the OTP (5-6 digits):\n⚠️ You have 3 attempts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Failed to resend OTP: {e}")

# ---------- /list ----------
async def list_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions = get_all_sessions()
    if not sessions:
        await update.message.reply_text("❌ No sessions.")
        return
    msg = "📁 **Registered Sessions:**\n\n"
    for name, phone, status, created_at in sessions:
        claimed_by = claim_map.get(name, 'Nobody')
        status_emoji = "✅" if status == 'claimed' else "📂"
        msg += f"{status_emoji} `{name}`\n   📱 {phone}\n   👤 {claimed_by}\n   📅 {created_at[:10]}\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ---------- /claim ----------
async def claim_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /claim <session_name>")
        return
    session_name = context.args[0]
    session = get_session(session_name)
    if not session:
        await update.message.reply_text(f"❌ Session `{session_name}` not found.", parse_mode='Markdown')
        return

    user_id = update.effective_user.id
    claim_map[session_name] = user_id
    add_claim(session_name, user_id)

    if session_name not in clients:
        import threading
        password = session[3]
        thread = threading.Thread(target=run_listener, args=(session_name, password), daemon=True)
        thread.start()
        listener_tasks[session_name] = thread
        await update.message.reply_text(f"⏳ Listener starting for `{session_name}`...", parse_mode='Markdown')

    await update.message.reply_text(
        f"✅ **Session claimed!**\n\n📱 Phone: `{session[2]}`\n📁 Name: `{session_name}`\n\n📌 Please login to Telegram with this number.",
        parse_mode='Markdown'
    )
    await send_login_instruction(user_id, session_name, session[2])

# ---------- /unclaim ----------
async def unclaim_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unclaim <session_name>")
        return
    session_name = context.args[0]
    user_id = update.effective_user.id
    if claim_map.get(session_name) == user_id:
        claim_map.pop(session_name, None)
        update_status(session_name, 'available')
        await update.message.reply_text(f"✅ Released `{session_name}`.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ You don't own `{session_name}`.", parse_mode='Markdown')

# ---------- /delete (admin) ----------
async def delete_session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /delete <session_name>")
        return
    session_name = context.args[0]
    session = get_session(session_name)
    if not session:
        await update.message.reply_text(f"❌ Session `{session_name}` not found.", parse_mode='Markdown')
        return
    if session_name in clients:
        try:
            await clients[session_name].disconnect()
        except:
            pass
        clients.pop(session_name, None)
    delete_session(session_name)
    claim_map.pop(session_name, None)
    await update.message.reply_text(f"🗑️ Deleted `{session_name}`.", parse_mode='Markdown')

# ---------- /stats (admin) ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM sessions')
    total = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM sessions WHERE status = "claimed"')
    claimed = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM claims')
    claims_total = c.fetchone()[0]
    conn.close()
    msg = f"📊 **Stats**\n\n📁 Total: {total}\n📌 Claimed: {claimed}\n🔗 Claims: {claims_total}\n🟢 Listeners: {len(clients)}"
    await update.message.reply_text(msg, parse_mode='Markdown')

# ---------- FILE HANDLER ----------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document or not document.file_name:
        await update.message.reply_text("❌ Send a valid file.")
        return
    if not document.file_name.endswith('.session'):
        await update.message.reply_text("❌ Only `.session` files allowed.")
        return

    session_name = document.file_name.replace('.session', '')
    session = get_session(session_name)
    if not session:
        await update.message.reply_text(
            f"❌ **Session `{session_name}` not registered.**\nUse `/create` first.",
            parse_mode='Markdown'
        )
        return

    file = await document.get_file()
    save_path = SESSION_DIR / f"{session_name}.session"
    await file.download_to_drive(save_path)

    claim_map[session_name] = user_id
    add_claim(session_name, user_id)

    if session_name not in clients:
        import threading
        password = session[3]
        thread = threading.Thread(target=run_listener, args=(session_name, password), daemon=True)
        thread.start()
        listener_tasks[session_name] = thread

    await update.message.reply_text(
        f"✅ **Session verified & loaded!**\n\n📱 Phone: `{session[2]}`\n📁 Name: `{session_name}`\n\n📌 Please login to Telegram with this number.\nOTP will appear here.",
        parse_mode='Markdown'
    )
    await send_login_instruction(user_id, session_name, session[2])

# ---------- CONVERSATION HANDLER ----------
conv_create = ConversationHandler(
    entry_points=[CommandHandler('create', create_start)],
    states={
        WAITING_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_phone)],
        WAITING_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_otp)],
        WAITING_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_2fa)],
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

# ---------- REGISTER HANDLERS ----------
app.add_handler(CommandHandler('start', start))
app.add_handler(CommandHandler('list', list_sessions))
app.add_handler(CommandHandler('claim', claim_session))
app.add_handler(CommandHandler('unclaim', unclaim_session))
app.add_handler(CommandHandler('delete', delete_session_cmd))
app.add_handler(CommandHandler('stats', stats))
app.add_handler(CommandHandler('resend_otp', resend_otp_command))
app.add_handler(CallbackQueryHandler(resend_otp_callback, pattern='^resend_otp$'))
app.add_handler(conv_create)
app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

# ---------- MAIN ----------
if __name__ == '__main__':
    init_db()
    print("🤖 Bot starting...")
    print(f"📁 Sessions: {SESSION_DIR.absolute()}")
    print(f"📊 Database: {DB_PATH.absolute()}")
    # 🔽 FIX: drop_pending_updates=True to avoid conflict
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
