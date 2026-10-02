import os
import json
import sqlite3
import requests
import threading
import urllib.parse
import re
from flask import Flask, request, jsonify
import google.generativeai as genai
from PIL import Image
import io
from datetime import datetime, timedelta

app = Flask(__name__)

# ۱. دریافت متغیرهای محیطی
BALE_TOKEN = os.environ.get("BALE_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID")

BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}" if BALE_TOKEN else ""

PROCESSED_UPDATES = set()
MAX_CACHE_SIZE = 2000

SYSTEM_INSTRUCTION = "پاسخ را فقط و فقط به زبان فارسی، روان و صمیمی بنویس."

CACHED_MODELS = ['gemini-1.5-flash', 'gemini-1.5-pro']

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("--- Gemini API configured successfully ---", flush=True)
    except Exception as e:
        print(f"!!! Error configuring Gemini API: {e} !!!", flush=True)

# پاکسازی متون اضافی
def clean_persian_colloquial(prompt):
    words_to_remove = ['عکس واقعی:', 'عکس فانتزی:', 'عکس جمنای:', 'عکس:', 'بزن', 'بیار', 'برام بیار', 'بکش', 'درست کن', 'بفرست']
    cleaned = prompt
    for w in words_to_remove:
        cleaned = cleaned.replace(w, '')
    return cleaned.strip()

# پاکسازی پاسخ‌های متنی
def clean_bot_response(text):
    if not text:
        return ""
    lines = text.strip().split('\n')
    filtered_lines = []
    for line in lines:
        l = line.strip().lower()
        if any(keyword in l for keyword in ['user input', 'constraint', 'persona', 'final response']):
            continue
        filtered_lines.append(line)
    return '\n'.join(filtered_lines).strip()

# بازنویسی مستقیم و دقیق پرامپت توسط جمنای
def enhance_prompt_with_gemini(prompt, style_mode="auto"):
    cleaned_prompt = clean_persian_colloquial(prompt)
    if not GEMINI_API_KEY:
        return cleaned_prompt
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        system_prompt = (
            f"You are a direct translator for an AI image generator. "
            f"Translate the following Persian request into a very simple, direct, high-quality English image prompt. "
            f"Focus strictly on the main subject and characters mentioned. Do not add background scenery unless requested.\n"
            f"User Request: {cleaned_prompt}\n"
            f"Style: {'Photorealistic, high detail photography' if style_mode == 'realistic' else 'Vibrant digital art style' if style_mode == 'fantasy' else 'High quality photo'}\n"
            f"Return ONLY the plain English prompt text."
        )
        
        response = model.generate_content(system_prompt)
        if response and response.text:
            enhanced = response.text.strip().replace('"', '')
            print(f"Direct Prompt Translation: '{prompt}' -> '{enhanced}'", flush=True)
            return enhanced
    except Exception as e:
        print(f"Prompt Enhancement Error: {e}", flush=True)
    return cleaned_prompt

# پاسخ متنی جمنای
def generate_gemini_response(contents):
    if not GEMINI_API_KEY:
        return "❌ کلید GEMINI_API_KEY تنظیم نشده است."

    try:
        model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=SYSTEM_INSTRUCTION)
        response = model.generate_content(contents)
        if response and hasattr(response, 'text') and response.text:
            return clean_bot_response(response.text)
    except Exception as e:
        return f"⚠️ خطا در دریافت پاسخ: {e}"

# پایگاه داده
DB_NAME = "users.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vip_users (
                user_id INTEGER PRIMARY KEY,
                expire_date TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}", flush=True)

init_db()

def is_vip(user_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT expire_date FROM vip_users WHERE user_id = ?", (user_id,))
        result = cursor.fetchone()
        conn.close()
        if result:
            expire_date = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
            if expire_date > datetime.now():
                return True
    except Exception as e:
        print(f"VIP check error: {e}", flush=True)
    return False

def add_vip_user(user_id, days):
    expire_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO vip_users (user_id, expire_date) VALUES (?, ?)", (user_id, expire_date))
    conn.commit()
    conn.close()
    return expire_date

# توابع بله
def send_message(chat_id, text):
    if not BALE_API_URL:
        return
    url = f"{BALE_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending message to Bale: {e}", flush=True)

