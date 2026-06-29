"""
Telegram Audio Batch Bot
Hugging Face Spaces - Free Tier
Polling mode (no webhook needed)
"""

import asyncio
import logging
import os
import json
import re
from datetime import datetime, timedelta

# Local development: load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # On HF Spaces dotenv not needed

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

from db import Database
from drive import download_from_drive

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.ERROR
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # our own bot's status/info lines stay visible
                                # even though library noise (httpx, apscheduler,
                                # telegram.ext) is silenced above at ERROR.

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
OWNER_ID       = int(os.environ["OWNER_ID"])
BOT_USERNAME   = os.environ["BOT_USERNAME"].strip().lstrip("@")
DATABASE_URL   = os.environ["DATABASE_URL"]
BATCH_MAX      = 50
DELETE_MINUTES = 5
# CHANNEL_ID removed — every batch now belongs to a folder, and every folder
# carries its own channel_id. There is no single default channel anymore.

db = Database(DATABASE_URL)

# ── In-memory owner state machine (single owner, sequential flows only) ──────
# Only one of these is ever active at a time. Each represents "the bot is
# waiting for the owner's next text message to mean something specific."
upload_session: list | None = None       # collecting drive links
pending_links: list | None = None        # links collected, waiting on batch name
selected_folder_id: int | None = None    # folder chosen for the current upload

awaiting_new_folder_name: bool = False             # waiting for new folder's name
awaiting_channel_id_for_folder: int | None = None  # folder id waiting for a channel_id


def _reset_owner_state():
    """Clear all in-progress owner flows. Used when starting a new flow so
    stray state from an abandoned previous flow can't bleed into it."""
    global upload_session, pending_links, selected_folder_id
    global awaiting_new_folder_name, awaiting_channel_id_for_folder
    upload_session = None
    pending_links = None
    selected_folder_id = None
    awaiting_new_folder_name = False
    awaiting_channel_id_for_folder = None


# ── /folders — manage folders (list / create / update channel) ───────────────
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
        "⚠️ Bot ko us channel mein admin banana zaroori hai (Post Messages permission ke saath), "
        "warna posting fail hogi."
    )


# ── /startupload — pick folder, then collect links ────────────────────────────
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


