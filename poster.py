import asyncio
import logging
import os
from telegram import Bot
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import RPCError
import shutil
from storage import load_data, save_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
report_chat_id = 6431566351
# Global control flags
posting_task = None
stop_posting = False

def set_report_chat_id(chat_id):
    global report_chat_id
    report_chat_id = chat_id

def get_client(account):
    """Create a Telethon client from stored session string."""
    return TelegramClient(
        StringSession(account["session_string"]),
        int(account["api_id"]),
        account["api_hash"]
    )

async def send_post_to_group(client, group, post_data, retries=3):
    identifier = group["identifier"]
    for attempt in range(retries):
        try:
            try:
                entity = await client.get_entity(identifier)
            except ValueError:
                if identifier.startswith("https://t.me/joinchat/") or identifier.startswith("https://t.me/+"):
                    await client.join_chat(identifier)
                    entity = await client.get_entity(identifier)
                else:
                    raise

            if post_data and post_data.get("media_path") and os.path.exists(post_data["media_path"]):
                await client.send_file(entity, post_data["media_path"], caption=post_data.get("text", ""))
            else:
                await client.send_message(entity, post_data.get("text", ""))
            return {"success": True, "identifier": identifier, "error": None}
        except Exception as e:
            error_msg = str(e)
            # No retry on permission errors
            if "can't write" in error_msg or "not a member" in error_msg:
                return {"success": False, "identifier": identifier, "error": error_msg}
            # Retry on session errors
            if "wrong session ID" in error_msg and attempt < retries - 1:
                logger.warning(f"Session error, retrying {identifier} (attempt {attempt+1})")
                await asyncio.sleep(3)
                # Force reconnect for next attempt (client will be reused, but we can reconnect)
                await client.disconnect()
                await client.connect()
                continue
            if attempt == retries - 1:
                return {"success": False, "identifier": identifier, "error": error_msg}
    return {"success": False, "identifier": identifier, "error": "Max retries exceeded"}

async def send_account_messages(client, groups, post):
    results = []
    for group in groups:
        result = await send_post_to_group(client, group, post)
        results.append(result)
        await asyncio.sleep(120)
    await client.disconnect()
    return results

async def posting_loop():
    global stop_posting, report_chat_id
    while not stop_posting:
        data = load_data()
        accounts = data.get("accounts", [])
        groups = data.get("groups", [])
        post = data.get("post")

        if not accounts or not groups or not post:
            logger.info("⏳ Missing accounts, groups, or post. Waiting...")
            await asyncio.sleep(60)
            continue

        # Distribute groups
        total_groups = len(groups)
        total_accounts = len(accounts)
        groups_per_account = total_groups // total_accounts
        remainder = total_groups % total_accounts

        assignments = []
        idx = 0
        for i in range(total_accounts):
            count = groups_per_account + (1 if i < remainder else 0)
            assignments.append(groups[idx:idx+count])
            idx += count

        for i, assigned in enumerate(assignments):
            logger.info(f"👤 Account {i+1} assigned {len(assigned)} groups")

        tasks = []
        for acc, assigned_groups in zip(accounts, assignments):
            if not assigned_groups:
                continue
            client = get_client(acc)
            await client.connect()
            tasks.append(send_account_messages(client, assigned_groups, post))

        all_results = []
        if tasks:
            task_results = await asyncio.gather(*tasks)
            for res in task_results:
                all_results.extend(res)

        # Build stats
        sent = sum(1 for r in all_results if r["success"])
        failed = len(all_results) - sent
        failed_details = [r for r in all_results if not r["success"]]

        stats_msg = f"📊 **Posting Cycle Complete**\n"
        stats_msg += f"Total groups processed: {len(all_results)}\n"
        stats_msg += f"✅ Successfully sent: {sent}\n"
        stats_msg += f"❌ Failed: {failed}\n"

        if failed_details:
            stats_msg += "\n**Failures:**\n"
            for r in failed_details[:10]:
                stats_msg += f"• {r['identifier']}: {r['error'][:50]}...\n"
            if len(failed_details) > 10:
                stats_msg += f"... and {len(failed_details)-10} more."

        # Send report
        if report_chat_id:
            try:
                bot = Bot(token=os.getenv("BOT_TOKEN", "8738394002:AAHHYwnbCFUvBeDXabWQ4-mnaTXSVIMp4XU"))
                await bot.send_message(chat_id=report_chat_id, text=stats_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send stats: {e}")

        logger.info("🔄 Cycle completed. Waiting 1 hour...")
        await asyncio.sleep(3600)

def start_posting():
    global posting_task, stop_posting
    if posting_task and not posting_task.done():
        return
    stop_posting = False
    posting_task = asyncio.create_task(posting_loop())

def stop_posting_loop():
    global stop_posting
    stop_posting = True