import os
import uuid
import json
import random
import threading
import requests
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import telebot
from telebot import types

# ============================================================
# Configuration - All secrets loaded from environment variables
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

# ============================================================
# App Version (Force Update)
# ============================================================
APP_VERSION = {
    "versionCode": 1,
    "versionName": "1.0.0",
    "force": True,
    "apkUrl": "",
    "message": "নতুন ভার্সন এসেছে। নতুন ফিচার ও বাগ ফিক্স সহ আপডেট করুন।",
    "changelog": [
        "প্রথম অফিসিয়াল রিলিজ",
        "Force Update সিস্টেম যোগ করা হয়েছে",
        "পারফরম্যান্স উন্নতি"
    ]
}

# ============================================================
# Initialize
# ============================================================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# In-memory stores
accepted_ids = set()
conversations = {}
telegram_msg_map = {}
admin_states = {}
notifications = {}
support_msg_map = {}
chat_links = {}


def pair_key(id1, id2):
    a, b = sorted([str(id1), str(id2)])
    return f"{a}_{b}"


def add_notif(user_id, message, is_read=False):
    notif = {
        "id": str(uuid.uuid4()),
        "message": message,
        "time": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "is_read": is_read,
    }
    if user_id not in notifications:
        notifications[user_id] = []
    notifications[user_id].insert(0, notif)
    try:
        sb_insert("notifications", {
            "user_id": user_id,
            "message": message,
            "is_read": is_read,
        })
    except Exception:
        pass
    return notif


# -------------------- Supabase Helpers --------------------
def sb_headers(prefer=None):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def sb_get(table, params=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    try:
        r = requests.get(url, headers=sb_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
        print(f"[SB] GET {table} → {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[SB] GET exception: {e}")
        return None


def sb_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    try:
        r = requests.post(
            url,
            headers=sb_headers("return=representation"),
            json=data,
            timeout=15
        )
        if r.status_code in (200, 201):
            return r.json()
        print(f"[SB] INSERT {table} → {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        print(f"[SB] INSERT exception: {e}")
        return None


def sb_update(table, match_params, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match_params}"
    try:
        r = requests.patch(
            url,
            headers=sb_headers("return=representation"),
            json=data,
            timeout=15
        )
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"[SB] UPDATE exception: {e}")
        return False


def sb_delete(table, match_params):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{match_params}"
    try:
        r = requests.delete(url, headers=sb_headers(), timeout=15)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"[SB] DELETE exception: {e}")
        return False


# -------------------- Admin Panel --------------------
def admin_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("📊 Total Users", callback_data="admin_total"),
        types.InlineKeyboardButton("⏳ Pending Users", callback_data="admin_pending"),
        types.InlineKeyboardButton("👤 Single Member Control", callback_data="admin_single"),
    )
    return markup


@bot.message_handler(commands=["admin", "start", "panel"])
def cmd_admin(message):
    if message.chat.id != GROUP_CHAT_ID:
        return
    bot.send_message(
        GROUP_CHAT_ID,
        "🔧 *Quick SMS Admin Panel*\n\nনিচের বাটন থেকে অপশন বেছে নাও:",
        parse_mode="Markdown",
        reply_markup=admin_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def handle_admin_callback(call):
    if call.message.chat.id != GROUP_CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return

    data = call.data

    if data == "admin_total":
        bot.answer_callback_query(call.id)
        users = sb_get("users", "status=eq.active&select=unique_id")
        count = len(users) if users else 0
        bot.send_message(
            GROUP_CHAT_ID,
            f"📊 *Total Active Users:* `{count}`",
            parse_mode="Markdown",
            reply_markup=admin_keyboard(),
        )

    elif data == "admin_pending":
        bot.answer_callback_query(call.id, "লোড হচ্ছে...")
        users = sb_get("users", "status=eq.pending&select=*&order=created_at.desc")
        if not users:
            users = sb_get("users", "status=eq.pending&select=*")
        if not users:
            bot.send_message(GROUP_CHAT_ID, "✅ কোনো Pending User নেই।", reply_markup=admin_keyboard())
            return

        bot.send_message(
            GROUP_CHAT_ID,
            f"⏳ *Pending Users:* `{len(users)}` জন",
            parse_mode="Markdown"
        )

        for u in users[:20]:
            uid = u.get("unique_id", "N/A")
            name = u.get("name", "—")
            email = u.get("email", "—")
            text = f"👤 *{name}*\n📧 `{email}`\n🆔 `{uid}`"
            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton("✅ Accept", callback_data=f"accept_reg_{uid}"),
                types.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_reg_{uid}"),
            )
            bot.send_message(GROUP_CHAT_ID, text, parse_mode="Markdown", reply_markup=kb)

        bot.send_message(GROUP_CHAT_ID, "─────", reply_markup=admin_keyboard())

    elif data == "admin_single":
        bot.answer_callback_query(call.id)
        admin_states[call.from_user.id] = {"step": "await_email"}
        bot.send_message(
            GROUP_CHAT_ID,
            "🔍 *Single Member Control*\n\nইউজারের *Gmail / Email* অথবা *User ID* লিখে পাঠাও:",
            parse_mode="Markdown",
        )


