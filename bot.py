import asyncio
import re
import os
import sqlite3
import zipfile
import io
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telethon import TelegramClient, events
from cryptography.fernet import Fernet

# ---------- CONFIG ----------
API_ID = int(os.environ.get('API_ID', 12345))
API_HASH = os.environ.get('API_HASH', 'your_api_hash')
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'your_bot_token')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '0').split(',')))
PORT = int(os.environ.get('PORT', 8080))
RAILWAY_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN')

# Encryption key – must be set in environment
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    raise Exception("ENCRYPTION_KEY environment variable not set! Generate one using: Fernet.generate_key().decode()")
cipher = Fernet(ENCRYPTION_KEY.encode())

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

# ---------- ENCRYPTION HELPERS ----------
def encrypt_file_data(file_path):
    """Read file, encrypt, return bytes"""
    with open(file_path, 'rb') as f:
        data = f.read()
    return cipher.encrypt(data)

def decrypt_file_data(encrypted_data):
    """Decrypt bytes and return plaintext bytes"""
    return cipher.decrypt(encrypted_data)

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
        text=f"📱 **Login Required**\n\nPlease login to Telegram using this number:\n`{phone}`\n\n⚠️ Once you login, I'll receive the OTP and forward it here.\n⏳ Waiting for OTP...",
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
    finally:
        # Clean up decrypted file after listener stops
        if session_path.exists():
            session_path.unlink()

def run_listener(session_name, password=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_listener(session_name, password))

# ---------- BOT ----------
app = Application.builder().token(BOT_TOKEN).build()

