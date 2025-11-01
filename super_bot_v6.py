import os
import time
import json
import queue
import threading
import traceback
from urllib.parse import quote_plus, unquote_plus

import requests
from flask import Flask
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ---------------------- إعدادات يجب تحريرها قبل التشغيل ----------------------
BOT_TOKEN = "7986969586:AAHbGqY5EoDWeDHnmZ6V285SbwB9JxmbU9w"            # <-- ضع توكن البوت هنا بين " "
ADMIN_ID = 5931899735           # <-- ضع آيدي المالك هنا (مثال: 5931899735)
# ---------------------------------------------------------------------------

# مجلد وملفات البيانات
TMP_DIR = "tmp_files"
os.makedirs(TMP_DIR, exist_ok=True)
SETTINGS_FILE = "settings.json"    # يخزن إعدادات التحميل (endpoints, keys, options)
USERS_FILE = "users.json"          # يخزن المستخدمين المسجلين
CACHE_FILE = "cache.json"          # كاش للروابط المحملة مؤخراً (لتسريع)
LOG_FILE = "bot_log.txt"

# إعداد Flask و TeleBot
app = Flask("super_bot_alive")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# ذاكرتنا الافتراضية
DEFAULT_SETTINGS = {
    "api_tiktok": {"endpoint": "", "api_key": ""},
    "api_instagram": {"endpoint": "", "api_key": ""},
    "default_quality": "hd",        # hd or sd
    "allow_audio": True,
    "rate_limit_seconds": 5         # حد زمني بين طلبات نفس المستخدم
}

# ---------------------- أدوات قراءة/حفظ JSON ----------------------
def load_json(path, default):
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
users = load_json(USERS_FILE, [])
cache = load_json(CACHE_FILE, {})

# ---------------------- لوج بسيط للأخطاء والأحداث ----------------------
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())

# ---------------------- حماية الاشتراك و تسجيل المستخدم ----------------------
def register_user(uid, username=None):
    uid_s = str(uid)
    if uid_s not in users:
        users.append(uid_s)
        save_json(USERS_FILE, users)
        log(f"مستخدم جديد: {uid_s} ({username})")
        # إشعار الأدمن عند الانضمام
        try:
            if ADMIN_ID:
                bot.send_message(ADMIN_ID, f"👤 مستخدم جديد انضم: <code>{uid_s}</code>\n🔗 @{username or '---'}", parse_mode="HTML")
        except Exception:
            pass

def is_subscribed(uid):
    # القنوات المطلوبة مخزنة في الإعدادات تحت "required_channels" (قابلة للتعديل من لوحة التحكم)
    required = settings.get("required_channels", [])
    if not required:
        return True
    for ch in required:
        try:
            member = bot.get_chat_member(ch, uid)
            if member.status in ["left", "kicked"]:
                return False
        except Exception:
            # إذا حدث خطأ نعتبره غير مشترك لحماية البوت
            return False
    return True

# ---------------------- صف التحميل وعمّال الخلفية ----------------------
download_queue = queue.Queue()
worker_threads = []
WORKER_COUNT = 2  # عدد عمال التحميل المتزامنين (يمكن رفعه بحذر)

# rate-limiter per user (user_id -> last_request_ts)
last_request = {}

RATE_LIMIT_SECONDS = lambda: settings.get("rate_limit_seconds", 5)

def enqueue_download(task):
    # task = dict: { "uid":..., "chat_id":..., "url":..., "platform": "tiktok"|"instagram", "quality":"hd", "audio":False }
    download_queue.put(task)
    log(f"تم إضافة مهمة تحميل إلى الطابور: {task.get('url')} (من المستخدم {task.get('uid')})")

def worker_loop(index):
    while True:
        try:
            task = download_queue.get()
            if task is None:
                break
            process_task(task)
        except Exception as e:
            log(f"عامل {index} — خطأ أثناء المعالجة: {e}")
            traceback.print_exc()
        finally:
            download_queue.task_done()

def start_workers():
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, args=(i+1,), daemon=True)
        t.start()
        worker_threads.append(t)
    log("بدء عمال التحميل (workers).")

