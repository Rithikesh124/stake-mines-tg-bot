
import hmac, hashlib, random, io
from PIL import Image, ImageDraw
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# In-memory nonce tracking
user_nonces = {}

def generate_safe_tiles(server_seed, client_seed, nonce, mines_count):
    key = server_seed.encode()
    message = f"{client_seed}:{nonce}".encode()
    hash_hex = hmac.new(key, message, hashlib.sha256).hexdigest()
    random.seed(int(hash_hex[:16], 16))
    all_tiles = list(range(25))
    mine_tiles = random.sample(all_tiles, mines_count)
    safe_tiles = [i for i in all_tiles if i not in mine_tiles]
    safe_tile_ranges = {
        1: (5, 9),
        2: (4, 6),
        3: (4, 6),
        4: (3, 5),
        5: (3, 4),
        6: (2, 3),
        24: (1, 1),
    }
    low, high = safe_tile_ranges.get(mines_count, (1, 2))
    final_safe_tiles = random.sample(safe_tiles, random.randint(low, high))
    return final_safe_tiles

def draw_prediction_image(safe_tiles):
    grid_size = 5
    img_size = 500
    tile_size = img_size // grid_size
    img = Image.new("RGB", (img_size, img_size), "black")
    draw = ImageDraw.Draw(img)
    for i in range(25):
        x = (i % 5) * tile_size
        y = (i // 5) * tile_size
        color = "green" if i in safe_tiles else "gray"
        draw.rectangle([x+5, y+5, x+tile_size-5, y+tile_size-5], fill=color)
    return img

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send: /predict <server_seed> <client_seed> <bets_made> <mines>")

async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.message.from_user.id
        s_seed, c_seed, nonce, mines = context.args
        nonce = int(nonce)
        mines = int(mines)
        user_nonces[user_id] = (s_seed, c_seed, nonce + 1, mines)
        safe = generate_safe_tiles(s_seed, c_seed, nonce, mines)
        img = draw_prediction_image(safe)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await update.message.reply_photo(photo=buffer, caption=f"Predicted Safe Tiles: {safe}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def next_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.message.from_user.id
        if user_id not in user_nonces:
            await update.message.reply_text("Use /predict first.")
            return
        s_seed, c_seed, nonce, mines = user_nonces[user_id]
        safe = generate_safe_tiles(s_seed, c_seed, nonce, mines)
        user_nonces[user_id] = (s_seed, c_seed, nonce + 1, mines)
        img = draw_prediction_image(safe)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        await update.message.reply_photo(photo=buffer, caption=f"Next Predicted Safe Tiles: {safe}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

def run_bot():
    import os
    token = os.getenv("TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("predict", predict))
    app.add_handler(CommandHandler("next", next_prediction))
    app.run_polling()

if __name__ == "__main__":
    run_bot()
