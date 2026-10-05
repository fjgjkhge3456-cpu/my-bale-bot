import os
import json
import sqlite3
import requests
import threading
import urllib.parse
import re
import base64
import random
from flask import Flask, request, jsonify
import google.generativeai as genai
from PIL import Image
import io
from datetime import datetime, timedelta

app = Flask(__name__)

# ۱. دریافت تنظیمات محیطی
BALE_TOKEN = os.environ.get("BALE_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID")

BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}" if BALE_TOKEN else ""
BALE_FILE_URL = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}" if BALE_TOKEN else ""

PROCESSED_UPDATES = set()
MAX_CACHE_SIZE = 2000

# دستور سیستم فارسی صمیمی
SYSTEM_INSTRUCTION = "پاسخ را فقط و فقط به زبان فارسی، روان و صمیمی بنویس."

# کش کردن نام مدل کارآمد جهت جلوگیری از کندی چت
CACHED_MODEL_NAME = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("--- Gemini API configured successfully ---", flush=True)
    except Exception as e:
        print(f"!!! Error configuring Gemini API: {e} !!!", flush=True)

# پاکسازی متون اضافی ورودی
def clean_persian_colloquial(prompt):
    words_to_remove = [
        'عکس واقعی:', 'عکس فانتزی:', 'عکس جمنای:', 'عکس فلوکس:', 
        'واقعی:', 'فانتزی:', 'جمنای:', 'فلوکس:', 'عکس:', 'ویرایش:',
        'بزن', 'بیار', 'برام بیار', 'بکش', 'درست کن', 'بفرست'
    ]
    cleaned = prompt
    for w in words_to_remove:
        cleaned = cleaned.replace(w, '')
    return cleaned.strip()

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

# سیستم هوشمند و سریع دریافت مدل‌های کاری اکانت شما (جایگزین ارور 404)
def get_working_robust_model():
    """
    Get a robust model name dynamically or via fallback list to prevent 404 errors.
    Caches the result to prevent slow chat.
    """
    global CACHED_MODEL_NAME
    if CACHED_MODEL_NAME:
        return CACHED_MODEL_NAME

    # امتحان سریع مدل‌های استاندارد و سریع (Flash/Pro)
    standard_names = ['models/gemini-1.5-flash', 'gemini-1.5-flash', 'models/gemini-pro']
    
    for m_id in standard_names:
        try:
            m = genai.GenerativeModel(m_id)
            # تست مدل با یه درخواست کوچیک
            res = m.generate_content(" ping ")
            if res:
                CACHED_MODEL_NAME = m_id
                print(f"--- Fast Cached model found: {m_id} ---", flush=True)
                return CACHED_MODEL_NAME
        except Exception:
            continue

    # اگر نشد، دریافت لیست مدل‌های فعال از خود گوگل
    try:
        supported_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                supported_models.append(m.name)
        
        # انتخاب اولین مدل فلاش موجود
        for m_name in supported_models:
            if 'flash' in m_name.lower():
                CACHED_MODEL_NAME = m_name
                return CACHED_MODEL_NAME
        
        # اگر فلاش نبود، اولین مدل چت
        if supported_models:
            CACHED_MODEL_NAME = supported_models[0]
            return CACHED_MODEL_NAME
            
    except Exception as e:
        print(f"List models error: {e}", flush=True)

    # در صورت شکست کلchecks، پیش‌فرض روی فلاش
    CACHED_MODEL_NAME = 'gemini-1.5-flash'
    return CACHED_MODEL_NAME

# فراخوانی سریع جمنای برای چت عادی متنی (دست نخورده)
def call_gemini_dynamic(contents, use_system_instruction=True):
    if not GEMINI_API_KEY:
        return None, "❌ کلید GEMINI_API_KEY تنظیم نشده است."

    try:
        # ۱. دریافت سریع اسم مدل از کش
        model_name = get_working_robust_model()
        
        kwargs = {}
        if use_system_instruction:
            kwargs['system_instruction'] = SYSTEM_INSTRUCTION
        
        model = genai.GenerativeModel(model_name, **kwargs)
        response = model.generate_content(contents)
        
        if response and hasattr(response, 'text') and response.text:
            return clean_bot_response(response.text), None
        return None, "⚠️ پاسخی دریافت نشد."
    except Exception as e:
        # در صورت خطا، کش بازنشانی می‌شود
        global CACHED_MODEL_NAME
        CACHED_MODEL_NAME = None
        return None, f"⚠️ خطای جمنای:\n{str(e)}"

# دانلود فایل از سرور بله
def download_bale_file(file_id):
    try:
        url = f"{BALE_API_URL}/getFile?file_id={file_id}"
        res = requests.get(url, timeout=15)
        if res.status_code == 200:
            file_path = res.json().get("result", {}).get("file_path")
            if file_path:
                dl_url = f"{BALE_FILE_URL}/{file_path}"
                img_res = requests.get(dl_url, timeout=30)
                if img_res.status_code == 200:
                    return img_res.content
    except Exception as e:
        print(f"Error downloading photo: {e}", flush=True)
    return None