# ---------------------- دوال جلب روابط التحميل المباشرة (مخصصة عبر لوحة التحكم) ----------------------
def fetch_direct_url_from_api(url, platform, quality="hd", audio=False):
    """
    هذه الدالة تستخدم إعدادات API من settings لتنادي endpoint الخاص بك.
    من المتوقع أن تعيد الخدمة JSON مع حقل 'download_url' أو 'url' — عدّل حسب الـ API لديك.
    """
    api_info = settings.get("api_tiktok") if platform == "tiktok" else settings.get("api_instagram")
    endpoint = api_info.get("endpoint", "").strip()
    key = api_info.get("api_key", "").strip()
    if not endpoint:
        return None
    try:
        params = {"url": url, "quality": quality, "audio_only": int(bool(audio))}
        headers = {}
        if key:
            headers["Authorization"] = key
        resp = requests.get(endpoint, params=params, headers=headers, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        # دعم متوقع لبنية: {"success":true, "download_url":"..."}
        if isinstance(data, dict):
            if data.get("download_url"):
                return data.get("download_url")
            if data.get("url"):
                return data.get("url")
            # إن كانت البنية مختلفة ضع هنا التكييف المطلوب أو عدل API
    except Exception as e:
        log(f"fetch_direct_url_from_api error: {e}")
    return None

# ---------------------- تحميل بالـ stream لملف مؤقت ----------------------
def download_stream_to_file(file_url, filename):
    path = os.path.join(TMP_DIR, filename)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        with requests.get(file_url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*256):
                    if chunk:
                        f.write(chunk)
        return path
    except Exception as e:
        log(f"download_stream error: {e}")
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass
        return None

# ---------------------- معالجة مهمة التحميل ----------------------
def process_task(task):
    uid = task.get("uid")
    chat_id = task.get("chat_id")
    url = task.get("url")
    platform = task.get("platform")
    quality = task.get("quality", settings.get("default_quality", "hd"))
    audio = task.get("audio", False)

    log(f"بدء معالجة: {url} (platform={platform}, audio={audio}, quality={quality})")

    # تحقق الكاش أولاً
    cached = cache.get(url)
    if cached and cached.get("audio") == audio and cached.get("quality") == quality:
        download_url = cached.get("download_url")
        log(f"وجدت في الكاش رابط مباشر: {download_url}")
    else:
        download_url = fetch_direct_url_from_api(url, platform, quality, audio)
        if download_url:
            cache[url] = {"download_url": download_url, "quality": quality, "audio": audio, "ts": time.time()}
            save_json(CACHE_FILE, cache)

    if not download_url:
        bot.send_message(chat_id, "❌ لم أستطع استخراج رابط التحميل. تحقق من إعدادات API أو حاول لاحقاً.")
        return

    # تنزيل الملف مؤقتاً
    ext = ".mp3" if audio else ".mp4"
    fname = f"{platform}_{int(time.time())}{ext}"
    tmp_path = download_stream_to_file(download_url, fname)
    if not tmp_path:
        bot.send_message(chat_id, "⚠️ حدث خطأ أثناء تنزيل الملف.")
        return

    # فحص الحجم قبل الإرسال
    try:
        size = os.path.getsize(tmp_path)
        if size > settings.get("max_send_bytes", 45*1024*1024):
            bot.send_message(chat_id, "❌ الملف كبير جداً للإرسال عبر تيليجرام.")
            os.remove(tmp_path)
            return
    except Exception:
        pass

    # إرسال الملف
    try:
        with open(tmp_path, "rb") as f:
            if audio:
                bot.send_audio(chat_id, f)
            else:
                bot.send_video(chat_id, f)
        bot.send_message(chat_id, "✅ تم الإرسال بنجاح!")
    except Exception as e:
        log(f"send file error: {e}")
        bot.send_message(chat_id, f"⚠️ خطأ أثناء الإرسال: {e}")
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except:
            pass

# ---------------------- Rate limiting و Flood protection بسيط لكل مستخدم ----------------------
def check_rate_limit(uid):
    last = last_request.get(uid, 0)
    now = time.time()
    if now - last < RATE_LIMIT_SECONDS():
        return False, RATE_LIMIT_SECONDS() - (now - last)
    last_request[uid] = now
    return True, 0

# ---------------------- واجهة الـ Flask للحفظ (keep-alive) ----------------------
@app.route("/")
def home():
    return "Super Bot v5.0 is alive"

def run_flask():
    try:
        app.run(host="0.0.0.0", port=8080)
    except Exception as e:
        log(f"Flask error: {e}")

# ---------------------- لوحة تحكم المالك (عربية) لإدارة إعدادات التحميل ----------------------
def make_admin_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🛠️ إعدادات التحميل", callback_data="admin_settings"),
        types.InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")
    )
    kb.add(
        types.InlineKeyboardButton("📥 معاينة الكاش", callback_data="admin_cache"),
        types.InlineKeyboardButton("📢 بث رسالة", callback_data="admin_broadcast")
    )
    kb.add(types.InlineKeyboardButton("🔁 إعادة تشغيل يدوي", callback_data="admin_restart"))
    return kb

