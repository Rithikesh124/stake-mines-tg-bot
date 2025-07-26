import logging
import hmac
import hashlib
import random
import io
import httpx
import uuid
import os
import asyncio
from datetime import datetime, timedelta

from PIL import Image
from io import BytesIO

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    PicklePersistence,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest

# --- Configuration ---
BOT_TOKEN = "7857552768:AAFAveQgiTVtbemAr9X5rf6tv1FEOBpzkAc"
DEV_USERNAME = "@DEVELOPERSTAKEBOT"
ADMIN_ACTIVATION_KEY = "SUPER-ADMIN-2024"

# Final, correct image URLs
SINGLE_CELL_URL = "https://i.postimg.cc/dtVfWTSd/Screenshot-20250716-163347-Chrome.jpg"
DIAMOND_IMAGE_URL = "https://i.postimg.cc/TYpt961H/Screenshot-20250713-204556-Lemur-Browser-removebg-preview-removebg-preview.jpg"
SERVER_SEED_GUIDE_URL = "https://i.postimg.cc/LsMv2gTr/Screenshot-20250716-164325-Chrome.jpg"
BET_AMOUNT_GUIDE_URL = "https://i.postimg.cc/qvKQPx8s/Screenshot-20250716-164700-Chrome.jpg"


INITIAL_TIMED_KEYS = { "ALPHA-1122": 30, "BETA-3344": 30, "GAMMA-5566": 7, "DELTA-7788": 7, "EPSILON-9900": 1, "ZETA-2244": 1, "THETA-6688": 365, "IOTA-1357": 365 }

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Unified Conversation States ---
(
    CHOOSE_MINES, AWAIT_SERVER_SEED, AWAIT_BET_AMOUNT, AWAIT_ACTIVATION_KEY,
    ADMIN_AWAIT_NEW_KEY, ADMIN_AWAIT_KEY_DURATION, ADMIN_AWAIT_BROADCAST_MESSAGE,
    ADMIN_AWAIT_DIRECT_MESSAGE
) = range(8)

# ==============================================================================
# 1. Core Logic & Helpers
# ==============================================================================
def provably_fair_mines(server_seed: str, client_seed: str, nonce: int, mine_count: int) -> list:
    message = f"{client_seed}-{nonce}"; h = hmac.new(server_seed.encode(), message.encode(), hashlib.sha256).hexdigest()
    bombs, tile_indices = set(), list(range(25))
    for i in range(0, len(h), 2):
        if len(bombs) == mine_count: break
        chunk = h[i:i+2]; value = int(chunk, 16)
        if value < len(tile_indices): bombs.add(tile_indices.pop(value))
    while len(bombs) < mine_count and tile_indices: bombs.add(tile_indices.pop(random.randrange(len(tile_indices))))
    return [i for i in range(25) if i not in bombs]

async def generate_prediction_image(safe_tiles: list) -> io.BytesIO | None:
    async with httpx.AsyncClient() as client:
        try:
            cell_task, diamond_task = client.get(SINGLE_CELL_URL), client.get(DIAMOND_IMAGE_URL)
            cell_response, diamond_response = await asyncio.gather(cell_task, diamond_task)
            cell_response.raise_for_status(); diamond_response.raise_for_status()
        except httpx.RequestError as e: logger.error(f"Asset download failed: {e}"); return None
    cell, diamond_original = Image.open(BytesIO(cell_response.content)).convert("RGBA"), Image.open(BytesIO(diamond_response.content)).convert("RGBA")
    GRID_SIZE = 5; cell_width, cell_height = cell.size
    background = Image.new('RGBA', (cell_width * GRID_SIZE, cell_height * GRID_SIZE))
    for r in range(GRID_SIZE):
        for c in range(GRID_SIZE): background.paste(cell, (c * cell_width, r * cell_height))
    diamond = diamond_original.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
    num_to_show = random.randint(4, 6)
    for index in random.sample(safe_tiles, min(len(safe_tiles), num_to_show)):
        r, c = index // GRID_SIZE, index % GRID_SIZE
        background.paste(diamond, (c * cell_width, r * cell_height), diamond)
    buffer = io.BytesIO(); background.save(buffer, format="PNG"); buffer.seek(0)
    return buffer

