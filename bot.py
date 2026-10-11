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

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables থেকে টোকেন ও আইডি নেওয়া
BOT_TOKEN = os.getenv("TOKEN")
ADMIN_ID_STR = os.getenv("ADMIN_ID", "8549397639")
DB_PATH = "referral_bot.db"

# =====================================================================
# FLASK WEB SERVER FOR HEALTH CHECK (RENDER FREE SERVICE OPTIMIZATION)
# =====================================================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is active and running!", 200

@flask_app.route('/health')
def health():
    return "OK", 200

def run_flask_server():
    # Render স্বয়ংক্রিয়ভাবে PORT এনভায়রনমেন্ট ভ্যারিয়েবল পাঠায়
    port = int(os.environ.get("PORT", 8080))
    # Flask রানিং ইন ব্যাকগ্রাউন্ড (Thread)
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# Withdrawal States for ConversationHandler
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
# SHARED TELEGRAM & UTILITY HELPER LOGIC
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

# =====================================================================
# COMMANDS & MAIN MENU HANDLERS
# =====================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "No Username"
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 0.0)",
            (user_id, username)
        )
        await db.commit()

    welcome_text = (
        "👋 Welcome to the Advanced Referral & Payout Bot!\n\n"
        "Earn real payouts by sharing your exclusive invite links to our channels. "
        "Use the keyboard menu below to get started!"
    )
    await update.message.reply_text(
        welcome_text,
        reply_markup=await get_main_keyboard(user_id)
    )

# =====================================================================
# USER ACCOUNT DASHBOARD LOGIC
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
# ADVANCED MEMBER VERIFICATION SYSTEM
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
                        text=f"✅ Referral Matured!\nUser {referred_id} has been active for 24 hours. ৳{reward_amount:.2f} has been added to your balance."
                    )
                except Exception:
                    pass
            else:
                await db.execute("UPDATE referrals SET status = 'invalid' WHERE id = ?", (ref_id,))
                await db.commit()
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"❌ Referral Verification Failed!\nUser {referred_id} left before 24 Hours. Earning discarded."
                    )
                except Exception:
                    pass

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
        url = invite_link_obj.invite_link
        await update.message.reply_text(
            f"🔗 Here is your exclusive Invite Link:\n{url}\n\n"
            f"Share this link with your friends! After joining, if they remain in the channel for 24 hours, "
            f"you will be rewarded."
        )
    except Exception as e:
        logger.error(f"Failed to generate Invite link: {e}")
        bot_username = context.bot.username
        fallback_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        await update.message.reply_text(
            f"⚠️ Bot is currently running on localized Invite Links. Here is your backup Invite Link:\n{fallback_link}"
        )

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
        await update.message.reply_text(
            f"❌ Insufficient Balance! Minimum withdrawal limit is ৳{min_withdrawal:.2f}. "
            f"Your Balance: ৳{balance:.2f}."
        )
        return ConversationHandler.END

    reply_markup = ReplyKeyboardMarkup(
        [[KeyboardButton("bKash"), KeyboardButton("Nagad")], [KeyboardButton("Cancel")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text("💳 Select Your Payout Method:", reply_markup=reply_markup)
    return SELECT_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    method = update.message.text
    if method == "Cancel":
        await update.message.reply_text("Withdrawal process cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END

    if method not in ["bKash", "Nagad"]:
        await update.message.reply_text("Please use the keyboard to select either bKash or Nagad.")
        return SELECT_METHOD

    context.user_data["payout_method"] = method
    await update.message.reply_text(
        f"📱 Please enter your {method} personal account number (11 digits):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Cancel")]], resize_keyboard=True)
    )
    return ENTER_WALLET

