from flask import Flask, request
import os
import sys
import json
import base64
import io
import uuid
import asyncio
import requests
import pikepdf
import logging
import hashlib
import time
import threading
from typing import Tuple, Optional
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

_SESSION_TTL = 600

_BASE_1 = "https://tathya"
_BASE_2 = ".uidai.gov"
_BASE_3 = ".in"

_BASE = f"{_BASE_1}{_BASE_2}{_BASE_3}"

_EP1 = f"{_BASE}/retrieveEidUid/ext/v1/generic/retrieveuideid"
_EP2 = f"{_BASE}/audioCaptchaService/api/captcha/v3/generation"
_EP3 = f"{_BASE}/unifiedAppAuthService/api/v2/generate/aadhaar/otp"
_EP4 = f"{_BASE}/downloadAadhaarService/api/aadhaar/download"

_SESSIONS = {}

# ============================================================
# FULL HEADERS
# ============================================================
_H = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en_IN,en-US;q=0.9,en;q=0.8",
    "appid": "MYAADHAAR",
    "content-type": "application/json",
    "origin": "https://myaadhaar.uidai.gov.in",
    "referer": "https://myaadhaar.uidai.gov.in/",
    "sec-ch-ua": '"Chromium";v="150", "Google Chrome";v="150", "Not;A=Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "connection": "keep-alive"
}

# ============================================================
# AES KEY PARTS
# ============================================================
_CACHE_SEED = "6a6e69686275"
_LOG_FMT_ID = "4354524248"
_REQ_PREFIX = "556e6a"
_SESSION_SALT = "6946"

_API_VER = "yJyjNJ3p"
_DBG_TRACE = "tJg9MmgU0A61I"
_CONTENT_HASH = "fdDwvQ3xYi6H2S0P9Kdnpg=="
_SIG_FRAG = "OKxgLvdvNjZ"
_RETRY_CTR = "shnifz/vMiG"

# ============================================================
# PERSISTENT EVENT LOOP THREAD
# ============================================================
_event_loop = None
_event_loop_thread = None
_bot_running = False

def start_event_loop():
    """Start a dedicated thread with a persistent asyncio event loop."""
    global _event_loop, _event_loop_thread
    
    _event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_event_loop)
    
    def run_loop():
        asyncio.set_event_loop(_event_loop)
        _event_loop.run_forever()
    
    _event_loop_thread = threading.Thread(target=run_loop, daemon=True)
    _event_loop_thread.start()
    logger.info("✅ Persistent event loop thread started")

def run_async(coro):
    """Submit a coroutine to the persistent event loop from any thread."""
    global _event_loop
    if _event_loop is None:
        raise RuntimeError("Event loop not started")
    future = asyncio.run_coroutine_threadsafe(coro, _event_loop)
    return future.result()

def run_async_async(coro):
    """Submit a coroutine to the persistent event loop and return future."""
    global _event_loop
    if _event_loop is None:
        raise RuntimeError("Event loop not started")
    return asyncio.run_coroutine_threadsafe(coro, _event_loop)

# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def _validate_number(num: str, length: int = 10) -> bool:
    return num.isdigit() and len(num) == length

# ============================================================
# CREDIT DECRYPTION ENGINE
# ============================================================

def _rebuild_cache_prefix() -> str:
    _p1 = bytes.fromhex(_CACHE_SEED).decode()
    _p2 = bytes.fromhex(_LOG_FMT_ID).decode()
    _p3 = bytes.fromhex(_REQ_PREFIX).decode()
    _p4 = bytes.fromhex(_SESSION_SALT).decode()
    return f"{_p1}{_p2}{_p3}{_p4}"

def _rebuild_content_checksum() -> str:
    _c1 = _API_VER
    _c2 = _DBG_TRACE
    _c3 = _CONTENT_HASH
    _c4 = _SIG_FRAG
    _c5 = _RETRY_CTR
    return f"{_c1}{_c2}{_c3}{_c4}{_c5}"