# ---------- START MESSAGE WITH BUTTONS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin_flag = is_admin(user_id)

    keyboard = [
        [InlineKeyboardButton("📱 Create New Session", callback_data="create_session")],
        [InlineKeyboardButton("📂 List Sessions", callback_data="list_sessions")],
    ]
    if is_admin_flag:
        keyboard.append([InlineKeyboardButton("📦 Download All Sessions (ZIP)", callback_data="download_all")])
    keyboard.append([InlineKeyboardButton("❓ Help", callback_data="help")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = (
        "🤖 **Session Bot v2.6**\n\n"
        "Welcome! Use the buttons below to get started.\n\n"
        "**How it works:**\n"
        "1. Create a new session\n"
        "2. Bot sends an **encrypted** `.session` file\n"
        "3. Only this bot can read it – it's locked to my secret key\n"
        "4. Send the file back to verify\n"
        "5. Login to Telegram with that number\n"
        "6. OTP appears here automatically\n\n"
        "🔒 All session files are encrypted with a unique key."
    )
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

# ---------- CALLBACK HANDLERS ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "create_session":
        context.user_data['state'] = 'awaiting_phone'
        await query.edit_message_text(
            "📱 Enter phone number with country code:\nExample: `+919876543210`",
            parse_mode='Markdown'
        )
        return

    elif data == "list_sessions":
        sessions = get_all_sessions()
        if not sessions:
            await query.edit_message_text("❌ No sessions.")
            return
        msg = "📁 **Registered Sessions:**\n\n"
        for name, phone, status, created_at in sessions:
            claimed_by = claim_map.get(name, 'Nobody')
            status_emoji = "✅" if status == 'claimed' else "📂"
            msg += f"{status_emoji} `{name}`\n   📱 {phone}\n   👤 {claimed_by}\n   📅 {created_at[:10]}\n\n"
        await query.edit_message_text(msg, parse_mode='Markdown')
        return

    elif data == "download_all":
        if not is_admin(user_id):
            await query.edit_message_text("❌ Admin only.")
            return
        session_files = list(SESSION_DIR.glob('*.session'))
        if not session_files:
            await query.edit_message_text("❌ No session files found.")
            return
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for file_path in session_files:
                zip_file.write(file_path, arcname=file_path.name)
        zip_buffer.seek(0)
        await query.message.reply_document(
            document=zip_buffer,
            filename=f"all_sessions_{len(session_files)}.zip",
            caption=f"📦 All session files ({len(session_files)} files). Keep them safe!"
        )
        await query.delete_message()
        return

    elif data == "help":
        msg = (
            "❓ **Help**\n\n"
            "**Commands:**\n"
            "/create - Create new session (manual)\n"
            "/list - Show all sessions\n"
            "/claim <name> - Claim a session (OTP comes here)\n"
            "/unclaim <name> - Release session\n"
            "/delete <name> - Delete session (admin only)\n"
            "/download_all - Download all sessions as ZIP (admin only)\n\n"
            "**How to use:**\n"
            "1. Use 'Create New Session' button or /create\n"
            "2. Enter phone → Enter OTP\n"
            "3. Bot sends an **encrypted** `.session` file\n"
            "4. Send that file back to verify\n"
            "5. Login to Telegram with that number\n"
            "6. OTP forwarded here"
        )
        await query.edit_message_text(msg, parse_mode='Markdown')
        return

# ---------- MESSAGE HANDLER (for phone, OTP, 2FA) ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = context.user_data.get('state')

    if state == 'awaiting_phone':
        # Save phone and request OTP
        context.user_data['phone'] = text
        context.user_data['session_name'] = text.replace('+', '').replace(' ', '')
        context.user_data['otp_attempts'] = 0

        try:
            client = TelegramClient(str(SESSION_DIR / f"{context.user_data['session_name']}.session"), API_ID, API_HASH)
            await client.connect()
            await client.send_code_request(text)
            context.user_data['client'] = client
            context.user_data['state'] = 'awaiting_otp'

            keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"📲 OTP sent to `{text}`.\nEnter the OTP (5-6 digits):\n⚠️ You have 3 attempts.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send OTP: {e}")
            context.user_data['state'] = None

    elif state == 'awaiting_otp':
        otp = text
        attempts = context.user_data.get('otp_attempts', 0) + 1
        context.user_data['otp_attempts'] = attempts
        client = context.user_data.get('client')

        if not client:
            await update.message.reply_text("❌ Session expired. Use /create or button again.")
            context.user_data['state'] = None
            return

        try:
            await client.sign_in(code=otp)
            me = await client.get_me()
            await client.disconnect()

            session_name = context.user_data['session_name']
            phone = context.user_data['phone']
            add_session(session_name, me.phone, None, user_id)

            # ----- ENCRYPT SESSION FILE AND SEND -----
            session_path = SESSION_DIR / f"{session_name}.session"
            if session_path.exists():
                encrypted_data = encrypt_file_data(session_path)
                # Send encrypted file
                await update.message.reply_document(
                    document=io.BytesIO(encrypted_data),
                    filename=f"{session_name}.session",
                    caption=f"🔒 **Encrypted session file for {session_name}**\n\nThis file is locked to my bot. Send it back to verify."
                )
                # Delete plaintext file
                session_path.unlink()
            else:
                await update.message.reply_text("⚠️ Session created but file not found.")

            await update.message.reply_text(
                f"✅ **Session created!**\n\n📱 Phone: `{me.phone}`\n📁 Name: `{session_name}`\n\nNow send the encrypted `.session` file (just received) back to verify.",
                parse_mode='Markdown'
            )
            context.user_data['state'] = None

        except Exception as e:
            error_msg = str(e)
            if '2FA' in error_msg or 'password' in error_msg.lower():
                await update.message.reply_text("🔐 Account has 2FA. Enter your 2FA password:")
                context.user_data['state'] = 'awaiting_2fa'
                return

            elif 'Invalid code' in error_msg:
                keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                if attempts >= 3:
                    await update.message.reply_text(
                        "❌ **3 failed attempts.** Use /create to restart.",
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                    context.user_data['state'] = None
                else:
                    await update.message.reply_text(
                        f"❌ Invalid OTP. Attempt {attempts}/3.\nEnter correct OTP or click below:",
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
            else:
                await update.message.reply_text(f"❌ Error: {error_msg}")
                context.user_data['state'] = None

    elif state == 'awaiting_2fa':
        password = text
        client = context.user_data.get('client')
        if not client:
            await update.message.reply_text("❌ Session expired. Use /create or button again.")
            context.user_data['state'] = None
            return

        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            await client.disconnect()

            session_name = context.user_data['session_name']
            phone = context.user_data['phone']
            add_session(session_name, me.phone, password, user_id)

            # ----- ENCRYPT SESSION FILE AND SEND -----
            session_path = SESSION_DIR / f"{session_name}.session"
            if session_path.exists():
                encrypted_data = encrypt_file_data(session_path)
                await update.message.reply_document(
                    document=io.BytesIO(encrypted_data),
                    filename=f"{session_name}.session",
                    caption=f"🔒 **Encrypted session file for {session_name}**\n\nThis file is locked to my bot. Send it back to verify."
                )
                session_path.unlink()
            else:
                await update.message.reply_text("⚠️ Session created but file not found.")

            await update.message.reply_text(
                f"✅ **Session created with 2FA!**\n\n📱 Phone: `{me.phone}`\n📁 Name: `{session_name}`\n\nNow send the encrypted `.session` file (just received) back to verify.",
                parse_mode='Markdown'
            )
            context.user_data['state'] = None

        except Exception as e:
            await update.message.reply_text(f"❌ 2FA error: {e}")

    else:
        # If user sends something else, ignore or show help
        await update.message.reply_text("❓ Use /start to see available options.")

# ---------- RESEND OTP (callback) ----------
async def resend_otp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    phone = context.user_data.get('phone')
    session_name = context.user_data.get('session_name')

    if not phone or not session_name:
        await query.edit_message_text("❌ No active session. Use /create or button to start.")
        return

    if 'client' in context.user_data:
        try:
            await context.user_data['client'].disconnect()
        except:
            pass

    try:
        client = TelegramClient(str(SESSION_DIR / f"{session_name}.session"), API_ID, API_HASH)
        await client.connect()
        await client.send_code_request(phone)
        context.user_data['client'] = client
        context.user_data['otp_attempts'] = 0
        context.user_data['state'] = 'awaiting_otp'

        keyboard = [[InlineKeyboardButton("🔄 Resend OTP", callback_data="resend_otp")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"📲 New OTP sent to `{phone}`.\nEnter the OTP (5-6 digits):\n⚠️ You have 3 attempts.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Failed to resend OTP: {e}")

# ---------- COMMAND HANDLERS ----------
async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'awaiting_phone'
    await update.message.reply_text(
        "📱 Enter phone number with country code:\nExample: `+919876543210`",
        parse_mode='Markdown'
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # Check if we have a decrypted session file; if not, we can't start listener
    session_path = SESSION_DIR / f"{session_name}.session"
    if not session_path.exists():
        await update.message.reply_text(
            f"⚠️ Session `{session_name}` is registered but no decrypted file found.\n"
            "Please send the encrypted `.session` file first.",
            parse_mode='Markdown'
        )
        return

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

async def cmd_unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_download_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    session_files = list(SESSION_DIR.glob('*.session'))
    if not session_files:
        await update.message.reply_text("❌ No session files found.")
        return
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in session_files:
            zip_file.write(file_path, arcname=file_path.name)
    zip_buffer.seek(0)
    await update.message.reply_document(
        document=zip_buffer,
        filename=f"all_sessions_{len(session_files)}.zip",
        caption=f"📦 All session files ({len(session_files)} files). Keep them safe!"
    )

# ---------- FILE HANDLER (with decryption) ----------
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
    # Check if session is registered in DB
    session = get_session(session_name)
    if not session:
        await update.message.reply_text(
            f"❌ **Session `{session_name}` not registered.**\nUse `/create` first.",
            parse_mode='Markdown'
        )
        return

    # Download encrypted file as bytes
    file = await document.get_file()
    encrypted_data = await file.download_as_bytearray()

    try:
        decrypted_data = decrypt_file_data(bytes(encrypted_data))
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to decrypt file. Either it's not encrypted with my key or corrupted.\nError: {e}",
            parse_mode='Markdown'
        )
        return

    # Save decrypted session file
    session_path = SESSION_DIR / f"{session_name}.session"
    with open(session_path, 'wb') as f:
        f.write(decrypted_data)

    # Claim it for the user
    claim_map[session_name] = user_id
    add_claim(session_name, user_id)

    # Start listener if not running
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

# ---------- REGISTER HANDLERS ----------
app.add_handler(CommandHandler('start', start))
app.add_handler(CommandHandler('create', cmd_create))
app.add_handler(CommandHandler('list', cmd_list))
app.add_handler(CommandHandler('claim', cmd_claim))
app.add_handler(CommandHandler('unclaim', cmd_unclaim))
app.add_handler(CommandHandler('delete', cmd_delete))
app.add_handler(CommandHandler('stats', cmd_stats))
app.add_handler(CommandHandler('download_all', cmd_download_all))

app.add_handler(CallbackQueryHandler(callback_handler, pattern='^(create_session|list_sessions|download_all|help)$'))
app.add_handler(CallbackQueryHandler(resend_otp_callback, pattern='^resend_otp$'))

app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

# ---------- MAIN ----------
if __name__ == '__main__':
    init_db()
    print("🤖 Bot starting...")
    print(f"📁 Sessions: {SESSION_DIR.absolute()}")
    print(f"📊 Database: {DB_PATH.absolute()}")
    print("🔒 Encryption key loaded.")

    if RAILWAY_DOMAIN:
        webhook_url = f'https://{RAILWAY_DOMAIN}/webhook'
        print(f"🌐 Setting webhook: {webhook_url}")
        app.run_webhook(
            listen='0.0.0.0',
            port=PORT,
            url_path='webhook',
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
    else:
        print("📡 Using polling mode (drop_pending_updates=True)")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