def count_user_messages(user_id):
    total = 0
    today = 0
    for key, msgs in conversations.items():
        if str(user_id) in key.split("_"):
            for m in msgs:
                if m.get("from") == user_id or m.get("to") == user_id:
                    total += 1
                    today += 1
    return total, today


def user_dashboard_text(user):
    uid = user.get("unique_id", "—")
    total_sms, today_sms = count_user_messages(uid)
    status = user.get("status", "—")
    status_emoji = {"active": "✅", "pending": "⏳", "suspended": "⏸"}.get(status, "•")
    return (
        f"🎛 *User Dashboard*\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"👤 নাম: *{user.get('name', '—')}*\n"
        f"📧 Email: `{user.get('email', '—')}`\n"
        f"🆔 ID: `{uid}`\n"
        f"{status_emoji} Status: `{status}`\n"
        f"🔑 Password: `{user.get('password', '—')}`\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💬 Total SMS: `{total_sms}`\n"
        f"📅 SMS (Session): `{today_sms}`\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"নিচের বাটন থেকে অ্যাকশন নাও:"
    )


def user_dashboard_keyboard(unique_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("🔑 Password Reset", callback_data=f"ud_pwreset_{unique_id}"),
        types.InlineKeyboardButton("👁 Show Password", callback_data=f"ud_showpw_{unique_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("⏸ Suspend", callback_data=f"ud_suspend_{unique_id}"),
        types.InlineKeyboardButton("✅ Activate", callback_data=f"ud_activate_{unique_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("📩 Single SMS", callback_data=f"ud_sms_{unique_id}"),
        types.InlineKeyboardButton("📊 Total SMS", callback_data=f"ud_totalsms_{unique_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("📅 SMS Today", callback_data=f"ud_todaysms_{unique_id}"),
        types.InlineKeyboardButton("🔄 Refresh", callback_data=f"ud_refresh_{unique_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("🗑 Delete Account", callback_data=f"ud_delete_{unique_id}"),
    )
    kb.row(
        types.InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_back"),
    )
    return kb


def fetch_user_by_id_or_email(query):
    query = (query or "").strip()
    if not query:
        return None
    if "@" in query:
        users = sb_get("users", f"email=eq.{query.lower()}&select=*")
        if users:
            return users[0]
    users = sb_get("users", f"unique_id=eq.{query.upper()}&select=*")
    if users:
        return users[0]
    users = sb_get("users", f"unique_id=eq.{query}&select=*")
    if users:
        return users[0]
    return None


@bot.message_handler(func=lambda m: m.chat.id == GROUP_CHAT_ID and m.from_user.id in admin_states)
def handle_admin_text(message):
    uid = message.from_user.id
    state = admin_states.get(uid)
    if not state:
        return

    if state["step"] == "await_email":
        query = (message.text or "").strip()
        user = fetch_user_by_id_or_email(query)
        if not user:
            bot.reply_to(message, f"❌ `{query}` দিয়ে কোনো ইউজার পাওয়া যায়নি।", parse_mode="Markdown")
            admin_states.pop(uid, None)
            return

        admin_states[uid] = {"step": "user_loaded", "user": user}
        bot.send_message(
            GROUP_CHAT_ID,
            user_dashboard_text(user),
            parse_mode="Markdown",
            reply_markup=user_dashboard_keyboard(user.get("unique_id")),
        )

    elif state["step"] == "await_sms_text":
        text = (message.text or "").strip()
        if not text:
            bot.reply_to(message, "❌ খালি মেসেজ পাঠানো যাবে না।")
            return

        target_id = state.get("target_id")
        if not target_id:
            admin_states.pop(uid, None)
            return

        add_notif(target_id, text, False)
        bot.reply_to(message, f"✅ Notification পাঠানো হয়েছে → `{target_id}`", parse_mode="Markdown")

        user = fetch_user_by_id_or_email(target_id)
        if user:
            admin_states[uid] = {"step": "user_loaded", "user": user}
            bot.send_message(
                GROUP_CHAT_ID,
                user_dashboard_text(user),
                parse_mode="Markdown",
                reply_markup=user_dashboard_keyboard(user.get("unique_id")),
            )
        else:
            admin_states.pop(uid, None)

    elif state["step"] == "await_new_password":
        new_pass = (message.text or "").strip()
        if len(new_pass) < 4:
            bot.reply_to(message, "❌ Password কমপক্ষে ৪ অক্ষর হতে হবে।")
            return

        target_id = state.get("target_id")
        ok = sb_update("users", f"unique_id=eq.{target_id}", {"password": new_pass})
        if ok:
            bot.reply_to(message, f"✅ Password রিসেট হয়েছে!\n🔑 `{new_pass}`", parse_mode="Markdown")
            add_notif(target_id, "🔑 Admin তোমার Password রিসেট করেছে।", False)
        else:
            bot.reply_to(message, "❌ Password আপডেট ব্যর্থ")

        user = fetch_user_by_id_or_email(target_id)
        if user:
            user["password"] = new_pass
            admin_states[uid] = {"step": "user_loaded", "user": user}
            bot.send_message(
                GROUP_CHAT_ID,
                user_dashboard_text(user),
                parse_mode="Markdown",
                reply_markup=user_dashboard_keyboard(user.get("unique_id")),
            )
        else:
            admin_states.pop(uid, None)


@bot.callback_query_handler(func=lambda call: call.data.startswith("sms_") or call.data == "admin_back" or call.data.startswith("ud_"))
def handle_sms_and_back(call):
    if call.message.chat.id != GROUP_CHAT_ID:
        return

    data = call.data

    if data == "admin_back":
        bot.answer_callback_query(call.id)
        admin_states.pop(call.from_user.id, None)
        bot.send_message(GROUP_CHAT_ID, "🔧 Admin Panel", reply_markup=admin_keyboard())
        return

    if data.startswith("sms_") and not data.startswith("ud_"):
        target_id = data.replace("sms_", "", 1)
        bot.answer_callback_query(call.id)
        admin_states[call.from_user.id] = {"step": "await_sms_text", "target_id": target_id}
        bot.send_message(
            GROUP_CHAT_ID,
            f"📩 *SMS / Notification*\n\nইউজার `{target_id}`-কে মেসেজ লিখে পাঠাও:",
            parse_mode="Markdown",
        )
        return

    if not data.startswith("ud_"):
        return

    rest = data[3:]
    actions = ["pwreset", "showpw", "suspend", "activate", "sms", "totalsms", "todaysms", "refresh", "delete", "delconfirm"]
    action = None
    target_id = None
    for a in actions:
        prefix = a + "_"
        if rest.startswith(prefix):
            action = a
            target_id = rest[len(prefix):]
            break

    if not action or not target_id:
        bot.answer_callback_query(call.id, "Invalid action")
        return

    if action == "sms":
        bot.answer_callback_query(call.id)
        admin_states[call.from_user.id] = {"step": "await_sms_text", "target_id": target_id}
        bot.send_message(GROUP_CHAT_ID, f"📩 *Single SMS*\n\nইউজার `{target_id}`-কে মেসেজ লিখে পাঠাও:", parse_mode="Markdown")
        return

    if action == "pwreset":
        bot.answer_callback_query(call.id)
        admin_states[call.from_user.id] = {"step": "await_new_password", "target_id": target_id}
        bot.send_message(GROUP_CHAT_ID, f"🔑 *Password Reset*\n\nনতুন Password লিখে পাঠাও:", parse_mode="Markdown")
        return

    if action == "showpw":
        user = fetch_user_by_id_or_email(target_id)
        if not user:
            bot.answer_callback_query(call.id, "User not found")
            return
        bot.answer_callback_query(call.id)
        bot.send_message(GROUP_CHAT_ID, f"🔑 *Password*\n\n🆔 `{target_id}`\nPassword: `{user.get('password', '—')}`", parse_mode="Markdown")
        return

    if action == "suspend":
        ok = sb_update("users", f"unique_id=eq.{target_id}", {"status": "suspended"})
        if ok:
            bot.answer_callback_query(call.id, "Suspended!")
            add_notif(target_id, "⏸ তোমার অ্যাকাউন্ট Suspend করা হয়েছে।", False)
            user = fetch_user_by_id_or_email(target_id)
            if user:
                bot.send_message(GROUP_CHAT_ID, user_dashboard_text(user), parse_mode="Markdown", reply_markup=user_dashboard_keyboard(target_id))
        else:
            bot.answer_callback_query(call.id, "Failed")
        return

    if action == "activate":
        ok = sb_update("users", f"unique_id=eq.{target_id}", {"status": "active"})
        if ok:
            bot.answer_callback_query(call.id, "Activated!")
            add_notif(target_id, "✅ তোমার অ্যাকাউন্ট Active করা হয়েছে।", False)
            user = fetch_user_by_id_or_email(target_id)
            if user:
                bot.send_message(GROUP_CHAT_ID, user_dashboard_text(user), parse_mode="Markdown", reply_markup=user_dashboard_keyboard(target_id))
        else:
            bot.answer_callback_query(call.id, "Failed")
        return

    if action == "totalsms":
        total, _ = count_user_messages(target_id)
        bot.answer_callback_query(call.id)
        bot.send_message(GROUP_CHAT_ID, f"📊 *Total SMS*\n\n🆔 `{target_id}`\nমোট মেসেজ: *{total}*", parse_mode="Markdown")
        return

    if action == "todaysms":
        _, today = count_user_messages(target_id)
        bot.answer_callback_query(call.id)
        bot.send_message(GROUP_CHAT_ID, f"📅 *SMS Today*\n\n🆔 `{target_id}`\nমেসেজ: *{today}*", parse_mode="Markdown")
        return

    if action == "refresh":
        user = fetch_user_by_id_or_email(target_id)
        if not user:
            bot.answer_callback_query(call.id, "User not found")
            return
        bot.answer_callback_query(call.id, "Refreshed")
        admin_states[call.from_user.id] = {"step": "user_loaded", "user": user}
        bot.send_message(GROUP_CHAT_ID, user_dashboard_text(user), parse_mode="Markdown", reply_markup=user_dashboard_keyboard(target_id))
        return

    if action == "delete":
        bot.answer_callback_query(call.id)
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("⚠️ হ্যাঁ, Delete করো", callback_data=f"ud_delconfirm_{target_id}"),
            types.InlineKeyboardButton("❌ না", callback_data=f"ud_refresh_{target_id}"),
        )
        bot.send_message(GROUP_CHAT_ID, f"⚠️ `{target_id}` অ্যাকাউন্ট স্থায়ীভাবে ডিলিট হবে। নিশ্চিত?", parse_mode="Markdown", reply_markup=kb)
        return

    if action == "delconfirm":
        ok = sb_delete("users", f"unique_id=eq.{target_id}")
        if ok:
            bot.answer_callback_query(call.id, "Deleted!")
            admin_states.pop(call.from_user.id, None)
            bot.send_message(GROUP_CHAT_ID, f"🗑 Account Deleted\n🆔 `{target_id}`", parse_mode="Markdown", reply_markup=admin_keyboard())
        else:
            bot.answer_callback_query(call.id, "Delete failed")
        return


# -------------------- Registration Accept / Delete --------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_reg_") or call.data.startswith("delete_reg_"))
def handle_reg_callback(call):
    if call.message.chat.id != GROUP_CHAT_ID:
        bot.answer_callback_query(call.id, "Unauthorized")
        return

    data = call.data

    if data.startswith("accept_reg_"):
        unique_id = data.replace("accept_reg_", "", 1)
        ok = sb_update("users", f"unique_id=eq.{unique_id}", {"status": "active"})
        if ok:
            bot.answer_callback_query(call.id, "Accepted!")
            bot.send_message(GROUP_CHAT_ID, f"✅ Account Accepted\n🆔 `{unique_id}`", parse_mode="Markdown")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "Update failed")

    elif data.startswith("delete_reg_"):
        unique_id = data.replace("delete_reg_", "", 1)
        ok = sb_delete("users", f"unique_id=eq.{unique_id}")
        if ok:
            bot.answer_callback_query(call.id, "Deleted!")
            bot.send_message(GROUP_CHAT_ID, f"🗑️ Account Deleted\n🆔 `{unique_id}`", parse_mode="Markdown")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "Delete failed")


# -------------------- Friend / Chat Request Handlers --------------------
@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_") or call.data.startswith("cancel_"))
def handle_friend_callback(call):
    data = call.data

    if data.startswith("accept_reg_") or data.startswith("delete_reg_"):
        return

    if data.startswith("accept_chat_"):
        rest = data.replace("accept_chat_", "", 1)
        parts = rest.split("_", 1)
        if len(parts) != 2:
            bot.answer_callback_query(call.id, "Invalid data")
            return
        from_id, to_id = parts[0], parts[1]
        ok = do_admin_accept_chat(from_id, to_id, call.message.message_id, call.message.chat.id)
        if ok:
            bot.answer_callback_query(call.id, "Accepted!")
        else:
            bot.answer_callback_query(call.id, "Already processed")
        return

    if data.startswith("cancel_chat_"):
        rest = data.replace("cancel_chat_", "", 1)
        parts = rest.split("_", 1)
        if len(parts) == 2:
            key = f"{parts[0]}_{parts[1]}"
            if key in chat_links:
                chat_links[key]["status"] = "rejected"
        bot.send_message(GROUP_CHAT_ID, f"❌ Chat Request Cancelled: `{rest}`", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Cancelled!")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return

    if data.startswith("accept_"):
        friend_id = data.replace("accept_", "", 1)
        if friend_id.startswith("reg_") or friend_id.startswith("chat_"):
            return
        accepted_ids.add(friend_id)
        bot.send_message(GROUP_CHAT_ID, f"✅ Friend Request Accepted! ID `{friend_id}`", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Accepted!")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

    elif data.startswith("cancel_"):
        friend_id = data.replace("cancel_", "", 1)
        if friend_id.startswith("chat_"):
            return
        bot.send_message(GROUP_CHAT_ID, f"❌ Friend Request Cancelled: `{friend_id}`", parse_mode="Markdown")
        bot.answer_callback_query(call.id, "Cancelled!")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass


@bot.message_handler(func=lambda message: message.reply_to_message is not None)
def handle_reply(message):
    if message.chat.id != GROUP_CHAT_ID:
        return
    if message.from_user.id in admin_states:
        return

    replied_msg_id = message.reply_to_message.message_id
    reply_text = message.text or ""
    if not reply_text.strip():
        return

    if replied_msg_id in support_msg_map:
        user_id = support_msg_map[replied_msg_id]
        add_notif(user_id, f"💬 Support Reply: {reply_text}", False)
        bot.reply_to(message, f"✅ Support রিপ্লাই পাঠানো হয়েছে → `{user_id}`", parse_mode="Markdown")
        return

    if replied_msg_id not in telegram_msg_map:
        return

    conv_id = telegram_msg_map[replied_msg_id]
    if conv_id not in conversations:
        conversations[conv_id] = []

    msg_obj = {
        "id": str(uuid.uuid4()),
        "from": "admin",
        "text": reply_text,
        "time": datetime.now().strftime("%I:%M %p"),
        "direction": "received",
    }
    conversations[conv_id].append(msg_obj)
    bot.reply_to(message, f"✅ রিপ্লাই পাঠানো হয়েছে → `{conv_id}`", parse_mode="Markdown")


# -------------------- Flask Routes --------------------
@app.route("/")
def home():
    candidates = ["index.html", "QuickSMS.html"]
    for name in candidates:
        path = os.path.join(BASE_DIR, name)
        if os.path.isfile(path):
            return send_from_directory(BASE_DIR, name)
    return jsonify({"status": "Quick SMS API is running", "version": APP_VERSION["versionName"]})


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "নাম লিখুন"}), 400
    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "সঠিক Email দিন"}), 400
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Password কমপক্ষে ৪ অক্ষর হতে হবে"}), 400

    existing = sb_get("users", f"email=eq.{email}&select=email")
    if existing:
        return jsonify({"ok": False, "error": "এই Email দিয়ে আগেই Registration করা আছে"}), 400

    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    unique_id = "".join(random.choice(chars) for _ in range(7))

    row = {
        "name": name,
        "email": email,
        "password": password,
        "unique_id": unique_id,
        "status": "pending",
    }
    result = sb_insert("users", row)
    if result is None:
        return jsonify({"ok": False, "error": "ডাটাবেসে সেভ হয়নি"}), 500

    msg = (
        f"🆕 *New Registration Request*\n\n"
        f"নাম: *{name}*\n"
        f"ID: `{unique_id}`\n"
        f"Email: `{email}`\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
    )
    try:
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ Accept", callback_data=f"accept_reg_{unique_id}"),
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_reg_{unique_id}"),
        )
        bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        print("Telegram notify error:", e)

    return jsonify({
        "ok": True,
        "message": "Registration সফল! Admin Accept করলে Login করতে পারবে।",
        "unique_id": unique_id,
        "status": "pending",
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"ok": False, "error": "Email ও Password দিন"}), 400

    users = sb_get("users", f"email=eq.{email}&password=eq.{password}&select=*")
    if not users:
        return jsonify({"ok": False, "error": "Email বা Password ভুল"}), 401

    user = users[0]
    status = user.get("status") or "active"

    if status == "pending":
        return jsonify({"ok": False, "error": "⏳ তোমার অ্যাকাউন্ট এখনো Pending।"}), 403
    if status == "suspended":
        return jsonify({"ok": False, "error": "⏸ তোমার অ্যাকাউন্ট Suspend করা আছে।"}), 403
    if status != "active":
        return jsonify({"ok": False, "error": "অ্যাকাউন্টটি Active নয়।"}), 403

    return jsonify({
        "ok": True,
        "user": {
            "name": user.get("name"),
            "email": user.get("email"),
            "id": user.get("unique_id"),
        },
    })