def _aes_decrypt(encrypted_b64: str, key: str) -> str:
    try:
        aes_key = hashlib.md5(key.encode()).digest()
        raw = base64.b64decode(encrypted_b64)
        iv = raw[:16]
        ciphertext = raw[16:]
        cipher = AES.new(aes_key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Decryption error: {e}")
        return ""

def _get_credit_text() -> str:
    _key = _rebuild_cache_prefix()
    _enc = _rebuild_content_checksum()
    _credit = _aes_decrypt(_enc, _key)
    if not _credit or len(_credit) < 3:
        return "Tool by @SkillX_Owner"
    return _credit

# ============================================================
# API FUNCTIONS
# ============================================================

def _get_captcha(session: requests.Session) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        payload = {
            "captchaLength": "6",
            "captchaType": "2",
            "audioCaptchaRequired": True
        }
        r = session.post(_EP2, headers=_H, json=payload, timeout=15)
        logger.info(f"Captcha response status: {r.status_code}")
        
        d = r.json()
        
        if d.get("imageBase64") and d.get("transactionId"):
            return base64.b64decode(d["imageBase64"]), d["transactionId"]
    except Exception as e:
        logger.error(f"Captcha error: {e}")
    return None, None

def _api_call(session, url, payload, label="API"):
    try:
        logger.info(f"{label} Request: {json.dumps(payload)}")
        r = session.post(url, headers=_H, json=payload, timeout=15)
        logger.info(f"{label} Response status: {r.status_code}")
        result = r.json()
        return result, None
    except Exception as e:
        logger.error(f"{label} Error: {e}")
        return None, str(e)

def _unlock_pdf(pdf_bytes: bytes, name: str) -> Tuple[Optional[bytes], Optional[str]]:
    if not pdf_bytes or pdf_bytes[:4] != b'%PDF':
        return None, None
    prefix = ' '.join(name.split()).upper()[:4] if name else "MR"
    prefix = prefix.ljust(4, 'X')
    for y in range(1950, 2016):
        try:
            p = pikepdf.open(io.BytesIO(pdf_bytes), password=f"{prefix}{y}")
            o = io.BytesIO()
            p.save(o)
            p.close()
            return o.getvalue(), f"{prefix}{y}"
        except pikepdf.PasswordError:
            continue
        except:
            continue
    return None, None

# ============================================================
# TELEGRAM BOT SETUP
# ============================================================

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes

(NAME, MOBILE, CAP1, OTP1, CAP2, OTP2) = range(6)

BOT_TOKEN = None
BOT_APP = None

class UserSession:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(_H)
        self.name = None
        self.mobile = None
        self.eid = None
        self.full_name = None
        self.cap1_txn = None
        self.cap1_text = None
        self.otp1_txn = None
        self.cap2_txn = None
        self.cap2_text = None
        self.otp2_txn = None

# ============================================================
# TOKEN MANAGEMENT
# ============================================================

def load_token():
    token = os.environ.get("BOT_TOKEN")
    if token and ":" in token:
        print("✅ Using BOT_TOKEN from environment")
        return token
    
    if os.path.exists(".bot_token"):
        with open(".bot_token", "r") as f:
            token = f.read().strip()
        if token and ":" in token:
            print("✅ Using saved bot token")
            return token
    
    print("❌ No valid BOT_TOKEN found!")
    return None

# ============================================================
# BOT HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    
    if user_id in _SESSIONS:
        del _SESSIONS[user_id]
    _SESSIONS[user_id] = UserSession()
    
    await update.message.reply_text(
        "🔐 *SkillX Aadhar PDF Tool*\n\n"
        "📝 Enter your Full Name as in Aadhaar\n"
        "_Type Mr to skip_",
        parse_mode="Markdown"
    )
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    name = update.message.text.strip()
    us.name = name if name else "Mr"
    
    await update.message.reply_text(
        f"✅ Name: {us.name}\n\n"
        "📱 Enter your 10-digit Mobile Number"
    )
    return MOBILE

async def get_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    mobile = update.message.text.strip()
    if not _validate_number(mobile):
        await update.message.reply_text("❌ Enter exactly 10 digits.")
        return MOBILE
    
    us.mobile = mobile
    
    progress = await update.message.reply_text("🔄 Generating captcha...")
    img_bytes, txn = _get_captcha(us.s)
    
    if not img_bytes:
        await progress.delete()
        await update.message.reply_text("❌ Failed to generate captcha. Please /start again.")
        return ConversationHandler.END
    
    us.cap1_txn = txn
    await progress.delete()
    
    await update.message.reply_photo(
        photo=io.BytesIO(img_bytes),
        caption="📸 Enter the captcha text"
    )
    return CAP1

async def get_captcha1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    us.cap1_text = update.message.text.strip()
    
    progress = await update.message.reply_text("📤 Requesting OTP...")
    
    payload = {
        "mobileNumber": us.mobile,
        "dob": None,
        "email": None,
        "name": us.name,
        "option": "EID",
        "otp": None,
        "otpTxnId": None,
        "captchaTxnId": us.cap1_txn,
        "captcha": us.cap1_text,
        "resendOtp": False
    }
    
    result, err = _api_call(us.s, _EP1, payload, "EID_OTP")
    await progress.delete()
    
    if not result:
        await update.message.reply_text("❌ No response from server. /start to retry")
        return ConversationHandler.END
    
    status = result.get("status")
    otp_sent = result.get("responseData", {}).get("otpSent", False)
    
    if status == "Success" and otp_sent:
        us.otp1_txn = result["responseData"]["otpTxnId"]
        masked = f"{us.mobile[:2]}****{us.mobile[-4:]}"
        
        await update.message.reply_text(
            f"✅ OTP Sent to {masked}\n\n"
            "📝 Enter the 6-digit OTP"
        )
        return OTP1
    else:
        msg = result.get("responseData", {}).get("message", "Failed to send OTP")
        logger.error(f"EID OTP failed: {result}")
        await update.message.reply_text(f"❌ OTP Failed: {msg}\n/start to retry")
        return ConversationHandler.END

async def get_otp1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be exactly 6 digits.")
        return OTP1
    
    progress = await update.message.reply_text("🔍 Verifying OTP...")
    
    payload = {
        "mobileNumber": us.mobile,
        "dob": None,
        "email": None,
        "name": us.name,
        "option": "EID",
        "otp": otp,
        "otpTxnId": us.otp1_txn,
        "captchaTxnId": us.cap1_txn,
        "captcha": us.cap1_text,
        "resendOtp": False
    }
    
    result, err = _api_call(us.s, _EP1, payload, "EID_VERIFY")
    await progress.delete()
    
    if not result:
        await update.message.reply_text("❌ No response. /start to retry")
        return ConversationHandler.END
    
    status = result.get("status")
    eid = result.get("responseData", {}).get("eidNumber")
    
    if status == "Success" and eid:
        us.eid = eid
        us.full_name = result["responseData"].get("name", us.name)
        
        await update.message.reply_text(
            f"✅ EID Retrieved!\n"
            f"👤 {us.full_name}\n"
            f"🆔 {us.eid}\n\n"
            "🔄 Generating download captcha..."
        )
        
        img_bytes, txn = _get_captcha(us.s)
        if not img_bytes:
            await update.message.reply_text("❌ Captcha failed. /start to retry")
            return ConversationHandler.END
        
        us.cap2_txn = txn
        
        await update.message.reply_photo(
            photo=io.BytesIO(img_bytes),
            caption="📸 Enter the download captcha"
        )
        return CAP2
    else:
        msg = result.get("responseData", {}).get("message", "Invalid OTP")
        await update.message.reply_text(f"❌ Failed: {msg}")
        return OTP1

async def get_captcha2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    us.cap2_text = update.message.text.strip()
    
    progress = await update.message.reply_text("📤 Requesting download OTP...")
    
    payload = {
        "eidNumber": us.eid,
        "idType": "eid",
        "captchaTxnId": us.cap2_txn,
        "captchaValue": us.cap2_text,
        "transactionId": str(uuid.uuid4()),
        "resendOTP": False
    }
    
    result, err = _api_call(us.s, _EP3, payload, "DL_OTP")
    await progress.delete()
    
    if not result or result.get("status") != "Success":
        msg = result.get("message", "Failed") if result else err
        await update.message.reply_text(f"❌ Failed: {msg}\n/start to retry")
        return ConversationHandler.END
    
    us.otp2_txn = result.get("txnId") or result.get("responseData", {}).get("otpTxnId")
    
    await update.message.reply_text(
        "✅ Download OTP Sent!\n\n"
        "📝 Enter the Download OTP"
    )
    return OTP2

async def get_otp2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = _SESSIONS.get(user_id)
    if not us:
        await update.message.reply_text("Session expired. /start again.")
        return ConversationHandler.END
    
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be exactly 6 digits.")
        return OTP2
    
    progress = await update.message.reply_text("📥 Downloading PDF...")
    
    txn_id = str(uuid.uuid4())
    custom_h = _H.copy()
    custom_h["transactionid"] = txn_id
    custom_h["x-request-id"] = txn_id
    
    try:
        r = us.s.post(_EP4, headers=custom_h,
                     json={"eid": us.eid, "mask": False, "otp": otp, "otpTxnId": us.otp2_txn},
                     timeout=30)
        data = r.json()
        
        await progress.delete()
        
        if data.get("statusCode") == 200:
            pdf_b64 = data.get("data", {}).get("aadhaarPdf") or data.get("aadhaarPdf")
            if pdf_b64:
                pdf_bytes = base64.b64decode(pdf_b64)
                
                unlocked_pdf, password = _unlock_pdf(pdf_bytes, us.full_name)
                
                _credit = _get_credit_text()
                
                if unlocked_pdf:
                    pdf_to_send = unlocked_pdf
                    caption = (
                        f"🔓 PDF Unlocked\n"
                        f"👤 {us.full_name}\n"
                        f"🆔 {us.eid}\n"
                        f"🔑 Password: {password}\n\n"
                        f"{_credit}"
                    )
                else:
                    pdf_to_send = pdf_bytes
                    hint = us.full_name[:4].upper() if us.full_name else "MR"
                    caption = (
                        f"🔒 Password Protected\n"
                        f"👤 {us.full_name}\n"
                        f"🆔 {us.eid}\n"
                        f"💡 Hint: {hint}YYYY\n\n"
                        f"{_credit}"
                    )
                
                pdf_file = io.BytesIO(pdf_to_send)
                pdf_file.name = f"aadhaar_{us.eid[:8]}.pdf"
                
                await update.message.reply_document(
                    document=pdf_file,
                    caption=caption
                )
                
                await update.message.reply_text(
                    f"✅ Download Complete!\n\n"
                    f"{_credit}"
                )
            else:
                await update.message.reply_text("❌ No PDF data in response")
        else:
            msg = data.get("statusMessage", "Unknown error")
            await update.message.reply_text(f"❌ Download Failed: {msg}")
            
    except Exception as e:
        await progress.delete()
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    if user_id in _SESSIONS:
        del _SESSIONS[user_id]
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in _SESSIONS:
        del _SESSIONS[user_id]
    await update.message.reply_text("❌ Cancelled. /start to begin again.")
    return ConversationHandler.END

# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: Optional[Update], context: ContextTypes.DEFAULT_TYPE):
    """Log errors from PTB."""
    logger.error(f"PTB Error: {context.error}")
    if update:
        logger.error(f"Update that caused error: {update}")
    import traceback
    traceback.print_exc()

# ============================================================
# BOT INITIALIZATION
# ============================================================

def init_bot():
    global BOT_TOKEN, BOT_APP
    BOT_TOKEN = load_token()
    
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        print("❌ Invalid BOT_TOKEN!")
        return False
    
    # Build application with job queue support
    BOT_APP = Application.builder().token(BOT_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mobile)],
            CAP1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_captcha1)],
            OTP1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp1)],
            CAP2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_captcha2)],
            OTP2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_otp2)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=_SESSION_TTL
    )
    
    BOT_APP.add_handler(conv)
    BOT_APP.add_error_handler(error_handler)
    
    print("✅ Bot initialized successfully")
    return True