# ساخت تصویر با Flux
def generate_image_flux(english_prompt):
    try:
        encoded_prompt = urllib.parse.quote(english_prompt)
        rand_seed = random.randint(1, 999999)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed={rand_seed}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(image_url, headers=headers, timeout=45)
        if res.status_code == 200:
            return res.content, None
        return None, f"خطای سرور تصویرساز (کد {res.status_code})"
    except Exception as e:
        return None, f"خطای ارتباط: {e}"

# ساخت تصویر با Google Imagen
def generate_image_google_imagen(english_prompt):
    if not GEMINI_API_KEY:
        return None, "کلید API تنظیم نشده است."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:generateImages?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "prompt": english_prompt,
        "config": {"numberOfImages": 1, "outputMimeType": "image/jpeg", "aspectRatio": "1:1"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=45)
        if response.status_code == 200:
            data = response.json()
            if "generatedImages" in data and len(data["generatedImages"]) > 0:
                img_b64 = data["generatedImages"][0]["image"]["imageBytes"]
                return base64.b64decode(img_b64), None
        return None, f"عدم دسترسی گوگلی"
    except Exception as e:
        return None, str(e)

# ترجمه و بهینه‌سازی پرامپت فارسی توسط جمنای
def translate_and_enhance_prompt(prompt, style_mode="auto"):
    cleaned_prompt = clean_persian_colloquial(prompt)
    style_desc = (
        "photorealistic high resolution realistic photography, 8k" 
        if style_mode == "realistic" 
        else "vibrant fantasy digital art illustration, 3d render" 
        if style_mode == "fantasy" 
        else "high quality clear photo"
    )
    system_prompt = (
        f"Translate the following Persian text into a clear English image prompt Focusing ONLY on subjects requested: '{cleaned_prompt}'. "
        f"Style constraint: {style_desc}. Return ONLY plain English prompt text without quotes."
    )
    translated, err = call_gemini_dynamic(system_prompt, use_system_instruction=False)
    if translated:
        return translated.replace('"', '').strip()
    return cleaned_prompt

# *** اصلاح اصلی: تحلیل تصویر واقعی توسط Vision جمنای ***
def analyze_image_with_gemini(photo_bytes, user_question=""):
    try:
        # ۱. باز کردن عکس با PIL
        img = Image.open(io.BytesIO(photo_bytes))
        
        # ۲. انتخاب مدل Vision (فلش سریع‌ترین و بهترین گزینه چندمنظوره است)
        # Using flash is essential for vision tasks, especially via proxying libraries.
        model_vision = genai.GenerativeModel('gemini-1.5-flash', system_instruction=SYSTEM_INSTRUCTION)
        
        # ۳. دستور چت شامل سوال کاربر یا دستور پیش‌فرض
        prompt = user_question if user_question else "این تصویر را به دقت تحلیل کن و توضیحات کاملی ارائه بده."
        
        # ۴. فراخوانی تحلیل (ارسال لیست شامل متن دستور و عکس)
        # We send both prompt text and the raw Image object.
        response = model_vision.generate_content([prompt, img])
        
        if response and hasattr(response, 'text') and response.text:
            return clean_bot_response(response.text)
    except Exception as e:
        return f"⚠️ خطا در پردازش تصویر: {e}"
    return "متأسفانه تصویری تحلیل نشد."

# ویرایش تصویر
def edit_image_with_gemini(photo_bytes, edit_instruction):
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        prompt_prep = (
            f"Analyze this input image carefully and describe it. Then incorporate the user's requested edit: '{edit_instruction}'. "
            f"Output ONLY a detailed English image generation prompt describing the updated final image."
        )
        # We need a prompt for an edit task, which is text-only but based on an image.
        new_prompt, err = call_gemini_dynamic([prompt_prep, img], use_system_instruction=False)
        if new_prompt:
            return generate_image_flux(new_prompt)
        return None, err
    except Exception as e:
        return None, f"خطا در پردازش ویرایش: {e}"

# دیتابیس VIP
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

# توابع ارسال پیام بله
def send_message(chat_id, text):
    if not BALE_API_URL:
        return
    url = f"{BALE_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending message: {e}", flush=True)

def send_photo(chat_id, photo_bytes, caption=""):
    url = f"{BALE_API_URL}/sendPhoto"
    try:
        files = {'photo': ('image.jpg', photo_bytes, 'image/jpeg')}
        data = {'chat_id': chat_id, 'caption': caption}
        requests.post(url, data=data, files=files, timeout=30)
    except Exception as e:
        print(f"Error sending photo: {e}", flush=True)