@bot.message_handler(commands=["admin"])
def admin_entry(m):
    if ADMIN_ID is None or m.from_user.id != ADMIN_ID:
        bot.reply_to(m, "❌ أنت لست المالك. لا يمكن الدخول.")
        return
    bot.send_message(m.chat.id, "⚙️ لوحة تحكم المالك — اختر عملية:", reply_markup=make_admin_keyboard())

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def admin_callbacks(call):
    if ADMIN_ID is None or call.from_user.id != ADMIN_ID:
        return
    action = call.data
    if action == "admin_settings":
        # لوحة فرعية لتعديل API
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔗 تعديل API تيك توك", callback_data="set_api_tiktok"))
        kb.add(types.InlineKeyboardButton("🔗 تعديل API إنستغرام", callback_data="set_api_insta"))
        kb.add(types.InlineKeyboardButton("⚙️ خيارات عامة", callback_data="set_options"))
        bot.send_message(call.message.chat.id, "🧩 إعدادات التحميل:", reply_markup=kb)

    elif action == "set_api_tiktok":
        bot.send_message(call.message.chat.id, "🔧 أرسل عنوان endpoint الخاص بتيك توك (URL) أو اكتب 'إلغاء':")
        bot.register_next_step_handler(call.message, set_api_tiktok_step)
    elif action == "set_api_insta":
        bot.send_message(call.message.chat.id, "🔧 أرسل عنوان endpoint الخاص بإنستغرام (URL) أو اكتب 'إلغاء':")
        bot.register_next_step_handler(call.message, set_api_insta_step)
    elif action == "set_options":
        # إظهار الخيارات الحالية وتعديلها
        opts = settings.copy()
        text = f"🔎 الإعدادات الحالية:\n- الجودة الافتراضية: {opts.get('default_quality')}\n- السماح بالصوت: {opts.get('allow_audio')}\n- حد الإيقاع (ثواني): {opts.get('rate_limit_seconds')}"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("تغيير الجودة", callback_data="opt_quality"))
        kb.add(types.InlineKeyboardButton("تبديل السماح بالصوت", callback_data="opt_toggle_audio"))
        kb.add(types.InlineKeyboardButton("تغيير حد الإيقاع", callback_data="opt_rate"))
        bot.send_message(call.message.chat.id, text, reply_markup=kb)
    elif action == "admin_stats":
        bot.send_message(call.message.chat.id, f"📊 إحصائيات:\n- مستخدمون مسجلون: {len(users)}\n- حجم الكاش: {len(cache)} رابط")
    elif action == "admin_cache":
        bot.send_message(call.message.chat.id, "🗂️ عرض الكاش (الأول 20):\n" + "\n".join(list(cache.keys())[:20]) or "لا توجد عناصر.")
    elif action == "admin_broadcast":
        bot.send_message(call.message.chat.id, "📝 أرسل النص المراد بثه للجميع:")
        bot.register_next_step_handler(call.message, broadcast_step)
    elif action == "admin_restart":
        bot.send_message(call.message.chat.id, "🔁 جاري إعادة تشغيل البوت (يدوي) — سيتم إعادة محاولة الاتصال تلقائياً.")
        # سنقوم بإظهار رسالة وسيتم إعادة محاولة الاتصال عبر حلقة التشغيل الرئيسية
    # خيارات إضافية (quality/audio/rate)
    elif action == "opt_quality":
        bot.send_message(call.message.chat.id, "اختر الجودة الافتراضية: (hd / sd)")
        bot.register_next_step_handler(call.message, set_quality_step)
    elif action == "opt_toggle_audio":
        settings["allow_audio"] = not settings.get("allow_audio", True)
        save_json(SETTINGS_FILE, settings)
        bot.send_message(call.message.chat.id, f"تم ضبط السماح بالصوت: {settings['allow_audio']}")
    elif action == "opt_rate":
        bot.send_message(call.message.chat.id, "أرسل عدد الثواني لحد الإيقاع بين طلبات نفس المستخدم (مثال: 5):")
        bot.register_next_step_handler(call.message, set_rate_step)