@app.route("/api/friend_request", methods=["POST"])
def api_friend_request():
    data = request.get_json() or {}
    from_id = (data.get("from_id") or "").strip()
    from_name = (data.get("from_name") or "").strip()
    to_id = (data.get("to_id") or "").strip().upper()

    if not from_id or not to_id:
        return jsonify({"ok": False, "error": "from_id এবং to_id লাগবে"}), 400
    if from_id == to_id:
        return jsonify({"ok": False, "error": "নিজেকে Friend Request পাঠানো যাবে না"}), 400

    target = sb_get("users", f"unique_id=eq.{to_id}&select=unique_id,name,status")
    if not target:
        return jsonify({"ok": False, "error": "এই Friend ID পাওয়া যায়নি"}), 404
    if (target[0].get("status") or "active") != "active":
        return jsonify({"ok": False, "error": "এই ইউজার Active নয়"}), 400

    existing_acc = sb_get(
        "friend_requests",
        f"or=(and(from_id.eq.{from_id},to_id.eq.{to_id}),and(from_id.eq.{to_id},to_id.eq.{from_id}))&status=eq.accepted&select=id"
    )
    if existing_acc:
        return jsonify({"ok": False, "error": "ইতিমধ্যে Friend আছে"}), 400

    existing_pend = sb_get("friend_requests", f"from_id=eq.{from_id}&to_id=eq.{to_id}&status=eq.pending&select=id")
    if existing_pend:
        return jsonify({"ok": False, "error": "Request ইতিমধ্যে পাঠানো আছে"}), 400

    row = {
        "from_id": from_id,
        "from_name": from_name or from_id,
        "to_id": to_id,
        "status": "pending",
    }
    result = sb_insert("friend_requests", row)
    if result is None:
        return jsonify({"ok": False, "error": "Request সেভ হয়নি"}), 500

    return jsonify({"ok": True, "message": "Friend Request পাঠানো হয়েছে!"})


