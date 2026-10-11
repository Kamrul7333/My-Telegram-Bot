import os
import logging
import asyncio
import re
from datetime import datetime, timedelta
import aiosqlite
import threading
from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from telegram.error import TelegramError

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID", "8549397639")
DB_PATH = "referral_bot.db"

# =====================================================================
# FLASK WEB SERVER FOR HEALTH CHECK
# =====================================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is Running!", 200

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask_server():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

SELECT_METHOD, ENTER_WALLET, ENTER_AMOUNT = range(3)

# =====================================================================
# DATABASE MANAGER
# =====================================================================
class DatabaseManager:
    @staticmethod
    async def init_db():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance REAL DEFAULT 0.0,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER,
                    referred_id INTEGER,
                    channel_id INTEGER,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    reward_amount REAL,
                    compensated INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    method TEXT,
                    wallet_number TEXT,
                    status TEXT DEFAULT 'pending',
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('referral_reward', '10.0')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal', '50.0')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channel_id', '-1001234567890')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('channel_invite_link', 'https://t.me/your_channel')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('admin_id', ?)", (ADMIN_ID_STR,))
            await db.commit()

    @staticmethod
    async def get_setting(key: str) -> str:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else ""

    @staticmethod
    async def set_setting(key: str, value: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

# =====================================================================
# HELPERS & KEYBOARDS
# =====================================================================
async def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    keyboard = [
        [KeyboardButton("👤 My Account"), KeyboardButton("🔗 Invite Link")],
        [KeyboardButton("💳 Withdraw")]
    ]
    if str(user_id) == admin_id_str:
        keyboard.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "No Username"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0.0)",
            (user_id, username)
        )
        await db.commit()
    await update.message.reply_text(
        "👋 Welcome to the Bot!\nUse the keyboard menu to start.",
        reply_markup=await get_main_keyboard(user_id)
    )

# =====================================================================
# USER ACCOUNT DASHBOARD
# =====================================================================
async def display_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reward_rate = float(await DatabaseManager.get_setting("referral_reward"))

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            available_balance = row[0] if row else 0.0
        async with db.execute("SELECT COUNT(DISTINCT channel_id) FROM referrals WHERE referrer_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            active_links = max(1, row[0] if row else 1)
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'counted'", (user_id,)) as cursor:
            row = await cursor.fetchone()
            counted_members = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'pending'", (user_id,)) as cursor:
            row = await cursor.fetchone()
            pending_24h = row[0] if row else 0
        async with db.execute(
            "SELECT id, method, amount, status FROM withdrawals WHERE user_id = ? ORDER BY requested_at DESC LIMIT 5",
            (user_id,)
        ) as cursor:
            withdrawals = await cursor.fetchall()

    estimated_payable = counted_members + pending_24h
    pending_earning = pending_24h * reward_rate

    wd_strings = []
    for wd in withdrawals:
        wd_id, method, amount, status = wd
        wd_strings.append(f"#{wd_id} • {method.upper()} • ৳{amount:.2f} • [{status}]")
    wd_log_display = "\n".join(wd_strings) if wd_strings else "No recent withdrawal history found."

    dashboard = (
        f"👤 My Account\n"
        f"🆔 Telegram ID: {user_id}\n"
        f"🔗 Active Links: {active_links}\n"
        f"✅ Counted Members: {counted_members}\n"
        f"⏳ Pending 24H: {pending_24h}\n"
        f"🛡 Estimated Payable: {estimated_payable}\n"
        f"💰 Pending Earning: ৳{pending_earning:.2f}\n"
        f"💳 Available Balance: ৳{available_balance:.2f}\n\n"
        f"💸 Recent Withdrawals\n"
        f"{wd_log_display}"
    )
    await update.message.reply_text(dashboard, reply_markup=await get_main_keyboard(user_id))

# =====================================================================
# ADVANCED MEMBER VERIFICATION (24H HOLD)
# =====================================================================
async def track_chat_member_joins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_update = update.chat_member
    if not join_update:
        return
    if join_update.old_chat_member.status in ["left", "kicked"] and join_update.new_chat_member.status in ["member", "administrator", "creator"]:
        referred_id = join_update.new_chat_member.user.id
        channel_id = join_update.chat.id
        invite_link = join_update.invite_link

        if invite_link and invite_link.name and invite_link.name.startswith("ref_"):
            try:
                referrer_id = int(invite_link.name.split("_")[1])
            except (IndexError, ValueError):
                return
            if referrer_id == referred_id:
                return

            reward_rate = float(await DatabaseManager.get_setting("referral_reward"))
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT id FROM referrals WHERE referred_id = ? AND channel_id = ?", (referred_id, channel_id)) as cursor:
                    already_exists = await cursor.fetchone()
                if not already_exists:
                    await db.execute(
                        "INSERT INTO referrals (referrer_id, referred_id, channel_id, status, reward_amount) VALUES (?, ?, ?, 'pending', ?)",
                        (referrer_id, referred_id, channel_id, reward_rate)
                    )
                    await db.commit()

async def referral_verification_scheduler(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    cutoff_time = now - timedelta(hours=24)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, referrer_id, referred_id, channel_id, reward_amount FROM referrals WHERE status = 'pending' AND joined_at <= ?",
            (cutoff_time.strftime("%Y-%m-%d %H:%M:%S"),)
        ) as cursor:
            pending_list = await cursor.fetchall()

        for ref_id, referrer_id, referred_id, channel_id, reward_amount in pending_list:
            is_member = False
            try:
                member_obj = await context.bot.get_chat_member(chat_id=channel_id, user_id=referred_id)
                if member_obj.status in ["member", "administrator", "creator"]:
                    is_member = True
            except TelegramError:
                is_member = False

            if is_member:
                await db.execute("UPDATE referrals SET status = 'counted' WHERE id = ?", (ref_id,))
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward_amount, referrer_id))
                await db.commit()
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"✅ Referral Matured! ৳{reward_amount:.2f} credited."
                    )
                except Exception:
                    pass
            else:
                await db.execute("UPDATE referrals SET status = 'invalid' WHERE id = ?", (ref_id,))
                await db.commit()