def is_user_premium(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_activation_info = context.bot_data.get('user_activation_info', {});
    if user_id not in user_activation_info: return False
    info = user_activation_info[user_id]; key, activated_at = info['key'], info['activated_at']
    if key == ADMIN_ACTIVATION_KEY: return True
    all_keys = context.bot_data.get('activation_keys', {}); duration_days = all_keys.get(key)
    if duration_days is None: return False
    return datetime.now() < (activated_at + timedelta(days=duration_days))

async def send_guide_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, image_url: str, caption: str, reply_markup=None):
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(image_url); response.raise_for_status()
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=response.content, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except httpx.RequestError as e:
            logger.error(f"Failed to fetch guide photo from {image_url}: {e}")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# ==============================================================================
# 2. Admin Panel & Features
# ==============================================================================
def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_user.id in context.bot_data.get('admin_users', set())
async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE, query: Update.callback_query = None):
    if not is_admin(update, context): return
    keyboard = [[InlineKeyboardButton("🔑 Manage Keys", callback_data="admin_manage_keys")], [InlineKeyboardButton("👤 Active Users", callback_data="admin_active_users")], [InlineKeyboardButton("📣 Broadcast Message", callback_data="admin_broadcast")]]
    message_text = "👑 <b>Admin Panel</b>"
    if query: await query.answer(); await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else: await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
async def admin_manage_keys_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    activation_keys, user_activation_info = context.bot_data.setdefault('activation_keys', {}), context.bot_data.setdefault('user_activation_info', {})
    active_keys = {info['key'] for info in user_activation_info.values()}
    available_keys = {key: duration for key, duration in activation_keys.items() if key not in active_keys}
    keyboard = [[InlineKeyboardButton("➕ Create New Key", callback_data="admin_create_key")]]
    if available_keys:
        for key, duration in available_keys.items(): keyboard.append([InlineKeyboardButton(f"{key} ({duration} days)", callback_data="noop"), InlineKeyboardButton("🗑️", callback_data=f"admin_delete_key_{key}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_back")])
    await query.edit_message_text("<b>🔑 Key Management</b>\n\nHere are the currently available (inactive) keys.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
async def admin_active_users_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); user_activation_info = context.bot_data.get('user_activation_info', {})
    keyboard, active_users = [], {uid: info for uid, info in user_activation_info.items() if is_user_premium(uid, context)}
    if not active_users: keyboard.append([InlineKeyboardButton("No active users found.", callback_data="noop")])
    else:
        for user_id, info in active_users.items(): keyboard.append([InlineKeyboardButton(f"ID: {user_id} | Key: {info['key']}", callback_data="noop"), InlineKeyboardButton("💬 Msg", callback_data=f"admin_dm_{user_id}"), InlineKeyboardButton("🚪 Logout", callback_data=f"admin_logout_{user_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin_back")])
    await query.edit_message_text("👤 <b>Active Users Management</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
async def admin_force_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); user_id_to_logout = int(query.data.split('_')[2]); user_activation_info = context.bot_data.setdefault('user_activation_info', {})
    if user_id_to_logout in user_activation_info:
        key = user_activation_info.pop(user_id_to_logout)['key']; logger.info(f"Admin forced logout for user {user_id_to_logout} who was using key {key}.")
    await admin_active_users_view(update, context)
async def admin_ask_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); user_id_to_message = int(query.data.split('_')[2]); context.user_data['dm_user_id'] = user_id_to_message
    await query.message.reply_text(f"Send the message you want to deliver to user <code>{user_id_to_message}</code>.", parse_mode=ParseMode.HTML); return ADMIN_AWAIT_DIRECT_MESSAGE
async def admin_send_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = context.user_data.pop('dm_user_id')
    try:
        await context.bot.send_message(chat_id=user_id, text=f"🔔 <b>A message from the admin:</b>\n\n{update.message.text_html}", parse_mode=ParseMode.HTML)
        await update.message.reply_text("✅ Message sent successfully.")
    except (Forbidden, BadRequest) as e: await update.message.reply_text(f"❌ Failed to send message: {e}")
    await admin_panel_command(update, context); return ConversationHandler.END
async def admin_delete_key_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    key_to_delete = query.data.split('_', 2)[2]; activation_keys = context.bot_data.setdefault('activation_keys', {})
    if key_to_delete in activation_keys:
        del activation_keys[key_to_delete]; logger.info(f"Admin {query.from_user.id} fast-deleted key {key_to_delete}")
    await admin_manage_keys_view(update, context)
