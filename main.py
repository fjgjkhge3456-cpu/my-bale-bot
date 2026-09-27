import os
import sqlite3
import requests
from io import BytesIO
from PIL import Image
from flask import Flask, request, jsonify
import google.generativeai as genai

app = Flask(__name__)

# ----------------- تنظیمات متغیرهای محیطی -----------------
BALE_TOKEN = os.environ.get("BALE_TOKEN", "872030909:um2DWEeCRcCd-tkGPyC2Qu-k3FpA-Niaq8")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # عددی شناسه بله خودت

# تنظیم کلید API گوگل
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction="تو یک دستیار هوشمند و بسیار مودب، صمیمی و مسلط به زبان فارسی هستی. به تمام سوالات کاربر دقیق، کامل و با لحنی روان پاسخ بده."
    )

# ----------------- پایگاه داده دیتابیس (اشتراک‌ها) -----------------
def init_db():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            is_vip INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()

def is_user_vip(chat_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT is_vip FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    return row and row[0] == 1

def set_user_vip(chat_id, status=1):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (chat_id, is_vip) VALUES (?, ?)", (chat_id, status))
    conn.commit()
    conn.close()

# ----------------- توابع ارسال پیام در بله -----------------
def send_message(chat_id, text):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending message: {e}")

def send_photo(chat_id, photo_url, caption=""):
    url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_url, "caption": caption}
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print(f"Error sending photo: {e}")

def forward_photo_to_admin(photo_file_id, caption):
    # دانلود فایل از بله و ارسال برای ادمین
    file_info_url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/getFile?file_id={photo_file_id}"
    res = requests.get(file_info_url).json()
    if res.get("ok"):
        file_path = res["result"]["file_path"]
        download_url = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{file_path}"
        send_photo(ADMIN_ID, download_url, caption)

# ----------------- دریافت تصویر از بله برای جمنای -----------------
def get_pil_image_from_bale(file_id):
    file_info_url = f"https://tapi.bale.ai/bot{BALE_TOKEN}/getFile?file_id={file_id}"
    res = requests.get(file_info_url).json()
    if res.get("ok"):
        file_path = res["result"]["file_path"]
        download_url = f"https://tapi.bale.ai/file/bot{BALE_TOKEN}/{file_path}"
        img_data = requests.get(download_url).content
        return Image.open(BytesIO(img_data))
    return None

# ----------------- پردازش اصلی وب‌هوک -----------------
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"status": "ok"}), 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    # ۱. دستورات عمومی
    if text == "/start":
        welcome_txt = (
            "سلام! به ربات هوش مصنوعی خوش آمدید. 🌟\n\n"
            "✨ **امکانات:**\n"
            "1️⃣ **سوال متنی:** هر سوالی داری بنویس تا پاسخ دهم.\n"
            "2️⃣ **تحلیل عکس:** یک عکس بفرست و درباره‌اش سوال بپرس.\n"
            "3️⃣ **تولید عکس با Flux:** کلمه `عکس:` را اول توصیفت بنویس (مثلاً: `عکس: یک ماشین اسپرت قرمز`).\n"
            "4️⃣ **ارتقا به حساب VIP:** دستور `/vip` را بفرستید."
        )
        send_message(chat_id, welcome_txt)
        return jsonify({"status": "ok"}), 200

    # ۲. دستور خرید اشتراک
    if text == "/vip":
        vip_info = (
            "💎 **خرید اشتراک VIP**\n\n"
            "برای استفاده نامحدود و سرعت بالا می‌توانید اشتراک تهیه کنید:\n"
            "💳 **شماره کارت:** `6037-9918-0000-0000` (به نام تولیدی تهران)\n"
            "💵 **مبلغ ماهانه:** ۵۰,۰۰۰ تومان\n\n"
            "👇 **راهنما:** پس از واریز، **تصویر فیش واریزی** را همین‌جا ارسال کنید تا حساب شما فعال شود."
        )
        send_message(chat_id, vip_info)
        return jsonify({"status": "ok"}), 200

    # ۳. دستور ادمین برای فعال‌سازی اشتراک: /addvip 1234567
    if text.startswith("/addvip") and chat_id == ADMIN_ID:
        parts = text.split()
        if len(parts) > 1:
            target_id = int(parts[1])
            set_user_vip(target_id, 1)
            send_message(ADMIN_ID, f"✅ اشتراک VIP برای کاربر {target_id} فعال شد.")
            send_message(target_id, "🎉 تبریک! اشتراک VIP شما با موفقیت فعال شد.")
        return jsonify({"status": "ok"}), 200

    # ۴. اگر کاربر تصویر ارسال کرده باشد (فیش واریزی یا تحلیل عکس)
    if "photo" in msg:
        photos = msg["photo"]
        largest_photo = photos[-1]  # بهترین کیفیت
        file_id = largest_photo["file_id"]

        # اگر کپشن شامل سوال بود -> تحلیل عکس با جمنای
        if caption or "تحلیل" in caption:
            send_message(chat_id, "🔍 در حال بررسی و تحلیل تصویر...")
            img = get_pil_image_from_bale(file_id)
            if img:
                prompt = caption if caption else "این تصویر را به دقت توضیح بده."
                response = gemini_model.generate_content([prompt, img])
                send_message(chat_id, response.text)
            else:
                send_message(chat_id, "خطا در دریافت تصویر.")
        else:
            # ارسال فیش برای ادمین جهت تایید
            send_message(chat_id, "📩 فیش واریزی شما دریافت شد و برای ادمین ارسال گردید. به‌زودی بررسی می‌شود.")
            info_text = f"💳 **فیش واریزی جدید**\nشناسه کاربر: `{chat_id}`\n\nبرای فعال‌سازی دستور زیر را بزنید:\n`/addvip {chat_id}`"
            forward_photo_to_admin(file_id, info_text)

        return jsonify({"status": "ok"}), 200

    # ۵. تولید عکس با Flux (مثال: عکس: یک فضانورد در مریخ)
    if text.startswith("عکس:") or text.startswith("/image"):
        prompt = text.replace("عکس:", "").replace("/image", "").strip()
        if not prompt:
            send_message(chat_id, "لطفاً توصیف عکس را بنویسید. مثال:\n`عکس: یک کلبه چوبی در جنگل برفی`")
            return jsonify({"status": "ok"}), 200

        send_message(chat_id, "🎨 در حال ساخت تصویر با مدل Flux... لطفاً چند ثانیه صبر کنید.")
        # استفاده از مدل Flux بدون نیاز به API Key
        flux_url = f"https://image.pollinations.ai/prompt/{prompt}?model=flux&width=1024&height=1024&nologo=true"
        send_photo(chat_id, flux_url, f"🖼 تصویر ساخته شده برای: {prompt}")
        return jsonify({"status": "ok"}), 200

    # ۶. گفتگو و پاسخ متنی معمولی با Gemini
    if text:
        send_message(chat_id, "🤔 در حال تفکر...")
        try:
            response = gemini_model.generate_content(text)
            send_message(chat_id, response.text)
        except Exception as e:
            send_message(chat_id, "متأسفانه مشکلی در پاسخگویی پیش آمد. دوباره تلاش کنید.")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))