# =====================================================================
# DYNAMIC INVITE LINK GENERATION
# =====================================================================
async def get_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    channel_id_str = await DatabaseManager.get_setting("channel_id")
    try:
        channel_id = int(channel_id_str)
        invite_link_obj = await context.bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"ref_{user_id}",
            creates_join_request=False
        )
        await update.message.reply_text(f"🔗 Here is your Link:\n{invite_link_obj.invite_link}")
    except Exception:
        bot_username = context.bot.username
        await update.message.reply_text(f"🔗 Link:\nhttps://t.me/{bot_username}?start=ref_{user_id}")

# =====================================================================
# PAYMENT & WITHDRAWAL SYSTEM
# =====================================================================
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = row[0] if row else 0.0
    min_withdrawal = float(await DatabaseManager.get_setting("min_withdrawal"))
    if balance < min_withdrawal:
        await update.message.reply_text(f"❌ Min payout is ৳{min_withdrawal:.2f}. Your Balance: ৳{balance:.2f}.")
        return ConversationHandler.END
    reply_markup = ReplyKeyboardMarkup([[KeyboardButton("bKash"), KeyboardButton("Nagad")], [KeyboardButton("Cancel")]], resize_keyboard=True)
    await update.message.reply_text("💳 Select Your Method:", reply_markup=reply_markup)
    return SELECT_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    method = update.message.text
    if method == "Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    context.user_data["payout_method"] = method
    await update.message.reply_text(f"📱 Enter your {method} personal number:")
    return ENTER_WALLET

async def withdraw_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    if not re.match(r"^01[3-9]\d{8}$", text):
        await update.message.reply_text("❌ Invalid mobile number! Try again:")
        return ENTER_WALLET
    context.user_data["payout_wallet"] = text
    await update.message.reply_text("💰 Enter withdrawal amount:")
    return ENTER_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("❌ Enter a valid number:")
        return ENTER_AMOUNT
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            balance = (await cursor.fetchone())[0]
        if amount > balance or amount < 50:
            await update.message.reply_text("❌ Invalid amount.")
            return ENTER_AMOUNT
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        cursor = await db.execute("INSERT INTO withdrawals (user_id, amount, method, wallet_number, status) VALUES (?, ?, ?, ?, 'pending')", (user_id, amount, context.user_data["payout_method"], context.user_data["payout_wallet"]))
        wd_id = cursor.lastrowid
        await db.commit()
    await update.message.reply_text(f"✅ Requested Withdrawal #{wd_id} for ৳{amount:.2f}", reply_markup=await get_main_keyboard(user_id))
    return ConversationHandler.END

# =====================================================================
# ADMIN ACTIONS
# =====================================================================
async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(query.from_user.id) != admin_id_str:
        return
    # Process inline buttons
    data = query.data
    action, wd_id = None, None
    if data.startswith("wd_approve_"):
        action, wd_id = "approved", int(data.replace("wd_approve_", ""))
    elif data.startswith("wd_reject_"):
        action, wd_id = "rejected", int(data.replace("wd_reject_", ""))
    elif data.startswith("wd_return_"):
        action, wd_id = "returned", int(data.replace("wd_return_", ""))

    if action and wd_id:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id, amount FROM withdrawals WHERE id = ?", (wd_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    user_id, amount = row
                    await db.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (action, wd_id))
                    if action == "returned":
                        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
                    await db.commit()
                    await query.edit_message_text(f"💰 Tx #{wd_id} is {action.upper()}")