async def _repost_all_batches_for_folder(folder_id, new_channel_id, update, ctx):
    """Repost every batch belonging to this folder into its new channel.
    Used when a folder's channel_id is changed — old posts stay in the old
    channel forever (Telegram has no "move message" operation), so this
    creates fresh posts in the new channel instead and updates each batch's
    channel_message_id to point at the new post."""
    batches = await db.fetch(
        "SELECT id, name, total_links FROM batches WHERE folder_id = $1 ORDER BY id",
        folder_id
    )
    if not batches:
        await update.message.reply_text("ℹ️ Is folder mein abhi koi batch nahi hai — repost karne ko kuch nahi.")
        return

    await update.message.reply_text(
        f"🔁 {len(batches)} batches naye channel mein repost ho rahe hain... isme time lagega."
    )

    REPOST_DELAY = 2  # seconds between posts — stays well under Telegram's rate limit
    success_count = 0
    failed_ids = []

    for batch in batches:
        try:
            msg_id = await post_to_channel(
                batch["id"], batch["total_links"], batch["name"], new_channel_id, ctx
            )
            await db.execute(
                "UPDATE batches SET channel_message_id = $1 WHERE id = $2",
                str(msg_id), batch["id"]
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Repost failed for batch {batch['id']}: {e}")
            failed_ids.append(batch["id"])

        await asyncio.sleep(REPOST_DELAY)

    summary = f"✅ {success_count}/{len(batches)} batches repost ho gaye naye channel mein."
    if failed_ids:
        summary += f"\n⚠️ Fail hue: batch #{', #'.join(str(i) for i in failed_ids)}"
    await update.message.reply_text(summary)


# ── Collect links / handle text replies for the active flow ──────────────────
async def handle_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    text = (update.message.text or "").strip()

    # State: waiting for a new folder's name
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

    # State: waiting for a channel_id (new folder, or updating an existing one)
    if awaiting_channel_id_for_folder is not None:
        if not text:
            await update.message.reply_text("⚠️ Channel ID khali nahi ho sakta.")
            return
        folder_id = awaiting_channel_id_for_folder
        awaiting_channel_id_for_folder = None

        folder_before = await db.fetchrow("SELECT channel_id FROM folders WHERE id = $1", folder_id)
        had_previous_channel = bool(folder_before and folder_before["channel_id"])

        # Verify the bot can actually post here before saving — catches a
        # wrong ID or missing admin rights immediately instead of failing
        # silently on the first real batch later.
        try:
            await ctx.bot.send_message(chat_id=text, text="✅ Channel linked successfully.")
            await db.execute("UPDATE folders SET channel_id = $1 WHERE id = $2", text, folder_id)
            await update.message.reply_text("✅ Channel ID save ho gaya aur verify ho gaya.")
        except Exception as e:
            logger.error(f"Channel verify failed for folder {folder_id}: {e}")
            await update.message.reply_text(
                f"❌ Channel ID save nahi hua — bot wahan post nahi kar saka.\n"
                f"Check karein: (1) ID sahi hai (2) bot us channel mein admin hai (3) Post Messages permission ON hai.\n\n"
                f"Phir se try karein /folders se."
            )
            return

        # Channel was UPDATED (not set for the first time) — repost every
        # batch this folder already has into the new channel, since the
        # old posts live in the old channel and don't move on their own.
        if had_previous_channel:
            await _repost_all_batches_for_folder(folder_id, text, update, ctx)
        return

    # State: waiting for batch name
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

    # State: collecting drive links
    if upload_session is None:
        return

    links = re.findall(r'https://drive\.google\.com/\S+', update.message.text or "")
    if not links:
        await update.message.reply_text("⚠️ Koi valid Google Drive link nahi mila.")
        return

    upload_session.extend(links)
    await update.message.reply_text(
        f"✅ {len(links)} link(s) add hue. Total: {len(upload_session)}"
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

    # Fill existing incomplete batch first, but only within the SAME folder —
    # filling a Hindi-folder batch with Punjabi links would be wrong, so the
    # "most recent incomplete batch" search is now scoped per folder.
    existing = await db.fetchrow(
        "SELECT id, total_links, name, channel_message_id FROM batches "
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
            await db.execute(
                "UPDATE batches SET name = $1 WHERE id = $2",
                batch_name, existing["id"]
            )

        for link in to_fill:
            await db.execute(
                "INSERT INTO audios (batch_id, drive_link) VALUES ($1, $2)",
                existing["id"], link
            )
        await db.execute(
            "UPDATE batches SET total_links = total_links + $1 WHERE id = $2",
            len(to_fill), existing["id"]
        )

        edited = False
        if existing["channel_message_id"]:
            edited = await edit_channel_post(
                existing["id"], batch_name, new_total,
                existing["channel_message_id"], channel_id, ctx
            )

        if edited:
            note = "Channel post update hua."
        else:
            # Edit failed — most likely the folder's channel_id changed since
            # this batch was originally posted, so the old message_id belongs
            # to a different chat than channel_id now points to. Don't leave
            # the batch silently unposted in the current channel — post fresh.
            new_msg_id = await post_to_channel(existing["id"], new_total, batch_name, channel_id, ctx)
            await db.execute(
                "UPDATE batches SET channel_message_id = $1 WHERE id = $2",
                str(new_msg_id), existing["id"]
            )
            note = "⚠️ Purana channel post edit nahi ho saka (channel badal gaya tha) — naya post is channel mein bheja gaya."

        await update.message.reply_text(
            f"📥 Batch #{existing['id']} mein {len(to_fill)} audios fill hue (ab total {new_total}).\n"
            f"{note}"
        )

    # New batches for remaining — split into chunks if remaining > BATCH_MAX
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

        msg_id = await post_to_channel(batch_id, len(chunk), chunk_name, channel_id, ctx)
        await db.execute(
            "UPDATE batches SET channel_message_id = $1 WHERE id = $2",
            str(msg_id), batch_id
        )
        await update.message.reply_text(
            f"✅ Batch #{batch_id} \"{chunk_name}\" create hua ({len(chunk)} audios). "
            f"\"{folder['name']}\" channel par post bheja gaya."
        )

    await update.message.reply_text("🎉 Upload complete!")


def _channel_text(name, total) -> str:
    display_name = name or "Audio Collection"
    return (
        f"🎵 {display_name}\n"
        f"📦 Total Audios: {total}\n"
        f"👇 Niche button par click karke bot se audio prapt karein."
    )


def _channel_button(batch_id, name) -> InlineKeyboardMarkup:
    display_name = name or "Audios"
    # Telegram inline buttons render badly past ~40-50 chars on mobile —
    # truncate so the button stays one clean line instead of wrapping/clipping.
    label_name = display_name if len(display_name) <= 40 else display_name[:37] + "..."
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🎧 Get {label_name}",
            url=f"https://t.me/{BOT_USERNAME}?start=batch_{batch_id}"
        )
    ]])


