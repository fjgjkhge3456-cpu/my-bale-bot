import os
import json
import sqlite3
import requests
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

# دستورالعمل سیستم برای لحن صمیمی و روان
SYSTEM_INSTRUCTION = (
    "تو یک دستیار هوش مصنوعی صمیمی، هوشمند و فارسی‌زبان هستی. "
    "پاسخ‌هایت باید بسیار روان، کوتاه، جذاب و همراه با ایموجی باشد. "
    "مستقیماً پاسخ بده و از تکرار جملات، آوردن لیست قوانین، یا تکرار سوال کاربر خودداری کن."
)

# تنظیمات اولیه گوگل جمنای
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("--- Gemini API configured successfully ---", flush=True)
    except Exception as e:
        print(f"!!! Error configuring Gemini API: {e} !!!", flush=True)
else:
    print("!!! WARNING: GEMINI_API_KEY is missing !!!", flush=True)

BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}" if BALE_TOKEN else ""

# تابع فراخوانی هوش مصنوعی با جدیدترین مدل‌های فعال
def generate_gemini_response(contents):
    if not GEMINI_API_KEY:
        return "❌ کلید GEMINI_API_KEY در تنظیمات Render وارد نشده است."

    # مدل‌های بروز و سریع
    candidate_models = [
        'gemini-2.0-flash',
        'gemini-2.5-flash',
        'gemini-1.5-flash',
        'gemini-2.0-flash-lite'
    ]
    
    last_error = None
    for m_name in candidate_models:
        try:
            print(f"Trying model: {m_name}", flush=True)
            model = genai.GenerativeModel(m_name)
            
            if isinstance(contents, str):
                full_prompt = f"{SYSTEM_INSTRUCTION}\n\nپیام کاربر: {contents}"
                response = model.generate_content(full_prompt)
            else:
                response = model.generate_content(contents)
            
            if response and hasattr(response, 'text') and response.text:
                return response.text.strip()
        except Exception as e:
            last_error = e
            print(f"Model {m_name} failed: {e}", flush=True)
            continue

    raise Exception(f"پاسخی از گوگل دریافت نشد: {last_error}")

# ۲. پایگاه داده SQLite برای اشتراک VIP
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

# ۳. توابع ارتباط با API پیام‌رسان بله
def send_message(chat_id, text):
    if not BALE_API_URL:
        print("Error: BALE_TOKEN is not defined!", flush=True)
        return
    url = f"{BALE_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"SendMessage status: {res.status_code}", flush=True)
    except Exception as e:
        print(f"Error sending message to Bale: {e}", flush=True)

def send_photo(chat_id, photo_bytes, caption=""):
    url = f"{BALE_API_URL}/sendPhoto"
    try:
        files = {'photo': ('image.jpg', photo_bytes, 'image/jpeg')}
        data = {'chat_id': chat_id, 'caption': caption}
        res = requests.post(url, data=data, files=files, timeout=30)
        print(f"SendPhoto status: {res.status_code}", flush=True)
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

# ۴. پردازش پیام‌های دریافتی از وب‌هوک
@app.route('/', methods=['POST', 'GET'])
def webhook():
    if request.method == 'GET':
        return "Server is Live!", 200

    data = request.get_json()
    print(f"Incoming Update: {json.dumps(data, ensure_ascii=False)}", flush=True)

    if not data or "message" not in data:
        return jsonify({"status": "ok"}), 200

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
            "2️⃣ ساخت عکس با Flux (مثال: عکس: یک ماشین اسپرت)\n"
            "3️⃣ تحلیل عکس (عکس بفرستید و سوال بپرسید)\n"
            "4️⃣ وضعیت اشتراک با دستور /vip"
        )
        send_message(chat_id, welcome_msg)
        return jsonify({"status": "ok"}), 200

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
        return jsonify({"status": "ok"}), 200

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
        return jsonify({"status": "ok"}), 200

    # ساخت عکس با Pollinations (Flux)
    if text.lower().startswith("عکس:") or text.lower().startswith("image:"):
        prompt = text.split(":", 1)[1].strip()
        if not prompt:
            send_message(chat_id, "لطفاً بعد از 'عکس:' توصیف تصویر را بنویسید.")
            return jsonify({"status": "ok"}), 200

        send_message(chat_id, "🎨 در حال ساخت تصویر با Flux... کمی صبر کنید.")
        try:
            image_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?model=flux&width=1024&height=1024"
            img_res = requests.get(image_url, timeout=40)
            if img_res.status_code == 200:
                send_photo(chat_id, img_res.content, caption=f"🖼 تصویر ساخته شده برای: {prompt}")
            else:
                send_message(chat_id, f"خطا در ساخت تصویر (کد: {img_res.status_code})")
        except Exception as e:
            print(f"Flux Error: {e}", flush=True)
            send_message(chat_id, f"خطا در ارتباط با سرور تصویرساز: {e}")
        return jsonify({"status": "ok"}), 200

    # تحلیل تصویر (Vision)
    if "photo" in message:
        photos = message["photo"]
        file_id = photos[-1]["file_id"]
        caption = message.get("caption", "این عکس را به دقت تحلیل و توصیف کن.")

        send_message(chat_id, "🔍 در حال تحلیل تصویر با Gemini...")
        try:
            photo_bytes = get_bale_file_bytes(file_id)
            if photo_bytes:
                image = Image.open(io.BytesIO(photo_bytes))
                answer = generate_gemini_response([caption, image])
                send_message(chat_id, answer)
            else:
                send_message(chat_id, "خطا در دریافت فایل تصویر از بله.")
        except Exception as e:
            print(f"Gemini Vision Error: {e}", flush=True)
            send_message(chat_id, f"خطا در تحلیل تصویر:\n{e}")
        return jsonify({"status": "ok"}), 200

    # چت متنی با Gemini
    if text:
        send_message(chat_id, "🤔 در حال تفکر...")
        try:
            answer = generate_gemini_response(text)
            send_message(chat_id, answer)
        except Exception as e:
            print(f"!!! CRITICAL GEMINI ERROR: {e} !!!", flush=True)
            send_message(chat_id, f"متأسفانه مشکلی پیش آمد:\n{e}")

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