async def withdraw_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "Cancel":
        await update.message.reply_text("Withdrawal process cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END

    if not re.match(r"^01[3-9]\d{8}$", text):
        await update.message.reply_text("❌ Invalid Bangladeshi mobile number! Please try again:")
        return ENTER_WALLET

    context.user_data["payout_wallet"] = text
    
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = row[0] if row else 0.0

    await update.message.reply_text(
        f"💰 Please enter your withdrawal amount (Max: ৳{balance:.2f}):",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Cancel")]], resize_keyboard=True)
    )
    return ENTER_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text == "Cancel":
        await update.message.reply_text("Withdrawal process cancelled.", reply_markup=await get_main_keyboard(update.effective_user.id))
        return ConversationHandler.END

    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount format. Please enter a valid number:")
        return ENTER_AMOUNT

    user_id = update.effective_user.id
    min_withdrawal = float(await DatabaseManager.get_setting("min_withdrawal"))

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            balance = row[0] if row else 0.0

        if amount > balance:
            await update.message.reply_text(f"❌ You cannot withdraw more than your available balance (৳{balance:.2f}).")
            return ENTER_AMOUNT
        if amount < min_withdrawal:
            await update.message.reply_text(f"❌ Minimum withdrawal amount is ৳{min_withdrawal:.2f}.")
            return ENTER_AMOUNT

        new_balance = balance - amount
        await db.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        
        method = context.user_data["payout_method"]
        wallet = context.user_data["payout_wallet"]
        cursor = await db.execute(
            "INSERT INTO withdrawals (user_id, amount, method, wallet_number, status) VALUES (?, ?, ?, ?, 'pending')",
            (user_id, amount, method, wallet)
        )
        wd_id = cursor.lastrowid
        await db.commit()

    await update.message.reply_text(
        f"✅ Withdrawal Requested!\n"
        f"Request ID: #{wd_id}\n"
        f"Method: {method}\n"
        f"Wallet Number: {wallet}\n"
        f"Amount: ৳{amount:.2f}\n"
        f"Our team will process it shortly.",
        reply_markup=await get_main_keyboard(user_id)
    )

    admin_id = await DatabaseManager.get_setting("admin_id")
    try:
        admin_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{wd_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{wd_id}"),
                InlineKeyboardButton("🔄 Return", callback_data=f"wd_return_{wd_id}")
            ]
        ])
        await context.bot.send_message(
            chat_id=int(admin_id),
            text=f"🚨 New Withdrawal Request!\n\n"
                 f"User ID: {user_id}\n"
                 f"Req ID: #{wd_id}\n"
                 f"Method: {method}\n"
                 f"Wallet: {wallet}\n"
                 f"Amount: ৳{amount:.2f}",
            reply_markup=admin_keyboard
        )
    except Exception as e:
        logger.error(f"Failed to alert admin: {e}")

    return ConversationHandler.END

# =====================================================================
# ADMIN CONTROLS
# =====================================================================
async def handle_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(query.from_user.id) != admin_id_str:
        await query.edit_message_text("❌ Unauthorized Command.")
        return

    data = query.data
    action, wd_id = None, None
    if data.startswith("wd_approve_"):
        action = "approved"
        wd_id = int(data.replace("wd_approve_", ""))
    elif data.startswith("wd_reject_"):
        action = "rejected"
        wd_id = int(data.replace("wd_reject_", ""))
    elif data.startswith("wd_return_"):
        action = "returned"
        wd_id = int(data.replace("wd_return_", ""))

    if action and wd_id:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id, amount, method, wallet_number, status FROM withdrawals WHERE id = ?", (wd_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await query.edit_message_text("❌ Withdrawal request not found.")
                    return
                user_id, amount, method, wallet, current_status = row

                if current_status != "pending":
                    await query.edit_message_text(f"⚠️ Action already completed! This was: {current_status}")
                    return

                await db.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (action, wd_id))
                
                if action == "returned":
                    await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
                
                await db.commit()

        await query.edit_message_text(
            f"💰 Request #{wd_id} Updated to: {action.upper()}\n"
            f"User: {user_id} | Amount: ৳{amount:.2f} | Method: {method}"
        )

        try:
            status_symbol = "✅" if action == "approved" else "❌" if action == "rejected" else "🔄"
            await context.bot.send_message(
                chat_id=user_id,
                text=f"{status_symbol} Your Withdrawal Request #{wd_id} for ৳{amount:.2f} via {method} has been {action.upper()}."
            )
        except Exception:
            pass