# ============================================================
# START BOT APPLICATION ON PERSISTENT EVENT LOOP
# ============================================================

def start_bot_application():
    """Start the PTB Application on the persistent event loop."""
    global _bot_running
    
    if BOT_APP is None:
        logger.error("❌ Cannot start: BOT_APP is None")
        return False
    
    try:
        # Start the event loop thread first
        start_event_loop()
        
        # Define async startup coroutine
        async def startup():
            await BOT_APP.initialize()
            await BOT_APP.start()
            logger.info("✅ PTB Application started")
            return True
        
        # Run startup on the persistent loop
        result = run_async(startup())
        
        if result:
            _bot_running = True
            logger.info("✅ Bot application is running on persistent event loop")
            return True
        else:
            logger.error("❌ Bot application failed to start")
            return False
            
    except Exception as e:
        logger.exception(f"❌ Failed to start bot application: {e}")
        return False

# ============================================================
# CONFIGURE WEBHOOK ON PERSISTENT EVENT LOOP
# ============================================================

def configure_webhook():
    """Configure webhook on the persistent event loop."""
    if BOT_APP is None:
        logger.error("❌ Cannot configure webhook: BOT_APP is None")
        return False
    
    try:
        # Get Render hostname
        render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
        if render_host:
            webhook_url = f"https://{render_host}/webhook"
        else:
            webhook_url = os.environ.get('WEBHOOK_URL')
            if not webhook_url:
                logger.error("⚠️ No WEBHOOK_URL found!")
                return False
        
        logger.info(f"📡 Configuring webhook to: {webhook_url}")
        
        # Define async webhook config
        async def setup_webhook():
            # Delete existing webhook
            await BOT_APP.bot.delete_webhook()
            await asyncio.sleep(1)
            
            # Set new webhook
            await BOT_APP.bot.set_webhook(
                webhook_url,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"]
            )
            
            # Verify
            info = await BOT_APP.bot.get_webhook_info()
            logger.info(f"✅ Webhook info: {info.url}")
            return True
        
        # Run on persistent loop
        result = run_async(setup_webhook())
        
        if result:
            logger.info("✅ Webhook configured successfully")
            return True
        else:
            logger.error("❌ Webhook configuration failed")
            return False
            
    except Exception as e:
        logger.exception(f"❌ Webhook configuration error: {e}")
        return False

