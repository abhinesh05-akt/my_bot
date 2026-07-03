"""
Telegram Audio Batch Bot — Render deployment
Webhook mode: Telegram -> Render directly (no relay needed for inbound).
Outbound (bot -> Telegram): tries api.telegram.org directly first.
If TELEGRAM_API_BASE_URL is set, routes through that Worker instead
(only needed if Render blocks outbound to api.telegram.org).
"""

import asyncio
import logging
import os
import json
import re
import time
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatJoinRequestHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest
from telegram.error import Forbidden

from db import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Live Drive download: file-presence gate ─────────────────────────────────
# download_from_drive local_sync.py mein hai (standalone, kisi module par
# depend nahi karta). Agar deploy karte waqt ye file bhi saath upload ki
# gayi hai (Render ho ya local, farak nahi padta), import succeed hoga aur
# live Drive download available hoga. Agar file hata di gayi hai deploy se
# pehle, ImportError aayega aur feature khud-ba-khud band ho jayega —
# uncached audios ke liye seedha "abhi available nahi hai" message jayega,
# koi crash nahi.
try:
    from local_sync import download_from_drive
    LIVE_DOWNLOAD_AVAILABLE = True
except ImportError:
    download_from_drive = None
    LIVE_DOWNLOAD_AVAILABLE = False

logger.info(
    "Live Drive download: %s (local_sync.py %s)",
    "ENABLED" if LIVE_DOWNLOAD_AVAILABLE else "DISABLED",
    "found" if LIVE_DOWNLOAD_AVAILABLE else "missing"
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
OWNER_ID       = int(os.environ["OWNER_ID"])
BOT_USERNAME   = os.environ["BOT_USERNAME"].strip().lstrip("@")
DATABASE_URL   = os.environ["DATABASE_URL"]
BATCH_MAX      = 50
DELETE_MINUTES = 5

# Render ka apna public HTTPS URL — Telegram seedha yahan POST karta hai.
# Format: https://your-service.onrender.com/webhook
WEBHOOK_URL = os.environ["WEBHOOK_URL"].strip()

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"].strip()

# Render inject karta hai PORT khud — default 7860 sirf local ke liye.
PORT = int(os.environ.get("PORT", "7860"))

# Optional — sirf tab set karo jab Render outbound api.telegram.org block kare.
# Agar unset hai, bot seedha api.telegram.org se baat karta hai (preferred).
# Agar set hai, har outbound call is Worker URL se route hoga.
TELEGRAM_API_BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "").strip()

db = Database(DATABASE_URL)

# ── In-memory owner state machine ────────────────────────────────────────────
upload_session: list | None = None
pending_links: list | None = None
selected_folder_id: int | None = None
awaiting_new_folder_name: bool = False
awaiting_channel_id_for_folder: int | None = None

# Force-join add flow: "id" step waits for channel_id, "link" step waits
# for the invite link for the channel_id captured in the previous step.
awaiting_force_join_step: str | None = None   # None | "id" | "link"
force_join_pending_channel_id: str | None = None
force_join_pending_title: str | None = None

# Force-join edit flow: waits for a replacement invite link for an
# already-existing channel_id (set when owner taps "✏️ Edit Link").
awaiting_force_join_edit_channel_id: str | None = None

# Broadcast flow: owner's fallback text (no other active state) is held here
# until they confirm via inline button — NOT sent immediately, so a stray
# typo with no active session can't blast every user.
pending_broadcast_text: str | None = None


def _reset_owner_state():
    global upload_session, pending_links, selected_folder_id
    global awaiting_new_folder_name, awaiting_channel_id_for_folder
    global awaiting_force_join_step, force_join_pending_channel_id, force_join_pending_title
    global awaiting_force_join_edit_channel_id
    global pending_broadcast_text
    upload_session = None
    pending_links = None
    selected_folder_id = None
    awaiting_new_folder_name = False
    awaiting_channel_id_for_folder = None
    awaiting_force_join_step = None
    force_join_pending_channel_id = None
    force_join_pending_title = None
    awaiting_force_join_edit_channel_id = None
    pending_broadcast_text = None


# ── /folders ──────────────────────────────────────────────────────────────────
async def cmd_folders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await _show_folder_management(update, ctx)


async def _show_folder_management(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    folders = await db.fetch("SELECT id, name, channel_id FROM folders ORDER BY name")

    rows = []
    for f in folders:
        label = f["name"] if f["channel_id"] else f"{f['name']} (⚠️ no channel)"
        rows.append([InlineKeyboardButton(label, callback_data=f"folder_manage_{f['id']}")])
    rows.append([InlineKeyboardButton("➕ New Folder", callback_data="folder_new")])

    text = "📁 *Folders*\n\nManage karne ke liye tap karein, ya naya banayein:" if folders \
        else "📁 Koi folder nahi hai abhi.\n\n➕ New Folder se shuru karein."

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
        )


async def cb_folder_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    _reset_owner_state()
    global awaiting_new_folder_name
    awaiting_new_folder_name = True
    await update.callback_query.edit_message_text("📁 Naye folder ka naam bhejein:")


