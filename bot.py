import asyncio
import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import shutil

from flask import Flask
from threading import Thread

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive"


from storage import load_data, save_data
from poster import start_posting, stop_posting_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states for adding account
(API_ID, API_HASH, PHONE, CODE, PASSWORD) = range(5)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome! \nUse /add_account to add a Telegram account.\n"
        "Use /add_group to add a group (public @username or private invite link).\n"
        "Use /remove_group to remove a group (Enter public uername or private link.\n)"
        "Use /set_post to set the advertisement post (send a message with media).\n"
        "Use /start_posting to begin the ad loop.\n"
        "Use /stop_posting to stop the loop.\n"
        "Use /status to see current data.\n"
        "Use /show_post to see the current post."
    )


async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send your API ID (integer):")
    return API_ID


async def add_account_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["api_id"] = int(update.message.text)
        await update.message.reply_text("Now send your API hash:")
        return API_HASH
    except ValueError:
        await update.message.reply_text("Invalid API ID. Please send an integer.")
        return API_ID


async def add_account_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send your phone number (with country code, e.g., +1234567890):"
    )
    return PHONE


async def add_account_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["phone"] = phone
    api_id = context.user_data["api_id"]
    api_hash = context.user_data["api_hash"]

    # Create a temporary client to send code
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone)
        context.user_data["temp_client"] = client
        await update.message.reply_text(
            "A verification code has been sent. Please send it (or /cancel to abort)."
        )
        return CODE
    except Exception as e:
        await update.message.reply_text(f"Error: {e}. Please try again.")
        return ConversationHandler.END


async def add_account_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    client = context.user_data.get("temp_client")
    if not client:
        await update.message.reply_text(
            "Session expired. Start over with /add_account."
        )
        return ConversationHandler.END

    try:
        await client.sign_in(phone=context.user_data["phone"], code=code)
        # Save session string
        session_string = client.session.save()
        # Store account
        data = load_data()
        data["accounts"].append(
            {
                "api_id": context.user_data["api_id"],
                "api_hash": context.user_data["api_hash"],
                "phone": context.user_data["phone"],
                "session_string": session_string,
            }
        )
        save_data(data)
        await update.message.reply_text("Account added successfully!")
        await client.disconnect()
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "Two-factor authentication is enabled. Please send your password:"
        )
        return PASSWORD
    except Exception as e:
        await update.message.reply_text(f"Failed to sign in: {e}")
        return ConversationHandler.END


async def add_account_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client = context.user_data.get("temp_client")
    if not client:
        await update.message.reply_text("Session expired. Start over.")
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        data = load_data()
        data["accounts"].append(
            {
                "api_id": context.user_data["api_id"],
                "api_hash": context.user_data["api_hash"],
                "phone": context.user_data["phone"],
                "session_string": session_string,
            }
        )
        save_data(data)
        await update.message.reply_text("Account added successfully!")
        await client.disconnect()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Failed: {e}")
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()  # optional cleanup
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END


async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add one or more groups by username or private invite link (space-            separated)."""
    text = update.message.text or ""
    if text.startswith("/add_group"):
        text = text[len("/add_group") :].strip()
    if not text:
        await update.message.reply_text(
            "Please provide at least one group identifier.\n"
            "Example: /add_group @group1 https://t.me/+abc @group2"
        )
        return

    # Split by spaces to get multiple identifiers
    identifiers = text.split()
    data = load_data()

    if not data.get("accounts"):
        await update.message.reply_text("No accounts available. Add an account first.")
        return

    # Use the first account to resolve entities
    acc = data["accounts"][0]
    client = TelegramClient(
        StringSession(acc["session_string"]), int(acc["api_id"]), acc["api_hash"]
    )
    await client.connect()

    results = []
    for identifier in identifiers:
        try:
            entity = await client.get_entity(identifier)
            chat_id = entity.id
            group_entry = {
                "name": entity.title if hasattr(entity, "title") else identifier,
                "chat_id": chat_id,
                "identifier": identifier,
            }
            # Check for duplicates
            if any(
                g["identifier"] == identifier or g["chat_id"] == chat_id
                for g in data["groups"]
            ):
                results.append(f"⚠️ Group '{identifier}' already exists.")
                continue

            data["groups"].append(group_entry)
            save_data(data)  # save after each successful addition
            results.append(f"✅ Added: {group_entry['name']} ({identifier})")
        except Exception as e:
            results.append(f"❌ Failed to add '{identifier}': {e}")

    await client.disconnect()

    # Send summary
    await update.message.reply_text(
        "\n".join(results) if results else "No groups processed."
    )


async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a group by its public username, private invite link, or chat ID."""
    text = update.message.text or ""
    if text.startswith("/remove_group"):
        text = text[len("/remove_group") :].strip()
    if not text:
        await update.message.reply_text(
            "Please provide the group identifier (e.g., @mygroup, https://t.me/+, or chat ID)."
        )
        return

    data = load_data()
    groups = data.get("groups", [])
    removed = False
    new_groups = []

    for group in groups:
        # Match by identifier or chat_id (converted to string)
        identifier = group.get("identifier", "")
        chat_id_str = str(group.get("chat_id", ""))
        if identifier == text or chat_id_str == text:
            removed = True
            continue
        new_groups.append(group)

    if removed:
        data["groups"] = new_groups
        save_data(data)
        await update.message.reply_text(
            f"✅ Group matching '{text}' removed successfully."
        )
    else:
        await update.message.reply_text(f"❌ No group found matching '{text}'.")