async def post_to_channel(batch_id, total, name, channel_id, ctx) -> int:
    msg = await ctx.bot.send_message(
        chat_id=channel_id,
        text=_channel_text(name, total),
        reply_markup=_channel_button(batch_id, name)
    )
    return msg.message_id


async def edit_channel_post(batch_id, name, total, channel_message_id, channel_id, ctx) -> bool:
    """Edit the existing channel post in place (text + button) instead of posting new.
    Returns False if the edit fails (e.g. message too old, deleted, or id missing)."""
    try:
        await ctx.bot.edit_message_text(
            chat_id=channel_id,
            message_id=int(channel_message_id),
            text=_channel_text(name, total),
            reply_markup=_channel_button(batch_id, name)
        )
        return True
    except Exception as e:
        logger.error(f"Failed to edit channel post for batch {batch_id}: {e}")
        return False


# ── /start batch_N (user flow) ────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args

    # Owner ka plain /start — control panel dikhao
    if update.effective_user.id == OWNER_ID:
        if not args or not args[0].startswith("batch_"):
            await update.message.reply_text(
                "👑 *Owner Panel*\n\n"
                "Commands:\n"
                "/folders — folders manage karein (create/update channel)\n"
                "/startupload — links upload karna shuru karein\n"
                "/done — upload finish karein\n\n"
                "Abhi active session nahi hai.",
                parse_mode="Markdown"
            )
            return

    # Normal user bina batch arg ke
    if not args or not args[0].startswith("batch_"):
        await update.message.reply_text("👋 Is bot ko channel ke through use karein.")
        return

    batch_id = int(args[0].replace("batch_", ""))
    chat_id  = update.effective_chat.id
    user_id  = str(update.effective_user.id)

    batch = await db.fetchrow(
        "SELECT id, total_links FROM batches WHERE id = $1", batch_id
    )
    if not batch:
        await update.message.reply_text("❌ Yeh collection exist nahi karta.")
        return

    warn = await update.message.reply_text(
        f"⚠️ *Warning*\n\n"
        f"Ye {batch['total_links']} audio files *{DELETE_MINUTES} minute* baad delete ho jayenge.\n\n"
        f"Abhi forward karein dusri jagah!\n\nBhej rahe hain... 📤",
        parse_mode="Markdown"
    )

    audios = await db.fetch(
        "SELECT id, drive_link, telegram_file_id FROM audios WHERE batch_id = $1",
        batch_id
    )
    # Send cached (telegram_file_id already known) audios first — these are
    # instant since they skip Drive entirely. Uncached ones (needing a Drive
    # download) go after, so the user isn't stuck waiting on the slowest
    # item before getting anything.
    audios = sorted(audios, key=lambda a: a["telegram_file_id"] is None)

    sent_ids = [warn.message_id]
    failed_audios = []
    MAX_ATTEMPTS = 3  # 1 initial + 2 retries
    RETRY_DELAY = 3  # seconds

    for audio in audios:
        msg = None
        file_bytes = None
        filename = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if audio["telegram_file_id"]:
                    msg = await ctx.bot.send_audio(chat_id=chat_id, audio=audio["telegram_file_id"])
                else:
                    # Download once, reuse the bytes across retries — no point
                    # re-hitting Drive if only the Telegram upload failed.
                    if file_bytes is None:
                        file_bytes, filename = await download_from_drive(audio["drive_link"])
                    msg = await ctx.bot.send_audio(chat_id=chat_id, audio=file_bytes, filename=filename)
                    if msg.audio:
                        await db.execute(
                            "UPDATE audios SET telegram_file_id = $1 WHERE id = $2",
                            msg.audio.file_id, audio["id"]
                        )
                break  # success
            except Exception as e:
                logger.error(f"Audio {audio['id']} attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_DELAY)

        if msg is not None:
            sent_ids.append(msg.message_id)
        else:
            failed_audios.append(audio["id"])

    if failed_audios:
        await update.message.reply_text(
            f"⚠️ {len(failed_audios)}/{len(audios)} audio bhejne mein fail hue "
            f"({MAX_ATTEMPTS} attempts ke baad bhi). Phir se /start try karein."
        )

    sent_count = len(audios) - len(failed_audios)
    if sent_count > 0:
        closing = await update.message.reply_text(
            f"✅ {sent_count} audio files bhej diye gaye.\n\n"
            f"⏳ Ye *{DELETE_MINUTES} minute* mein delete ho jayenge — abhi forward kar lein!",
            parse_mode="Markdown"
        )
        sent_ids.append(closing.message_id)

    delete_at = datetime.utcnow() + timedelta(minutes=DELETE_MINUTES)
    await db.execute(
        "INSERT INTO sent_logs (user_id, batch_id, message_ids, delete_at) VALUES ($1,$2,$3,$4)",
        user_id, batch_id, json.dumps(sent_ids), delete_at
    )


# ── Auto-delete job (runs every 30s via JobQueue) ─────────────────────────────
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
async def post_init(application: Application):
    """Runs inside PTB's own event loop — safe place to connect DB."""
    await db.connect()
    await db.init_schema()
    logger.info("Database connected and schema ready.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Without this, PTB dumps a full traceback for every transient network
    blip (e.g. httpcore.ReadError on get_updates) — normal on long-polling
    over a home internet connection, not a bot bug. PTB's own retry loop
    already handles reconnecting; this just keeps the log readable."""
    logger.error(f"Unhandled error: {ctx.error}")


def main():
    # Default PTB request timeouts (read ~5s, write ~5s) are far too short for
    # uploading large audio files. This is the actual cause of repeated
    # "Timed out" errors after the Drive download already succeeded —
    # those errors were happening on the send_audio leg, not the download.
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)   # DB connect here, inside PTB's loop
        .build()
    )

    app.add_handler(CommandHandler("startupload", cmd_startupload))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("folders", cmd_folders))

    app.add_handler(CallbackQueryHandler(cb_folder_new, pattern=r"^folder_new$"))
    app.add_handler(CallbackQueryHandler(cb_folder_list, pattern=r"^folder_list$"))
    app.add_handler(CallbackQueryHandler(cb_folder_manage, pattern=r"^folder_manage_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_folder_setchannel, pattern=r"^folder_setchannel_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_upload_folder, pattern=r"^upload_folder_\d+$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_error_handler(on_error)

    # Auto-delete every 30 seconds
    app.job_queue.run_repeating(auto_delete_job, interval=30, first=10)

    logger.info("Bot polling started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)  # plain call, NOT awaited


if __name__ == "__main__":
    main()  # no asyncio.run()