# =====================================================================
# CHANNEL REPLACEMENT & COMPENSATION
# =====================================================================
async def cmd_replace_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str:
        await update.message.reply_text("❌ Unauthorized command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("💡 Usage: `/replace_channel <new_channel_id> <new_channel_invite_link>`")
        return

    new_channel_id = context.args[0]
    new_channel_link = context.args[1]

    old_channel_id_str = await DatabaseManager.get_setting("channel_id")
    reward_rate = float(await DatabaseManager.get_setting("referral_reward"))

    await DatabaseManager.set_setting("channel_id", new_channel_id)
    await DatabaseManager.set_setting("channel_invite_link", new_channel_link)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT user_id FROM users") as cursor:
            users_list = await cursor.fetchall()

        for (target_user_id,) in users_list:
            async with db.execute(
                "SELECT id, amount FROM withdrawals WHERE user_id = ? AND status = 'pending'", (target_user_id,)
            ) as wd_cursor:
                pending_withdrawals = await wd_cursor.fetchall()

            total_returned_amount = 0.0
            for wd_id, wd_amount in pending_withdrawals:
                total_returned_amount += wd_amount
                await db.execute("UPDATE withdrawals SET status = 'returned' WHERE id = ?", (wd_id,))

            if total_returned_amount > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_returned_amount, target_user_id))

            async with db.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,)) as bal_cursor:
                user_bal_row = await bal_cursor.fetchone()
                current_available_balance = user_bal_row[0] if user_bal_row else 0.0

            async with db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND channel_id = ? AND compensated = 0 AND (status = 'pending' OR status = 'counted')",
                (target_user_id, int(old_channel_id_str))
            ) as ref_count_cursor:
                unpaid_referrals_count = (await ref_count_cursor.fetchone())[0]

            old_unpaid_earnings = unpaid_referrals_count * reward_rate
            compensation_base = current_available_balance + old_unpaid_earnings
            credited_amount = compensation_base * 0.50

            if compensation_base > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (credited_amount, target_user_id))
                await db.execute(
                    "UPDATE referrals SET compensated = 1 WHERE referrer_id = ? AND channel_id = ? AND compensated = 0",
                    (target_user_id, int(old_channel_id_str))
                )
                await db.commit()

                compensation_notice = (
                    "--------------------------------------------------\n"
                    "✅ Channel Replacement Credit\n\n"
                    "পুরনো Channel-এর unpaid eligible balance থেকে 50% compensation আপনার নতুন balance-এ যোগ হয়েছে。\n\n"
                    f"💰 Compensation Base: ৳{compensation_base:.2f}\n"
                    f"✅ Credited Amount: ৳{credited_amount:.2f}\n\n"
                    "আগে Approved/Paid হয়ে যাওয়া Withdrawal এই হিসাবের মধ্যে ধরা হয়নি。\n\n"
                    "Pending Withdrawal compensation হিসাবের মধ্যে রাখা হয়েছে এবং পুরনো request Return করা হয়েছে。\n"
                    "--------------------------------------------------"
                )
                try:
                    await context.bot.send_message(chat_id=target_user_id, text=compensation_notice)
                except Exception:
                    pass

    await update.message.reply_text("✅ Channel Migration & 50% compensation processing complete!")

# =====================================================================
# GLOBAL STATS & ADMIN PANEL
# =====================================================================
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str:
        await update.message.reply_text("❌ Unauthorized.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'") as cursor:
            pending_wd_count = (await cursor.fetchone())[0]

    current_reward = await DatabaseManager.get_setting("referral_reward")
    current_channel = await DatabaseManager.get_setting("channel_id")

    admin_panel_text = (
        "⚙️ Bot Master Panel\n\n"
        f"👥 Registered Users: {total_users}\n"
        f"⏳ Pending Withdrawals: {pending_wd_count}\n"
        f"💰 Current Referral Rate: ৳{current_reward}\n"
        f"📡 Bound Channel ID: {current_channel}\n\n"
        "🛠 Admin Commands available:\n"
        "• `/set_rate <amount>`\n"
        "• `/replace_channel <new_channel_id> <new_channel_invite_link>`\n"
        "• `/broadcast <your_message_here>`"
    )
    await update.message.reply_text(admin_panel_text)

async def cmd_set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str:
        return
    if not context.args:
        await update.message.reply_text("💡 Usage: `/set_rate <decimal_value>`")
        return
    
    rate = context.args[0]
    await DatabaseManager.set_setting("referral_reward", rate)
    await update.message.reply_text(f"✅ Referral Reward rate has been set to: ৳{rate}")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    admin_id_str = await DatabaseManager.get_setting("admin_id")
    if str(user_id) != admin_id_str:
        return
    
    if not context.args:
        await update.message.reply_text("💡 Usage: `/broadcast <message>`")
        return

    broadcast_msg = " ".join(context.args)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users_list = await cursor.fetchall()

    sent_count = 0
    for (target_user_id,) in users_list:
        try:
            await context.bot.send_message(chat_id=target_user_id, text=broadcast_msg)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await update.message.reply_text(f"📢 Broadcast sent to {sent_count} active users.")

# =====================================================================
# SYSTEM INITIALIZER & PIPELINE MAIN LOOP
# =====================================================================
def main():
    # ১. প্রথমে ব্যাকগ্রাউন্ড থ্রেডে ফ্লাস্ক ওয়েব সার্ভার রান করা
    # এটি রেন্ডার পোর্ট বাইন্ডিং ও লাইভ হেলথ চেক সাকসেসফুলি হ্যান্ডেল করবে।
    threading.Thread(target=run_flask_server, daemon=True).start()

    asyncio.get_event_loop().run_until_complete(DatabaseManager.init_db())

    if not BOT_TOKEN:
        logger.error("No BOT_TOKEN found! Add 'TOKEN' environment variable.")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    job_queue = application.job_queue
    job_queue.run_repeating(referral_verification_scheduler, interval=60, first=10)

    withdraw_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💳 Withdraw$") | filters.Regex("^/withdraw$"), withdraw_start)
        ],
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
    application.add_handler(CallbackQueryHandler(handle_admin_action))

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
