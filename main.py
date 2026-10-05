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

app = Flask(__name__)

# دریافت تنظیمات محیطی
BALE_TOKEN = os.environ.get("BALE_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID")

BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}" if BALE_TOKEN else ""
BALE_FILE_URL = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}" if BALE_TOKEN else ""

PROCESSED_UPDATES = set()
MAX_CACHE_SIZE = 2000

# دستور سیستم برای پاسخ‌دهی صمیمی و فارسی
SYSTEM_INSTRUCTION = "پاسخ را فقط و فقط به زبان فارسی روان، صمیمی و با استفاده مناسب از ایموجی بنویس."

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("--- Gemini API آماده است ---", flush=True)
    except Exception as e:
        print(f"!!! خطا در تنظیم API جمنای: {e} !!!", flush=True)

# گرفتن خودکار لیست مدل‌های فعال اکانت شما
def get_active_models():
    models = []
    if GEMINI_API_KEY:
        try:
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    models.append(m.name)
        except Exception as e:
            print(f"Error listing models: {e}", flush=True)
    
    # اگر استعلام خودکار ناموفق بود، مدل‌های جایگزین استاندارد استفاده می‌شوند
    if not models:
        models = ['models/gemini-1.5-flash', 'models/gemini-1.5-pro', 'gemini-1.5-flash']
    return models

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

# چت متنی خودکار با مدل‌های فعال اکانت
def call_gemini_chat(text_prompt):
    if not GEMINI_API_KEY:
        return "❌ کلید API جمنای تنظیم نشده است."
    
    available_models = get_active_models()
    last_err = ""
    
    for model_name in available_models:
        try:
            model = genai.GenerativeModel(model_name, system_instruction=SYSTEM_INSTRUCTION)
            response = model.generate_content(text_prompt)
            if response and hasattr(response, 'text') and response.text:
                return clean_bot_response(response.text)
        except Exception as e:
            last_err = str(e)
            print(f"Chat error on {model_name}: {e}", flush=True)
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(f"{SYSTEM_INSTRUCTION}\n\n{text_prompt}")
                if response and hasattr(response, 'text') and response.text:
                    return clean_bot_response(response.text)
            except Exception as inner_e:
                print(f"Inner error on {model_name}: {inner_e}", flush=True)
            continue
            
    return f"⚠️ خطای جمنای: {last_err}" if last_err else "⚠️ مشکلی در پاسخگویی پیش آمد."

# تحلیل تصویر خودکار با مدل‌های فعال اکانت
def analyze_image_with_gemini(photo_bytes, user_question=""):
    if not GEMINI_API_KEY:
        return "❌ کلید API جمنای تنظیم نشده است."
    
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        prompt = user_question if user_question else "این تصویر را با دقت تحلیل کن و جزییاتش را به فارسی روان و صمیمی توضیح بده."
        
        available_models = get_active_models()
        last_err = ""
        
        for model_name in available_models:
            try:
                model = genai.GenerativeModel(model_name, system_instruction=SYSTEM_INSTRUCTION)
                response = model.generate_content([prompt, img])
                if response and hasattr(response, 'text') and response.text:
                    return clean_bot_response(response.text)
            except Exception as model_err:
                last_err = str(model_err)
                print(f"Vision error on {model_name}: {model_err}", flush=True)
                try:
                    model = genai.GenerativeModel(model_name)
                    response = model.generate_content([f"{SYSTEM_INSTRUCTION}\n\n{prompt}", img])
                    if response and hasattr(response, 'text') and response.text:
                        return clean_bot_response(response.text)
                except Exception as inner_err:
                    print(f"Inner vision error on {model_name}: {inner_err}", flush=True)
                continue

        return f"⚠️ تحلیل تصویر با خطا مواجه شد: {last_err}"
    except Exception as e:
        return f"⚠️ خطا در پردازش فایل تصویر: {e}"

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

def send_message(chat_id, text):
    if not BALE_API_URL or not text:
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

def generate_image_flux(english_prompt):
    try:
        encoded_prompt = urllib.parse.quote(english_prompt)
        rand_seed = random.randint(1, 999999)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width=1024&height=1024&nologo=true&seed={rand_seed}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(image_url, headers=headers, timeout=45)
        if res.status_code == 200:
            return res.content, None
        return None, f"خطای سرور تصویرساز ({res.status_code})"
    except Exception as e:
        return None, str(e)

def translate_prompt(prompt):
    cleaned = clean_persian_colloquial(prompt)
    p = f"Translate to clear English image prompt: '{cleaned}'. Return only plain text."
    res = call_gemini_chat(p)
    return res.replace('"', '').strip() if res else cleaned

def process_image_command(chat_id, user_prompt):
    send_message(chat_id, "🎨 در حال ساخت تصویر...")
    eng_prompt = translate_prompt(user_prompt)
    photo_bytes, err = generate_image_flux(eng_prompt)
    if photo_bytes:
        send_photo(chat_id, photo_bytes, caption=f"🖼 تصویر ساخته شده برای:\n{user_prompt}")
    else:
        send_message(chat_id, f"❌ خطا در ساخت تصویر: {err}")

def process_update_async(data):
    try:
        if not data or "message" not in data:
            return

        message = data["message"]
        chat_id = message["chat"]["id"]

        # ۱. بخش دریافت و تحلیل عکس
        if "photo" in message and isinstance(message["photo"], list):
            send_message(chat_id, "🔍 در حال دریافت و تحلیل تصویر...")
            photo_info = message["photo"][-1]
            file_id = photo_info.get("file_id")
            caption = message.get("caption", "").strip()

            photo_bytes = download_bale_file(file_id)
            if not photo_bytes:
                send_message(chat_id, "❌ خطا در دانلود تصویر از بله.")
                return

            analysis_res = analyze_image_with_gemini(photo_bytes, caption)
            send_message(chat_id, analysis_res)
            return

        # ۲. بخش متن‌ها
        text = message.get("text", "").strip()
        if not text:
            return

        if text == "/start":
            welcome_msg = (
                "سلام! خوش اومدی 👋🤖\n\n"
                "🔹 برای چت کردن کافیه سوالت رو برام بفرستی.\n"
                "🔹 برای تحلیل عکس، کافیه عکس بفرستی.\n"
                "🔹 برای ساخت عکس بنویس: `عکس: برج میلاد در شب`"
            )
            send_message(chat_id, welcome_msg)
            return

        if text.startswith("عکس:") or text.startswith("عکس "):
            prompt = text.replace("عکس:", "").replace("عکس", "").strip()
            if prompt:
                process_image_command(chat_id, prompt)
            else:
                send_message(chat_id, "لطفاً توصیف عکسی که می‌خوای رو جلوی کلمه «عکس:» بنویس.")
            return

        # چت متنی عادی
        answer = call_gemini_chat(text)
        send_message(chat_id, answer)

    except Exception as main_err:
        print(f"Error in process_update_async: {main_err}", flush=True)

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
