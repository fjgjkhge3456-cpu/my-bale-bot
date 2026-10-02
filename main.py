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

# حافظه موقت برای جلوگیری از پیام‌های تکراری
PROCESSED_UPDATES = set()
MAX_CACHE_SIZE = 2000

# دستورالعمل سیستم
SYSTEM_INSTRUCTION = "پاسخ را فقط و فقط به زبان فارسی، روان و صمیمی بنویس."

# کش کردن مدل‌های جمنای
CACHED_MODELS = ['gemini-1.5-flash', 'gemini-1.5-pro']

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("--- Gemini API configured successfully ---", flush=True)
        try:
            fetched_models = []
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    clean_name = m.name.replace('models/', '')
                    if clean_name not in fetched_models:
                        fetched_models.append(clean_name)
            if fetched_models:
                CACHED_MODELS = fetched_models
            print(f"Loaded models on startup: {CACHED_MODELS}", flush=True)
        except Exception as e:
            print(f"Could not fetch list_models on startup: {e}", flush=True)
    except Exception as e:
        print(f"!!! Error configuring Gemini API: {e} !!!", flush=True)

# پاکسازی متون عامیانه فارسی مثل "بزن"، "بیار" و...
def clean_persian_colloquial(prompt):
    words_to_remove = ['بزن', 'بیار', 'برام بیار', 'بکش', 'درست کن', 'ساز', 'بفرست', 'لطفا']
    pattern = r'\b(' + '|'.join(words_to_remove) + r')\b'
    cleaned = re.sub(pattern, '', prompt).strip()
    return cleaned if cleaned else prompt

# پاکسازی هوشمند پاسخ‌های متنی جمنای
def clean_bot_response(text):
    if not text:
        return ""
    lines = text.strip().split('\n')
    filtered_lines = []
    for line in lines:
        l = line.strip().lower()
        if any(keyword in l for keyword in ['user input', 'constraint', 'persona', 'final response', 'option 1', 'meaning:', 'check against']):
            continue
        filtered_lines.append(line)
    
    result = '\n'.join(filtered_lines).strip()
    return result if result else text.strip()

# بازنویسی و تقویت پرامپت ساخت عکس توسط جمنای به انگلیسی دقیق
def enhance_prompt_with_gemini(prompt):
    cleaned_prompt = clean_persian_colloquial(prompt)
    if not GEMINI_API_KEY:
        return cleaned_prompt
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(
            f"Convert this image request into a concise, highly realistic, professional English image generation prompt. Ignore conversational Persian words. Return ONLY the English prompt text.\nRequest: {cleaned_prompt}"
        )
        if response and response.text:
            enhanced = response.text.strip().replace('"', '')
            print(f"Enhanced Prompt: '{prompt}' -> '{enhanced}'", flush=True)
            return enhanced
    except Exception as e:
        print(f"Prompt Enhancement Error: {e}", flush=True)
    return cleaned_prompt

# بازنویسی پرامپت برای ویرایش عکس
def generate_edit_prompt_with_gemini(image, user_instruction):
    cleaned_instruction = clean_persian_colloquial(user_instruction)
    if not GEMINI_API_KEY:
        return cleaned_instruction
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        query = (
            f"Analyze this image and the user's editing request: '{cleaned_instruction}'. "
            "Write a detailed English prompt describing the modified scene so an AI image generator can reconstruct it with the requested edits. "
            "Return ONLY the detailed English prompt without explanation or quotes."
        )
        response = model.generate_content([query, image])
        if response and response.text:
            enhanced = response.text.strip().replace('"', '')
            print(f"Image Edit Prompt: '{user_instruction}' -> '{enhanced}'", flush=True)
            return enhanced
    except Exception as e:
        print(f"Image Edit Prompt Error: {e}", flush=True)
    return cleaned_instruction

