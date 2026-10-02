import os
import json
import sqlite3
import requests
import threading
import urllib.parse
import re
import base64
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

# ترجمه متن فارسی به پرامپت دقیق انگلیسی جهت ارسال به Imagen 3
def translate_to_english_prompt(prompt, style_mode="auto"):
    cleaned_prompt = clean_persian_colloquial(prompt)
    if not GEMINI_API_KEY:
        return cleaned_prompt
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        style_desc = "photorealistic high quality photo" if style_mode == "realistic" else "fantasy digital art" if style_mode == "fantasy" else "high quality image"
        
        system_prompt = (
            f"Translate this Persian prompt to a simple, clear English prompt for image generation ({style_desc}). "
            f"Persian text: '{cleaned_prompt}'\n"
            f"Return ONLY the direct English translation without explanations or quotes."
        )
        
        response = model.generate_content(system_prompt)
        if response and response.text:
            return response.text.strip().replace('"', '')
    except Exception as e:
        print(f"Translation Error: {e}", flush=True)
    return cleaned_prompt

# ساخت عکس مستقیم با مدل رسمی گوگل (Imagen 3)
def generate_image_with_google_imagen(english_prompt):
    if not GEMINI_API_KEY:
        return None, "کلید GEMINI_API_KEY تنظیم نشده است."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:generateImages?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "prompt": english_prompt,
        "config": {
            "numberOfImages": 1,
            "outputMimeType": "image/jpeg",
            "aspectRatio": "1:1"
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code == 200:
            data = response.json()
            if "generatedImages" in data and len(data["generatedImages"]) > 0:
                img_b64 = data["generatedImages"][0]["image"]["imageBytes"]
                img_bytes = base64.b64decode(img_b64)
                return img_bytes, None
            else:
                return None, "گوگل عکسی تولید نکرد."
        else:
            err_text = response.text
            print(f"Google Imagen API Error ({response.status_code}): {err_text}", flush=True)
            if response.status_code == 429:
                return None, "سقف درخواست‌های مجانی روزانه گوگل (Quota) پر شده است. لطفاً بعداً تلاش کنید."
            elif response.status_code == 400:
                return None, "درخواست شما توسط قوانین محتوایی و ایمنی گوگل مسدود شد."
            return None, f"خطای سرور گوگل (کد {response.status_code})"
    except Exception as e:
        print(f"Imagen Request Exception: {e}", flush=True)
        return None, f"خطا در ارتباط با سرور تصویرساز گوگل: {e}"

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

# پردازش ساخت و ارسال عکس مستقیم گوگل
def handle_image_request(chat_id, user_prompt, style_mode="auto"):
    send_message(chat_id, "🎨 Google Imagen 3 در حال ساخت تصویر مستقیم است...")
    
    # ۱. ترجمه توسط جمنای
    english_prompt = translate_to_english_prompt(user_prompt, style_mode)
    
    # ۲. ارسال مستقیم به موتور Imagen 3 گوگل
    photo_bytes, error_msg = generate_image_with_google_imagen(english_prompt)
    
    if photo_bytes:
        send_photo(chat_id, photo_bytes, caption=f"✨ ساخته شده مستقیماً توسط Google Imagen 3:\n{user_prompt}")
    else:
        send_message(chat_id, f"❌ ساخت تصویر ناموفق بود:\n{error_msg}")

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
            "سلام! به ربات تصویرساز مستقیم گوگل خوش آمدید 🤖✨\n\n"
            "تصاویر مستقیماً توسط مدل Imagen 3 گوگل ساخته می‌شوند:\n"
            "📸 `عکس واقعی: نیمار و مسی`\n"
            "🎨 `عکس فانتزی: ماشین اسپرت در تهران`\n"
            "🖼 `عکس: یک کلبه جنگلی در برف`"
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
        handle_image_request(chat_id, prompt, style_mode="realistic")
        return

    elif lower_text.startswith("عکس فانتزی:") or lower_text.startswith("فانتزی:"):
        prompt = text.split(":", 1)[1].strip()
        handle_image_request(chat_id, prompt, style_mode="fantasy")
        return

    elif lower_text.startswith("عکس:") or lower_text.startswith("عکس جمنای:") or lower_text.startswith("جمنای:"):
        prompt = text.split(":", 1)[1].strip() if ":" in text else text
        handle_image_request(chat_id, prompt, style_mode="auto")
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