async def admin_ask_for_new_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.edit_message_text("Enter the name for the new key:"); return ADMIN_AWAIT_NEW_KEY
async def admin_get_key_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['new_key_name'] = update.message.text.strip().upper(); await update.message.reply_text("Enter the key's duration in <b>days</b>:", parse_mode=ParseMode.HTML); return ADMIN_AWAIT_KEY_DURATION
async def admin_save_timed_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: duration = int(update.message.text);
    except (ValueError, TypeError): await update.message.reply_text("Invalid number. Please try again."); return ADMIN_AWAIT_KEY_DURATION
    key_name = context.user_data.pop('new_key_name'); activation_keys = context.bot_data.setdefault('activation_keys', {})
    activation_keys[key_name] = duration
    await update.message.reply_text(f"✅ Key <code>{key_name}</code> created for <b>{duration} days</b>.", parse_mode=ParseMode.HTML)
    await admin_panel_command(update, context); return ConversationHandler.END
async def admin_ask_for_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); await query.edit_message_text("Send the message to broadcast:"); return ADMIN_AWAIT_BROADCAST_MESSAGE
async def admin_send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_to_send = update.message.text_html; user_activation_info = context.bot_data.get('user_activation_info', {}); active_user_ids = list(user_activation_info.keys())
    success_count, failure_count = 0, 0
    await update.message.reply_text(f"Starting broadcast to {len(active_user_ids)} users...")
    for user_id in active_user_ids:
        try: await context.bot.send_message(chat_id=user_id, text=message_to_send, parse_mode=ParseMode.HTML); success_count += 1
        except (Forbidden, BadRequest): failure_count += 1
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"📢 <b>Broadcast Complete!</b>\nSent: {success_count} | Failed: {failure_count}", parse_mode=ParseMode.HTML)
    return ConversationHandler.END
async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("Admin action cancelled."); await admin_panel_command(update, context); return ConversationHandler.END

# ==============================================================================
# 3. New Streamlined User Flow
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton("Click Here To Start 🚀", callback_data="start_prediction_flow")]]
    await update.message.reply_text("Start STAKE MINES Predictor 💣", reply_markup=InlineKeyboardMarkup(keyboard))

async def choose_mines_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """This is the single, robust entry point for the entire user flow."""
    query = update.callback_query; await query.answer()
    await query.message.delete()
    buttons = [InlineKeyboardButton(str(i), callback_data=f"mine_{i}") for i in range(3, 25)];
    keyboard = [[button] for button in buttons]
    await query.message.reply_text("Choose Mines Number From 3-24 ⬇️", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_MINES

async def get_mine_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); mine_count = int(query.data.split('_')[1]); context.user_data['mine_count'] = mine_count
    await query.message.delete()
    await send_guide_photo(update, context, SERVER_SEED_GUIDE_URL, "Get the <b>Server Seed</b> from the game and paste it below:")
    return AWAIT_SERVER_SEED

async def get_server_seed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id - 1)
    except BadRequest: pass
    await update.message.delete()
    context.user_data['server_seed'] = update.message.text; context.user_data['client_seed'] = "0" * 64
    await send_guide_photo(update, context, BET_AMOUNT_GUIDE_URL, "Great! Now get the <b>Bet Amount</b> and paste it below:")
    return AWAIT_BET_AMOUNT

async def get_bet_amount_and_check_activation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id - 1)
    except BadRequest: pass
    await update.message.delete()
    context.user_data['bet_amount'] = update.message.text;
    if is_user_premium(update.effective_user.id, context):
        await run_prediction_logic(update, context); return ConversationHandler.END
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="❗<b>Activation Required</b>\nYour key is invalid or has expired. Please enter a key:", parse_mode=ParseMode.HTML); return AWAIT_ACTIVATION_KEY

async def process_activation_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id - 1)
    except BadRequest: pass
    await update.message.delete()
    user, key = update.effective_user, update.message.text.strip().upper(); activation_keys = context.bot_data.setdefault('activation_keys', {}); user_activation_info = context.bot_data.setdefault('user_activation_info', {}); admin_users = context.bot_data.setdefault('admin_users', set())
    if key == ADMIN_ACTIVATION_KEY:
        admin_users.add(user.id); user_activation_info[user.id] = {'key': ADMIN_ACTIVATION_KEY, 'activated_at': datetime.now()}; logger.info(f"User {user.id} activated as ADMIN.")
        await run_prediction_logic(update, context); return ConversationHandler.END
    active_keys_in_use = {info['key'] for info in user_activation_info.values()}
    if key in active_keys_in_use: await context.bot.send_message(chat_id=user.id, text="❌ <b>Error!</b> Key is already in use.", parse_mode=ParseMode.HTML); return AWAIT_ACTIVATION_KEY
    if key in activation_keys:
        user_activation_info[user.id] = {'key': key, 'activated_at': datetime.now()}; logger.info(f"User {user.id} activated with key {key}.")
        await run_prediction_logic(update, context); return ConversationHandler.END
    else:
        buy_button = [[InlineKeyboardButton("Buy It From Here 🚀", url=f"https://t.me/{DEV_USERNAME.lstrip('@')}")]]
        await context.bot.send_message(chat_id=user.id, text="❌ <b>Error!</b> The key is invalid.", reply_markup=InlineKeyboardMarkup(buy_button), parse_mode=ParseMode.HTML); return AWAIT_ACTIVATION_KEY