# ------- خطوات إدخال القيم من المالك -------
def set_api_tiktok_step(msg):
    text = msg.text.strip()
    if text.lower() == "إلغاء" or text.lower() == "cancel":
        bot.reply_to(msg, "تم الإلغاء.")
        return
    settings.setdefault("api_tiktok", {})["endpoint"] = text
    bot.reply_to(msg, "🔑 الآن أرسل مفتاح API (أو اتركه فارغًا):")
    bot.register_next_step_handler(msg, set_api_tiktok_key_step)

def set_api_tiktok_key_step(msg):
    key = msg.text.strip()
    settings.setdefault("api_tiktok", {})["api_key"] = key
    save_json(SETTINGS_FILE, settings)
    bot.reply_to(msg, "✅ تم حفظ إعدادات API تيك توك.")

def set_api_insta_step(msg):
    text = msg.text.strip()
    if text.lower() == "إلغاء" or text.lower() == "cancel":
        bot.reply_to(msg, "تم الإلغاء.")
        return
    settings.setdefault("api_instagram", {})["endpoint"] = text
    bot.reply_to(msg, "🔑 الآن أرسل مفتاح API (أو اتركه فارغًا):")
    bot.register_next_step_handler(msg, set_api_insta_key_step)

def set_api_insta_key_step(msg):
    key = msg.text.strip()
    settings.setdefault("api_instagram", {})["api_key"] = key
    save_json(SETTINGS_FILE, settings)
    bot.reply_to(msg, "✅ تم حفظ إعدادات API إنستغرام.")

def set_quality_step(msg):
    val = msg.text.strip().lower()
    if val in ("hd", "sd"):
        settings["default_quality"] = val
        save_json(SETTINGS_FILE, settings)
        bot.reply_to(msg, f"✅ تم تعيين الجودة الافتراضية: {val}")
    else:
        bot.reply_to(msg, "❌ قيمة غير صحيحة. أرسل 'hd' أو 'sd'.")

def set_rate_step(msg):
    try:
        n = int(msg.text.strip())
        settings["rate_limit_seconds"] = max(1, n)
        save_json(SETTINGS_FILE, settings)
        bot.reply_to(msg, f"✅ تم تعيين حد الإيقاع: {n} ثانية")
    except:
        bot.reply_to(msg, "❌ أدخل رقم صحيح.")

def broadcast_step(msg):
    text = msg.text
    count = 0
    for uid in users:
        try:
            bot.send_message(int(uid), text)
            count += 1
        except:
            pass
    bot.send_message(msg.chat.id, f"📢 تم الإرسال إلى {count} مستخدم.")

# ---------------------- استقبال روابط المستخدمين وبدء الطابور ----------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    register_user(uid, getattr(m.from_user, "username", None))
    if not is_subscribed(uid):
        # عرض أزرار للاشتراك
        kb = types.InlineKeyboardMarkup()
        for ch in settings.get("required_channels", []):
            kb.add(types.InlineKeyboardButton(f"📢 اشتراك في {ch}", url=f"https://t.me/{ch.lstrip('@')}"))
        kb.add(types.InlineKeyboardButton("✅ تم الاشتراك", callback_data="check_sub"))
        bot.send_message(uid, "⚠️ يجب الاشتراك في القنوات المطلوبة أولاً:", reply_markup=kb)
        return
    bot.send_message(uid, "👋 أهلاً! أرسل رابط TikTok أو Instagram لتحميله (أو استخدم الأزرار).")

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def cb_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.edit_message_text("✅ تم التحقق! الآن أرسل الرابط.", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "لم يُسجَّل اشتراكك بعد.", show_alert=True)

