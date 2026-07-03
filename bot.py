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
from io import BytesIO


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

# Optional — shown as an "UPDATE CHANNEL" button on the pre-send "Please
# wait..." message. If unset, that button is simply omitted.
UPDATE_CHANNEL_URL = os.environ.get("UPDATE_CHANNEL_URL", "").strip()

UPDATE_SUPPORT_GROUP = os.environ.get("UPDATE_SUPPORT_GROUP", "").strip()

# Optional — shown to a non-owner user who /starts the bot directly (no
# batch_ payload). If unset, they get the old plain-text redirect instead.
OTHER_BOT_URL = os.environ.get("OTHER_BOT_URL", "").strip()

db = Database(DATABASE_URL)

# In-flight deliveries the user has cancelled via the "please wait" screen.
# Checked between audio sends in _deliver_batch; not a hard kill switch —
# an audio already mid-upload when cancel is pressed still finishes.
cancelled_deliveries: set[tuple[int, int]] = set()

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

    text = "📁 *Folders*\n\nTap to manage, or create a new one:" if folders \
        else "📁 No folders yet.\n\n➕ Start with New Folder."

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
    await update.callback_query.edit_message_text("📁 Send the name for the new folder:")


async def cb_folder_manage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    folder_id = int(update.callback_query.data.replace("folder_manage_", ""))
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder:
        await update.callback_query.edit_message_text("❌ Folder not found.")
        return

    channel_line = folder["channel_id"] or "⚠️ not set"
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
        "📡 Send the Channel ID (e.g. @channelusername or -100xxxxxxxxxx).\n\n"
        "⚠️ The bot must be made an admin in that channel (with Post Messages permission)."
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
        [InlineKeyboardButton(f"🔗 Join Channel {i}", url=c["invite_link"])]
        for i, c in enumerate(not_joined, start=1)
    ]
    recheck_data = f"checkjoin_{batch_id}" if batch_id is not None else "checkjoin_0"
    rows.append([InlineKeyboardButton("🔄 Try Again", callback_data=recheck_data)])

    arrows = " ".join(["⬇️"] * min(len(not_joined) * 3, 9))
    text = (
        f"❤️ HEY THERE ✨\n\n"
        f"🔥 TO USE THIS BOT, YOU MUST\n"
        f"JOIN ALL [{len(not_joined)}] CHANNELS.\n\n"
        f"👇 JOIN ALL CHANNELS AND\n"
        f"PRESS \"TRY AGAIN\".\n\n"
        f"{arrows}\n\n"
        f"⚠️ If a channel is private, you'll need to send a join request "
        f"(no need to wait for approval — as soon as you've sent the "
        f"request, press \"Try Again\")."
    )
    if update.callback_query:
        await update.callback_query.answer("You have not joined all the channels yet.", show_alert=True)
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
        await update.callback_query.message.reply_text("✅ Verified. Send /start again.")


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

    text = "🔒 *Force Join Channels*\n\nTap to remove, or add a new one:" if channels \
        else "🔒 No force-join channel set yet.\n\n➕ Start with Add Channel/Group."

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
        "📡 Send the Channel/Group ID or @username (e.g. @channelusername or -100xxxxxxxxxx).\n\n"
        "⚠️ The bot must be made an admin there (to see members, and to receive join "
        "requests — the bot will NOT approve them, only record them)."
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
        await update.callback_query.answer("⚠️ Channel not found (it may already have been removed).", show_alert=True)
        await _show_force_join_management(update, ctx)
        return
    awaiting_force_join_edit_channel_id = row["channel_id"]
    await update.callback_query.edit_message_text(
        f"🔗 Send a new invite link for \"{row['title'] or row['channel_id']}\".\n\n"
        "⚠️ If the link is expiring or showing 'invalid', keep both the expiry date "
        "and member limit OFF/blank when creating a new link in Telegram — otherwise "
        "it will go invalid again after some time/uses."
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
            "❌ No folders exist. Create one first with /folders."
        )
        return

    _reset_owner_state()
    rows = [
        [InlineKeyboardButton(f["name"], callback_data=f"upload_folder_{f['id']}")]
        for f in folders
    ]
    await update.message.reply_text(
        "📁 Which folder do you want to upload to?",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def cb_upload_folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    folder_id = int(update.callback_query.data.replace("upload_folder_", ""))
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder:
        await update.callback_query.edit_message_text("❌ Folder not found.")
        return
    if not folder["channel_id"]:
        await update.callback_query.edit_message_text(
            f"⚠️ \"{folder['name']}\" has no channel_id set.\n"
            f"Set it via /folders, then try /startupload again."
        )
        return

    global upload_session, selected_folder_id
    upload_session = []
    selected_folder_id = folder_id

    await update.callback_query.edit_message_text(
        f"✅ Folder: *{folder['name']}*\n\nSend the Google Drive links.\nType /done once you've sent all the links.",
        parse_mode="Markdown"
    )


async def _repost_all_pages_for_folder(folder_id, folder_name, new_channel_id, update, ctx):
    batches = await db.fetch(
        "SELECT id, total_links FROM batches WHERE folder_id = $1 ORDER BY id",
        folder_id
    )
    if not batches:
        await update.message.reply_text("ℹ️ This folder has no batches yet — nothing to repost.")
        return

    total_pages = (len(batches) + PAGE_SIZE - 1) // PAGE_SIZE
    await update.message.reply_text(
        f"🔁 Reposting {total_pages} message(s) to the new channel... this will take some time."
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

    summary = f"✅ {success_count}/{total_pages} messages reposted to the new channel."
    if failed_pages:
        summary += f"\n⚠️ Failed: page #{', #'.join(str(i) for i in failed_pages)}"
    await update.message.reply_text(summary)


# ── Text message handler ──────────────────────────────────────────────────────
async def handle_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    text = (update.message.text or "").strip()

    global awaiting_new_folder_name
    if awaiting_new_folder_name:
        if not text:
            await update.message.reply_text("⚠️ Folder name cannot be empty.")
            return
        awaiting_new_folder_name = False
        try:
            folder_id = await db.fetchval(
                "INSERT INTO folders (name) VALUES ($1) RETURNING id", text
            )
        except Exception:
            await update.message.reply_text(
                f"⚠️ A folder named \"{text}\" already exists. Try /folders again."
            )
            return

        global awaiting_channel_id_for_folder
        awaiting_channel_id_for_folder = folder_id
        await update.message.reply_text(
            f"✅ Folder \"{text}\" created.\n\n"
            f"📡 Now send this folder's Channel ID (e.g. @channelusername or -100xxxxxxxxxx).\n\n"
            f"⚠️ The bot must be made an admin in that channel (with Post Messages permission)."
        )
        return

    global awaiting_force_join_edit_channel_id
    if awaiting_force_join_edit_channel_id is not None:
        if not text:
            await update.message.reply_text("⚠️ Invite link cannot be empty.")
            return
        await db.execute(
            "UPDATE force_join_channels SET invite_link = $1 WHERE channel_id = $2",
            text, awaiting_force_join_edit_channel_id
        )
        awaiting_force_join_edit_channel_id = None
        await update.message.reply_text("✅ Invite link updated.")
        return

    global awaiting_force_join_step, force_join_pending_channel_id, force_join_pending_title
    if awaiting_force_join_step == "id":
        if not text:
            await update.message.reply_text("⚠️ Channel/Group ID cannot be empty.")
            return
        try:
            chat = await ctx.bot.get_chat(text)

            # Sirf channel aur groups allow karo
            if chat.type not in ("channel", "supergroup", "group"):
                await update.message.reply_text(
                    "❌ Only channels and groups can be added."
                )
                return

            # Bot ki actual ID lo
            me = await ctx.bot.get_me()

            member = await ctx.bot.get_chat_member(
                chat_id=chat.id,
                user_id=me.id
            )

            if member.status not in ("administrator", "creator"):
                raise ValueError("bot is not an admin")

        except Exception as e:
            logger.exception("Force-join verification failed")

            awaiting_force_join_step = None

            await update.message.reply_text(
                f"❌ Verification failed:\n\n{e}\n\n"
                "Please check:\n"
                "• The ID is correct\n"
                "• The bot is an admin\n"
                "• The group/channel is accessible"
            )
            return

        existing = await db.fetchrow(
            "SELECT id FROM force_join_channels WHERE channel_id = $1", str(chat.id)
        )
        if existing:
            awaiting_force_join_step = None
            await update.message.reply_text("⚠️ This channel/group is already in the force-join list.")
            return

        force_join_pending_channel_id = str(chat.id)
        force_join_pending_title = chat.title or chat.username or text
        awaiting_force_join_step = "link"
        await update.message.reply_text(
            f"✅ \"{force_join_pending_title}\" verified.\n\n"
            f"🔗 Now send its invite link — for a public channel, https://t.me/username "
            f"also works; for a private one, use a link exported/created via the bot.\n\n"
            f"ℹ️ If you need an approval-required (join request) link, generate that "
            f"link yourself in Telegram and paste it here — the bot does not create "
            f"an approval-required link on its own."
        )
        return

    if awaiting_force_join_step == "link":
        if not text:
            await update.message.reply_text("⚠️ Invite link cannot be empty.")
            return
        await db.execute(
            "INSERT INTO force_join_channels (channel_id, invite_link, title) VALUES ($1, $2, $3)",
            force_join_pending_channel_id, text, force_join_pending_title
        )
        title_done = force_join_pending_title
        awaiting_force_join_step = None
        force_join_pending_channel_id = None
        force_join_pending_title = None
        await update.message.reply_text(f"✅ \"{title_done}\" added to the force-join list.")
        return

    if awaiting_channel_id_for_folder is not None:
        if not text:
            await update.message.reply_text("⚠️ Channel ID cannot be empty.")
            return
        folder_id = awaiting_channel_id_for_folder

        folder_before = await db.fetchrow("SELECT channel_id FROM folders WHERE id = $1", folder_id)
        had_previous_channel = bool(folder_before and folder_before["channel_id"])

        try:
            await ctx.bot.send_message(chat_id=text, text="✅ Channel linked successfully.")
            await db.execute("UPDATE folders SET channel_id = $1 WHERE id = $2", text, folder_id)
            awaiting_channel_id_for_folder = None
            await update.message.reply_text("✅ Channel ID saved and verified.")
        except Exception as e:
            awaiting_channel_id_for_folder = None
            logger.error(f"Channel verify failed for folder {folder_id}: {e}")
            await update.message.reply_text(
                f"❌ Channel ID not saved — the bot could not post there.\n"
                f"Please check: (1) the ID is correct (2) the bot is an admin in that channel (3) Post Messages permission is ON.\n\n"
                f"Try again via /folders."
            )
            return

        if had_previous_channel:
            folder_row = await db.fetchrow("SELECT name FROM folders WHERE id = $1", folder_id)
            await _repost_all_pages_for_folder(folder_id, folder_row["name"], text, update, ctx)
        return

    global pending_links
    if pending_links is not None:
        if not text:
            await update.message.reply_text("⚠️ Batch name cannot be empty. Please send it again.")
            return
        links = pending_links
        pending_links = None
        await update.message.reply_text(f"⏳ \"{text}\" — processing {len(links)} links...")
        await process_links(links, text, selected_folder_id, update, ctx)
        return

    if upload_session is None:
        if not text:
            await update.message.reply_text("⚠️ Send a text message for the broadcast.")
            return

        global pending_broadcast_text
        pending_broadcast_text = text
        recipient_count = await db.fetchval(
            "SELECT COUNT(*) FROM users WHERE user_id != $1", str(OWNER_ID)
        )

        if not recipient_count:
            pending_broadcast_text = None
            await update.message.reply_text("ℹ️ There are no users to broadcast to.")
            return

        rows = [[
            InlineKeyboardButton("✅ Confirm Broadcast", callback_data="broadcast_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel"),
        ]]
        await update.message.reply_text(
            f"📢 Send this message to *{recipient_count} user(s)*?\n\n"
            f"—\n{text}\n—\n\n"
            f"⚠️ This action cannot be undone.",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown"
        )
        return

    links = re.findall(r'https://drive\.google\.com/\S+', update.message.text or "")
    if not links:
        await update.message.reply_text("⚠️ No valid Google Drive link found.")
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
        "Send text, photo, video, audio, document, voice, sticker, or animation.\n\n"
        "❌ Use /exitbroadcast to turn it off."
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
        await update.message.reply_text("❌ No links. Run /startupload first.")
        upload_session = None
        return

    pending_links = list(upload_session)
    upload_session = None

    await update.message.reply_text(
        f"📝 Got {len(pending_links)} links.\n\nSend a name for this batch:"
    )


async def process_links(links, name, folder_id, update, ctx):
    folder = await db.fetchrow("SELECT id, name, channel_id FROM folders WHERE id = $1", folder_id)
    if not folder or not folder["channel_id"]:
        await update.message.reply_text("❌ Folder or channel_id missing — upload cancelled.")
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
            note = "Channel post updated."
        except Exception as e:
            logger.error(f"Channel page render failed for folder {folder_id}: {e}")
            note = "⚠️ Channel post could not be updated — audios were saved in the DB, check the channel via /folders."

        await update.message.reply_text(
            f"📥 Filled {len(to_fill)} audios into Batch #{existing['id']} (now {new_total} total).\n{note}"
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
            note = f"Post updated on \"{folder['name']}\" channel."
        except Exception as e:
            logger.error(f"Channel page render failed for folder {folder_id}: {e}")
            note = "⚠️ Channel post could not be updated — audios were saved in the DB, check the channel via /folders."

        await update.message.reply_text(
            f"✅ Batch #{batch_id} \"{chunk_name}\" created ({len(chunk)} audios). {note}"
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
    display_name = (folder_name or "Audio Collection").upper()

    part_text = (
        f"『 ℙ𝕒𝕣𝕥 {page_index} 』"
        if total_pages > 1
        else "『 ℂ𝕠𝕞𝕡𝕝𝕖𝕥𝕖 』"
    )

    total_start = (page_index - 1) * 1000 + 1
    total_end = min(page_index * 1000, total_pages * 1000)

    return (
        "╔════❖•❄️•❖════╗\n"
        f"🎧 {display_name}\n"
        f"{part_text}\n"
        "╚════❖•❄️•❖════╝\n\n"
        f"📦 Total Episodes: {total_start} to {total_end}\n"
        "⚡ Instant Delivery\n"
        "🎶 Premium Audio Collection\n\n"
        "👇 Click the button below\n"
        "to receive your episodes instantly."
    )


def _page_buttons(batches_in_page: list, start_offset: int) -> InlineKeyboardMarkup:
    rows = []
    running = start_offset
    for b in batches_in_page:
        end = running + b["total_links"] - 1
        label = f"Ep ❄️ {running} to {end}" if b["total_links"] > 1 else f"Ep ❄️ {running}"
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
                "/folders — manage folders (create/update channel)\n"
                "/startupload — start uploading links\n"
                "/done — finish the upload\n"
                "/forcejoin — manage force-join channels/groups\n\n"
                "No active session right now.",
                parse_mode="Markdown"
            )
            return

    if not args or not args[0].startswith("batch_"):
        if OTHER_BOT_URL:
            await update.message.reply_text(
                "👋 Use my another bot to schedule message\n"
                "schedule message, auto approve, QR generator.\n"
                "any bot related query to use support page\n"
                "                    ⬇️                ",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🤖 Open Other Bot", url=OTHER_BOT_URL)],
                        [InlineKeyboardButton("Support Page", url=UPDATE_SUPPORT_GROUP)],
                    ]
                )
            )
        else:
            await update.message.reply_text("👋 Please use this bot through the channel.")
        return

    batch_id = int(args[0].replace("batch_", ""))

    if not await _check_force_join(update, ctx, batch_id):
        return

    await _deliver_batch(batch_id, update.effective_chat.id, update.effective_user.id, ctx)


async def _deliver_batch(batch_id: int, chat_id: int, user_id_int: int, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = str(user_id_int)

    batch = await db.fetchrow("SELECT id, total_links FROM batches WHERE id = $1", batch_id)
    if not batch:
        await ctx.bot.send_message(chat_id=chat_id, text="❌ This collection does not exist.")
        return

    wait_rows = [[InlineKeyboardButton("• Cancel", callback_data=f"cancelsend_{batch_id}")]]
    if UPDATE_CHANNEL_URL:
        wait_rows.append([InlineKeyboardButton("📟 UPDATE CHANNEL", url=UPDATE_CHANNEL_URL)])

    warn = await ctx.bot.send_message(
        chat_id=chat_id,
        text="⏳ Please wait...",
        reply_markup=InlineKeyboardMarkup(wait_rows)
    )

    audios = await db.fetch(
        "SELECT id, drive_link, telegram_file_id FROM audios WHERE batch_id = $1 ORDER BY id",
        batch_id
    )
    audios = sorted(audios, key=lambda a: a["telegram_file_id"] is None)

    has_uncached = any(a["telegram_file_id"] is None for a in audios)
    sent_ids = []

    if has_uncached and LIVE_DOWNLOAD_AVAILABLE:
        delay_notice = await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Some audios are downloading for the first time, this may take *up to 2 minutes*. "
                "Cached audios will arrive instantly."
            ),
            parse_mode="Markdown"
        )
        sent_ids.append(delay_notice.message_id)

    failed_audios = []
    uncached_missing = []
    MAX_ATTEMPTS = 3
    RETRY_DELAY = 3
    cancel_key = (chat_id, batch_id)
    was_cancelled = False
    sent_audio_count = 0

    for audio in audios:
        if cancel_key in cancelled_deliveries:
            was_cancelled = True
            break

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
                        logger.info(
                            f"Audio {audio['id']} | Size: {len(file_bytes)/1024/1024:.2f} MB"
                        )

                        start_time = time.time()

                        bio = BytesIO(file_bytes)
                        bio.name = filename

                        msg = await ctx.bot.send_audio(
                            chat_id=chat_id,
                            audio=bio
                        )

                        logger.info(
                            f"Audio {audio['id']} uploaded in {time.time() - start_time:.2f} seconds"
                        )
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
            sent_audio_count += 1
        else:
            failed_audios.append(audio["id"])
            if not LIVE_DOWNLOAD_AVAILABLE and audio["telegram_file_id"] is None:
                uncached_missing.append(audio["id"])

    cancelled_deliveries.discard(cancel_key)
    try:
        await ctx.bot.delete_message(chat_id=chat_id, message_id=warn.message_id)
    except Exception as e:
        logger.warning(f"Could not remove please-wait message for batch {batch_id}: {e}")

    if was_cancelled:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ Cancelled. {sent_audio_count} audio file(s) already sent will still "
                f"be deleted in *{DELETE_MINUTES} minutes* like normal."
            ),
            parse_mode="Markdown"
        )
    else:
        if uncached_missing:
            await ctx.bot.send_message(chat_id=chat_id, text="⚠️ This audio is not available right now.")

        other_failures = len(failed_audios) - len(uncached_missing)
        if other_failures > 0:
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Failed to send {other_failures}/{len(audios)} audio files "
                    f"(even after {MAX_ATTEMPTS} attempts). Please try /start again."
                )
            )

        if sent_audio_count > 0:
            hands = " ".join(["🖐️"] * 8)
            closing_rows = None
            if UPDATE_CHANNEL_URL:
                closing_rows = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📟 UPDATE CHANNEL", url=UPDATE_CHANNEL_URL)]]
                )
            closing = await ctx.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❤️ HEY BRO ⬇️\n\n"
                    f"📁 FILES WILL BE DELETED AFTER [{DELETE_MINUTES} minutes] "
                    f"PLEASE SAVE THEM SOMEWHERE SAFE.\n"
                    f"TO GET IT AGAIN, REPEAT THE SAME PROCESS.\n\n"
                    f"{hands}"
                ),
                reply_markup=closing_rows
            )
            sent_ids.append(closing.message_id)

    if not sent_ids:
        return

    delete_at = datetime.utcnow() + timedelta(minutes=DELETE_MINUTES)
    await db.execute(
        "INSERT INTO sent_logs (user_id, batch_id, message_ids, delete_at) VALUES ($1,$2,$3,$4)",
        user_id, batch_id, json.dumps(sent_ids), delete_at
    )