# تابع دریافت پاسخ متنی جمنای
def generate_gemini_response(contents):
    if not GEMINI_API_KEY:
        return "❌ کلید GEMINI_API_KEY در تنظیمات Render وارد نشده است."

    last_error = None
    for model_name in CACHED_MODELS:
        try:
            clean_name = model_name.replace('models/', '')
            model = genai.GenerativeModel(
                model_name=clean_name,
                system_instruction=SYSTEM_INSTRUCTION
            )
            response = model.generate_content(contents)
            
            if response and hasattr(response, 'text') and response.text:
                cleaned_text = clean_bot_response(response.text)
                return cleaned_text
        except Exception as e:
            last_error = e
            print(f"Model {model_name} failed: {e}", flush=True)
            continue

    return f"⚠️ خطا در دریافت پاسخ از مدل‌ها: {last_error}"

# ۲. پایگاه داده SQLite
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

# ۳. توابع ارتباط با API بله
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

def get_bale_file_bytes(file_id):
    try:
        res = requests.get(f"{BALE_API_URL}/getFile?file_id={file_id}", timeout=10)
        file_info = res.json()
        if file_info.get("ok"):
            file_path = file_info["result"]["file_path"]
            file_url = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{file_path}"
            img_res = requests.get(file_url, timeout=20)
            return img_res.content
    except Exception as e:
        print(f"Error downloading file: {e}", flush=True)
    return None