@bot.message_handler(func=lambda m: isinstance(m.text, str) and ("tiktok.com" in m.text or "instagram.com" in m.text))
def handle_link(m):
    uid = m.from_user.id
    if not is_subscribed(uid):
        bot.reply_to(m, "⚠️ يجب الاشتراك أولاً.")
        return

    ok, wait = check_rate_limit(uid)
    if not ok:
        bot.reply_to(m, f"⏳ الرجاء الانتظار {int(wait)} ثانية قبل إرسال طلب جديد.")
        return

    url = m.text.strip()
    platform = "tiktok" if "tiktok" in url else "instagram"
    quality = settings.get("default_quality", "hd")
    audio = False  # نبدأ بالافتراضي فيديو
    # نعرض أزرار الاختيار
    enc = quote_plus(url)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🎬 فيديو (HD)", callback_data=f"dl|video|hd|{enc}"))
    kb.add(types.InlineKeyboardButton("🎬 فيديو (SD)", callback_data=f"dl|video|sd|{enc}"))
    if settings.get("allow_audio", True):
        kb.add(types.InlineKeyboardButton("🎵 صوت فقط", callback_data=f"dl|audio|best|{enc}"))
    bot.reply_to(m, "اختر نوع التحميل:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("dl|"))
def cb_download(call):
    try:
        _, mode, qual, enc = call.data.split("|", 3)
        url = unquote_plus(enc)
    except:
        bot.answer_callback_query(call.id, "خطأ في البيانات.")
        return
    uid = call.from_user.id
    chat = call.message.chat.id
    platform = "tiktok" if "tiktok" in url else "instagram"
    audio = (mode == "audio")
    task = {"uid": uid, "chat_id": chat, "url": url, "platform": platform, "quality": qual, "audio": audio}
    enqueue_download(task)
    bot.answer_callback_query(call.id, "✅ تم وضع طلبك في الطابور. سيتم الإرسال عند انتهاء التحميل.")

# ---------------------- SmartGuard: مراقبة العمال وذاكرة الكاش ----------------------
def smartguard_loop():
    while True:
        try:
            # مراقبة العاملين
            alive = any(t.is_alive() for t in worker_threads)
            if not alive:
                log("SmartGuard: لا توجد عمال نشطة. إعادة تشغيل العمال...")
                start_workers()
            # تنظيف الكاش من العناصر القديمة (> 24 ساعة)
            now = time.time()
            removed = []
            for k, v in list(cache.items()):
                if now - v.get("ts", 0) > 24 * 3600:
                    del cache[k]
                    removed.append(k)
            if removed:
                save_json(CACHE_FILE, cache)
                log(f"SmartGuard: نظف الكاش، أزالت {len(removed)} عناصر.")
        except Exception as e:
            log(f"SmartGuard error: {e}")
        time.sleep(60)

# ---------------------- حلقة تشغيل البوت مع التعامل مع FloodWait واعادة الاتصال ----------------------
def run_bot_loop():
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except ApiTelegramException as e:
            s = str(e)
            log(f"ApiTelegramException: {s}")
            if "FLOOD_WAIT" in s or "FloodWait" in s:
                digits = "".join(ch for ch in s if ch.isdigit())
                wait = int(digits) if digits.isdigit() else 60
                log(f"FloodWait detected — waiting {wait}s")
                time.sleep(wait + 5)
            else:
                time.sleep(5)
        except Exception as e:
            log(f"run_bot_loop exception: {e}")
            traceback.print_exc()
            time.sleep(5)

# ---------------------- بدء كل شيء ----------------------
def start_all():
    # بدء Flask keep-alive
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    # بدء عمال التحميل
    start_workers()
    # بدء SmartGuard
    sg = threading.Thread(target=smartguard_loop, daemon=True)
    sg.start()
    # بدء حلقة البوت
    rb = threading.Thread(target=run_bot_loop, daemon=True)
    rb.start()
    log("تم تشغيل Super Bot v5.0 — جاهز للعمل")

# ---------------------- عند التشغيل كملف رئيسي ----------------------
if __name__ == "__main__":
    # تحقق مبدئي من وجود توكن و ADMIN_ID
    if not BOT_TOKEN:
        print("⚠️ لم تضع توكن البوت في المتغير BOT_TOKEN. عدّل الملف وأضفه ثم أعد التشغيل.")
    elif ADMIN_ID is None:
        print("⚠️ لم تضع آيدي المالك في ADMIN_ID. عدّل الملف وأضفه ثم أعد التشغيل.")
    else:
        # حفظ الإعدادات الافتراضية إن لم تكن موجودة
        save_json(SETTINGS_FILE, settings)
        save_json(USERS_FILE, users)
        save_json(CACHE_FILE, cache)
        start_all()
        # نبقي الخيط الرئيسي حياً
        while True:
            time.sleep(3600)