def send_photo(chat_id, photo_bytes, caption=""):
    url = f"{BALE_API_URL}/sendPhoto"
    try:
        files = {'photo': ('image.jpg', photo_bytes, 'image/jpeg')}
        data = {'chat_id': chat_id, 'caption': caption}
        requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print(f"Error sending photo to Bale: {e}", flush=True)

def generate_and_send_image(chat_id, english_prompt, caption_text):
    try:
        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed=42"
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        img_res = requests.get(image_url, headers=headers, timeout=45)

        if img_res.status_code == 200:
            send_photo(chat_id, img_res.content, caption=caption_text)
        else:
            send_message(chat_id, f"❌ خطا در ساخت تصویر (کد: {img_res.status_code}).")
    except Exception as e:
        print(f"Image Generation Error: {e}", flush=True)
        send_message(chat_id, f"❌ خطا در سرور تصویرساز: {e}")

# پردازش پیام‌ها
def process_update_async(data):
    if not data or "message" not in data:
        return

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        welcome_msg = (
            "سلام! به ربات هوش مصنوعی خوش آمدید 🤖✨\n\n"
            "دستورات ساخت عکس:\n"
            "📸 `عکس واقعی: نیمار پیش هالند`\n"
            "🎨 `عکس فانتزی: بتمن در تهران`\n"
            "🖼 `عکس: ماشین اسپرت`"
        )
        send_message(chat_id, welcome_msg)
        return

    elif text == "/vip":
        vip_status = "فعال ✅" if is_vip(user_id) else "غیرفعال ❌"
        send_message(chat_id, f"وضعیت VIP شما: {vip_status}")
        return

    lower_text = text.lower()
    if lower_text.startswith("عکس واقعی:") or lower_text.startswith("واقعی:"):
        prompt = text.split(":", 1)[1].strip()
        send_message(chat_id, "📸 Gemini در حال تنظیم دقیق تصویر...")
        english_prompt = enhance_prompt_with_gemini(prompt, style_mode="realistic")
        generate_and_send_image(chat_id, english_prompt, f"📸 تصویر واقعی ساخته شده برای:\n{prompt}")
        return

    elif lower_text.startswith("عکس فانتزی:") or lower_text.startswith("فانتزی:"):
        prompt = text.split(":", 1)[1].strip()
        send_message(chat_id, "🎨 Gemini در حال ساخت مدل فانتزی...")
        english_prompt = enhance_prompt_with_gemini(prompt, style_mode="fantasy")
        generate_and_send_image(chat_id, english_prompt, f"🎨 تصویر فانتزی ساخته شده برای:\n{prompt}")
        return

    elif lower_text.startswith("عکس:") or lower_text.startswith("عکس جمنای:") or lower_text.startswith("جمنای:"):
        prompt = text.split(":", 1)[1].strip() if ":" in text else text
        send_message(chat_id, "🤖 Gemini در حال پردازش تصویر...")
        english_prompt = enhance_prompt_with_gemini(prompt, style_mode="auto")
        generate_and_send_image(chat_id, english_prompt, f"🖼 تصویر ساخته شده برای:\n{prompt}")
        return

    if text:
        send_message(chat_id, "🤔 در حال تفکر...")
        answer = generate_gemini_response(text)
        send_message(chat_id, answer)

@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Server is Live!", 200

    data = request.get_json()
    if not data:
        return jsonify({"status": "ok"}), 200

    update_id = data.get("update_id")
    if update_id:
        if update_id in PROCESSED_UPDATES:
            return jsonify({"status": "already processed"}), 200
        PROCESSED_UPDATES.add(update_id)
        if len(PROCESSED_UPDATES) > MAX_CACHE_SIZE:
            PROCESSED_UPDATES.clear()

    threading.Thread(target=process_update_async, args=(data,)).start()
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