async def cb_cancel_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Marks a batch delivery as cancelled. This is checked once per audio,
    between sends — it does not abort an upload already in progress, so a
    file mid-transfer when the user taps Cancel will still land."""
    batch_id = int(update.callback_query.data.replace("cancelsend_", ""))
    cancelled_deliveries.add((update.effective_chat.id, batch_id))
    await update.callback_query.answer("Cancelling after the current file finishes...")


async def cmd_refreshbuttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    msg = await update.message.reply_text(
        "🔄 Refreshing all folder buttons..."
    )

    folders = await db.fetch(
        "SELECT id, name, channel_id FROM folders "
        "WHERE channel_id IS NOT NULL"
    )

    total_pages = 0
    updated_pages = 0

    for folder in folders:
        batches = await db.fetch(
            "SELECT id FROM batches WHERE folder_id = $1 ORDER BY id",
            folder["id"]
        )

        pages = (len(batches) + PAGE_SIZE - 1) // PAGE_SIZE
        total_pages += pages

        for page in range(1, pages + 1):
            try:
                await render_folder_page(
                    folder["id"],
                    folder["name"],
                    folder["channel_id"],
                    page,
                    ctx
                )
                updated_pages += 1

                # Telegram rate limit se bachne ke liye
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(
                    f"Refresh failed: folder={folder['id']} page={page} error={e}"
                )

    await msg.edit_text(
        f"✅ Refresh complete.\n\n"
        f"Updated: {updated_pages}/{total_pages} pages"
    )

async def exit_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BROADCAST_MODE

    if update.effective_user.id != OWNER_ID:
        return

    if not BROADCAST_MODE:
        await update.message.reply_text(
            "ℹ️ Broadcast mode is already OFF."
        )
        return

    BROADCAST_MODE = False

    await update.message.reply_text(
        "❌ Broadcast mode turned OFF."
    )

async def cb_broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    global pending_broadcast_text
    pending_broadcast_text = None
    await update.callback_query.edit_message_text("❌ Broadcast cancelled.")


async def cb_broadcast_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    global pending_broadcast_text
    text = pending_broadcast_text
    pending_broadcast_text = None

    if not text:
        await update.callback_query.edit_message_text("⚠️ Broadcast text not found — please try again.")
        return

    await update.callback_query.edit_message_text("⏳ Sending broadcast...")

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
        BotCommand("start", "Start the bot"),
    ]
    owner_commands = public_commands + [
        BotCommand("folders", "Manage folders"),
        BotCommand("startupload", "Start a new batch upload"),
        BotCommand("done", "Finish the current upload batch"),
        BotCommand("forcejoin", "Manage force-join channels"),
        BotCommand("broadcast", "Send a broadcast message"),
        BotCommand("refreshbuttons", "Refresh all channel buttons"),
        BotCommand("exitbroadcast", "Exit broadcast mode"),
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
            text="🔄 Bot restarted. If an upload session was active, it has been reset — start again with /startupload."
        )
    except Exception as e:
        logger.error(f"Restart notice to owner failed: {e}")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {ctx.error}")


def main():
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)

    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=300.0,
        write_timeout=300.0,
        pool_timeout=60.0,
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
    app.add_handler(CommandHandler("refreshbuttons", cmd_refreshbuttons))
    app.add_handler(CommandHandler("exitbroadcast", exit_broadcast))

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
    app.add_handler(CallbackQueryHandler(cb_cancel_send, pattern=r"^cancelsend_\d+$"))
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


    if os.getenv("LOCAL_TEST") == "1":
        app.run_polling()
    else:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
        )

    # logger.info(f"Starting webhook server on 0.0.0.0:{PORT}, registering {WEBHOOK_URL}")
    # app.run_webhook(
    #     listen="0.0.0.0",
    #     port=PORT,
    #     url_path="webhook",
    #     webhook_url=WEBHOOK_URL,
    #     secret_token=WEBHOOK_SECRET,
    #     allowed_updates=Update.ALL_TYPES,
    # )


if __name__ == "__main__":
    main()