async def cb_folder_manage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    folder_id = int(update.callback_query.data.replace("folder_manage_", ""))
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder:
        await update.callback_query.edit_message_text("❌ Folder nahi mila.")
        return

    channel_line = folder["channel_id"] or "⚠️ set nahi hai"
    text = f"📁 *{folder['name']}*\n\nChannel ID: `{channel_line}`"
    rows = [
        [InlineKeyboardButton("✏️ Update Channel ID", callback_data=f"folder_setchannel_{folder_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="folder_list")],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )


async def cb_folder_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await _show_folder_management(update, ctx)


async def cb_folder_setchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    folder_id = int(update.callback_query.data.replace("folder_setchannel_", ""))
    _reset_owner_state()
    global awaiting_channel_id_for_folder
    awaiting_channel_id_for_folder = folder_id
    await update.callback_query.edit_message_text(
        "📡 Channel ID bhejein (e.g. @channelusername ya -100xxxxxxxxxx).\n\n"
        "⚠️ Bot ko us channel mein admin banana zaroori hai (Post Messages permission ke saath)."
    )


# ── Force-join ────────────────────────────────────────────────────────────────
async def _has_join_request(channel_id: str, user_id: int) -> bool:
    row = await db.fetchrow(
        "SELECT 1 FROM join_requests WHERE channel_id = $1 AND user_id = $2",
        channel_id, str(user_id)
    )
    return row is not None


async def _is_member(bot, channel_id: str, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        if member.status not in ("left", "kicked"):
            return True
    except Exception as e:
        # get_chat_member errors (bot lost admin, channel deleted, bad
        # stored id, or user not found because they only have a pending
        # join request) — fall through to the join_requests check below
        # instead of failing closed outright.
        logger.warning(f"get_chat_member failed for channel {channel_id}, user {user_id}: {e}")

    # Not (yet) an approved member. Auto-approve is removed — the bot no
    # longer approves join requests itself. Instead, a recorded join
    # request (sent, whether or not the owner has approved it) is enough
    # to pass the gate. NOTE: this means the gate can be satisfied just by
    # clicking "Request to Join" without ever actually being let into the
    # channel — weaker than a real membership check, by design per request.
    return await _has_join_request(channel_id, user_id)


async def _check_force_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE, batch_id: int | None) -> bool:
    """Returns True if the user may proceed. Otherwise sends a join prompt
    (with per-channel join buttons + a recheck button) and returns False.
    Owner always bypasses."""
    user = update.effective_user
    if user.id == OWNER_ID:
        return True

    channels = await db.fetch(
        "SELECT id, channel_id, invite_link, title FROM force_join_channels ORDER BY id"
    )
    if not channels:
        return True

    not_joined = [c for c in channels if not await _is_member(ctx.bot, c["channel_id"], user.id)]
    if not not_joined:
        return True

    rows = [
        [InlineKeyboardButton(f"📢 Join {c['title'] or 'Channel'}", url=c["invite_link"])]
        for c in not_joined
    ]
    recheck_data = f"checkjoin_{batch_id}" if batch_id is not None else "checkjoin_0"
    rows.append([InlineKeyboardButton("✅ Maine Join Kar Liya", callback_data=recheck_data)])

    text = (
        "🔒 Is bot ko use karne se pehle neeche diye gaye channel(s)/group(s) "
        "join karna zaroori hai.\n\n"
        "⚠️ Agar channel private hai to join request bhejni hogi (approve hone ka "
        "wait karne ki zaroorat nahi — request bhejte hi \"Maine Join Kar Liya\" "
        "dobara dabayein)."
    )
    if update.callback_query:
        await update.callback_query.answer("Abhi tak sabhi channels join nahi hue.", show_alert=True)
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    return False


async def cb_checkjoin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data.replace("checkjoin_", "")
    batch_id = int(data) if data != "0" else None

    ok = await _check_force_join(update, ctx, batch_id)
    if not ok:
        return

    await update.callback_query.answer("✅ Verified!")
    if batch_id is not None:
        await _deliver_batch(batch_id, update.effective_chat.id, update.effective_user.id, ctx)
    else:
        await update.callback_query.message.reply_text("✅ Verify ho gaya. /start dobara bhejein.")


async def cmd_forcejoin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await _show_force_join_management(update, ctx)


async def _show_force_join_management(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    channels = await db.fetch("SELECT id, title, channel_id FROM force_join_channels ORDER BY id")
    rows = [
        [
            InlineKeyboardButton(f"❌ {c['title'] or c['channel_id']}", callback_data=f"forcejoin_remove_{c['id']}"),
            InlineKeyboardButton("✏️ Edit Link", callback_data=f"forcejoin_editlink_{c['id']}"),
        ]
        for c in channels
    ]
    rows.append([InlineKeyboardButton("➕ Add Channel/Group", callback_data="forcejoin_add")])

    text = "🔒 *Force Join Channels*\n\nRemove karne ke liye tap karein, ya naya add karein:" if channels \
        else "🔒 Koi force-join channel set nahi hai.\n\n➕ Add Channel/Group se shuru karein."

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
        )


async def cb_forcejoin_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await _show_force_join_management(update, ctx)


async def cb_forcejoin_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    _reset_owner_state()
    global awaiting_force_join_step
    awaiting_force_join_step = "id"
    await update.callback_query.edit_message_text(
        "📡 Channel/Group ki ID ya @username bhejein (e.g. @channelusername ya -100xxxxxxxxxx).\n\n"
        "⚠️ Bot ko wahan admin banana zaroori hai (members dekhne ke liye, aur join "
        "requests receive karne ke liye — bot unhe approve NAHI karega, sirf record karega)."
    )


async def cb_forcejoin_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    row_id = int(update.callback_query.data.replace("forcejoin_remove_", ""))
    await db.execute("DELETE FROM force_join_channels WHERE id = $1", row_id)
    await _show_force_join_management(update, ctx)


async def cb_forcejoin_editlink(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    _reset_owner_state()
    global awaiting_force_join_edit_channel_id
    row_id = int(update.callback_query.data.replace("forcejoin_editlink_", ""))
    row = await db.fetchrow(
        "SELECT id, channel_id, title FROM force_join_channels WHERE id = $1", row_id
    )
    if not row:
        await update.callback_query.answer("⚠️ Channel nahi mila (shayad already remove ho chuka hai).", show_alert=True)
        await _show_force_join_management(update, ctx)
        return
    awaiting_force_join_edit_channel_id = row["channel_id"]
    await update.callback_query.edit_message_text(
        f"🔗 \"{row['title'] or row['channel_id']}\" ke liye naya invite link bhejein.\n\n"
        "⚠️ Agar link expire ho raha hai ya 'invalid' dikha raha hai, Telegram mein naya link "
        "banate waqt expiry date aur member limit dono OFF/blank rakhein — warna ye dobara "
        "kuch time/uses ke baad invalid ho jayega."
    )


async def cb_chat_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Records join requests for any channel/group registered under
    /forcejoin. Does NOT approve them — approval is left to the owner
    (manually, in Telegram). The force-join gate treats "request sent"
    as sufficient to proceed; see _is_member/_has_join_request."""
    req = update.chat_join_request
    chat_id_str = str(req.chat.id)
    row = await db.fetchrow(
        "SELECT id FROM force_join_channels WHERE channel_id = $1", chat_id_str
    )
    if not row:
        return
    try:
        await db.execute(
            """INSERT INTO join_requests (channel_id, user_id)
               VALUES ($1, $2)
               ON CONFLICT (channel_id, user_id) DO NOTHING""",
            chat_id_str, str(req.from_user.id)
        )
    except Exception as e:
        logger.warning(f"Failed to record join request for chat {chat_id_str}, user {req.from_user.id}: {e}")


# ── /startupload ──────────────────────────────────────────────────────────────
async def cmd_startupload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    folders = await db.fetch("SELECT id, name, channel_id FROM folders ORDER BY name")
    if not folders:
        await update.message.reply_text(
            "❌ Koi folder nahi hai. Pehle /folders se ek folder banayein."
        )
        return

    _reset_owner_state()
    rows = [
        [InlineKeyboardButton(f["name"], callback_data=f"upload_folder_{f['id']}")]
        for f in folders
    ]
    await update.message.reply_text(
        "📁 Kis folder mein upload karna hai?",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_upload_folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    folder_id = int(update.callback_query.data.replace("upload_folder_", ""))
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder:
        await update.callback_query.edit_message_text("❌ Folder nahi mila.")
        return
    if not folder["channel_id"]:
        await update.callback_query.edit_message_text(
            f"⚠️ \"{folder['name']}\" ka channel_id set nahi hai.\n"
            f"/folders se set karein, phir /startupload phir se try karein."
        )
        return

    global upload_session, selected_folder_id
    upload_session = []
    selected_folder_id = folder_id

    await update.callback_query.edit_message_text(
        f"✅ Folder: *{folder['name']}*\n\nGoogle Drive links bhejiye.\n/done likhein jab sab links bhej dein.",
        parse_mode="Markdown"
    )


async def _repost_all_pages_for_folder(folder_id, folder_name, new_channel_id, update, ctx):
    batches = await db.fetch(
        "SELECT id, total_links FROM batches WHERE folder_id = $1 ORDER BY id",
        folder_id
    )
    if not batches:
        await update.message.reply_text("ℹ️ Is folder mein abhi koi batch nahi hai — repost karne ko kuch nahi.")
        return

    total_pages = (len(batches) + PAGE_SIZE - 1) // PAGE_SIZE
    await update.message.reply_text(
        f"🔁 {total_pages} message(s) naye channel mein repost ho rahe hain... isme time lagega."
    )

    REPOST_DELAY = 2
    success_count = 0
    failed_pages = []

    for page_index in range(1, total_pages + 1):
        try:
            # Naya channel = purana message_id wahan invalid hai, isliye
            # force_new=True taaki edit try na ho, seedha naya message bhejein.
            await render_folder_page(
                folder_id, folder_name, new_channel_id, page_index, ctx, force_new=True
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Repost failed for folder {folder_id} page {page_index}: {e}")
            failed_pages.append(page_index)

        await asyncio.sleep(REPOST_DELAY)

    summary = f"✅ {success_count}/{total_pages} messages repost ho gaye naye channel mein."
    if failed_pages:
        summary += f"\n⚠️ Fail hue: page #{', #'.join(str(i) for i in failed_pages)}"
    await update.message.reply_text(summary)


# ── Text message handler ──────────────────────────────────────────────────────
async def handle_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    text = (update.message.text or "").strip()

    global awaiting_new_folder_name
    if awaiting_new_folder_name:
        if not text:
            await update.message.reply_text("⚠️ Folder ka naam khali nahi ho sakta.")
            return
        awaiting_new_folder_name = False
        try:
            folder_id = await db.fetchval(
                "INSERT INTO folders (name) VALUES ($1) RETURNING id", text
            )
        except Exception:
            await update.message.reply_text(
                f"⚠️ \"{text}\" naam ka folder already exist karta hai. Phir se /folders try karein."
            )
            return

        global awaiting_channel_id_for_folder
        awaiting_channel_id_for_folder = folder_id
        await update.message.reply_text(
            f"✅ Folder \"{text}\" ban gaya.\n\n"
            f"📡 Ab is folder ka Channel ID bhejein (e.g. @channelusername ya -100xxxxxxxxxx).\n\n"
            f"⚠️ Bot ko us channel mein admin banana zaroori hai (Post Messages permission ke saath)."
        )
        return

    global awaiting_force_join_edit_channel_id
    if awaiting_force_join_edit_channel_id is not None:
        if not text:
            await update.message.reply_text("⚠️ Invite link khali nahi ho sakta.")
            return
        await db.execute(
            "UPDATE force_join_channels SET invite_link = $1 WHERE channel_id = $2",
            text, awaiting_force_join_edit_channel_id
        )
        awaiting_force_join_edit_channel_id = None
        await update.message.reply_text("✅ Invite link update ho gaya.")
        return

    global awaiting_force_join_step, force_join_pending_channel_id, force_join_pending_title
    if awaiting_force_join_step == "id":
        if not text:
            await update.message.reply_text("⚠️ Channel/Group ID khali nahi ho sakta.")
            return
        try:
            chat = await ctx.bot.get_chat(text)

            # Sirf channel aur groups allow karo
            if chat.type not in ("channel", "supergroup", "group"):
                await update.message.reply_text(
                    "❌ Sirf channels aur groups add kiye ja sakte hain."
                )
                return

            # Bot ki actual ID lo
            me = await ctx.bot.get_me()

            member = await ctx.bot.get_chat_member(
                chat_id=chat.id,
                user_id=me.id
            )

            if member.status not in ("administrator", "creator"):
                raise ValueError("bot admin nahi hai")

        except Exception as e:
            logger.exception("Force-join verification failed")

            awaiting_force_join_step = None

            await update.message.reply_text(
                f"❌ Verify fail hua:\n\n{e}\n\n"
                "Check karein:\n"
                "• ID sahi hai\n"
                "• Bot admin hai\n"
                "• Group/Channel accessible hai"
            )
            return

        existing = await db.fetchrow(
            "SELECT id FROM force_join_channels WHERE channel_id = $1", str(chat.id)
        )
        if existing:
            awaiting_force_join_step = None
            await update.message.reply_text("⚠️ Ye channel/group already force-join list mein hai.")
            return

        force_join_pending_channel_id = str(chat.id)
        force_join_pending_title = chat.title or chat.username or text
        awaiting_force_join_step = "link"
        await update.message.reply_text(
            f"✅ \"{force_join_pending_title}\" verify ho gaya.\n\n"
            f"🔗 Ab iska invite link bhejein — public channel ho to https://t.me/username "
            f"bhi chalega, private ho to bot's export/create karke bheja hua link.\n\n"
            f"ℹ️ Agar approval-required (join request) link chahiye, wo link Telegram mein "
            f"khud generate karke yahan paste karein — bot ka approval-required link "
            f"khud nahi banata."
        )
        return

    if awaiting_force_join_step == "link":
        if not text:
            await update.message.reply_text("⚠️ Invite link khali nahi ho sakta.")
            return
        await db.execute(
            "INSERT INTO force_join_channels (channel_id, invite_link, title) VALUES ($1, $2, $3)",
            force_join_pending_channel_id, text, force_join_pending_title
        )
        title_done = force_join_pending_title
        awaiting_force_join_step = None
        force_join_pending_channel_id = None
        force_join_pending_title = None
        await update.message.reply_text(f"✅ \"{title_done}\" force-join list mein add ho gaya.")
        return

    if awaiting_channel_id_for_folder is not None:
        if not text:
            await update.message.reply_text("⚠️ Channel ID khali nahi ho sakta.")
            return
        folder_id = awaiting_channel_id_for_folder

        folder_before = await db.fetchrow("SELECT channel_id FROM folders WHERE id = $1", folder_id)
        had_previous_channel = bool(folder_before and folder_before["channel_id"])

        try:
            await ctx.bot.send_message(chat_id=text, text="✅ Channel linked successfully.")
            await db.execute("UPDATE folders SET channel_id = $1 WHERE id = $2", text, folder_id)
            awaiting_channel_id_for_folder = None
            await update.message.reply_text("✅ Channel ID save ho gaya aur verify ho gaya.")
        except Exception as e:
            awaiting_channel_id_for_folder = None
            logger.error(f"Channel verify failed for folder {folder_id}: {e}")
            await update.message.reply_text(
                f"❌ Channel ID save nahi hua — bot wahan post nahi kar saka.\n"
                f"Check karein: (1) ID sahi hai (2) bot us channel mein admin hai (3) Post Messages permission ON hai.\n\n"
                f"Phir se try karein /folders se."
            )
            return

        if had_previous_channel:
            folder_row = await db.fetchrow("SELECT name FROM folders WHERE id = $1", folder_id)
            await _repost_all_pages_for_folder(folder_id, folder_row["name"], text, update, ctx)
        return

    global pending_links
    if pending_links is not None:
        if not text:
            await update.message.reply_text("⚠️ Batch ka naam khali nahi ho sakta. Phir se bhejein.")
            return
        links = pending_links
        pending_links = None
        await update.message.reply_text(f"⏳ \"{text}\" — {len(links)} links process ho rahe hain...")
        await process_links(links, text, selected_folder_id, update, ctx)
        return

    if upload_session is None:
        if not text:
            await update.message.reply_text("⚠️ Broadcast ke liye text message bhejein.")
            return

        global pending_broadcast_text
        pending_broadcast_text = text
        recipient_count = await db.fetchval(
            "SELECT COUNT(*) FROM users WHERE user_id != $1", str(OWNER_ID)
        )

        if not recipient_count:
            pending_broadcast_text = None
            await update.message.reply_text("ℹ️ Koi user nahi hai jinhe broadcast kiya ja sake.")
            return

        rows = [[
            InlineKeyboardButton("✅ Confirm Broadcast", callback_data="broadcast_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel"),
        ]]
        await update.message.reply_text(
            f"📢 Ye message *{recipient_count} user(s)* ko bhejna hai?\n\n"
            f"—\n{text}\n—\n\n"
            f"⚠️ Ye action undo nahi ho sakta.",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown"
        )
        return

    links = re.findall(r'https://drive\.google\.com/\S+', update.message.text or "")
    if not links:
        await update.message.reply_text("⚠️ Koi valid Google Drive link nahi mila.")
        return

    upload_session.extend(links)
    await update.message.reply_text(
        f"✅ {len(links)} link(s) add hue. Total: {len(upload_session)}"
    )


BROADCAST_MODE = False


async def broadcast(update, context):
    global BROADCAST_MODE

    if update.effective_user.id != OWNER_ID:
        return

    BROADCAST_MODE = True
    await update.message.reply_text(
        "📢 Broadcast mode ON.\n\n"
        "Ab text, photo, video, audio, document, voice, sticker ya animation bhejiye."
    )


async def handle_broadcast(update, context):
    global BROADCAST_MODE

    if not BROADCAST_MODE:
        return

    users = await db.fetch("SELECT DISTINCT user_id FROM sent_logs")

    success = 0
    failed = 0

    for user in users:
        uid = int(user["user_id"])

        try:
            if update.message.text:
                await context.bot.send_message(uid, update.message.text)

            elif update.message.photo:
                await context.bot.send_photo(
                    uid,
                    update.message.photo[-1].file_id,
                    caption=update.message.caption
                )

            elif update.message.video:
                await context.bot.send_video(
                    uid,
                    update.message.video.file_id,
                    caption=update.message.caption
                )

            elif update.message.audio:
                await context.bot.send_audio(
                    uid,
                    update.message.audio.file_id,
                    caption=update.message.caption
                )

            elif update.message.document:
                await context.bot.send_document(
                    uid,
                    update.message.document.file_id,
                    caption=update.message.caption
                )

            elif update.message.voice:
                await context.bot.send_voice(
                    uid,
                    update.message.voice.file_id,
                    caption=update.message.caption
                )

            elif update.message.animation:
                await context.bot.send_animation(
                    uid,
                    update.message.animation.file_id,
                    caption=update.message.caption
                )

            elif update.message.sticker:
                await context.bot.send_sticker(
                    uid,
                    update.message.sticker.file_id
                )

            success += 1

        except:
            failed += 1

    BROADCAST_MODE = False

    await update.message.reply_text(
        f"✅ Broadcast complete.\n"
        f"Success: {success}\n"
        f"Failed: {failed}"
    )




# ── /done ─────────────────────────────────────────────────────────────────────
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    global upload_session, pending_links
    if not upload_session:
        await update.message.reply_text("❌ Koi link nahi. Pehle /startupload karein.")
        upload_session = None
        return

    pending_links = list(upload_session)
    upload_session = None

    await update.message.reply_text(
        f"📝 {len(pending_links)} links mil gaye.\n\nIs batch ka naam bhejein:"
    )


async def process_links(links, name, folder_id, update, ctx):
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder or not folder["channel_id"]:
        await update.message.reply_text("❌ Folder ya channel_id missing — upload cancel hua.")
        return
    channel_id = folder["channel_id"]

    remaining = list(links)

    existing = await db.fetchrow(
        "SELECT id, total_links, name FROM batches "
        "WHERE folder_id = $1 AND total_links < $2 ORDER BY created_at DESC LIMIT 1",
        folder_id, BATCH_MAX
    )

    if existing:
        spots = BATCH_MAX - existing["total_links"]
        to_fill = remaining[:spots]
        remaining = remaining[spots:]
        new_total = existing["total_links"] + len(to_fill)

        batch_name = existing["name"] or name
        if not existing["name"]:
            await db.execute("UPDATE batches SET name = $1 WHERE id = $2", batch_name, existing["id"])

        for link in to_fill:
            await db.execute(
                "INSERT INTO audios (batch_id, drive_link) VALUES ($1, $2)",
                existing["id"], link
            )
        await db.execute(
            "UPDATE batches SET total_links = total_links + $1 WHERE id = $2",
            len(to_fill), existing["id"]
        )

        try:
            page_index = await _page_index_for_batch(folder_id, existing["id"])
            await render_folder_page(folder_id, folder["name"], channel_id, page_index, ctx)
            note = "Channel post update hua."
        except Exception as e:
            logger.error(f"Channel page render failed for folder {folder_id}: {e}")
            note = "⚠️ Channel post update nahi ho saka — audios DB mein save ho gaye hain, /folders se channel check karein."

        await update.message.reply_text(
            f"📥 Batch #{existing['id']} mein {len(to_fill)} audios fill hue (ab total {new_total}).\n{note}"
        )

    chunks = [remaining[i:i + BATCH_MAX] for i in range(0, len(remaining), BATCH_MAX)]
    multi = len(chunks) > 1

    for idx, chunk in enumerate(chunks, start=1):
        chunk_name = f"{name} (Part {idx})" if multi else name

        batch_id = await db.fetchval(
            "INSERT INTO batches (folder_id, total_links, name) VALUES ($1, $2, $3) RETURNING id",
            folder_id, len(chunk), chunk_name
        )
        for link in chunk:
            await db.execute(
                "INSERT INTO audios (batch_id, drive_link) VALUES ($1, $2)",
                batch_id, link
            )

        try:
            page_index = await _page_index_for_batch(folder_id, batch_id)
            await render_folder_page(folder_id, folder["name"], channel_id, page_index, ctx)
            note = f"\"{folder['name']}\" channel par post update ho gaya."
        except Exception as e:
            logger.error(f"Channel page render failed for folder {folder_id}: {e}")
            note = "⚠️ Channel post update nahi ho saka — audios DB mein save ho gaye hain, /folders se channel check karein."

        await update.message.reply_text(
            f"✅ Batch #{batch_id} \"{chunk_name}\" create hua ({len(chunk)} audios). {note}"
        )

    await update.message.reply_text("🎉 Upload complete!")


PAGE_SIZE = 20  # ek channel message mein max itne inline buttons


async def _page_index_for_batch(folder_id: int, batch_id: int) -> int:
    """Folder ke andar is batch ki 1-based position se page number nikalta hai
    (batches purane se naye order mein, id ke hisaab se)."""
    position = await db.fetchval(
        "SELECT COUNT(*) FROM batches WHERE folder_id = $1 AND id <= $2",
        folder_id, batch_id
    )
    return ((position - 1) // PAGE_SIZE) + 1


def _page_text(folder_name: str, page_index: int, total_pages: int, total_in_page: int) -> str:
    display_name = folder_name or "Audio Collection"
    part_suffix = f" (Part {page_index})" if total_pages > 1 else ""
    return (
        f"🎵 {display_name}{part_suffix}\n"
        f"📦 Total Audios: {total_in_page}\n"
        f"👇 Niche button par click karke bot se audio prapt karein."
    )


def _page_buttons(batches_in_page: list, start_offset: int) -> InlineKeyboardMarkup:
    rows = []
    running = start_offset
    for b in batches_in_page:
        end = running + b["total_links"] - 1
        label = f"{running}-{end}" if b["total_links"] > 1 else str(running)
        rows.append([InlineKeyboardButton(label, url=f"https://t.me/{BOT_USERNAME}?start=batch_{b['id']}")])
        running = end + 1
    return InlineKeyboardMarkup(rows)


async def render_folder_page(folder_id: int, folder_name: str, channel_id: str, page_index: int, ctx,
                              force_new: bool = False) -> None:
    """Folder ke ek page (max 20 batches/buttons) ka channel message
    (re)build karta hai. Agar page pehle se maujood hai to edit karta hai,
    warna naya message bhejta hai. force_new=True (channel switch ke waqt)
    mein hamesha naya message bhejta hai, purane channel ke message_id ko
    edit karne ki koshish nahi karta."""
    all_batches = await db.fetch(
        "SELECT id, total_links FROM batches WHERE folder_id = $1 ORDER BY id",
        folder_id
    )
    total_pages = (len(all_batches) + PAGE_SIZE - 1) // PAGE_SIZE
    if total_pages == 0 or page_index > total_pages:
        return

    start_slice = (page_index - 1) * PAGE_SIZE
    end_slice = start_slice + PAGE_SIZE
    batches_in_page = all_batches[start_slice:end_slice]
    start_offset = sum(b["total_links"] for b in all_batches[:start_slice]) + 1
    total_in_page = sum(b["total_links"] for b in batches_in_page)

    text = _page_text(folder_name, page_index, total_pages, total_in_page)
    markup = _page_buttons(batches_in_page, start_offset)

    page_row = await db.fetchrow(
        "SELECT channel_message_id FROM folder_pages WHERE folder_id = $1 AND page_index = $2",
        folder_id, page_index
    )

    edited = False
    if page_row and page_row["channel_message_id"] and not force_new:
        try:
            await ctx.bot.edit_message_text(
                chat_id=channel_id,
                message_id=int(page_row["channel_message_id"]),
                text=text,
                reply_markup=markup
            )
            edited = True
        except Exception as e:
            logger.warning(f"Edit failed for folder {folder_id} page {page_index}, sending new: {e}")

    if not edited:
        msg = await ctx.bot.send_message(chat_id=channel_id, text=text, reply_markup=markup)
        await db.execute(
            """
            INSERT INTO folder_pages (folder_id, page_index, channel_message_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (folder_id, page_index)
            DO UPDATE SET channel_message_id = EXCLUDED.channel_message_id
            """,
            folder_id, page_index, str(msg.message_id)
        )


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args

    await db.execute(
        """INSERT INTO users (user_id) VALUES ($1)
           ON CONFLICT (user_id) DO UPDATE SET last_seen = NOW()""",
        str(update.effective_user.id)
    )

    if update.effective_user.id == OWNER_ID:
        if not args or not args[0].startswith("batch_"):
            await update.message.reply_text(
                "👑 *Owner Panel*\n\n"
                "Commands:\n"
                "/folders — folders manage karein (create/update channel)\n"
                "/startupload — links upload karna shuru karein\n"
                "/done — upload finish karein\n"
                "/forcejoin — force-join channels/groups manage karein\n\n"
                "Abhi active session nahi hai.",
                parse_mode="Markdown"
            )
            return

    if not args or not args[0].startswith("batch_"):
        await update.message.reply_text("👋 Is bot ko channel ke through use karein.")
        return

    batch_id = int(args[0].replace("batch_", ""))

    if not await _check_force_join(update, ctx, batch_id):
        return

    await _deliver_batch(batch_id, update.effective_chat.id, update.effective_user.id, ctx)


async def _deliver_batch(batch_id: int, chat_id: int, user_id_int: int, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(user_id_int)

    batch = await db.fetchrow("SELECT id, total_links FROM batches WHERE id = $1", batch_id)
    if not batch:
        await ctx.bot.send_message(chat_id=chat_id, text="❌ Yeh collection exist nahi karta.")
        return

    warn = await ctx.bot.send_message(
        chat_id=chat_id,
        text=(
            f"⚠️ *Warning*\n\n"
            f"Ye {batch['total_links']} audio files *{DELETE_MINUTES} minute* baad delete ho jayenge.\n\n"
            f"Abhi forward karein dusri jagah!\n\nBhej rahe hain... 📤"
        ),
        parse_mode="Markdown"
    )

    audios = await db.fetch(
        "SELECT id, drive_link, telegram_file_id FROM audios WHERE batch_id = $1 ORDER BY id",
        batch_id
    )
    audios = sorted(audios, key=lambda a: a["telegram_file_id"] is None)

    has_uncached = any(a["telegram_file_id"] is None for a in audios)
    sent_ids = [warn.message_id]

    if has_uncached and LIVE_DOWNLOAD_AVAILABLE:
        delay_notice = await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Kuch audios pehli baar download ho rahe hain, isme *2 minute tak* lag sakte hain. "
                "Cached audios turant aa jayenge."
            ),
            parse_mode="Markdown"
        )
        sent_ids.append(delay_notice.message_id)

    failed_audios = []
    uncached_missing = []
    MAX_ATTEMPTS = 3
    RETRY_DELAY = 3

    for audio in audios:
        msg = None
        file_bytes = None
        filename = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if audio["telegram_file_id"]:
                    try:
                        msg = await ctx.bot.send_audio(chat_id=chat_id, audio=audio["telegram_file_id"])
                    except Exception as e:
                        logger.error(
                            f"Audio {audio['id']} attempt {attempt}/{MAX_ATTEMPTS} "
                            f"— CACHED SEND failed, clearing bad telegram_file_id: {e}"
                        )
                        await db.execute(
                            "UPDATE audios SET telegram_file_id = NULL WHERE id = $1", audio["id"]
                        )
                        audio = dict(audio)
                        audio["telegram_file_id"] = None
                        if attempt < MAX_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY)
                        continue
                elif LIVE_DOWNLOAD_AVAILABLE:
                    if file_bytes is None:
                        try:
                            file_bytes, filename = await download_from_drive(audio["drive_link"])
                        except ValueError as e:
                            # Permanent problem (bad/private/malformed link) — retrying won't
                            # fix a broken URL, so fail this audio immediately.
                            logger.error(
                                f"Audio {audio['id']} — BAD DRIVE LINK, skipping retries: {e}"
                            )
                            msg = None
                            break
                        except Exception as e:
                            logger.error(
                                f"Audio {audio['id']} attempt {attempt}/{MAX_ATTEMPTS} "
                                f"— DRIVE DOWNLOAD failed: {e}"
                            )
                            if attempt < MAX_ATTEMPTS:
                                await asyncio.sleep(RETRY_DELAY)
                            continue
                    try:
                        msg = await ctx.bot.send_audio(chat_id=chat_id, audio=file_bytes, filename=filename)
                    except Exception as e:
                        logger.error(
                            f"Audio {audio['id']} attempt {attempt}/{MAX_ATTEMPTS} "
                            f"— TELEGRAM UPLOAD failed: {e}"
                        )
                        if attempt < MAX_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAY)
                        continue
                    if msg.audio:
                        await db.execute(
                            "UPDATE audios SET telegram_file_id = $1 WHERE id = $2",
                            msg.audio.file_id, audio["id"]
                        )
                else:
                    # local_sync.py deploy mein nahi hai — live download disabled.
                    logger.warning(
                        f"Audio {audio['id']} has no telegram_file_id and "
                        f"local_sync.py missing — skipping live download."
                    )
                    msg = None
                break
            except Exception as e:
                logger.error(
                    f"Audio {audio['id']} attempt {attempt}/{MAX_ATTEMPTS} "
                    f"— UNEXPECTED failure: {e}"
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)

        if msg is not None:
            sent_ids.append(msg.message_id)
        else:
            failed_audios.append(audio["id"])
            if not LIVE_DOWNLOAD_AVAILABLE and audio["telegram_file_id"] is None:
                uncached_missing.append(audio["id"])

    if uncached_missing:
        await ctx.bot.send_message(chat_id=chat_id, text="⚠️ Ye audio abhi available nahi hai.")

    other_failures = len(failed_audios) - len(uncached_missing)
    if other_failures > 0:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ {other_failures}/{len(audios)} audio bhejne mein fail hue "
                f"({MAX_ATTEMPTS} attempts ke baad bhi). Phir se /start try karein."
            )
        )

    sent_count = len(audios) - len(failed_audios)
    if sent_count > 0:
        closing = await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ {sent_count} audio files bhej diye gaye.\n\n"
                f"⏳ Ye *{DELETE_MINUTES} minute* mein delete ho jayenge — abhi forward kar lein!"
            ),
            parse_mode="Markdown"
        )
        sent_ids.append(closing.message_id)

    delete_at = datetime.utcnow() + timedelta(minutes=DELETE_MINUTES)
    await db.execute(
        "INSERT INTO sent_logs (user_id, batch_id, message_ids, delete_at) VALUES ($1,$2,$3,$4)",
        user_id, batch_id, json.dumps(sent_ids), delete_at
    )


async def cb_broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    global pending_broadcast_text
    pending_broadcast_text = None
    await update.callback_query.edit_message_text("❌ Broadcast cancel ho gaya.")


async def cb_broadcast_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    global pending_broadcast_text
    text = pending_broadcast_text
    pending_broadcast_text = None

    if not text:
        await update.callback_query.edit_message_text("⚠️ Broadcast text mil nahi raha — phir se try karein.")
        return

    await update.callback_query.edit_message_text("⏳ Broadcast bheja ja raha hai...")

    rows = await db.fetch("SELECT user_id FROM users WHERE user_id != $1", str(OWNER_ID))
    sent, failed, blocked = 0, 0, 0

    for row in rows:
        uid = row["user_id"]
        try:
            await ctx.bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except Forbidden:
            # User blocked the bot or deleted their account — remove them
            # so future broadcasts don't keep retrying a dead recipient.
            blocked += 1
            await db.execute("DELETE FROM users WHERE user_id = $1", uid)
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast to {uid} failed: {e}")

    await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"✅ Broadcast done.\n\n"
            f"Sent: {sent}\n"
            f"Blocked/removed: {blocked}\n"
            f"Other failures: {failed}"
        )
    )


# ── Auto-delete job ───────────────────────────────────────────────────────────
async def auto_delete_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    rows = await db.fetch(
        "SELECT id, user_id, message_ids FROM sent_logs WHERE delete_at <= $1", now
    )
    for row in rows:
        for msg_id in json.loads(row["message_ids"]):
            try:
                await ctx.bot.delete_message(chat_id=int(row["user_id"]), message_id=msg_id)
            except Exception:
                pass
        await db.execute("DELETE FROM sent_logs WHERE id = $1", row["id"])


# ── Main ──────────────────────────────────────────────────────────────────────
async def _setup_bot_commands(application: Application):
    """Public users only ever see /start — everything else here is
    owner-gated in the handlers themselves (see OWNER_ID checks above), so
    showing them in the global menu would just be dead buttons for regular
    users. Owner gets the full admin menu via a chat-scoped command list,
    which overrides the default scope only inside OWNER_ID's own chat."""
    public_commands = [
        BotCommand("start", "Bot shuru karein"),
    ]
    owner_commands = public_commands + [
        BotCommand("folders", "Folders manage karein"),
        BotCommand("startupload", "Naya batch upload shuru karein"),
        BotCommand("done", "Current upload batch finish karein"),
        BotCommand("forcejoin", "Force-join channels manage karein"),
        BotCommand("broadcast", "broadcast msg fro "),
    ]

    try:
        await application.bot.set_my_commands(
            public_commands, scope=BotCommandScopeDefault()
        )
        await application.bot.set_my_commands(
            owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID)
        )
        logger.info("Bot command menus registered (default + owner scope).")
    except Exception as e:
        # Non-fatal — command menu is cosmetic, bot should still run.
        logger.error(f"Failed to set bot commands: {e}")


async def post_init(application: Application):
    await db.connect()
    await db.init_schema()
    logger.info("Database connected and schema ready.")

    await _setup_bot_commands(application)

    try:
        await application.bot.send_message(
            chat_id=OWNER_ID,
            text="🔄 Bot restart hua. Agar koi upload session active tha, woh reset ho gaya hai — /startupload se phir shuru karein."
        )
    except Exception as e:
        logger.error(f"Restart notice to owner failed: {e}")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {ctx.error}")


def main():
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
    )

    # TELEGRAM_API_BASE_URL sirf tab set karo jab Render outbound block kare.
    # Unset = seedha api.telegram.org (preferred, simpler).
    if TELEGRAM_API_BASE_URL:
        builder = builder.base_url(TELEGRAM_API_BASE_URL.rstrip("/") + "/bot")
        logger.info(f"Outbound routed through Worker: {TELEGRAM_API_BASE_URL}")
    else:
        logger.info("Outbound: direct to api.telegram.org")

    app = builder.build()

    app.add_handler(CommandHandler("startupload", cmd_startupload))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("folders", cmd_folders))
    app.add_handler(CommandHandler("forcejoin", cmd_forcejoin))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(CallbackQueryHandler(cb_folder_new, pattern=r"^folder_new$"))
    app.add_handler(CallbackQueryHandler(cb_folder_list, pattern=r"^folder_list$"))
    app.add_handler(CallbackQueryHandler(cb_folder_manage, pattern=r"^folder_manage_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_folder_setchannel, pattern=r"^folder_setchannel_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_upload_folder, pattern=r"^upload_folder_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_forcejoin_list, pattern=r"^forcejoin_list$"))
    app.add_handler(CallbackQueryHandler(cb_forcejoin_add, pattern=r"^forcejoin_add$"))
    app.add_handler(CallbackQueryHandler(cb_forcejoin_remove, pattern=r"^forcejoin_remove_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_forcejoin_editlink, pattern=r"^forcejoin_editlink_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_checkjoin, pattern=r"^checkjoin_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_broadcast_confirm, pattern=r"^broadcast_confirm$"))
    app.add_handler(CallbackQueryHandler(cb_broadcast_cancel, pattern=r"^broadcast_cancel$"))
    

    
    		
    app.add_handler(ChatJoinRequestHandler(cb_chat_join_request))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_handler(
        MessageHandler(
            (
                filters.PHOTO
                | filters.VIDEO
                | filters.AUDIO
                | filters.Document.ALL
                | filters.VOICE
                | filters.Sticker.ALL
                | filters.ANIMATION
            ),
            handle_broadcast,
            block=False,
        )
    )
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(auto_delete_job, interval=30, first=10)

    logger.info(f"Starting webhook server on 0.0.0.0:{PORT}, registering {WEBHOOK_URL}")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