async def json_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the current data.json file as a document for backup."""
    # Optional: restrict to a specific admin user ID
    # ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    # if update.effective_user.id != ADMIN_ID:
    #     await update.message.reply_text("⛔ You are not authorized to use this command.")
    #     return

    data_file = "data.json"
    if not os.path.exists(data_file):
        await update.message.reply_text("❌ data.json file not found.")
        return

    try:
        with open(data_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="data.json",
                caption="📦 Current data backup – includes all accounts, groups, and post settings.",
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error sending file: {e}")


async def set_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the process of setting the post. If text is provided as argument, use it immediately."""
    # If text is given with the command, use it directly
    text = update.message.text or ""
    if text.startswith("/set_post"):
        text = text[len("/set_post") :].strip()
    if text:
        data = load_data()
        data["post"] = {"text": text}
        save_data(data)
        await update.message.reply_text("Post text saved successfully! (No media)")
        return

    # Otherwise, ask for the content
    context.user_data["awaiting_post"] = True
    await update.message.reply_text(
        "Please send the post content now. You can include text and/or media (photo, video, document). "
        "Send /cancel to abort."
    )


async def handle_post_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture the user's message as the post (text + optional media)."""
    # If not expecting a post, let other handlers process the message
    if not context.user_data.get("awaiting_post"):
        return False  # ← this tells PTB to continue to next handlers

    msg = update.message
    if not msg:
        await update.message.reply_text("Invalid message.")
        return

    data = load_data()
    post_data = {"text": msg.caption or msg.text or ""}

    # Download media if present
    if msg.photo:
        file = await msg.photo[-1].get_file()
        file_path = f"media/{file.file_id}.jpg"
        os.makedirs("media", exist_ok=True)
        await file.download_to_drive(file_path)
        post_data["media_path"] = file_path
    elif msg.document or msg.video or msg.audio or msg.animation:
        file_obj = msg.document or msg.video or msg.audio or msg.animation
        file = await file_obj.get_file()
        ext = file.file_path.split(".")[-1] if file.file_path else "bin"
        file_path = f"media/{file.file_id}.{ext}"
        os.makedirs("media", exist_ok=True)
        await file.download_to_drive(file_path)
        post_data["media_path"] = file_path

    data["post"] = post_data
    save_data(data)
    context.user_data["awaiting_post"] = False
    await update.message.reply_text("Post saved successfully!")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    accounts = data.get("accounts", [])
    groups = data.get("groups", [])
    post = data.get("post")
    text = f"Accounts: {len(accounts)}\nGroups: {len(groups)}\nPost: {'Set' if post else 'Not set'}"
    await update.message.reply_text(text)


async def start_posting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data["accounts"] or not data["groups"] or not data["post"]:
        await update.message.reply_text("Missing accounts, groups, or post. Cannot start.")
        return
    from poster import set_report_chat_id
    set_report_chat_id(update.effective_chat.id)
    start_posting()
    await update.message.reply_text("Posting loop started. Stats will be sent after each cycle.")


async def stop_posting_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_posting_loop()
    await update.message.reply_text("Posting loop stopping (after current cycle).")


async def show_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    post = data.get("post")
    if not post:
        await update.message.reply_text("No post has been set yet.")
        return

    text = post.get("text", "")
    media_path = post.get("media_path")

    if not media_path or not os.path.exists(media_path):
        await update.message.reply_text(
            text if text else "(Empty post – no media found)"
        )
        return

    # Determine file type from extension
    ext = os.path.splitext(media_path)[1].lower()
    try:
        with open(media_path, "rb") as f:
            if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                await update.message.reply_photo(photo=f, caption=text)
            elif ext in [".mp4", ".mov", ".avi"]:
                await update.message.reply_video(video=f, caption=text)
            elif ext in [".gif"]:
                await update.message.reply_animation(animation=f, caption=text)
            elif ext in [".mp3", ".wav", ".ogg"]:
                await update.message.reply_audio(audio=f, caption=text)
            else:
                # Fallback to document for unknown types
                await update.message.reply_document(document=f, caption=text)
    except Exception as e:
        await update.message.reply_text(f"Error sending preview: {e}")


def main():
    # Bot token from environment or set directly
    TOKEN = "8738394002:AAHHYwnbCFUvBeDXabWQ4-mnaTXSVIMp4XU"

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_group", add_group))
    app.add_handler(CommandHandler("remove_group", remove_group))
    app.add_handler(CommandHandler("set_post", set_post))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("start_posting", start_posting_cmd))
    app.add_handler(CommandHandler("stop_posting", stop_posting_cmd))
    app.add_handler(CommandHandler("json", json_export))
    app.add_handler(CommandHandler("show_post", show_post))
    # Conversation for adding account
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add_account", add_account_start)],
        states={
            API_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_api_id)
            ],
            API_HASH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_api_hash)
            ],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_code)],
            PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_password)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND)
            | filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO,
            handle_post_content,
        )
    )
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    ).start()

    import time
    while True:
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())  # fresh loop each attempt
            main()
        except Exception:
            logger.exception("Bot crashed, restarting in 5 seconds...")
            time.sleep(5)
