import os
import json
import random
import asyncio
import threading
from flask import Flask
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError, 
    SessionPasswordNeededError, 
    AuthRestartError,
    MessageNotModifiedError
)

# --- FLASK WEB SERVER ---
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "OK - Userbot Broadcaster Service Running", 200

def run_flask_server():
    port = int(os.getenv("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# --- CONFIGURATION & STORAGE ---
# Only BOT_TOKEN and ADMIN_ID are required as environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8738394002:AAGJRJZGD_V8vJYLIM55ExVyqFAj-T1hVuE").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "6905348308").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)
DATA_FILE = "config_data.json"

app_data = {
    "api_id": None,
    "api_hash": None,
    "min_delay": 30,
    "max_delay": 60,
    "cycle_interval": 60,
    "broadcast_msg": "",
    "account_groups": {},
    "account_credentials": {}  # {phone: {"api_id": ..., "api_hash": ...}}
}

def load_data():
    global app_data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                loaded = json.load(f)
                app_data.update(loaded)
        except Exception as e:
            print(f"Error loading config file: {e}")

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(app_data, f, indent=4)

load_data()

# Fallback default API credentials (official Telegram test credentials)
# Used only to initialize Telethon bot connection without breaking startup
DEFAULT_BOT_API_ID = 6
DEFAULT_BOT_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"

bot_api_id = app_data.get("api_id") or DEFAULT_BOT_API_ID
bot_api_hash = app_data.get("api_hash") or DEFAULT_BOT_API_HASH

bot = TelegramClient('control_bot', bot_api_id, bot_api_hash, auto_reconnect=True)
user_clients = {}  
login_states = {}

active_broadcasts = {}  # {phone: True/False}
broadcast_tasks = {}    # {phone: asyncio.Task}

# --- ADMIN BOT CONTROL PANEL ---

@bot.on(events.NewMessage(pattern='/start', chats=ADMIN_ID))
async def start_handler(event):
    min_d = app_data.get('min_delay', 30)
    max_d = app_data.get('max_delay', 60)
    cycle_interval = app_data.get('cycle_interval', 60)
    msg_text = app_data.get('broadcast_msg', 'Not set')
    default_api_set = "Configured" if (app_data.get("api_id") and app_data.get("api_hash")) else "Using Default"

    text = f"""
Multi-Account Independent Broadcaster

Global Settings:
• Telegram API Credentials: {default_api_set}
• Group Gap: {min_d}-{max_d} Random Seconds
• Cycle Repeat: {cycle_interval} Minutes
• Broadcast Message: {msg_text[:35] if msg_text else 'Not set'}
• Logged-In Accounts: {len(user_clients)}

Select an option below to configure or manage:
"""
    buttons = [
        [Button.inline("Add Account", b"add_acc"), Button.inline("Manage Accounts & Groups", b"manage_accs")],
        [Button.inline("Set Delay Range (Sec)", b"set_delay"), Button.inline("Set Cycle Repeat (Min)", b"set_cycle")],
        [Button.inline("Set Message", b"set_msg"), Button.inline("Set Default API ID/Hash", b"set_api")]
    ]
    await event.respond(text, buttons=buttons)

@bot.on(events.CallbackQuery(chats=ADMIN_ID))
async def callback_handler(event):
    data = event.data

    if data == b"add_acc":
        if not app_data.get("api_id") or not app_data.get("api_hash"):
            login_states[ADMIN_ID] = {'step': 'ask_api_id'}
            await event.respond(
                "Telegram API Credentials Needed\n\n"
                "Please send the API ID for this account (from my.telegram.org):\n"
                "(Or send /default to use standard default credentials)"
            )
        else:
            login_states[ADMIN_ID] = {
                'step': 'phone',
                'api_id': app_data["api_id"],
                'api_hash': app_data["api_hash"]
            }
            await event.respond("Send Account Phone Number with Country Code (e.g. +919876543210):")

    elif data == b"set_api":
        login_states[ADMIN_ID] = {'step': 'set_default_api_id'}
        await event.respond("Send the Default API ID for all future accounts (from my.telegram.org):")

    elif data == b"set_delay":
        login_states[ADMIN_ID] = {'step': 'set_delay'}
        await event.respond("Send Min and Max delay in seconds separated by space (e.g., 30 60):")

    elif data == b"set_cycle":
        login_states[ADMIN_ID] = {'step': 'set_cycle'}
        await event.respond("Enter cycle repeat interval in minutes (e.g. 60):")

    elif data == b"set_msg":
        login_states[ADMIN_ID] = {'step': 'set_msg'}
        await event.respond("Send Broadcast Message text:")

    elif data == b"manage_accs":
        if not user_clients:
            await event.respond("Pehle kam se kam 1 user account add karein!")
            return
        
        buttons = []
        for phone in user_clients.keys():
            status = "RUNNING" if active_broadcasts.get(phone, False) else "STOPPED"
            selected_count = len(app_data["account_groups"].get(phone, []))
            buttons.append([Button.inline(f"{phone} | {status} ({selected_count} Grps)", f"acc_menu_{phone}".encode())])
        
        buttons.append([Button.inline("Back to Main Menu", b"back_main")])
        await event.respond("Select an account to start/stop broadcast or edit groups:", buttons=buttons)

    elif data == b"back_main":
        await start_handler(event)

    elif data.startswith(b"acc_menu_"):
        phone = data.decode().replace("acc_menu_", "")
        await render_account_dashboard(event, phone, edit=True)

    elif data.startswith(b"start_acc_"):
        phone = data.decode().replace("start_acc_", "")
        if active_broadcasts.get(phone, False):
            await event.answer("This account is already broadcasting!", alert=True)
            return

        if not app_data.get('broadcast_msg'):
            await event.respond("Broadcast message set nahi hai! Pehle message set karein.")
            return

        if not app_data["account_groups"].get(phone, []):
            await event.respond("Is account ke liye koi groups select nahi hain!")
            return

        active_broadcasts[phone] = True
        task = asyncio.create_task(run_single_account_broadcast(phone))
        broadcast_tasks[phone] = task

        await event.answer(f"Broadcast started for {phone}!", alert=True)
        await render_account_dashboard(event, phone, edit=True)

    elif data.startswith(b"stop_acc_"):
        phone = data.decode().replace("stop_acc_", "")
        if not active_broadcasts.get(phone, False):
            await event.answer("This account is not running!", alert=True)
            return

        active_broadcasts[phone] = False
        if phone in broadcast_tasks:
            broadcast_tasks[phone].cancel()
            del broadcast_tasks[phone]

        await event.answer(f"Broadcast stopped for {phone}!", alert=True)
        await render_account_dashboard(event, phone, edit=True)

    elif data.startswith(b"sel_acc_"):
        phone = data.decode().replace("sel_acc_", "")
        await render_group_selection(event, phone, page=0)

    elif data.startswith(b"grp_page_"):
        parts = data.decode().replace("grp_page_", "").rsplit("_", 1)
        phone = parts[0]
        page = int(parts[1])
        await render_group_selection(event, phone, page=page, edit=True)

    elif data.startswith(b"toggle_g_"):
        parts = data.decode().replace("toggle_g_", "").rsplit("_", 2)
        phone = parts[0]
        group_id = int(parts[1])
        page = int(parts[2])

        if phone not in app_data["account_groups"]:
            app_data["account_groups"][phone] = []

        if group_id in app_data["account_groups"][phone]:
            app_data["account_groups"][phone].remove(group_id)
        else:
            app_data["account_groups"][phone].append(group_id)

        save_data()
        await render_group_selection(event, phone, page=page, edit=True)

    elif data.startswith(b"sel_all_"):
        parts = data.decode().replace("sel_all_", "").rsplit("_", 1)
        phone = parts[0]
        page = int(parts[1])
        
        client = user_clients.get(phone)
        if client:
            dialogs = await client.get_dialogs()
            all_g_ids = [d.id for d in dialogs if d.is_group]
            app_data["account_groups"][phone] = list(set(app_data["account_groups"].get(phone, []) + all_g_ids))
            save_data()
        
        await render_group_selection(event, phone, page=page, edit=True)

    elif data.startswith(b"desel_all_"):
        parts = data.decode().replace("desel_all_", "").rsplit("_", 1)
        phone = parts[0]
        page = int(parts[1])

        app_data["account_groups"][phone] = []
        save_data()

        await render_group_selection(event, phone, page=page, edit=True)

async def render_account_dashboard(event, phone, edit=False):
    is_running = active_broadcasts.get(phone, False)
    status_str = "RUNNING" if is_running else "STOPPED"
    grp_count = len(app_data["account_groups"].get(phone, []))

    text = f"""
Account Dashboard: {phone}

• Status: {status_str}
• Selected Groups: {grp_count}
"""
    buttons = []
    if is_running:
        buttons.append([Button.inline("STOP Broadcast", f"stop_acc_{phone}".encode())])
    else:
        buttons.append([Button.inline("START Broadcast", f"start_acc_{phone}".encode())])

    buttons.append([Button.inline("Select / Manage Groups", f"sel_acc_{phone}".encode())])
    buttons.append([Button.inline("Back to Accounts List", b"manage_accs")])

    try:
        if edit:
            await event.edit(text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)
    except MessageNotModifiedError:
        pass

async def render_group_selection(event, phone, page=0, edit=False):
    client = user_clients.get(phone)
    if not client:
        await event.respond("Account session active nahi hai.")
        return

    try:
        dialogs = await client.get_dialogs()
        groups = [d for d in dialogs if d.is_group]
        selected_ids = app_data["account_groups"].get(phone, [])

        PER_PAGE = 10
        total_groups = len(groups)
        total_pages = (total_groups + PER_PAGE - 1) // PER_PAGE if total_groups > 0 else 1

        if page < 0:
            page = 0
        elif page >= total_pages:
            page = total_pages - 1

        start_idx = page * PER_PAGE
        end_idx = start_idx + PER_PAGE
        current_groups = groups[start_idx:end_idx]

        buttons = []
        buttons.append([
            Button.inline("Select All", f"sel_all_{phone}_{page}".encode()),
            Button.inline("Deselect All", f"desel_all_{phone}_{page}".encode())
        ])

        for g in current_groups:
            status = "[v]" if g.id in selected_ids else "[x]"
            title = (g.title[:25] + '..') if len(g.title) > 25 else g.title
            buttons.append([Button.inline(f"{status} {title}", f"toggle_g_{phone}_{g.id}_{page}".encode())])

        nav_buttons = []
        if page > 0:
            nav_buttons.append(Button.inline("Previous", f"grp_page_{phone}_{page-1}".encode()))
        if page < total_pages - 1:
            nav_buttons.append(Button.inline("Next", f"grp_page_{phone}_{page+1}".encode()))
        
        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([Button.inline("Back to Account Menu", f"acc_menu_{phone}".encode())])

        text = f"Select Groups for Account {phone}\nPage {page+1} of {total_pages} (Total Groups: {total_groups})"
        
        if edit:
            try:
                await event.edit(text, buttons=buttons)
            except MessageNotModifiedError:
                await event.answer("Updated")
        else:
            await event.respond(text, buttons=buttons)
    except Exception as e:
        await event.respond(f"Error fetching groups: {e}")

# --- TEXT INPUT HANDLER ---

@bot.on(events.NewMessage(chats=ADMIN_ID))
async def input_handler(event):
    if ADMIN_ID not in login_states:
        return

    state = login_states[ADMIN_ID]
    step = state.get('step')
    text = event.text.strip()

    # Flow: Setting Default API ID & Hash
    if step == 'set_default_api_id':
        if not text.isdigit():
            await event.respond("Invalid API ID. Must be numeric. Try again:")
            return
        state['api_id'] = int(text)
        state['step'] = 'set_default_api_hash'
        await event.respond("Now send the API Hash:")

    elif step == 'set_default_api_hash':
        app_data['api_id'] = state['api_id']
        app_data['api_hash'] = text
        save_data()
        del login_states[ADMIN_ID]
        await event.respond("Default API ID and API Hash saved successfully!")

    # Flow: Setting Account-specific or Prompted API Credentials
    elif step == 'ask_api_id':
        if text == '/default':
            state['api_id'] = DEFAULT_BOT_API_ID
            state['api_hash'] = DEFAULT_BOT_API_HASH
            state['step'] = 'phone'
            await event.respond("Send Account Phone Number with Country Code (e.g. +919876543210):")
        else:
            if not text.isdigit():
                await event.respond("API ID must be numbers only. Send valid API ID or /default:")
                return
            state['api_id'] = int(text)
            state['step'] = 'ask_api_hash'
            await event.respond("Now send the API Hash:")

    elif step == 'ask_api_hash':
        state['api_hash'] = text
        state['step'] = 'phone'
        await event.respond("Send Account Phone Number with Country Code (e.g. +919876543210):")

    # Flow: Account Login
    elif step == 'phone':
        phone = text
        api_id = state.get('api_id') or app_data.get('api_id') or DEFAULT_BOT_API_ID
        api_hash = state.get('api_hash') or app_data.get('api_hash') or DEFAULT_BOT_API_HASH

        session_file = os.path.join(SESSIONS_DIR, f"acc_{phone.replace('+', '')}")
        
        if os.path.exists(f"{session_file}.session"):
            try:
                os.remove(f"{session_file}.session")
            except Exception:
                pass

        client = TelegramClient(session_file, api_id, api_hash, auto_reconnect=True)
        await client.connect()

        for attempt in range(2):
            try:
                res = await client.send_code_request(phone)
                login_states[ADMIN_ID] = {
                    'step': 'code', 
                    'client': client, 
                    'phone': phone, 
                    'hash': res.phone_code_hash,
                    'api_id': api_id,
                    'api_hash': api_hash
                }
                await event.respond("Enter Telegram Login OTP Code:")
                break
            except AuthRestartError:
                await client.disconnect()
                await asyncio.sleep(2)
                await client.connect()
                if attempt == 1:
                    await event.respond("Telegram Server Auth Error. Try again later.")
                    del login_states[ADMIN_ID]
            except Exception as e:
                await event.respond(f"Error sending OTP: {e}")
                del login_states[ADMIN_ID]
                break

    elif step == 'code':
        client = state['client']
        phone = state['phone']
        phone_hash = state['hash']
        api_id = state['api_id']
        api_hash = state['api_hash']
        try:
            await client.sign_in(phone, text, phone_code_hash=phone_hash)
            user_clients[phone] = client
            if "account_credentials" not in app_data:
                app_data["account_credentials"] = {}
            app_data["account_credentials"][phone] = {"api_id": api_id, "api_hash": api_hash}
            save_data()
            await event.respond(f"Account {phone} Logged In Successfully!")
            del login_states[ADMIN_ID]
        except SessionPasswordNeededError:
            login_states[ADMIN_ID] = {
                'step': 'password', 
                'client': client, 
                'phone': phone, 
                'api_id': api_id, 
                'api_hash': api_hash
            }
            await event.respond("2-Step Verification Enabled!\n\nKripya apna Telegram 2FA Password bhejein:")
        except Exception as e:
            await event.respond(f"Login Failed: {e}")
            del login_states[ADMIN_ID]

    elif step == 'password':
        client = state['client']
        phone = state['phone']
        api_id = state['api_id']
        api_hash = state['api_hash']
        try:
            await client.sign_in(password=text)
            user_clients[phone] = client
            if "account_credentials" not in app_data:
                app_data["account_credentials"] = {}
            app_data["account_credentials"][phone] = {"api_id": api_id, "api_hash": api_hash}
            save_data()
            await event.respond(f"Account {phone} Logged In Successfully (2FA Passed)!")
            del login_states[ADMIN_ID]
        except Exception as e:
            await event.respond(f"Password Incorrect ya Error: {e}")
            del login_states[ADMIN_ID]

    elif step == 'set_delay':
        try:
            parts = text.split()
            app_data['min_delay'] = int(parts[0])
            app_data['max_delay'] = int(parts[1]) if len(parts) > 1 else int(parts[0])
            save_data()
            await event.respond(f"Delay range set to {app_data['min_delay']}-{app_data['max_delay']} seconds.")
        except Exception:
            await event.respond("Format: 30 60.")
        del login_states[ADMIN_ID]

    elif step == 'set_cycle':
        try:
            app_data['cycle_interval'] = int(text)
            save_data()
            await event.respond(f"Cycle repeat set to {text} minutes.")
        except Exception:
            await event.respond("Please enter a valid number of minutes.")
        del login_states[ADMIN_ID]

    elif step == 'set_msg':
        app_data['broadcast_msg'] = text
        save_data()
        await event.respond("Broadcast message saved!")
        del login_states[ADMIN_ID]

# --- INDEPENDENT ACCOUNT BROADCAST ENGINE ---

async def run_single_account_broadcast(phone):
    client = user_clients.get(phone)
    if not client:
        return

    cycle_count = 0

    while active_broadcasts.get(phone, False):
        try:
            msg = app_data.get('broadcast_msg')
            cycle = app_data.get('cycle_interval', 60)
            target_groups = app_data["account_groups"].get(phone, [])
            min_d = app_data.get('min_delay', 30)
            max_d = app_data.get('max_delay', 60)

            if not msg or not target_groups:
                await bot.send_message(ADMIN_ID, f"Account {phone} Broadcast Stopped: Message ya target groups missing hain.")
                active_broadcasts[phone] = False
                break

            cycle_count += 1
            successful_sends = 0
            failed_sends = 0

            # Iterate through each group exactly ONCE in this cycle
            for group_id in target_groups:
                if not active_broadcasts.get(phone, False):
                    break
                try:
                    if not client.is_connected():
                        await client.connect()
                    
                    await client.send_message(group_id, msg)
                    successful_sends += 1

                except FloodWaitError as e:
                    await bot.send_message(ADMIN_ID, f"Account {phone} Limit Warning! Sleeping {e.seconds}s")
                    await asyncio.sleep(e.seconds)
                    failed_sends += 1
                except Exception:
                    failed_sends += 1

                sleep_time = random.randint(min_d, max_d)
                await asyncio.sleep(sleep_time)

            # Send a summary message AFTER the cycle is completely finished
            if active_broadcasts.get(phone, False):
                summary = (
                    f"Account {phone} - Cycle #{cycle_count} Finished!\n\n"
                    f"Sent: {successful_sends} / {len(target_groups)} Groups\n"
                    f"Failed: {failed_sends} Groups\n"
                    f"Next cycle in {cycle} minutes..."
                )
                await bot.send_message(ADMIN_ID, summary)
                await asyncio.sleep(cycle * 60)

        except asyncio.CancelledError:
            break
        except Exception as e:
            await bot.send_message(ADMIN_ID, f"Unexpected error in broadcast for {phone}: {e}")
            await asyncio.sleep(30)

# --- STARTUP ---

async def main():
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    print("Flask Web Server started on background thread.")

    # Reconnect stored user accounts
    account_credentials = app_data.get("account_credentials", {})
    default_api_id = app_data.get("api_id") or DEFAULT_BOT_API_ID
    default_api_hash = app_data.get("api_hash") or DEFAULT_BOT_API_HASH

    for f in os.listdir(SESSIONS_DIR):
        if f.endswith('.session'):
            phone = f.replace("acc_", "").replace(".session", "")
            creds = account_credentials.get(phone, {})
            api_id = creds.get("api_id") or default_api_id
            api_hash = creds.get("api_hash") or default_api_hash

            path = os.path.join(SESSIONS_DIR, f.replace('.session', ''))
            c = TelegramClient(path, api_id, api_hash, auto_reconnect=True)
            try:
                await c.connect()
                if await c.is_user_authorized():
                    user_clients[phone] = c
            except Exception as e:
                print(f"Error loading session {f}: {e}")

    print("Master Control Bot Running...")
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