@app.route("/api/friend_requests/<user_id>")
def api_get_friend_requests(user_id):
    reqs = sb_get("friend_requests", f"to_id=eq.{user_id}&status=eq.pending&select=*&order=created_at.desc") or []
    return jsonify({"requests": reqs})


@app.route("/api/friends/<user_id>")
def api_get_friends(user_id):
    as_from = sb_get("friend_requests", f"from_id=eq.{user_id}&status=eq.accepted&select=*") or []
    as_to = sb_get("friend_requests", f"to_id=eq.{user_id}&status=eq.accepted&select=*") or []
    friends = []
    for r in as_from:
        friends.append({"id": r.get("to_id"), "name": r.get("to_id"), "since": r.get("created_at")})
    for r in as_to:
        friends.append({
            "id": r.get("from_id"),
            "name": r.get("from_name") or r.get("from_id"),
            "since": r.get("created_at"),
        })
    return jsonify({"friends": friends, "count": len(friends)})


@app.route("/api/friend_respond", methods=["POST"])
def api_friend_respond():
    data = request.get_json() or {}
    request_id = (data.get("request_id") or "").strip()
    action = (data.get("action") or "").strip()

    if not request_id or action not in ("accept", "cancel"):
        return jsonify({"ok": False, "error": "request_id এবং action লাগবে"}), 400

    new_status = "accepted" if action == "accept" else "cancelled"
    ok = sb_update("friend_requests", f"id=eq.{request_id}", {"status": new_status})
    if not ok:
        return jsonify({"ok": False, "error": "Update ব্যর্থ"}), 500
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/support", methods=["POST"])
def api_support():
    data = request.get_json() or {}
    user_id = (data.get("user_id") or "").strip()
    user_name = (data.get("user_name") or "").strip()
    text = (data.get("text") or "").strip()

    if not user_id or not text:
        return jsonify({"ok": False, "error": "user_id এবং text লাগবে"}), 400

    telegram_text = (
        f"🆘 *Support Message*\n\n"
        f"👤 *From:* {user_name or '—'} (`{user_id}`)\n"
        f"────────────────\n"
        f"{text}\n\n"
        f"_এই মেসেজে Reply করলে ইউজারের Notification-এ যাবে_"
    )
    try:
        sent = bot.send_message(GROUP_CHAT_ID, telegram_text, parse_mode="Markdown")
        support_msg_map[sent.message_id] = user_id
        add_notif(user_id, f"🆘 Support: {text}", True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def do_admin_accept_chat(from_id, to_id, telegram_message_id=None, chat_id=None):
    key = f"{from_id}_{to_id}"
    link = chat_links.get(key)
    if not link:
        link = {
            "from_id": from_id,
            "to_id": to_id,
            "from_name": from_id,
            "to_name": to_id,
            "status": "awaiting_admin",
        }
        chat_links[key] = link

    if link.get("status") in ("awaiting_target", "connected", "rejected"):
        return False

    link["status"] = "awaiting_target"
    accepted_ids.add(to_id)

    add_notif(
        to_id,
        f"👥 Chat Request: {link.get('from_name', from_id)} ({from_id}) তোমার সাথে কথা বলতে চায়।",
        False,
    )
    link["pending_for"] = to_id

    try:
        bot.send_message(
            GROUP_CHAT_ID,
            f"✅ *Accepted*\n`{from_id}` → `{to_id}`\nএখন **{to_id}** এর অ্যাপে Request গেছে।",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    if telegram_message_id and chat_id:
        try:
            bot.edit_message_reply_markup(chat_id, telegram_message_id, reply_markup=None)
        except Exception:
            pass

    return True


@app.route("/api/chat_request", methods=["POST"])
def api_chat_request():
    data = request.get_json() or {}
    from_id = (data.get("from_id") or "").strip()
    from_name = (data.get("from_name") or "").strip()
    to_id = (data.get("to_id") or "").strip()

    if not from_id or not to_id:
        return jsonify({"ok": False, "error": "from_id এবং to_id লাগবে"}), 400
    if from_id == to_id:
        return jsonify({"ok": False, "error": "নিজের ID দিয়ে Request যাবে না"}), 400

    key = f"{from_id}_{to_id}"
    chat_links[key] = {
        "from_id": from_id,
        "from_name": from_name or from_id,
        "to_id": to_id,
        "to_name": to_id,
        "status": "awaiting_admin",
    }

    msg = (
        f"🔔 *New Chat Request*\n\n"
        f"👤 From: *{from_name or from_id}* (`{from_id}`)\n"
        f"👤 To: `{to_id}`\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n"
        f"⏳ *১ সেকেন্ড পর অটো Accept হবে...*"
    )
    try:
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("✅ Accept Now", callback_data=f"accept_chat_{from_id}_{to_id}"),
            types.InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_chat_{from_id}_{to_id}"),
        )
        sent = bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown", reply_markup=kb)

        def _auto_accept():
            do_admin_accept_chat(from_id, to_id, sent.message_id, sent.chat.id)

        timer = threading.Timer(1.0, _auto_accept)
        timer.daemon = True
        timer.start()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Telegram error: {e}"}), 500


@app.route("/api/chat_status/<from_id>/<to_id>")
def api_chat_status(from_id, to_id):
    key = f"{from_id}_{to_id}"
    link = chat_links.get(key)
    if not link:
        if to_id in accepted_ids:
            return jsonify({"status": "connected"})
        return jsonify({"status": "pending"})
    return jsonify({
        "status": link.get("status", "pending"),
        "link": {
            "from_id": link.get("from_id"),
            "to_id": link.get("to_id"),
            "from_name": link.get("from_name"),
        }
    })


@app.route("/api/incoming_chats/<user_id>")
def api_incoming_chats(user_id):
    incoming = []
    for key, link in chat_links.items():
        if link.get("to_id") == user_id and link.get("status") == "awaiting_target":
            incoming.append({
                "key": key,
                "from_id": link.get("from_id"),
                "from_name": link.get("from_name"),
                "to_id": link.get("to_id"),
            })
    return jsonify({"requests": incoming})


@app.route("/api/chat_respond", methods=["POST"])
def api_chat_respond():
    data = request.get_json() or {}
    from_id = (data.get("from_id") or "").strip()
    to_id = (data.get("to_id") or "").strip()
    action = (data.get("action") or "").strip()

    if not from_id or not to_id or action not in ("accept", "cancel"):
        return jsonify({"ok": False, "error": "from_id, to_id, action লাগবে"}), 400

    key = f"{from_id}_{to_id}"
    link = chat_links.get(key)
    if not link:
        return jsonify({"ok": False, "error": "Request পাওয়া যায়নি"}), 404

    if action == "accept":
        link["status"] = "connected"
        accepted_ids.add(to_id)
        accepted_ids.add(from_id)
        add_notif(from_id, f"✅ {to_id} তোমার Chat Request Accept করেছে!", False)
        try:
            bot.send_message(GROUP_CHAT_ID, f"🎉 *Chat Connected!*\n`{from_id}` ↔ `{to_id}`", parse_mode="Markdown")
        except Exception:
            pass
        return jsonify({"ok": True, "status": "connected"})
    else:
        link["status"] = "rejected"
        add_notif(from_id, f"❌ {to_id} তোমার Chat Request Cancel করেছে।", False)
        return jsonify({"ok": True, "status": "rejected"})


@app.route("/status/<friend_id>")
def check_status(friend_id):
    if friend_id in accepted_ids:
        return jsonify({"status": "accepted"})
    for key, link in chat_links.items():
        if link.get("to_id") == friend_id and link.get("status") == "connected":
            return jsonify({"status": "accepted"})
        if link.get("to_id") == friend_id and link.get("status") == "awaiting_target":
            return jsonify({"status": "pending"})
    return jsonify({"status": "pending"})


@app.route("/send_message", methods=["POST"])
def send_message():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400

    from_id = data.get("from_id", "").strip()
    to_id = data.get("to_id", "").strip()
    text = data.get("text", "").strip()

    if not from_id or not to_id or not text:
        return jsonify({"ok": False, "error": "from_id, to_id এবং text লাগবে"}), 400

    conv_id = pair_key(from_id, to_id)

    telegram_text = (
        f"📩 *New Message*\n\n"
        f"📤 *From:* `{from_id}`\n"
        f"📥 *To:* `{to_id}`\n"
        f"────────────────\n"
        f"{text}"
    )

    try:
        try:
            sent = bot.send_message(GROUP_CHAT_ID, telegram_text, parse_mode="Markdown")
            telegram_msg_map[sent.message_id] = conv_id
        except Exception as te:
            print("Telegram send failed:", te)

        if conv_id not in conversations:
            conversations[conv_id] = []

        msg_obj = {
            "id": str(uuid.uuid4()),
            "from": from_id,
            "to": to_id,
            "text": text,
            "time": datetime.now().strftime("%I:%M %p"),
        }
        conversations[conv_id].append(msg_obj)

        return jsonify({"ok": True, "message_id": msg_obj["id"], "time": msg_obj["time"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/messages/<from_id>/<to_id>")
def get_messages(from_id, to_id):
    conv_id = pair_key(from_id, to_id)
    raw = conversations.get(conv_id, [])
    viewer = from_id
    msgs = []
    for m in raw:
        direction = "sent" if m.get("from") == viewer else "received"
        msgs.append({
            "id": m.get("id"),
            "text": m.get("text"),
            "time": m.get("time"),
            "direction": direction,
            "from": m.get("from"),
        })
    return jsonify({"messages": msgs})


@app.route("/notifications/<user_id>")
def get_notifications(user_id):
    mem = notifications.get(user_id, [])
    sb_list = sb_get("notifications", f"user_id=eq.{user_id}&select=*&order=created_at.desc") or []
    result = []
    seen = set()
    for n in mem:
        if n["id"] not in seen:
            seen.add(n["id"])
            result.append(n)
    for n in sb_list:
        nid = str(n.get("id", ""))
        if nid and nid not in seen:
            seen.add(nid)
            result.append({
                "id": nid,
                "message": n.get("message", ""),
                "time": n.get("created_at", "")[:16].replace("T", " ") if n.get("created_at") else "",
                "is_read": n.get("is_read", False),
            })
    return jsonify({"notifications": result})


@app.route("/notifications/<user_id>/read", methods=["POST"])
def mark_notifications_read(user_id):
    if user_id in notifications:
        for n in notifications[user_id]:
            n["is_read"] = True
    sb_update("notifications", f"user_id=eq.{user_id}", {"is_read": True})
    return jsonify({"ok": True})


@app.route("/clear_messages/<from_id>/<to_id>", methods=["POST"])
def clear_messages(from_id, to_id):
    conv_id = f"{from_id}_{to_id}"
    if conv_id in conversations:
        conversations[conv_id] = []
    return jsonify({"ok": True})


@app.route("/api/version", methods=["GET"])
def api_version():
    data = dict(APP_VERSION)
    if not data.get("apkUrl"):
        version_name = data.get("versionName", "1.0.0")
        filename = f"QuickSMS-v{version_name}.apk"
        local_path = os.path.join(DOWNLOADS_DIR, filename)
        if os.path.isfile(local_path):
            host = request.host_url.rstrip("/")
            data["apkUrl"] = f"{host}/downloads/{filename}"
    return jsonify(data)


@app.route("/downloads/<path:filename>")
def serve_download(filename):
    return send_from_directory(DOWNLOADS_DIR, filename, as_attachment=True)


# -------------------- Startup --------------------
def run_bot():
    print("Telegram Bot started")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 5000))
    print(f"Starting server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