async def run_prediction_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    loading_msg = await context.bot.send_message(chat_id=chat_id, text="<i>Generating prediction...</i>", parse_mode=ParseMode.HTML)
    s_seed, c_seed, m_count = context.user_data['server_seed'], context.user_data['client_seed'], context.user_data['mine_count']
    safe_tiles = provably_fair_mines(s_seed, c_seed, random.randint(1, 10000), m_count)
    if not safe_tiles: await loading_msg.edit_text("⚠️ <b>Prediction failed</b>", parse_mode=ParseMode.HTML); return
    image_buffer = await generate_prediction_image(safe_tiles)
    await loading_msg.delete()
    if image_buffer: await context.bot.send_photo(chat_id=chat_id, photo=image_buffer, caption=f"💎 <b>Prediction Ready!</b> ({m_count}-mine game).", parse_mode=ParseMode.HTML)
    else: await context.bot.send_message(chat_id=chat_id, text="⚠️ <b>Error:</b> Could not generate image.", parse_mode=ParseMode.HTML)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query: await update.callback_query.message.delete()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="<b>Action Cancelled.</b>", parse_mode=ParseMode.HTML)
    await start(update, context); return ConversationHandler.END

# ==============================================================================
# 4. Main Bot Setup
# ==============================================================================
def main() -> None:
    data_path = "bot_data.pickle"
    logger.info(f"Using persistence file at: {data_path}")
    persistence = PicklePersistence(filepath=data_path)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
    
    if 'activation_keys' not in application.bot_data or not application.bot_data['activation_keys']:
        logger.info("No existing keys found. Populating with initial default keys.")
        application.bot_data['activation_keys'] = INITIAL_TIMED_KEYS.copy()
    application.bot_data.setdefault('user_activation_info', {}); application.bot_data.setdefault('admin_users', set())

    # Unified User Conversation Handler
    user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(choose_mines_handler, pattern="^start_prediction_flow$")],
        states={
            CHOOSE_MINES: [CallbackQueryHandler(get_mine_count, pattern="^mine_")],
            AWAIT_SERVER_SEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_server_seed)],
            AWAIT_BET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bet_amount_and_check_activation)],
            AWAIT_ACTIVATION_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_activation_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation), CommandHandler("start", start)],
        per_user=True,
    )
    
    # Admin conversations
    create_key_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_ask_for_new_key, pattern="^admin_create_key$")], states={ADMIN_AWAIT_NEW_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_key_name)], ADMIN_AWAIT_KEY_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_save_timed_key)]}, fallbacks=[CommandHandler("cancel", admin_cancel)], per_user=True)
    broadcast_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_ask_for_broadcast, pattern="^admin_broadcast$")], states={ADMIN_AWAIT_BROADCAST_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_broadcast)]}, fallbacks=[CommandHandler("cancel", admin_cancel)], per_user=True)
    direct_message_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_ask_direct_message, pattern="^admin_dm_")], states={ADMIN_AWAIT_DIRECT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_send_direct_message)]}, fallbacks=[CommandHandler("cancel", admin_cancel)], per_user=True)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel_command))
    application.add_handler(user_conv)
    application.add_handler(create_key_conv)
    application.add_handler(broadcast_conv)
    application.add_handler(direct_message_conv)
    
    application.add_handler(CallbackQueryHandler(admin_manage_keys_view, pattern="^admin_manage_keys$"))
    application.add_handler(CallbackQueryHandler(admin_active_users_view, pattern="^admin_active_users$"))
    application.add_handler(CallbackQueryHandler(admin_delete_key_fast, pattern="^admin_delete_key_"))
    application.add_handler(CallbackQueryHandler(admin_force_logout, pattern="^admin_logout_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: admin_panel_command(u, c, query=u.callback_query), pattern="^admin_back$"))
    
    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