# مدیریت ساخت تصویر
def process_image_command(chat_id, user_prompt, engine_choice="auto", style_mode="auto"):
    send_message(chat_id, "🤖 Gemini در حال پردازش و ساخت تصویر است...")
    english_prompt = translate_and_enhance_prompt(user_prompt, style_mode)

    if engine_choice == "flux":
        photo_bytes, err = generate_image_flux(english_prompt)
        if photo_bytes:
            send_photo(chat_id, photo_bytes, caption=f"🖼 تصویر ساخته شده با Flux:\n{user_prompt}")
        else:
            send_message(chat_id, f"❌ خطا در ساخت تصویر: {err}")
        return

    photo_bytes, err = generate_image_google_imagen(english_prompt)

    if photo_bytes:
        send_photo(chat_id, photo_bytes, caption=f"✨ تصویر ساخته شده با Google Imagen:\n{user_prompt}")
    else:
        photo_bytes, flux_err = generate_image_flux(english_prompt)
        if photo_bytes:
            send_photo(chat_id, photo_bytes, caption=f"🖼 تصویر ساخته شده برای شما:\n{user_prompt}")
        else:
            send_message(chat_id, f"❌ خطا در ساخت تصویر: {flux_err}")

# پردازش کامل پیام‌ها و عکس‌های دریافتی
def process_update_async(data):
    if not data or "message" not in data:
        return

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    
    # ۱. دریافت و تحلیل/ویرایش تصویر
    if "photo" in message and isinstance(message["photo"], list):
        send_message(chat_id, "🔍 در حال دریافت تصویر...")
        
        photo_info = message["photo"][-1]
        file_id = photo_info.get("file_id")
        caption = message.get("caption", "").strip()
        
        photo_bytes = download_bale_file(file_id)
        if not photo_bytes:
            send_message(chat_id, "❌ خطا در دانلود تصویر از بله.")
            return

        # الف) ویرایش عکس
        if caption.lower().startswith("ویرایش:") or caption.lower().startswith("edit:"):
            edit_instruction = clean_persian_colloquial(caption)
            send_message(chat_id, "🎨 در حال اعمال ویرایش روی تصویر شما...")
            new_photo, err = edit_image_with_gemini(photo_bytes, edit_instruction)
            if new_photo:
                send_photo(chat_id, new_photo, caption=f"✏️ تصویر ویرایش شده با موفقیت:\n{edit_instruction}")
            else:
                send_message(chat_id, f"❌ خطا در ویرایش: {err}")
            return

        # ب) تحلیل عکس واقعی (Vision)
        send_message(chat_id, "🤖 در حال تحلیل تصویر توسط Gemini...")
        analysis_res = analyze_image_with_gemini(photo_bytes, caption)
        send_message(chat_id, analysis_res)
        return

    # ۲. پیام‌های متنی چت
    text = message.get("text", "").strip()
    if not text:
        return

    if text == "/start":
        welcome_msg = (
            "سلام! به ربات هوش مصنوعی سریع و تصویرساز خوش آمدید 🤖✨\n\n"
            "راهنمای ساخت عکس با Gemini:\n"
            "📸 `عکس واقعی: رونالدو پیش مسی`\n"
            "🎨 `عکس فانتزی: رونالدو پیش مسی`\n"
            "🤖 `عکس جمنای: برج میلاد`\n"
            "⚡ `عکس فلوکس: نیمار پیش هالند`\n\n"
            "امکانات دیگر:\n"
            "🔹 چت متنی با Gemini (ارسال هرگونه سوال متنی)\n"
            "🔹 ویرایش عکس (ارسال عکس با کپشن: `ویرایش: موهاشو قرمز کن`)\n"
            "🔹 تحلیل عکس (ارسال عکس و نوشتن سوال یا عبارت «این را تحلیل کن»)\n"
            "🔹 وضعیت اشتراک: /vip"
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
        process_image_command(chat_id, prompt, engine_choice="auto", style_mode="realistic")
        return

    elif lower_text.startswith("عکس فانتزی:") or lower_text.startswith("فانتزی:"):
        prompt = text.split(":", 1)[1].strip()
        process_image_command(chat_id, prompt, engine_choice="auto", style_mode="fantasy")
        return

    elif lower_text.startswith("عکس فلوکس:") or lower_text.startswith("فلوکس:"):
        prompt = text.split(":", 1)[1].strip()
        process_image_command(chat_id, prompt, engine_choice="flux", style_mode="auto")
        return

    elif lower_text.startswith("عکس:") or lower_text.startswith("عکس جمنای:") or lower_text.startswith("جمنای:"):
        prompt = text.split(":", 1)[1].strip() if ":" in text else text
        process_image_command(chat_id, prompt, engine_choice="auto", style_mode="auto")
        return

    # چت متنی سریع و مستقیم با Gemini (دست نخورده)
    send_message(chat_id, "🤔 در حال تفکر...")
    answer, err = call_gemini_dynamic(text, use_system_instruction=True)
    if answer:
        send_message(chat_id, answer)
    else:
        # در صورت خطا، کش مدل بازنشانی می‌شود
        global CACHED_MODEL_NAME
        CACHED_MODEL_NAME = None
        send_message(chat_id, err)

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