# تابع ساخت و ارسال عکس
def generate_and_send_image(chat_id, english_prompt, caption_text, model_type="flux"):
    try:
        encoded_prompt = urllib.parse.quote(english_prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model={model_type}&width=1024&height=1024&nologo=true"
        
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        img_res = requests.get(image_url, headers=headers, timeout=45)

        if img_res.status_code == 200:
            send_photo(chat_id, img_res.content, caption=caption_text)
        else:
            send_message(chat_id, f"❌ خطا در ساخت تصویر (کد: {img_res.status_code}). لطفاً مجدداً تلاش کنید.")
    except Exception as e:
        print(f"Image Generation Error: {e}", flush=True)
        send_message(chat_id, f"❌ خطا در ارتباط با سرور تصویرساز: {e}")

# ۴. پردازش پس‌زمینه پیام‌ها
def process_update_async(data):
    if not data or "message" not in data:
        return

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()

    # دستور /start
    if text == "/start":
        welcome_msg = (
            "سلام! به ربات هوش مصنوعی خوش آمدید 🤖✨\n\n"
            "امکانات ربات:\n"
            "1️⃣ چت متنی با Gemini (سوال خود را بنویسید)\n"
            "2️⃣ ساخت عکس واقعی با فلاکس (مثال: عکس فلاکس: ماشین بی ام و)\n"
            "3️⃣ ساخت عکس فانتزی با جمنای (مثال: عکس جمنای: ماشین بی ام و)\n"
            "4️⃣ ویرایش عکس (عکس بفرستید و بنویسید: ویرایش: رنگشو قرمز کن)\n"
            "5️⃣ تحلیل عکس (عکس بفرستید و سوال بپرسید)\n"
            "6️⃣ وضعیت اشتراک با دستور /vip"
        )
        send_message(chat_id, welcome_msg)
        return

    # دستور /vip
    elif text == "/vip":
        vip_status = "فعال ✅" if is_vip(user_id) else "غیرفعال ❌"
        vip_msg = (
            f"اطلاعات حساب VIP شما:\n"
            f"آیدی عددی شما: {user_id}\n"
            f"وضعیت اشتراک: {vip_status}\n\n"
            f"💳 جهت خرید اشتراک VIP:\n"
            f"مبلغ: ۵۰,۰۰۰ تومان\n"
            f"شماره کارت: 6037-9999-9999-9999 (به نام مدیر)\n\n"
            f"پس از واریز، فیش را برای پشتیبانی بفرستید."
        )
        send_message(chat_id, vip_msg)
        return

    # دستور ارتقا به VIP توسط مدیر
    elif text.startswith("/addvip"):
        if str(user_id) == str(ADMIN_ID):
            try:
                parts = text.split()
                target_user_id = int(parts[1])
                days = int(parts[2])
                exp_date = add_vip_user(target_user_id, days)
                send_message(chat_id, f"✅ کاربر {target_user_id} به مدت {days} روز VIP شد.\nانقضا: {exp_date}")
            except Exception as e:
                send_message(chat_id, "فرمت اشتباه است! مثال صحیح:\n/addvip 123456789 30")
        else:
            send_message(chat_id, "شما دسترسی مدیریتی ندارید.")
        return

    # ۱. ساخت تصویر با مدل جمنای (Turbo)
    if text.lower().startswith("عکس جمنای:") or text.lower().startswith("جمنای:"):
        prompt = text.split(":", 1)[1].strip()
        send_message(chat_id, "🎨 در حال ساخت تصویر با مدل Gemini...")
        english_prompt = enhance_prompt_with_gemini(prompt)
        generate_and_send_image(chat_id, english_prompt, f"🖼 تصویر ساخته شده با Gemini:\n{prompt}", model_type="turbo")
        return

    # ۲. ساخت تصویر با مدل فلاکس (Flux)
    elif text.lower().startswith("عکس فلاکس:") or text.lower().startswith("فلاکس:") or text.lower().startswith("عکس:") or text.lower().startswith("image:"):
        prompt = text.split(":", 1)[1].strip() if ":" in text else text
        send_message(chat_id, "🎨 در حال ساخت تصویر با مدل Flux...")
        english_prompt = enhance_prompt_with_gemini(prompt)
        generate_and_send_image(chat_id, english_prompt, f"🖼 تصویر ساخته شده با Flux:\n{prompt}", model_type="flux")
        return

    # دریافت و تحلیل یا ویرایش تصویر ارسالی
    if "photo" in message:
        photos = message["photo"]
        file_id = photos[-1]["file_id"]
        caption = message.get("caption", "").strip()

        photo_bytes = get_bale_file_bytes(file_id)
        if not photo_bytes:
            send_message(chat_id, "خطا در دریافت فایل تصویر از بله.")
            return

        image = Image.open(io.BytesIO(photo_bytes))

        # ویرایش عکس
        if caption.lower().startswith("ویرایش:") or caption.lower().startswith("ادیت:"):
            edit_instruction = caption.split(":", 1)[1].strip()
            send_message(chat_id, "🎨 Gemini در حال تحلیل تصویر و اعمال ویرایش درخواست‌شده است...")
            
            english_edit_prompt = generate_edit_prompt_with_gemini(image, edit_instruction)
            generate_and_send_image(chat_id, english_edit_prompt, f"✏️ تصویر ویرایش شده با دستور:\n{edit_instruction}", model_type="flux")
            return

        # تحلیل عکس
        else:
            query_caption = caption if caption else "این عکس را به دقت تحلیل و توصیف کن."
            send_message(chat_id, "🔍 در حال تحلیل تصویر با Gemini...")
            try:
                answer = generate_gemini_response([query_caption, image])
                send_message(chat_id, answer)
            except Exception as e:
                print(f"Gemini Vision Error: {e}", flush=True)
                send_message(chat_id, f"خطا در تحلیل تصویر:\n{e}")
            return

    # چت متنی با Gemini
    if text:
        send_message(chat_id, "🤔 در حال تفکر...")
        try:
            answer = generate_gemini_response(text)
            send_message(chat_id, answer)
        except Exception as e:
            print(f"!!! CRITICAL GEMINI ERROR: {e} !!!", flush=True)
            send_message(chat_id, f"متأسفانه مشکلی پیش آمد:\n{e}")

# ۵. دریافت درخواست از وب‌هوک
@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Server is Live!", 200

    data = request.get_json()

    if not data:
        return jsonify({"status": "ok"}), 200

    # جلوگیری از پردازش درخواست‌های تکراری
    update_id = data.get("update_id")
    if update_id:
        if update_id in PROCESSED_UPDATES:
            print(f"Skipping duplicate update_id: {update_id}", flush=True)
            return jsonify({"status": "already processed"}), 200
        
        PROCESSED_UPDATES.add(update_id)
        if len(PROCESSED_UPDATES) > MAX_CACHE_SIZE:
            PROCESSED_UPDATES.clear()

    # ارجاع پردازش به یک Thread مجزا
    threading.Thread(target=process_update_async, args=(data,)).start()

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