# =====================================================================
# MIGRATION MODULE
# =====================================================================
async def cmd_replace_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str or len(context.args) < 2:
        return
    new_channel_id, new_channel_link = context.args[0], context.args[1]
    old_channel_id_str = await DatabaseManager.get_setting("channel_id")
    reward_rate = float(await DatabaseManager.get_setting("referral_reward"))
    await DatabaseManager.set_setting("channel_id", new_channel_id)
    await DatabaseManager.set_setting("channel_invite_link", new_channel_link)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM users") as cursor:
            users_list = await cursor.fetchall()
        for (target_user_id,) in users_list:
            async with db.execute("SELECT id, amount FROM withdrawals WHERE user_id = ? AND status = 'pending'", (target_user_id,)) as wd_cursor:
                pending_wds = await wd_cursor.fetchall()
            total_returned = 0.0
            for wd_id, wd_amount in pending_wds:
                total_returned += wd_amount
                await db.execute("UPDATE withdrawals SET status = 'returned' WHERE id = ?", (wd_id,))
            if total_returned > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_returned, target_user_id))
            async with db.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,)) as bal_cursor:
                current_bal = (await bal_cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND channel_id = ? AND compensated = 0", (target_user_id, int(old_channel_id_str))) as ref_cursor:
                unpaid_count = (await ref_cursor.fetchone())[0]
            
            comp_base = current_bal + (unpaid_count * reward_rate)
            credited = comp_base * 0.50
            if comp_base > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (credited, target_user_id))
                await db.execute("UPDATE referrals SET compensated = 1 WHERE referrer_id = ? AND channel_id = ?", (target_user_id, int(old_channel_id_str)))
                await db.commit()
                # Bengali migration notice
                notice = (
                    "--------------------------------------------------\n"
                    "✅ Channel Replacement Credit\n\n"
                    "পুরনো Channel-এর unpaid eligible balance থেকে 50% compensation আপনার নতুন balance-এ যোগ হয়েছে。\n\n"
                    f"💰 Compensation Base: ৳{comp_base:.2f}\n"
                                   "আগে Approved/Paid হয়ে যাওয়া Withdrawal এই হিসাবের মধ্যে ধরা হয়নি。\n\n"
                    "Pending Withdrawal compensation হিসাবের মধ্যে রাখা হয়েছে এবং পুরনো request Return করা হয়েছে。\n"
                    "--------------------------------------------------"
                )
                try:
                    await context.bot.send_message(chat_id=target_user_id, text=notice)
                except Exception:
                    pass
    await update.message.reply_text("✅ Migration Done!")

async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str:
        return
    await update.message.reply_text("⚙️ Admin commands:\n`/set_rate <rate>`\n`/replace_channel <id> <link>`\n`/broadcast <msg>`")

async def cmd_set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) == admin_id_str and context.args:
        await DatabaseManager.set_setting("referral_reward", context.args[0])
        await update.message.reply_text(f"Rate updated to: {context.args[0]}")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) == admin_id_str and context.args:
        msg = " ".join(context.args)
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users") as cursor:
                users = await cursor.fetchall()
        for (uid,) in users:
            try:
                await context.bot.send_message(chat_id=uid, text=msg)
                await asyncio.sleep(0.05)
            except Exception:
                pass

# =====================================================================
# MAIN INITIALIZER
# =====================================================================
async def post_init(application: Application) -> None:
    await DatabaseManager.init_db()
    logger.info("Database initiated successfully via post_init!")

def main():
    threading.Thread(target=run_flask_server, daemon=True).start()
    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN found!")
        return
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    job_queue = application.job_queue
    job_queue.run_repeating(referral_verification_scheduler, interval=60, first=10)

    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💳 Withdraw$") | filters.Regex("^/withdraw$"), withdraw_start)],
        states={
            SELECT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_method)],
            ENTER_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_wallet)],
            ENTER_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)]
        },
        fallbacks=[CommandHandler("start", start)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^👤 My Account$") | filters.Regex("^/account$"), display_account))
    application.add_handler(MessageHandler(filters.Regex("^🔗 Invite Link$") | filters.Regex("^/invite$"), get_invite_link))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ Admin Panel$") | filters.Regex("^/admin$"), cmd_admin_panel))
    
    application.add_handler(withdraw_conv)
    application.add_handler(CallbackQueryHandler(handle_admin_action, pattern="^wd_"))
    
    application.add_handler(CommandHandler("replace_channel", cmd_replace_channel))
    application.add_handler(CommandHandler("set_rate", cmd_set_rate))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, track_chat_member_joins))

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