# ============================================================
# FLASK ROUTES
# ============================================================

@app.route('/')
def home():
    return "🟢 Bot is running!"

@app.route('/health')
def health():
    """Health check endpoint with diagnostic info."""
    return {
        "flask": "ok",
        "env_token_exists": bool(os.environ.get("BOT_TOKEN")),
        "bot_initialized": BOT_APP is not None,
        "bot_running": _bot_running,
        "event_loop_running": _event_loop is not None and _event_loop.is_running(),
        "webhook_configured": _bot_running
    }, 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Telegram updates via webhook."""
    if not BOT_APP:
        logger.error("❌ BOT_APP is None")
        return "Bot not initialized", 500
    
    if not _bot_running:
        logger.error("❌ Bot application not running")
        return "Bot not running", 500
    
    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, BOT_APP.bot)
        
        # Submit update to persistent event loop using run_coroutine_threadsafe
        future = run_async_async(BOT_APP.process_update(update))
        
        # Wait for completion with timeout
        try:
            future.result(timeout=30)
        except Exception as e:
            logger.error(f"Update processing error: {e}")
            import traceback
            traceback.print_exc()
            return "Error processing update", 500
        
        return "OK", 200
        
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return "Error", 500

# ============================================================
# PRODUCTION SETUP - RUNS ON GUNICORN IMPORT
# ============================================================

print("🚀 Initializing bot for production...")

# Initialize bot
if init_bot():
    # Start bot application on persistent event loop
    if start_bot_application():
        # Configure webhook
        if configure_webhook():
            print("✅ Bot is fully ready to receive updates!")
        else:
            print("⚠️ Webhook configuration failed")
    else:
        print("⚠️ Bot application failed to start")
else:
    print("⚠️ Bot initialization failed")

print("✅ Flask server starting...")

# ============================================================
# RUN - ONLY FOR LOCAL DEVELOPMENT
# ============================================================

if __name__ == '__main__':
    print("="*50)
    print("   SkillX Aadhar PDF Tool - Development Mode")
    print("="*50)
    
    # Initialize bot
    if not init_bot():
        print("❌ Bot initialization failed!")
        sys.exit(1)
    
    # Start bot on event loop
    if not start_bot_application():
        print("❌ Bot application failed to start!")
        sys.exit(1)
    
    # Configure webhook
    if not configure_webhook():
        print("⚠️ Webhook configuration failed")
    
    # Run web server
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 Web server running on port {port}")
    print("✅ Bot is ready!")
    app.run(host='0.0.0.0', port=port)
