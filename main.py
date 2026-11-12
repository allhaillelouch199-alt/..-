# -*- coding: utf-8 -*-
"""
بوت النشر التلقائي والردود الذكية - ملف واحد
- بوت تحكم (Bot Token) تتحكم منه بكل شي بأزرار
- حسابات متعددة (جلسات مستخدم) كل حساب له مجموعاته
- نشر تلقائي مع تاك للأعضاء (كل شخص مرة كل X ساعة)
- ردود على كلمات محددة (كل شخص مرة كل Y ساعة)
- فواصل زمنية، ساعات هدوء، إحصائيات، إشعارات فشل
"""
import asyncio
import io
import logging
import math
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(i) for i in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "autoposter.db"))
# ربط بوت Strat: يسحب منه جلسات مستقلة (جهاز ثانٍ) جاهزة لكل رقم تلقائياً.
STRAT_API_URL = os.getenv("STRAT_API_URL", "").rstrip("/")
STRAT_API_KEY = os.getenv("STRAT_API_KEY", "")
STRAT_POLL_SECONDS = int(os.getenv("STRAT_POLL_SECONDS") or 30)

if not (API_ID and API_HASH and BOT_TOKEN and ADMIN_IDS):
    print("❌ عبّي API_ID و API_HASH و BOT_TOKEN و ADMIN_IDS بملف .env")
    sys.exit(1)

from telethon import TelegramClient, events, Button, utils, functions, types  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402
from telethon.errors import (  # noqa: E402
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PasswordHashInvalidError, UserAlreadyParticipantError, MessageNotModifiedError,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("autoposter")
logging.getLogger("telethon").setLevel(logging.WARNING)

# ============================== قاعدة البيانات ==============================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT, user_id INTEGER, session TEXT, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER, chat_id INTEGER, title TEXT, enabled INTEGER DEFAULT 1, fails INTEGER DEFAULT 0, UNIQUE(account_id, chat_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, mode TEXT DEFAULT 'contains', enabled INTEGER DEFAULT 1, hits INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS replies(id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS users(account_id INTEGER, chat_id INTEGER, user_id INTEGER, access_hash INTEGER, name TEXT, seen INTEGER, PRIMARY KEY(account_id, chat_id, user_id));
CREATE TABLE IF NOT EXISTS cooldowns(chat_id INTEGER, user_id INTEGER, kind TEXT, ts INTEGER, PRIMARY KEY(chat_id, user_id, kind));
CREATE TABLE IF NOT EXISTS blocked(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, account_id INTEGER, chat_id INTEGER, kind TEXT, ok INTEGER, n INTEGER, err TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS replied(chat_id INTEGER, msg_id INTEGER, PRIMARY KEY(chat_id, msg_id));
CREATE TABLE IF NOT EXISTS pm_contacts(account_id INTEGER, user_id INTEGER, access_hash INTEGER, name TEXT, PRIMARY KEY(account_id, user_id));
CREATE TABLE IF NOT EXISTS agroups(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, link TEXT, hash TEXT, uname TEXT, access TEXT, title TEXT, chat_id INTEGER, enabled INTEGER DEFAULT 1, gap_s INTEGER DEFAULT 30);
CREATE TABLE IF NOT EXISTS ag_members(id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, account_id INTEGER, status TEXT DEFAULT 'pending', tries INTEGER DEFAULT 0, err TEXT, checked INTEGER DEFAULT 0, UNIQUE(group_id, account_id));
CREATE TABLE IF NOT EXISTS ag_msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, text TEXT, enabled INTEGER DEFAULT 1);
""")
db.commit()
# أداء: WAL + بدون fsync لكل commit (كان يجمّد البوت لأن كل عملية كتابة = commit مستقل)
for _p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA temp_store=MEMORY"):
    try:
        db.execute(_p)
    except Exception:
        pass
db.executescript("""
CREATE INDEX IF NOT EXISTS ix_log_ts ON log(ts);
CREATE INDEX IF NOT EXISTS ix_log_acc_ts ON log(account_id, ts);
CREATE INDEX IF NOT EXISTS ix_users_acc_chat ON users(account_id, chat_id);
CREATE INDEX IF NOT EXISTS ix_chats_acc ON chats(account_id, enabled);
CREATE INDEX IF NOT EXISTS ix_replies_rule ON replies(rule_id);
""")
db.commit()


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    return db.execute(sql, a).fetchone()


def ex(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


# ================================ الإعدادات ================================
SETTINGS = {
    "power": ("1", "toggle", "⚡ النظام الكلي"),
    "broadcast_on": ("1", "toggle", "📨 النشر التلقائي"),
    "keyword_on": ("1", "toggle", "🔑 الردود بالكلمات"),
    "notify_on": ("1", "toggle", "🔔 إشعارات الأخطاء"),
    "tag_cd_h": ("6", "float", "⏱ انتظار التاك لنفس الشخص (ساعة)"),
    "kw_cd_h": ("3", "float", "⏱ انتظار الرد لنفس الشخص (ساعة)"),
    "tag_batch": ("3", "int", "👥 عدد الأشخاص بالرسالة الواحدة"),
    "round_max": ("20", "int", "🎯 أقصى أشخاص لكل مجموعة بالجولة"),
    "round_sleep": ("10", "int", "🔁 الانتظار بين الجولات (دقيقة)"),
    "delay_min": ("20", "int", "⏳ أقل فاصل بين رسائل النشر (ثانية)"),
    "delay_max": ("45", "int", "⏳ أكبر فاصل بين رسائل النشر (ثانية)"),
    "reply_delay_min": ("3", "int", "💬 أقل تأخير قبل الرد (ثانية)"),
    "reply_delay_max": ("10", "int", "💬 أكبر تأخير قبل الرد (ثانية)"),
    "quiet_start": ("-1", "int", "🌙 بداية ساعات الهدوء (0-23، -1 معطل)"),
    "quiet_end": ("-1", "int", "🌅 نهاية ساعات الهدوء (0-23)"),
    "tz": ("3", "int", "🌍 فرق التوقيت عن UTC (العراق 3)"),
    "report_hour": ("9", "int", "📊 ساعة التقرير اليومي (-1 معطل)"),
    "backlog_on": ("1", "toggle", "🕒 الرد على رسائل قديمة عند الخمول"),
    "idle_min": ("10", "int", "🕒 مدة الخمول قبل الرد على القديم (دقيقة)"),
    "history_scan": ("400", "int", "📜 عدد الرسائل القديمة اللي يفحصها"),
    "backlog_age_h": ("24", "int", "📜 أقدم رسالة يرد عليها (ساعة، 0 = بلا حد)"),
    "pm_forward": ("1", "toggle", "📩 تحويل الرسائل الخاصة إلك"),
    "max_dialogs": ("450", "int", "🧹 حد القنوات بالحساب قبل المغادرة التلقائية"),
    "leave_on_delete": ("1", "toggle", "🚪 مغادرة المجموعة عند حذفها من البوت"),
    "forced_on": ("1", "toggle", "📢 الانضمام التلقائي للقنوات الإجبارية"),
    "forced_leave_min": ("5", "int", "📢 مغادرة القناة الإجبارية بعد (دقيقة، 0 = لا يغادر)"),
    "forced_max_h": ("12", "int", "📢 أقصى انضمام للقنوات الإجبارية بالساعة لكل حساب"),
    "forced_gap_s": ("40", "int", "📢 أقل فاصل بين انضمامين (ثانية)"),
    "forced_admins": ("0", "toggle", "📢 الانضمام للقنوات اللي يرسلها المشرفون (البوتات دائماً، الأعضاء مرفوضين)"),
    "auto_on": ("1", "toggle", "🔁 الجروبات التلقائية"),
    "special_on": ("1", "toggle", "⭐ الجروبات المخصصة"),
    "daily_cap": ("300", "int", "🚦 أقصى رسائل لكل حساب باليوم (0 = بلا حد)"),
    "auto_gap_min": ("40", "int", "🔁 أقل فاصل بين رسالتين بالجروب التلقائي (ثانية)"),
    "auto_gap_max": ("58", "int", "🔁 أكبر فاصل بين رسالتين بالجروب التلقائي (ثانية)"),
    "auto_gap_msgs": ("20", "int", "🔁 رسائل الغرباء المطلوبة بين رسالتنا والتالية"),
    "auto_stale_min": ("60", "int", "🔁 مدة التأخر قبل إهمال الجروب (دقيقة، 0 = لا يهمل)"),
    "join_gap_min": ("40", "int", "🚪 أقل فاصل بين انضمامين لنفس الحساب (ثانية)"),
    "join_gap_max": ("90", "int", "🚪 أكبر فاصل بين انضمامين لنفس الحساب (ثانية)"),
}


_SC = {}


def _load_settings():
    _SC.clear()
    for r in q("SELECT key, value FROM settings"):
        _SC[r["key"]] = r["value"]


_load_settings()


def S(key):
    # كاش بالذاكرة: S() تنستدعى عشرات المرات لكل رسالة، وكانت كل مرة استعلام قاعدة
    v = _SC.get(key, SETTINGS[key][0])
    return float(v) if SETTINGS[key][1] == "float" else int(float(v))


def set_S(key, val):
    ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", key, str(val))
    _SC[key] = str(val)


def T(key, default=""):
    """قيمة نصية من جدول الإعدادات (رابط/مفتاح)، بلا تحويل لرقم مثل S()."""
    return _SC.get(key, default)


def set_T(key, val):
    set_S(key, val)


def strat_cfg():
    """إعدادات ربط Strat: من زر الإعدادات إن وُجدت، وإلا من متغيرات البيئة."""
    url = (T("strat_api_url", "") or STRAT_API_URL).rstrip("/")
    key = T("strat_api_key", "") or STRAT_API_KEY
    try:
        poll = int(T("strat_poll_seconds", "") or STRAT_POLL_SECONDS)
    except ValueError:
        poll = STRAT_POLL_SECONDS
    return url, key, poll


_BG = set()


def spawn(coro):
    """يشغّل coroutine بالخلفية ويحتفظ بمرجع إلها"""
    t = asyncio.ensure_future(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t


# ================================== أدوات ==================================
def now():
    return int(time.time())


def local_dt():
    return datetime.now(timezone.utc) + timedelta(hours=S("tz"))


def day_start():
    d = local_dt().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((d - timedelta(hours=S("tz"))).timestamp())


def in_quiet():
    a, b = S("quiet_start"), S("quiet_end")
    if a < 0 or b < 0 or a == b:
        return False
    h = local_dt().hour
    return (a <= h < b) if a < b else (h >= a or h < b)


_AR_MARKS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm(s):
    s = _AR_MARKS.sub("", (s or "").lower()).translate(_AR_MAP)
    return re.sub(r"\s+", " ", s).strip()


def plain(s):
    return re.sub(r"[^\w\s]", "", s).strip()


def num(s):
    return (s or "").translate(_DIGITS).replace("،", ".").strip()


def u16(s):
    return len(s.encode("utf-16-le")) // 2


def tick(v):
    return "✅" if v else "⛔"


def split_kw(s):
    return [k.strip() for k in re.split(r"[\n,،]+", s or "") if k.strip()]


_rules = None


def rules():
    global _rules
    if _rules is None:
        _rules = []
        for r in q("SELECT * FROM rules WHERE enabled=1"):
            f = (lambda k: plain(norm(k))) if r["mode"] == "exact" else norm
            kws = [f(k) for k in split_kw(r["keywords"])]
            reps = [x["text"] for x in q("SELECT text FROM replies WHERE rule_id=?", r["id"])]
            kws = [k for k in kws if k]
            if kws and reps:
                _rules.append((r["id"], r["mode"], kws, reps))
    return _rules


def inval():
    global _rules
    _rules = None


def log_event(acc, chat, kind, ok, n=1, err=""):
    ex("INSERT INTO log(ts,account_id,chat_id,kind,ok,n,err) VALUES(?,?,?,?,?,?,?)",
       now(), acc, chat, kind, 1 if ok else 0, n, err)


def member_row(acc, chat, u):
    if getattr(u, "bot", False) or getattr(u, "deleted", False):
        return None
    h = getattr(u, "access_hash", None)
    if not h or getattr(u, "min", False):
        return None
    name = " ".join(x for x in (u.first_name, u.last_name) if x) or "عضو"
    return (acc, chat, u.id, h, name, now())


def save_rows(rows):
    """حفظ دفعة أعضاء بعملية commit وحدة (بدل commit لكل عضو)"""
    if rows:
        db.executemany("INSERT OR REPLACE INTO users(account_id,chat_id,user_id,access_hash,name,seen) "
                       "VALUES(?,?,?,?,?,?)", rows)
        db.commit()


def remember(acc, chat, u):
    row = member_row(acc, chat, u)
    if row:
        save_rows([row])


def build_message(template, users):
    """يبني النص + المنشن (entities) بدون الحاجة لكاش التيليثون"""
    parts = template.split("{mention}")
    if len(parts) == 1:
        pre, post = template.rstrip() + "\n", ""
    else:
        pre, post = parts[0], "".join(parts[1:])
    text, ents = pre, []
    for i, u in enumerate(users):
        nm = re.sub(r"[\r\n]+", " ", u["name"] or "").strip()[:25] or "عضو"
        if i:
            text += "، "
        ents.append(types.InputMessageEntityMentionName(
            u16(text), u16(nm), types.InputUser(u["user_id"], u["access_hash"])))
        text += nm
    return text + post, ents


# ================================ الإشعارات ================================
bot = None
_last_note = {}


async def notify(text, key=None):
    if not S("notify_on"):
        return
    if key:
        if time.time() - _last_note.get(key, 0) < 900:
            return
        _last_note[key] = time.time()
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


# ============================== مشغّل الحساب ==============================
class Runner:
    def __init__(self, acc):
        self.id = acc["id"]
        self.phone = acc["phone"]
        self.session = acc["session"]
        self.client = None
        self.me_id = None
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.last_bc = 0.0  # آخر رسالة نشر (الفاصل الطويل يخص النشر فقط)
        self.paused_until = 0.0
        self.tasks = []
        self.refreshed = {}
        self.started = time.time()
        self.last_kw = {}
        self.forced_seen = {}
        self.forced_lock = asyncio.Lock()
        self.forced_pending = 0
        self.forced_last = 0.0
        self.forced_hist = []
        self.manual_busy = 0
        self.day_key = 0
        self.sent_n = 0
        self.gblock = {}


RUNNERS = {}
FATAL_ACC = {"AuthKeyUnregisteredError", "UserDeactivatedError", "UserDeactivatedBanError",
             "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError"}
GROUP_ERR = {"ChatWriteForbiddenError", "UserBannedInChannelError", "ChannelPrivateError",
             "ChatAdminRequiredError", "ChatRestrictedError", "UserNotParticipantError",
             "ChannelBannedError", "ChatGuestSendForbiddenError"}


def own_ids():
    return {r.me_id for r in RUNNERS.values() if r.me_id}


async def start_runner(acc_id):
    await stop_runner(acc_id)
    acc = q1("SELECT * FROM accounts WHERE id=?", acc_id)
    if not acc or not acc["enabled"]:
        return False
    r = Runner(acc)
    try:
        r.client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
        await r.client.connect()
        if not await r.client.is_user_authorized():
            raise RuntimeError("SessionRevokedError")
        r.me_id = (await r.client.get_me()).id
        try:
            await r.client.get_dialogs(limit=None)  # تعبئة كاش المجموعات
        except Exception as e:
            log.warning("dialogs: %s", e)
    except Exception as e:
        name = type(e).__name__ if not isinstance(e, RuntimeError) else str(e)
        try:
            await r.client.disconnect()
        except Exception:
            pass
        if name in FATAL_ACC:
            ex("UPDATE accounts SET enabled=0 WHERE id=?", acc_id)
            await notify(f"🚨 الحساب {acc['phone']} طلعت جلسته / انحظر ({name}). انوقف تلقائياً، أضفه من جديد.")
        else:
            await notify(f"⚠️ تعذّر تشغيل الحساب {acc['phone']}: {name}. راح أعيد المحاولة بعد دقيقة.", f"start{acc_id}")
            asyncio.get_event_loop().call_later(60, lambda: asyncio.create_task(start_runner(acc_id)))
        return False
    # كل رسالة واردة تنعالج بمهمة مستقلة حتى ما تنتظر الرسائل بعض (تيليثون يعالجها بالتسلسل)
    async def _incoming(ev, r=r):
        spawn(on_incoming(r, ev))  # يرجع فوراً، تيليثون ما ينتظر المعالجة

    r.client.add_event_handler(_incoming, events.NewMessage(incoming=True))
    RUNNERS[acc_id] = r
    ag_sync_account(acc_id)
    r.tasks = [asyncio.create_task(broadcast_loop(r)), asyncio.create_task(backlog_loop(r)),
               asyncio.create_task(ag_join_loop(r))]
    log.info("account %s started", acc["phone"])
    return True


async def stop_runner(acc_id):
    r = RUNNERS.pop(acc_id, None)
    if not r:
        return
    for t in r.tasks:
        t.cancel()
    try:
        await r.client.disconnect()
    except Exception:
        pass


async def account_fatal(r, e):
    name = type(e).__name__
    ex("UPDATE accounts SET enabled=0 WHERE id=?", r.id)
    await notify(f"🚨 الحساب {r.phone} تعطل نهائياً ({name}). انوقف، لازم تضيفه من جديد.")
    asyncio.create_task(stop_runner(r.id))


async def on_send_error(r, ch, kind, e):
    name = type(e).__name__
    log_event(r.id, ch["chat_id"], kind, False, 0, name)
    title = ch["title"]
    if name in ("SlowModeWaitError",):
        return
    if "FloodWait" in name or "FloodPremiumWait" in name:
        sec = int(getattr(e, "seconds", 60))
        r.paused_until = time.time() + sec + 5
        await notify(f"⏳ الحساب {r.phone} انحصر فلود {sec} ثانية (موقوف مؤقتاً).", f"fw{r.id}")
    elif name == "PeerFloodError":
        r.paused_until = time.time() + 3600
        await notify(f"🚫 الحساب {r.phone} جاله PeerFlood (سبام). انوقف ساعة.", f"pf{r.id}")
    elif name in FATAL_ACC:
        await account_fatal(r, e)
    elif name in GROUP_ERR:
        fails = ch["fails"] + 1
        ex("UPDATE chats SET fails=? WHERE id=?", fails, ch["id"])
        if fails >= 3:
            ex("UPDATE chats SET enabled=0 WHERE id=?", ch["id"])
            await notify(f"❌ الحساب {r.phone} ما يكدر يرسل بـ «{title}» ({name}). انوقفت المجموعة تلقائياً.")
        else:
            await notify(f"⚠️ فشل إرسال من {r.phone} بـ «{title}»: {name} ({fails}/3)", f"ge{ch['id']}")
    else:
        await notify(f"⚠️ فشل إرسال من {r.phone} بـ «{title}»: {name}: {str(e)[:150]}", f"oe{r.id}{name}")


async def safe_send(r, ch, text, kind, reply_to=None, entities=None, gap=None, n=1):
    if time.time() < r.paused_until:
        return False
    cap = S("daily_cap")
    if cap > 0 and sent_today(r) >= cap:
        return False
    is_kw = kind == "kw"
    if not is_kw:
        # فاصل النشر الطويل ننتظره "خارج" القفل، حتى ما تعلق الردود وراه (كانت تنتظر لين 45 ثانية)
        lo, hi = sorted((S("delay_min"), S("delay_max")))
        wait = r.last_bc + random.uniform(lo, hi) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        if time.time() < r.paused_until:
            return False
    async with r.lock:
        # فاصل قصير جداً بين أي رسالتين من نفس الحساب (حماية من الفلود)
        wait = r.last + random.uniform(0.5, 1.5) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await r.client.send_message(ch["chat_id"], text, reply_to=reply_to,
                                        formatting_entities=entities, parse_mode=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            r.last = time.time()
            if not is_kw:
                r.last_bc = r.last
            await on_send_error(r, ch, kind, e)
            return False
        r.last = time.time()
        if not is_kw:
            r.last_bc = r.last
        bump(r)
        log_event(r.id, ch["chat_id"], kind, True, n)
        ex("UPDATE chats SET fails=0 WHERE id=?", ch["id"])
        return True


# ================================== النشر ==================================
async def scan_history(r, ch, limit):
    """يجمع الأعضاء من كاتبي الرسائل الأخيرة (ينفع حتى لو الحساب مو مشرف)"""
    if limit <= 0:
        return
    peer = await r.client.get_input_entity(ch["chat_id"])
    pending = {}
    rows = []
    async for m in r.client.iter_messages(ch["chat_id"], limit=limit):
        uid = m.sender_id
        if not uid or uid < 0 or uid in own_ids() or uid in pending:
            continue
        s = m.sender
        if isinstance(s, types.User) and not s.min:
            row = member_row(r.id, ch["chat_id"], s)
            if row:
                rows.append(row)
        elif not (isinstance(s, types.User) and s.bot):
            pending[uid] = m.id
    save_rows(rows)
    items = list(pending.items())
    for i in range(0, len(items), 50):
        try:
            res = await r.client(functions.users.GetUsersRequest(
                [types.InputUserFromMessage(peer, mid, uid) for uid, mid in items[i:i + 50]]))
            save_rows([x for x in (member_row(r.id, ch["chat_id"], u) for u in res
                                   if isinstance(u, types.User)) if x])
        except Exception:
            pass


async def refresh_members(r, ch):
    key = ch["chat_id"]
    if time.time() - r.refreshed.get(key, 0) < 1800:
        return
    r.refreshed[key] = time.time()
    try:
        buf = []
        async for u in r.client.iter_participants(ch["chat_id"], limit=10000):
            row = member_row(r.id, ch["chat_id"], u)
            if row:
                buf.append(row)
            if len(buf) >= 500:  # commit واحد لكل 500 عضو (كان commit لكل عضو = تجميد البوت)
                save_rows(buf)
                buf = []
                await asyncio.sleep(0)
        save_rows(buf)
    except Exception as e:  # الحساب مو مشرف: نكمل بفحص الرسائل القديمة
        log.info("participants %s: %s", ch["title"], type(e).__name__)
    try:
        await scan_history(r, ch, S("history_scan"))
    except Exception as e:
        log.info("history %s: %s", ch["title"], type(e).__name__)


def active_now():
    return S("power") and S("broadcast_on") and not in_quiet()


async def process_chat(r, ch):
    msgs = [m["text"] for m in q("SELECT text FROM messages WHERE enabled=1")]
    if not msgs:
        return
    await refresh_members(r, ch)
    cd = int(S("tag_cd_h") * 3600)
    rows = q("""SELECT u.user_id, u.access_hash, u.name FROM users u
                WHERE u.account_id=? AND u.chat_id=?
                AND u.user_id NOT IN (SELECT user_id FROM blocked)
                AND NOT EXISTS (SELECT 1 FROM cooldowns c WHERE c.chat_id=u.chat_id
                                AND c.user_id=u.user_id AND c.kind='tag' AND c.ts>?)
                ORDER BY RANDOM() LIMIT ?""", r.id, ch["chat_id"], now() - cd, max(1, S("round_max")))
    rows = [u for u in rows if u["user_id"] not in own_ids()]
    batch = max(1, S("tag_batch"))
    for i in range(0, len(rows), batch):
        if not active_now() or time.time() < r.paused_until:
            return
        cur = q1("SELECT * FROM chats WHERE id=?", ch["id"])
        if not cur or not cur["enabled"]:
            return
        chunk = rows[i:i + batch]
        text, ents = build_message(random.choice(msgs), chunk)
        ok = await safe_send(r, cur, text, "tag", entities=ents, n=len(chunk))
        t = now()
        # عند الفشل نعيد المحاولة بعد 30 دقيقة بدل ما نعلق
        ts = t if ok else t - cd + 1800
        db.executemany("INSERT OR REPLACE INTO cooldowns(chat_id,user_id,kind,ts) VALUES(?,?,?,?)",
                       [(ch["chat_id"], u["user_id"], "tag", ts) for u in chunk])
        db.commit()


async def broadcast_loop(r):
    await asyncio.sleep(random.uniform(5, 20))
    while True:
        try:
            if active_now() and time.time() >= r.paused_until:
                for ch in q("SELECT * FROM chats WHERE account_id=? AND enabled=1", r.id):
                    if not active_now():
                        break
                    await process_chat(r, ch)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("broadcast loop")
        await asyncio.sleep(max(60, S("round_sleep") * 60))


# ============================ الردود على الكلمات ============================
# ===================== القنوات الإجبارية (انضمام تلقائي) =====================
_FORCED_LINK = re.compile(r"(?:https?://)?(?:t|telegram)\.me/[\w+/-]+|tg://(?:join\?invite=|resolve\?domain=)[\w-]+", re.I)
_FORCED_USER = re.compile(r"(?<![\w/])@([A-Za-z]\w{3,31})")
_leave_tasks = set()


def forced_refs(ev):
    """يستخرج روابط/يوزرات القنوات من نص الرسالة وروابطها المخفية وأزرارها (مثل رسالة الاشتراك الإجباري)"""
    text = ev.raw_text or ""
    refs = set(_FORCED_LINK.findall(text))
    refs |= {"@" + u for u in _FORCED_USER.findall(text)}
    for e in (ev.message.entities or []):
        url = getattr(e, "url", None)
        if url:
            refs |= set(_FORCED_LINK.findall(url))
    try:
        for row in (ev.message.buttons or []):
            for b in row:
                url = getattr(b, "url", None)
                if url:
                    refs |= set(_FORCED_LINK.findall(url))
    except Exception:
        pass
    return refs


async def forced_leave_later(r, ent, minutes):
    await asyncio.sleep(minutes * 60)
    try:
        managed = managed_ids(r.id)
        if utils.get_peer_id(ent) in managed:
            return  # مجموعة مضافة بالبوت ما تنغادر أبداً
        await r.client.delete_dialog(ent)
        log.info("forced: left %s", getattr(ent, "title", ent))
    except Exception as e:
        log.info("forced leave: %s", type(e).__name__)


async def forced_allowed(r, ev):
    """بوت = دائماً ينضم | مشرف = حسب إعداد المالك | عضو عادي = يتجاهل"""
    snd = await ev.get_sender()
    if getattr(snd, "bot", False):
        return True
    if not S("forced_admins"):
        return False
    if isinstance(snd, (types.Channel, types.Chat)):
        return True  # مشرف مجهول أو رسالة بإسم القناة/المجموعة
    try:
        p = await r.client.get_permissions(ev.chat_id, ev.sender_id)
        return bool(p.is_admin or p.is_creator)
    except Exception:
        return False


async def forced_join(r, ev):
    try:
        if r.manual_busy or r.forced_pending >= 5:
            return
        refs = forced_refs(ev)  # فحص رخيص أول، قبل أي طلب شبكة
        if not refs:
            return
        if not await forced_allowed(r, ev):
            return
        own_user = (getattr(ev.chat, "username", None) or "").lower()
        keep = max(0, S("forced_leave_min"))
        for ref in refs:
            kind, val = parse_ref(ref)
            if kind not in ("invite", "user"):
                continue
            if kind == "user" and val.lower() == own_user:
                continue
            key = f"{kind}:{val.lower()}"
            if time.time() - r.forced_seen.get(key, 0) < keep * 60 + 120:
                continue
            if r.manual_busy or r.forced_pending >= 5:
                return
            r.forced_seen[key] = time.time()
            r.forced_pending += 1
            try:
              async with r.forced_lock:
                gap = max(0, S("forced_gap_s")) - (time.time() - r.forced_last)
                await asyncio.sleep(max(0, gap) + random.uniform(2, 6))
                r.forced_hist = [t for t in r.forced_hist if time.time() - t < 3600]
                if r.manual_busy or len(r.forced_hist) >= S("forced_max_h"):
                    r.forced_seen[key] = 0
                    return
                r.forced_hist.append(time.time())
                r.forced_last = time.time()
                try:
                    if kind == "invite":
                        ent = await join_invite(r, val)
                    else:
                        ent = await join_public(r, await r.client.get_entity(val))
                except ValueError:
                    continue  # يوزر شخص/بوت أو رابط منتهي
                except Exception as e:
                    nm = type(e).__name__
                    if nm == "FloodWaitError":
                        wait = int(getattr(e, "seconds", 60))
                        r.forced_seen[key] = 0
                        await notify(f"⏳ الحساب {r.phone}: FloodWait {wait}ث أثناء الانضمام للقناة الإجبارية.", f"fj{r.id}")
                        await asyncio.sleep(min(wait, 300))
                    elif nm not in ("UsernameNotOccupiedError", "UsernameInvalidError", "ChannelPrivateError"):
                        await notify(f"⚠️ الحساب {r.phone}: تعذّر الانضمام للقناة الإجبارية {ref} ({nm})", f"fj{r.id}")
                    continue
            finally:
                r.forced_pending -= 1
            log.info("forced: %s joined %s", r.phone, getattr(ent, "title", val))
            if keep and ent is not None:
                t = asyncio.create_task(forced_leave_later(r, ent, keep))
                _leave_tasks.add(t)
                t.add_done_callback(_leave_tasks.discard)
    except Exception:
        log.exception("forced_join")


async def on_incoming(r, ev):
    try:
        await handle_incoming(r, ev)
    except Exception:
        log.exception("incoming")


async def handle_incoming(r, ev):
    if ev.is_private:
        return await forward_pm(r, ev)
    if not ev.message:
        return
    ag = ag_lookup(r, ev.chat_id)
    if ag:
        ag_track(ag, ev)  # عدّاد رسائل الغرباء للجروبات التلقائية
    if not ev.sender_id:
        return
    ch = q1("SELECT * FROM chats WHERE account_id=? AND chat_id=? AND enabled=1", r.id, ev.chat_id)
    if not ch and ag:
        # جروب تلقائي/مخصص: نفس نظام الكلمات والردود، وحساب واحد بس يرد
        ch = {"id": None, "chat_id": ev.chat_id, "title": ag["title"], "fails": 0, "_ag": True}
    if not ch:
        return
    uid = ev.sender_id
    if uid in own_ids():
        return
    if S("power") and S("forced_on"):
        asyncio.create_task(forced_join(r, ev))
    sender = await ev.get_sender()
    if not isinstance(sender, types.User) or sender.bot or sender.deleted:
        return
    if getattr(sender, "min", False):
        try:
            res = await r.client(functions.users.GetUsersRequest(
                [types.InputUserFromMessage(await ev.get_input_chat(), ev.id, uid)]))
            sender = res[0]
        except Exception:
            pass
    remember(r.id, ev.chat_id, sender)
    if not (S("power") and S("keyword_on")):
        return
    text = ev.raw_text
    if not text or q1("SELECT 1 FROM blocked WHERE user_id=?", uid):
        return
    hit = match_rule(text)
    if not hit:
        return
    if isinstance(ch, dict):
        # حساب غير جاهز (موقوف/وصل الحد اليومي) يترك الرد لحساب ثاني منضم لنفس الجروب
        cap = S("daily_cap")
        if time.time() < r.paused_until or (cap > 0 and sent_today(r) >= cap):
            return
    cd = int(S("kw_cd_h") * 3600)
    row = q1("SELECT ts FROM cooldowns WHERE chat_id=? AND user_id=? AND kind='kw'", ev.chat_id, uid)
    if row and now() - row["ts"] < cd:
        return
    ex("INSERT OR REPLACE INTO cooldowns(chat_id,user_id,kind,ts) VALUES(?,?,?,?)", ev.chat_id, uid, "kw", now())
    ex("UPDATE rules SET hits=hits+1 WHERE id=?", hit[0])
    ex("INSERT OR IGNORE INTO replied(chat_id,msg_id) VALUES(?,?)", ev.chat_id, ev.id)
    asyncio.create_task(send_reply(r, ch, ev.id, sender, hit[1]))


async def send_reply(r, ch, msg_id, sender, reps):
    await asyncio.sleep(random.uniform(*sorted((S("reply_delay_min"), S("reply_delay_max")))))
    text = random.choice(reps).replace("{name}", sender.first_name or "")
    ok = await safe_send(r, ch, text, "kw", reply_to=msg_id)
    if ok:
        r.last_kw[ch["chat_id"]] = time.time()
    if not ok:
        ex("DELETE FROM cooldowns WHERE chat_id=? AND user_id=? AND kind='kw'", ch["chat_id"], sender.id)


def match_rule(text):
    nt = norm(text)
    pt = plain(nt)
    for rid, mode, kws, reps in rules():
        if (mode == "exact" and pt in kws) or (mode != "exact" and any(k in nt for k in kws)):
            return rid, reps
    return None


async def backlog_reply(r, ch):
    """إذا مرت فترة خمول، يرد على رسالة قديمة فيها كلمة محفزة من شخص مو بوت وما انرد عليه"""
    cd = int(S("kw_cd_h") * 3600)
    max_age = S("backlog_age_h") * 3600
    me = own_ids()
    async for m in r.client.iter_messages(ch["chat_id"], limit=max(10, S("history_scan"))):
        uid = m.sender_id
        if not uid or uid < 0 or uid in me or not m.raw_text:
            continue
        if max_age and time.time() - m.date.timestamp() > max_age:
            break
        s = m.sender
        if not isinstance(s, types.User) or s.bot or s.deleted:
            continue
        if q1("SELECT 1 FROM replied WHERE chat_id=? AND msg_id=?", ch["chat_id"], m.id):
            continue
        if q1("SELECT 1 FROM blocked WHERE user_id=?", uid):
            continue
        row = q1("SELECT ts FROM cooldowns WHERE chat_id=? AND user_id=? AND kind='kw'", ch["chat_id"], uid)
        if row and now() - row["ts"] < cd:
            continue
        hit = match_rule(m.raw_text)
        if not hit:
            continue
        ex("INSERT OR REPLACE INTO cooldowns(chat_id,user_id,kind,ts) VALUES(?,?,?,?)", ch["chat_id"], uid, "kw", now())
        ex("INSERT OR IGNORE INTO replied(chat_id,msg_id) VALUES(?,?)", ch["chat_id"], m.id)
        ex("UPDATE rules SET hits=hits+1 WHERE id=?", hit[0])
        remember(r.id, ch["chat_id"], s)
        await send_reply(r, ch, m.id, s, hit[1])
        return True
    return False


async def backlog_loop(r):
    await asyncio.sleep(30)
    while True:
        try:
            if S("power") and S("keyword_on") and S("backlog_on") and time.time() >= r.paused_until:
                idle = max(1, S("idle_min")) * 60
                for ch in q("SELECT * FROM chats WHERE account_id=? AND enabled=1", r.id):
                    if time.time() - r.last_kw.get(ch["chat_id"], r.started) >= idle:
                        r.last_kw[ch["chat_id"]] = time.time()  # فترة خمول جديدة
                        await backlog_reply(r, ch)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("backlog")
        await asyncio.sleep(30)


async def forward_pm(r, ev):
    """الرسائل الخاصة للحساب تنحول للمالك مع زر رد"""
    if not (S("pm_forward") and ev.sender_id):
        return
    s = await ev.get_sender()
    if not isinstance(s, types.User) or s.is_self or s.bot:
        return
    if s.access_hash:
        ex("INSERT OR REPLACE INTO pm_contacts(account_id,user_id,access_hash,name) VALUES(?,?,?,?)",
           r.id, s.id, s.access_hash, utils.get_display_name(s))
    un = f" @{s.username}" if s.username else ""
    tag = "🔐 رسالة من تيليجرام" if s.id == 777000 else "📩 رسالة خاصة"
    head = f"{tag} للحساب {r.phone}\n👤 {utils.get_display_name(s)}{un}\n🆔 {s.id}"
    text = f"{head}\n\n{ev.raw_text or ''}".strip()
    btns = None if s.id == 777000 else [[B("↩️ رد", f"pmr:{r.id}:{s.id}"), B("🚫 حظر", f"blk:{s.id}")]]
    data, f = None, ev.file
    if f and (f.size or 0) < 15 * 1024 * 1024:
        try:
            data = await ev.download_media(bytes)
        except Exception:
            data = None
    for a in ADMIN_IDS:
        try:
            if data:
                bio = io.BytesIO(data)
                bio.name = f.name or ("file" + (f.ext or ""))
                await bot.send_file(a, bio, caption=text[:1000], buttons=btns)
            else:
                extra = "\n📎 (مرفق ما انحمّل)" if f else ""
                await bot.send_message(a, (text + extra)[:4000], buttons=btns)
        except Exception:
            log.exception("forward pm")


# ======================= الجروبات التلقائية والمخصصة =======================
class JoinRequestSent(ValueError):
    """انرسل طلب انضمام والإدارة لازم توافق"""


AG_KINDS = ("auto", "special")
AG_STATE = {}
AG_TASKS = {}
_AGC = None


def ag_inval():
    global _AGC
    _AGC = None


def ag_cache():
    """(chat_id -> جروب فعال, group_id -> مجموعة ايديات الحسابات المنضمة)"""
    global _AGC
    if _AGC is None:
        by_chat, joined = {}, {}
        for g in q("SELECT * FROM agroups WHERE chat_id IS NOT NULL AND enabled=1"):
            by_chat[g["chat_id"]] = dict(g)
        for m in q("SELECT group_id, account_id FROM ag_members WHERE status='joined'"):
            joined.setdefault(m["group_id"], set()).add(m["account_id"])
        _AGC = (by_chat, joined)
    return _AGC


def ag_lookup(r, chat_id):
    by_chat, joined = ag_cache()
    g = by_chat.get(chat_id)
    if g and r.id in joined.get(g["id"], ()):
        return g
    return None


def ag_joined_ids(gid):
    return ag_cache()[1].get(gid, set())


def ag_st(gid):
    st = AG_STATE.get(gid)
    if st is None:
        st = AG_STATE[gid] = {"gap": 0, "seen": set(), "last_sent": 0.0, "last_acc": 0, "next_ts": 0.0,
                              "wait_since": 0.0, "stale": False, "inited": False, "recent": []}
    return st


def ag_member_counts(gid):
    d = {"joined": 0, "requested": 0, "pending": 0, "failed": 0, "total": 0}
    for x in q("SELECT status, COUNT(*) c FROM ag_members WHERE group_id=? GROUP BY status", gid):
        d[x["status"]] = d.get(x["status"], 0) + x["c"]
        d["total"] += x["c"]
    return d


def managed_ids(acc_id):
    """كل مجموعة مضافة بالبوت (النشر القديم + التلقائي + المخصص) ما تنغادر أبداً"""
    s = {c["chat_id"] for c in q("SELECT chat_id FROM chats WHERE account_id=?", acc_id)}
    s |= {g["chat_id"] for g in q("SELECT chat_id FROM agroups WHERE chat_id IS NOT NULL")}
    return s


def sent_today(r):
    """عدد رسائل الحساب الناجحة اليوم (كل الأنواع) - بالذاكرة حتى ما نضغط القاعدة"""
    d = day_start()
    if r.day_key != d:
        r.day_key = d
        r.sent_n = q1("SELECT COUNT(*) c FROM log WHERE account_id=? AND ts>=? AND ok=1", r.id, d)["c"]
    return r.sent_n


def bump(r):
    sent_today(r)
    r.sent_n += 1


def ag_sync_account(acc_id):
    ex("INSERT OR IGNORE INTO ag_members(group_id, account_id) SELECT id, ? FROM agroups", acc_id)
    ag_inval()


def ag_sync_group(gid):
    ex("INSERT OR IGNORE INTO ag_members(group_id, account_id) SELECT ?, id FROM accounts", gid)
    ag_inval()


def ag_start(gid):
    t = AG_TASKS.get(gid)
    if t and not t.done():
        return
    AG_TASKS[gid] = asyncio.create_task(ag_loop(gid))


def ag_stop(gid):
    t = AG_TASKS.pop(gid, None)
    if t:
        t.cancel()
    AG_STATE.pop(gid, None)


# ---------------------------- الانضمام للجروبات ----------------------------
def ag_mark_joined(r, gid, mid, ent, was_requested=False):
    cid = utils.get_peer_id(ent)
    title = utils.get_display_name(ent) or ""
    ex("UPDATE agroups SET chat_id=?, title=? WHERE id=?", cid, title, gid)
    ex("UPDATE ag_members SET status='joined', err=NULL WHERE id=?", mid)
    ag_inval()
    log.info("ag: %s joined %s", r.phone, title)
    return title


async def ag_join_one(r, row):
    mid, gid = row["mid"], row["id"]
    label = row["title"] or row["link"]
    try:
        await asyncio.wait_for(ensure_room(r), 120)
        if row["hash"]:
            ent = await join_invite(r, row["hash"])
        else:
            ent = await join_public(r, await r.client.get_entity(row["uname"]))
    except JoinRequestSent:
        ex("UPDATE ag_members SET status='requested', err=NULL, checked=? WHERE id=?", now(), mid)
        ag_inval()
        await notify(f"⏳ انرسل طلب انضمام للجروب «{label}» (بانتظار موافقة الإدارة). "
                     f"أول ما ينقبل الحساب يبدأ ينشر تلقائياً.", f"agreq{gid}")
        return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        nm = type(e).__name__
        if nm == "FloodWaitError":
            wait = int(getattr(e, "seconds", 60))
            await notify(f"⏳ الحساب {r.phone}: FloodWait {wait}ث أثناء الانضمام للجروب «{label}».", f"agfw{r.id}")
            await asyncio.sleep(min(wait + 5, 900))
            return
        tries = (row["tries"] or 0) + 1
        final = (isinstance(e, ValueError) or tries >= 3 or nm in (
            "InviteHashExpiredError", "InviteHashInvalidError", "UsernameNotOccupiedError",
            "UsernameInvalidError", "ChannelPrivateError", "UserBannedInChannelError"))
        ex("UPDATE ag_members SET tries=?, status=?, err=? WHERE id=?",
           tries, "failed" if final else "pending", err_text(e)[:150], mid)
        ag_inval()
        if final:
            await notify(f"❌ الحساب {r.phone} ما كدر ينضم للجروب «{label}»: {err_text(e)}", f"agjf{gid}{nm}")
        return
    title = ag_mark_joined(r, gid, mid, ent)
    if isinstance(ent, types.Channel) and ent.broadcast and not ent.megagroup:
        await notify(f"⚠️ «{title}» قناة بث، الحساب لازم يكون مشرف حتى ينشر.", f"agbc{gid}")


async def ag_check_requested(r, row):
    """يفحص إذا طلب الانضمام انقبل"""
    mid, gid = row["mid"], row["id"]
    ent = None
    try:
        if row["hash"]:
            info = await r.client(functions.messages.CheckChatInviteRequest(row["hash"]))
            if type(info).__name__ == "ChatInviteAlready":
                ent = getattr(info, "chat", None)
        else:
            e0 = await r.client.get_entity(row["uname"])
            await r.client.get_permissions(e0, "me")  # يرمي خطأ إذا مو منضم
            ent = e0
    except asyncio.CancelledError:
        raise
    except Exception:
        ent = None
    if ent is not None:
        title = ag_mark_joined(r, gid, mid, ent, True)
        await notify(f"✅ انقبل طلب الانضمام للحساب {r.phone} بالجروب «{title}». يبدأ النشر هسه.", f"agok{gid}")
    else:
        ex("UPDATE ag_members SET checked=? WHERE id=?", now(), mid)


async def ag_join_loop(r):
    await asyncio.sleep(random.uniform(10, 40))
    while True:
        wait = 20
        try:
            if S("power") and time.time() >= r.paused_until and not r.manual_busy:
                row = q1("""SELECT m.id AS mid, m.tries AS tries, g.* FROM ag_members m
                            JOIN agroups g ON g.id=m.group_id
                            WHERE m.account_id=? AND m.status='pending' AND g.enabled=1
                            ORDER BY m.id LIMIT 1""", r.id)
                if row:
                    await ag_join_one(r, row)
                    wait = random.uniform(*sorted((S("join_gap_min"), S("join_gap_max"))))
                else:
                    row = q1("""SELECT m.id AS mid, m.tries AS tries, g.* FROM ag_members m
                                JOIN agroups g ON g.id=m.group_id
                                WHERE m.account_id=? AND m.status='requested' AND m.checked<? AND g.enabled=1
                                ORDER BY m.checked LIMIT 1""", r.id, now() - 300)
                    if row:
                        await ag_check_requested(r, row)
                        wait = 5
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ag join loop")
        await asyncio.sleep(wait)


async def ag_leave_all(chat_id, acc_ids):
    for aid in acc_ids:
        r = RUNNERS.get(aid)
        if r:
            await leave_chat(r, chat_id)
            await asyncio.sleep(random.uniform(2, 5))


# ------------------------------ تتبع الرسائل ------------------------------
def ag_track(g, ev):
    """عدّاد رسائل الغرباء (كل حساب منضم يستلم نفس الرسالة، فنمنع العد المكرر بالايدي)"""
    st = ag_st(g["id"])
    if ev.id in st["seen"]:
        return
    st["seen"].add(ev.id)
    if len(st["seen"]) > 600:
        st["seen"] = set(sorted(st["seen"])[-300:])
    if getattr(ev.message, "action", None):
        return  # رسائل النظام (انضمام/مغادرة)
    if ev.sender_id in own_ids():
        return
    st["gap"] += 1


async def ag_init(g, r):
    """عند أول تشغيل: نعدّ رسائل الغرباء من آخر رسالة لحساباتنا"""
    st = ag_st(g["id"])
    me = own_ids()
    try:
        async for m in r.client.iter_messages(g["chat_id"], limit=80):
            if m.sender_id in me:
                st["last_sent"] = m.date.timestamp()
                break
            if getattr(m, "action", None) or m.id in st["seen"]:
                continue
            st["seen"].add(m.id)
            st["gap"] += 1
    except Exception as e:
        log.info("ag init %s: %s", g["title"], type(e).__name__)
    if st["last_sent"] and time.time() - st["last_sent"] < S("auto_gap_min"):
        st["next_ts"] = st["last_sent"] + S("auto_gap_min")


# ------------------------------- الإرسال -------------------------------
async def on_group_error(r, g, kind, e):
    name = type(e).__name__
    log_event(r.id, g["chat_id"], kind, False, 0, name)
    label = g["title"] or g["link"]
    if name == "SlowModeWaitError":
        r.gblock[g["id"]] = time.time() + int(getattr(e, "seconds", 30)) + 2
    elif "FloodWait" in name or "FloodPremiumWait" in name:
        sec = int(getattr(e, "seconds", 60))
        r.paused_until = time.time() + sec + 5
        await notify(f"⏳ الحساب {r.phone} انحصر فلود {sec} ثانية (موقوف مؤقتاً).", f"fw{r.id}")
    elif name == "PeerFloodError":
        r.paused_until = time.time() + 3600
        await notify(f"🚫 الحساب {r.phone} جاله PeerFlood (سبام). انوقف ساعة.", f"pf{r.id}")
    elif name in FATAL_ACC:
        await account_fatal(r, e)
    elif name in GROUP_ERR:
        ex("UPDATE ag_members SET status='failed', err=? WHERE group_id=? AND account_id=?", name, g["id"], r.id)
        ag_inval()
        await notify(f"❌ الحساب {r.phone} ما يكدر يرسل بالجروب «{label}» ({name}). انشال من هذا الجروب، "
                     f"وتكدر ترجعه بزر إعادة المحاولة.", f"agge{g['id']}{r.id}")
    else:
        await notify(f"⚠️ فشل إرسال من {r.phone} بالجروب «{label}»: {name}: {str(e)[:150]}", f"ago{r.id}{name}")


async def send_group(r, g, text, kind):
    if time.time() < r.paused_until:
        return False
    cap = S("daily_cap")
    if cap > 0 and sent_today(r) >= cap:
        return False
    async with r.lock:
        wait = r.last + random.uniform(0.5, 1.5) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            try:
                await r.client.send_message(g["chat_id"], text, parse_mode=None)
            except ValueError:  # الكيان مو بالكاش (مثلاً بعد قبول الطلب): نحدّث الدايلوقات ونعيد مرة وحدة
                await r.client.get_dialogs(limit=None)
                await r.client.send_message(g["chat_id"], text, parse_mode=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            r.last = r.last_bc = time.time()
            await on_group_error(r, g, kind, e)
            return False
        r.last = r.last_bc = time.time()
        bump(r)
        log_event(r.id, g["chat_id"], kind, True, 1)
        return True


def ag_ready(g):
    """الحسابات المنضمة للجروب والجاهزة للإرسال هسه"""
    lo = min(S("delay_min"), S("delay_max"))
    cap = S("daily_cap")
    t = time.time()
    out = []
    for aid in ag_joined_ids(g["id"]):
        r = RUNNERS.get(aid)
        if not r or t < r.paused_until or t < r.gblock.get(g["id"], 0):
            continue
        if cap > 0 and sent_today(r) >= cap:
            continue
        if t - r.last_bc < lo:
            continue
        out.append(r)
    return out


def pick_auto_text(st):
    rows = [x["text"] for x in q("SELECT text FROM ag_msgs WHERE kind='auto' AND enabled=1")]
    if not rows:
        return None
    pool = [t for t in rows if t not in st["recent"]] or rows
    t = random.choice(pool)
    keep = min(5, len(rows) - 1)
    st["recent"] = (st["recent"] + [t])[-keep:] if keep > 0 else []
    return t


async def ag_tick_auto(g):
    """جروب تلقائي: رسالة كل دقيقة تقريباً، بشرط وصول X رسالة من غرباء بين رسالتنا والتالية"""
    st = ag_st(g["id"])
    t = time.time()
    if not st["inited"]:
        rs = [RUNNERS[a] for a in ag_joined_ids(g["id"]) if a in RUNNERS]
        if not rs:
            return 15
        st["inited"] = True
        await ag_init(g, random.choice(rs))
        t = time.time()
    if t < st["next_ts"]:
        return max(0.5, min(3, st["next_ts"] - t))
    need = max(0, S("auto_gap_msgs"))
    if st["gap"] < need:
        if not st["wait_since"]:
            st["wait_since"] = t
        stale = S("auto_stale_min") * 60
        if stale and not st["stale"] and t - st["wait_since"] > stale:
            st["stale"] = True
            await notify(f"⏸ الجروب «{g['title']}» متأخر: ما وصلت {need} رسالة من غرباء خلال "
                         f"{S('auto_stale_min')} دقيقة. انهمل مؤقتاً ويرجع تلقائياً لما ينشط.", f"stale{g['id']}")
        return 15 if st["stale"] else 3
    rs = ag_ready(g)
    if not rs:
        if ag_joined_ids(g["id"]):
            await notify(f"⚠️ ما اكو حساب متاح للجروب «{g['title']}» هسه (كلهم موقوفين أو وصلوا الحد اليومي "
                         f"{S('daily_cap')}). زيد حسابات.", f"nocap{g['id']}")
        return 5
    cand = [r for r in rs if r.id != st["last_acc"]] or rs
    cand.sort(key=lambda r: (sent_today(r), random.random()))
    r = cand[0]
    text = pick_auto_text(st)
    if not text:
        await notify("⚠️ ماكو كلائش تلقائية. أضف من: الجروبات التلقائية ← الكلائش.", "noam")
        return 30
    ok = await send_group(r, g, text, "auto")
    t = time.time()
    if ok:
        st.update(gap=0, last_sent=t, last_acc=r.id, wait_since=0.0, stale=False,
                  next_ts=t + random.uniform(*sorted((S("auto_gap_min"), S("auto_gap_max")))))
    else:
        st["next_ts"] = t + 3
    return 1


async def ag_tick_special(g):
    """جروب مخصص: الحسابات تدور بالتناوب، كل حساب بكليشته الثابتة، والفاصل من إعداد الجروب"""
    st = ag_st(g["id"])
    t = time.time()
    if t < st["next_ts"]:
        return max(0.5, min(3, st["next_ts"] - t))
    sp = [x["text"] for x in q("SELECT text FROM ag_msgs WHERE kind='special' AND enabled=1 ORDER BY id")]
    if not sp:
        await notify("⚠️ ماكو كلائش مخصصة. أضف من: الجروبات المخصصة ← الكلائش.", "nosp")
        return 30
    rs = ag_ready(g)
    if not rs:
        return 5
    rs.sort(key=lambda r: r.id)
    r = next((x for x in rs if x.id > st["last_acc"]), rs[0])
    ids = [a["id"] for a in q("SELECT id FROM accounts WHERE enabled=1 ORDER BY id")]
    idx = ids.index(r.id) if r.id in ids else 0
    ok = await send_group(r, g, sp[idx % len(sp)], "special")
    t = time.time()
    st["last_acc"] = r.id
    gap = max(3, g["gap_s"] or 30)
    st["next_ts"] = t + (gap * random.uniform(0.8, 1.2) if ok else 3)
    if ok:
        st["last_sent"] = t
    return 1


async def ag_loop(gid):
    await asyncio.sleep(random.uniform(2, 8))
    while True:
        delay = 5
        try:
            g = q1("SELECT * FROM agroups WHERE id=?", gid)
            if not g:
                return
            key = "auto_on" if g["kind"] == "auto" else "special_on"
            if S("power") and g["enabled"] and S(key) and not in_quiet() and g["chat_id"]:
                delay = await (ag_tick_auto if g["kind"] == "auto" else ag_tick_special)(g)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ag loop")
        await asyncio.sleep(delay)


# ================================ الإحصائيات ================================
def counts(since, acc=None):
    sql = "SELECT kind, ok, COUNT(*) c, COALESCE(SUM(n),0) n FROM log WHERE ts>=?"
    args = [since]
    if acc:
        sql += " AND account_id=?"
        args.append(acc)
    d = {"tag": 0, "tagp": 0, "kw": 0, "auto": 0, "fail": 0}
    for r in q(sql + " GROUP BY kind, ok", *args):
        if r["ok"]:
            if r["kind"] == "tag":
                d["tag"] += r["c"]
                d["tagp"] += r["n"]
            elif r["kind"] in ("auto", "special"):
                d["auto"] += r["c"]
            else:
                d["kw"] += r["c"]
        else:
            d["fail"] += r["c"]
    return d


def fmt(d):
    return f"📨 نشر: {d['tag']} رسالة ({d['tagp']} تاك) | 🔁 جروبات: {d['auto']} | 💬 ردود: {d['kw']} | ❌ فشل: {d['fail']}"


def stats_text():
    t0 = day_start()
    n_acc = q1("SELECT COUNT(*) c FROM accounts")["c"]
    n_chat = q1("SELECT COUNT(*) c FROM chats WHERE enabled=1")["c"]
    n_users = q1("SELECT COUNT(DISTINCT user_id) c FROM users")["c"]
    out = [f"📊 الإحصائيات\n",
           f"🟢 حسابات شغالة: {len(RUNNERS)}/{n_acc} | 📂 مجموعات فعالة: {n_chat}",
           f"👥 أعضاء معروفين: {n_users} | 📨 رسائل نشر: {q1('SELECT COUNT(*) c FROM messages WHERE enabled=1')['c']}"
           f" | 🔑 قواعد: {q1('SELECT COUNT(*) c FROM rules WHERE enabled=1')['c']}\n",
           f"📅 اليوم:\n{fmt(counts(t0))}",
           f"🗓 آخر 7 أيام:\n{fmt(counts(t0 - 6 * 86400))}",
           f"📈 الإجمالي:\n{fmt(counts(0))}"]
    ags = q("SELECT * FROM agroups ORDER BY kind, id")
    if ags:
        out.append("\n🔁 الجروبات التلقائية/المخصصة:")
        for g in ags:
            m = ag_member_counts(g["id"])
            n_today = (q1("SELECT COUNT(*) c FROM log WHERE chat_id=? AND ok=1 AND ts>=? AND kind IN ('auto','special')",
                          g["chat_id"], day_start())["c"] if g["chat_id"] else 0)
            flag = " ⏸" if ag_st(g["id"])["stale"] else ""
            out.append(f"• {'🔁' if g['kind'] == 'auto' else '⭐'} {g['title'] or g['link']}: "
                       f"منضم {m['joined']}/{m['total']} | اليوم {n_today}{flag}")
        cap = S("daily_cap")
        n_auto = sum(1 for g in ags if g["kind"] == "auto" and g["enabled"])
        if cap > 0 and n_auto:
            need = math.ceil(n_auto * 1440 / cap)
            if len(RUNNERS) < need:
                out.append(f"⚠️ لتغطية كل دقيقة بـ {n_auto} جروب تلقائي تحتاج تقريباً {need} حساب "
                           f"(الحد {cap} رسالة/يوم)، عندك {len(RUNNERS)}.")
    accs = q("SELECT * FROM accounts")
    if accs:
        out.append("\n👤 اليوم لكل حساب:")
        for a in accs:
            out.append(f"• {a['phone']}: {fmt(counts(t0, a['id']))}")
    top = q("SELECT chat_id, COUNT(*) c FROM log WHERE ts>=? AND ok=1 GROUP BY chat_id ORDER BY c DESC LIMIT 5", t0)
    if top:
        out.append("\n🏆 أنشط المجموعات اليوم:")
        for x in top:
            c = q1("SELECT title FROM chats WHERE chat_id=?", x["chat_id"])
            out.append(f"• {c['title'] if c else x['chat_id']}: {x['c']}")
    errs = q("SELECT err, COUNT(*) c FROM log WHERE ts>=? AND ok=0 GROUP BY err ORDER BY c DESC LIMIT 5", t0)
    if errs:
        out.append("\n⚠️ أخطاء اليوم:")
        out += [f"• {e['err']} × {e['c']}" for e in errs]
    return "\n".join(out)


def cleanup():
    ex("DELETE FROM log WHERE ts<?", now() - 90 * 86400)
    ex("DELETE FROM cooldowns WHERE ts<?", now() - 3 * 86400)
    ex("DELETE FROM replied WHERE rowid IN (SELECT rowid FROM replied ORDER BY rowid DESC LIMIT -1 OFFSET 20000)")


async def report_loop():
    last, ticks = None, 0
    while True:
        await asyncio.sleep(60)
        ticks += 1
        if ticks % 360 == 0:
            cleanup()
        h = S("report_hour")
        d = local_dt()
        if h >= 0 and d.hour == h and last != d.date():
            last = d.date()
            await notify("🗞 التقرير اليومي\n\n" + stats_text())


# ================================ واجهة البوت ================================
STATE = {}


async def _disconnect(c):
    try:
        await c.disconnect()
    except Exception:
        pass


def clear_state(uid):
    st = STATE.pop(uid, None)
    if st and st.get("client"):
        # إصلاح الخطأ: disconnect() بتيليثون يرجع Future مو coroutine فـ create_task تفشل
        spawn(_disconnect(st["client"]))


def admin_only(f):
    async def run(ev):
        try:
            await f(ev)
        except MessageNotModifiedError:
            pass
        except Exception:
            log.exception("ui error")

    async def w(ev):
        if ev.sender_id not in ADMIN_IDS:
            return
        # كل ضغطة/رسالة بمهمة مستقلة: العمليات الطويلة (إضافة مجموعة، فحص...) ما تجمّد باقي الأزرار
        spawn(run(ev))
    return w


async def ui(ev, text, btns=None):
    text = text[:4000]
    if isinstance(ev, events.CallbackQuery.Event):
        await ev.edit(text, buttons=btns)
    else:
        await ev.respond(text, buttons=btns)


def B(label, data):
    return Button.inline(label, data)


def main_menu():
    p = "🟢" if S("power") else "🔴"
    return [[B("👤 الحسابات", "accs"), B("📨 رسائل النشر", "msgs")],
            [B("🔑 الكلمات والردود", "rules"), B("📊 الإحصائيات", "stats")],
            [B("🔁 الجروبات التلقائية", "aglist:auto"), B("⭐ الجروبات المخصصة", "aglist:special")],
            [B("⚙️ الإعدادات", "sets"), B(f"{p} تشغيل/إيقاف الكل", "power")],
            [B("🔗 ربط Strat", "strat")]]


HELP = ("🤖 لوحة التحكم\n\nأوامر إضافية:\n"
        "/block ايدي  - حظر شخص من التاك والردود\n"
        "/unblock ايدي - فك الحظر\n"
        "/blocked - قائمة المحظورين\n"
        "/check - فحص الحسابات\n"
        "/status - الإحصائيات\n"
        "/backup - نسخة احتياطية من القاعدة\n"
        "/cancel - إلغاء الإدخال الحالي")


async def v_main(ev):
    await ui(ev, HELP, main_menu())


async def v_accs(ev):
    rows = q("SELECT * FROM accounts")
    btns = [[B(f"{tick(a['enabled'] and a['id'] in RUNNERS)} {a['name'] or ''} {a['phone']}", f"acc:{a['id']}")] for a in rows]
    btns += [[B("➕ إضافة حساب", "accadd")], [B("🔙 رجوع", "main")]]
    await ui(ev, f"👤 الحسابات ({len(rows)})\nكل حساب له مجموعاته الخاصة.", btns)


async def v_acc(ev, i):
    a = q1("SELECT * FROM accounts WHERE id=?", i)
    if not a:
        return await v_accs(ev)
    n = q1("SELECT COUNT(*) c FROM chats WHERE account_id=?", i)["c"]
    r = RUNNERS.get(i)
    st = "🟢 شغال" if r else ("⛔ متوقف" if not a["enabled"] else "🔴 غير متصل")
    if r and time.time() < r.paused_until:
        st += f" (⏳ موقوف مؤقتاً {int(r.paused_until - time.time()) // 60 + 1} د)"
    txt = f"👤 {a['name'] or ''}\n📱 {a['phone']}\nالحالة: {st}\n📂 المجموعات: {n}\n\n{fmt(counts(day_start(), i))}"
    btns = [[B("📂 المجموعات / القنوات", f"chats:{i}")],
            [B("🧹 تنظيف غير المضاف", f"accclean:{i}")],
            [B("⏸ إيقاف" if a["enabled"] else "▶️ تشغيل", f"acctog:{i}"), B("🗑 حذف", f"accdel:{i}")],
            [B("🔙 رجوع", "accs")]]
    await ui(ev, txt, btns)


async def v_chats(ev, i):
    rows = q("SELECT * FROM chats WHERE account_id=?", i)
    btns = [[B(f"{tick(c['enabled'])} {c['title']}", f"chat:{c['id']}")] for c in rows]
    btns += [[B("➕ إضافة مجموعة/قناة", f"chatadd:{i}")], [B("🔙 رجوع", f"acc:{i}")]]
    await ui(ev, f"📂 مجموعات هذا الحساب ({len(rows)})", btns)


async def v_chat(ev, i):
    c = q1("SELECT * FROM chats WHERE id=?", i)
    if not c:
        return await v_accs(ev)
    known = q1("SELECT COUNT(*) n FROM users WHERE account_id=? AND chat_id=?", c["account_id"], c["chat_id"])["n"]
    txt = (f"📂 {c['title']}\n🆔 {c['chat_id']}\nالحالة: {tick(c['enabled'])}\n"
           f"أخطاء متتالية: {c['fails']}\n👥 أعضاء معروفين: {known}")
    btns = [[B("⏸ إيقاف" if c["enabled"] else "▶️ تشغيل", f"chattog:{i}"), B("🗑 حذف", f"chatdel:{i}")],
            [B("🔙 رجوع", f"chats:{c['account_id']}")]]
    await ui(ev, txt, btns)


async def v_msgs(ev):
    rows = q("SELECT * FROM messages")
    btns = [[B(f"{tick(m['enabled'])} {m['text'][:35]}", f"msg:{m['id']}")] for m in rows]
    btns += [[B("➕ إضافة رسالة", "msgadd")], [B("🔙 رجوع", "main")]]
    await ui(ev, "📨 رسائل النشر (تنختار عشوائياً)\n\nتكدر تكتب {mention} بمكان التاك، وإذا ما كتبتها يندز التاك تحت الرسالة.", btns)


async def v_msg(ev, i):
    m = q1("SELECT * FROM messages WHERE id=?", i)
    if not m:
        return await v_msgs(ev)
    btns = [[B("⏸ إيقاف" if m["enabled"] else "▶️ تشغيل", f"msgtog:{i}"), B("🗑 حذف", f"msgdel:{i}")],
            [B("🔙 رجوع", "msgs")]]
    await ui(ev, f"📨 رسالة #{i} {tick(m['enabled'])}\n\n{m['text']}", btns)


async def v_rules(ev):
    rows = q("SELECT * FROM rules")
    btns = [[B(f"{tick(r['enabled'])} {' ، '.join(split_kw(r['keywords']))[:30]} ({r['hits']})", f"rule:{r['id']}")] for r in rows]
    btns += [[B("➕ قاعدة جديدة", "ruleadd")], [B("🔙 رجوع", "main")]]
    await ui(ev, "🔑 الكلمات والردود\nكل قاعدة: كلمات + ردود (يختار رد عشوائي).", btns)


async def v_rule(ev, i):
    r = q1("SELECT * FROM rules WHERE id=?", i)
    if not r:
        return await v_rules(ev)
    reps = q("SELECT * FROM replies WHERE rule_id=?", i)
    txt = (f"🔑 قاعدة #{i} {tick(r['enabled'])}\n\nالكلمات: {' ، '.join(split_kw(r['keywords']))}\n"
           f"المطابقة: {'تامة (الرسالة = الكلمة)' if r['mode'] == 'exact' else 'تحتوي الكلمة'}\n"
           f"مرات التفعيل: {r['hits']}\n\nالردود ({len(reps)}):\n"
           + "\n".join(f"{n}. {x['text'][:150]}" for n, x in enumerate(reps, 1)))
    btns = [[B("➕ رد جديد", f"repadd:{i}"), B("✏️ الكلمات", f"rulekw:{i}")],
            [B("🎯 نوع المطابقة", f"rulemode:{i}"), B("⏸ إيقاف" if r["enabled"] else "▶️ تشغيل", f"ruletog:{i}")]]
    row = []
    for n, x in enumerate(reps, 1):
        row.append(B(f"🗑 رد {n}", f"repdel:{x['id']}"))
        if len(row) == 3:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns += [[B("🗑 حذف القاعدة", f"ruledel:{i}"), B("🔙 رجوع", "rules")]]
    await ui(ev, txt, btns)


async def v_sets(ev):
    btns = []
    for k, (d, kind, label) in SETTINGS.items():
        v = S(k)
        btns.append([B(f"{tick(v)} {label}" if kind == "toggle" else f"{label}: {v}", f"set:{k}")])
    btns.append([B("🔙 رجوع", "main")])
    await ui(ev, "⚙️ الإعدادات\nاضغط على الإعداد لتغييره.", btns)


AG_TITLE = {"auto": "🔁 الجروبات التلقائية", "special": "⭐ الجروبات المخصصة"}
AG_INFO = {
    "auto": ("كل دقيقة لازم توصل رسالة من أحد الحسابات لكل جروب (حساب مختلف كل مرة)، "
             "وبشرط ما تنرسل إلا بعد وصول عدد معين من رسائل الغرباء بين رسالتنا والتالية. "
             "الجروب المتأخر ينهمل مؤقتاً."),
    "special": ("جروب يبقى يتفاعل بيه البوت دائماً: الحسابات ترسل بالتناوب وبفاصل تحدده لكل جروب، "
                "وكل حساب مربوط بكليشة وحدة ثابتة."),
}


async def v_aglist(ev, kind):
    rows = q("SELECT * FROM agroups WHERE kind=? ORDER BY id", kind)
    btns = []
    for g in rows:
        m = ag_member_counts(g["id"])
        btns.append([B(f"{tick(g['enabled'])} {g['title'] or g['link']} ({m['joined']}/{m['total']})"[:60], f"agv:{g['id']}")])
    msg_label = "📝 الكلائش التلقائية" if kind == "auto" else "🎭 كلائش الحسابات"
    btns += [[B("➕ إضافة جروبات", f"agadd:{kind}")], [B(msg_label, f"amlist:{kind}")], [B("🔙 رجوع", "main")]]
    await ui(ev, f"{AG_TITLE[kind]} ({len(rows)})\n\n{AG_INFO[kind]}\n\n(الرقم بين القوسين = الحسابات المنضمة/الكل)", btns)


async def v_ag(ev, gid):
    g = q1("SELECT * FROM agroups WHERE id=?", gid)
    if not g:
        return await v_main(ev)
    m = ag_member_counts(gid)
    st = ag_st(gid)
    kind = g["kind"]
    lines = [f"{AG_TITLE[kind][:2]} {g['title'] or g['link']}", f"🔗 {g['link']}",
             f"النوع: {'خاص' if g['access'] == 'private' else 'عام'} | الحالة: {tick(g['enabled'])}",
             f"👤 منضم: {m['joined']} | ⏳ بانتظار قبول الإدارة: {m['requested']} | "
             f"🕓 بانتظار الانضمام: {m['pending']} | ❌ فشل: {m['failed']}"]
    if kind == "auto":
        lines.append(f"📶 رسائل غرباء منذ آخر رسالة لنا: {st['gap']}/{S('auto_gap_msgs')}")
        if st["stale"]:
            lines.append("⏸ الجروب منهمل حالياً (متأخر)")
    else:
        lines.append(f"⏱ الفاصل بين حساب وحساب: ~{g['gap_s']} ثانية")
    if st["last_sent"]:
        lines.append(f"🕒 آخر رسالة: قبل {int(time.time() - st['last_sent'])} ثانية")
    if g["chat_id"]:
        c = q1("SELECT COUNT(*) c FROM log WHERE chat_id=? AND ok=1 AND ts>=? AND kind IN ('auto','special')",
               g["chat_id"], day_start())["c"]
        lines.append(f"📨 مرسل اليوم: {c}")
    bad = q("""SELECT a.phone, m.err FROM ag_members m JOIN accounts a ON a.id=m.account_id
               WHERE m.group_id=? AND m.status='failed' LIMIT 5""", gid)
    if bad:
        lines.append("\n❌ حسابات فشلت:")
        lines += [f"• {x['phone']}: {x['err'] or ''}" for x in bad]
    btns = [[B("⏸ إيقاف" if g["enabled"] else "▶️ تشغيل", f"agtog:{gid}"), B("🗑 حذف", f"agdel:{gid}")]]
    if kind == "special":
        btns.append([B("⏱ تعديل الفاصل", f"aggap:{gid}")])
    if m["failed"]:
        btns.append([B("🔄 إعادة محاولة الفاشلين", f"agretry:{gid}")])
    btns += [[B("🔄 تحديث", f"agv:{gid}")], [B("🔙 رجوع", f"aglist:{kind}")]]
    await ui(ev, "\n".join(lines), btns)


async def v_am(ev, kind):
    rows = q("SELECT * FROM ag_msgs WHERE kind=? ORDER BY id", kind)
    if kind == "auto":
        head = "📝 الكلائش التلقائية\nتنختار عشوائياً وما تتكرر ورا بعض."
    else:
        n = q1("SELECT COUNT(*) c FROM accounts WHERE enabled=1")["c"]
        head = f"🎭 كلائش الحسابات (للجروبات المخصصة)\nكل حساب يرتبط بكليشة وحدة ثابتة.\nالحسابات: {n} | كلائشك: {len(rows)}"
        head += (f"\n⚠️ المطلوب {n} كليشة، ناقصك {n - len(rows)}. إذا تركتها ناقصة الكليشة تنعاد على أكثر من حساب."
                 if len(rows) < n else "\n✅ العدد كافي")
    txt = head + "\n\n" + "\n".join(f"{i}. {x['text'][:100]}" for i, x in enumerate(rows, 1))
    btns = [[B("➕ إضافة كلائش", f"amadd:{kind}")]]
    row = []
    for i, x in enumerate(rows, 1):
        row.append(B(f"🗑 {i}", f"amdel:{x['id']}"))
        if len(row) == 4:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([B("🔙 رجوع", f"aglist:{kind}")])
    await ui(ev, txt, btns)


def _mask_key(k):
    if not k:
        return "—"
    return k[:3] + "•" * max(len(k) - 6, 3) + k[-3:] if len(k) > 6 else "•" * len(k)


async def v_strat(ev):
    url, key, poll = strat_cfg()
    txt = (
        "🔗 ربط Strat\n\n"
        f"الرابط: {url or '— غير مضبوط —'}\n"
        f"المفتاح: {_mask_key(key)}\n"
        f"كل كم ثانية: {poll}\n\n"
        "هذا البوت يسحب من Strat جلسات مستقلة جاهزة ويضيفها كحسابات تلقائياً."
    )
    btns = [
        [B("✏️ تعديل الرابط", "stratset:url")],
        [B("✏️ تعديل المفتاح", "stratset:key")],
        [B("✏️ تعديل زمن السحب", "stratset:poll")],
        [B("🔄 اسحب الآن", "stratnow")],
        [B("🔙 رجوع", "main")],
    ]
    await ui(ev, txt, btns)


async def on_cb(ev):
    try:
        await ev.answer()
    except Exception:
        pass
    uid = ev.sender_id
    clear_state(uid)
    p = ev.data.decode().split(":", 1)
    c, a = p[0], (p[1] if len(p) > 1 else "")
    i = int(a) if a.lstrip("-").isdigit() else None

    if c == "main":
        await v_main(ev)
    elif c == "power":
        set_S("power", 0 if S("power") else 1)
        await v_main(ev)
    elif c == "stats":
        await ui(ev, stats_text(), [[B("🔄 تحديث", "stats"), B("🔙 رجوع", "main")]])
    # ---- ربط Strat
    elif c == "strat":
        await v_strat(ev)
    elif c == "stratset" and a in {"url", "key", "poll"}:
        STATE[uid] = {"step": "stratset", "field": a}
        label = {"url": "رابط Strat (مثال: https://your-app.up.railway.app)",
                  "key": "المفتاح السري (يجب أن يطابق EXTERNAL_BOT_API_KEY عند Strat حرفياً)",
                  "poll": "كل كم ثانية يسحب (رقم بالثواني)"}[a]
        await ui(ev, f"✏️ أرسل القيمة الجديدة لـ {label}")
    elif c == "stratnow":
        await ui(ev, "⏳ جاري السحب من Strat...")
        await strat_poll_once()
        await v_strat(ev)
    # ---- حسابات
    elif c == "accs":
        await v_accs(ev)
    elif c == "accadd":
        STATE[uid] = {"step": "add_phone"}
        await ui(ev, "📱 ارسل رقم الحساب مع رمز الدولة، مثال:\n+9647XXXXXXXXX\n\n(/cancel للإلغاء)")
    elif c == "acc":
        await v_acc(ev, i)
    elif c == "acctog":
        acc = q1("SELECT * FROM accounts WHERE id=?", i)
        ex("UPDATE accounts SET enabled=? WHERE id=?", 0 if acc["enabled"] else 1, i)
        if acc["enabled"]:
            await stop_runner(i)
        else:
            await start_runner(i)
        await v_acc(ev, i)
    elif c == "accdel":
        await ui(ev, "⚠️ متأكد تحذف الحساب وكل مجموعاته؟", [[B("✅ نعم احذف", f"accdelok:{i}"), B("❌ لا", f"acc:{i}")]])
    elif c == "accdelok":
        await stop_runner(i)
        ex("DELETE FROM chats WHERE account_id=?", i)
        ex("DELETE FROM users WHERE account_id=?", i)
        ex("DELETE FROM ag_members WHERE account_id=?", i)
        ag_inval()
        ex("DELETE FROM accounts WHERE id=?", i)
        await v_accs(ev)
    # ---- مجموعات
    elif c == "chats":
        await v_chats(ev, i)
    elif c == "chatadd":
        STATE[uid] = {"step": "chat_add", "acc": i}
        await ui(ev, "📂 ارسل رابط المجموعة/القناة (أو @يوزر) والحساب راح ينضم إلها تلقائياً.\n\n(/cancel للإلغاء)")
    elif c == "chat":
        await v_chat(ev, i)
    elif c == "chattog":
        ch = q1("SELECT * FROM chats WHERE id=?", i)
        ex("UPDATE chats SET enabled=?, fails=0 WHERE id=?", 0 if ch["enabled"] else 1, i)
        await v_chat(ev, i)
    elif c == "chatdel":
        ch = q1("SELECT * FROM chats WHERE id=?", i)
        ex("DELETE FROM chats WHERE id=?", i)
        if S("leave_on_delete") and ch["account_id"] in RUNNERS:
            asyncio.create_task(leave_chat(RUNNERS[ch["account_id"]], ch["chat_id"]))
        await v_chats(ev, ch["account_id"])
    elif c == "accclean":
        await ui(ev, "🧹 الحساب راح يغادر كل المجموعات/القنوات الي مو مضافة بالبوت (الخاص ما ينلمس). متأكد؟",
                 [[B("✅ نعم نظّف", f"acccleanok:{i}"), B("❌ لا", f"acc:{i}")]])
    elif c == "acccleanok":
        if i in RUNNERS:
            asyncio.create_task(free_room(RUNNERS[i], 10 ** 6, channels_only=False))
            await ui(ev, "🧹 بدأ التنظيف بالخلفية، يوصلك إشعار لمن يخلص.", [[B("🔙 رجوع", f"acc:{i}")]])
        else:
            await ui(ev, "❌ الحساب مو شغال.", [[B("🔙 رجوع", f"acc:{i}")]])
    elif c == "pmr":
        acc, to = (int(x) for x in a.split(":"))
        STATE[uid] = {"step": "pm_reply", "acc": acc, "to": to}
        await ev.respond("↩️ اكتب ردك وراح يندز من الحساب نفسه (/cancel للإلغاء):")
    elif c == "blk":
        ex("INSERT OR IGNORE INTO blocked(user_id) VALUES(?)", i)
        await ev.respond("🚫 انحظر من التاك والردود.")
    # ---- رسائل النشر
    elif c == "msgs":
        await v_msgs(ev)
    elif c == "msgadd":
        STATE[uid] = {"step": "msg_add"}
        await ui(ev, "📨 ارسل نص رسالة النشر.\nتكدر تستخدم {mention} لمكان التاك.\n\n(/cancel للإلغاء)")
    elif c == "msg":
        await v_msg(ev, i)
    elif c == "msgtog":
        m = q1("SELECT * FROM messages WHERE id=?", i)
        ex("UPDATE messages SET enabled=? WHERE id=?", 0 if m["enabled"] else 1, i)
        await v_msg(ev, i)
    elif c == "msgdel":
        ex("DELETE FROM messages WHERE id=?", i)
        await v_msgs(ev)
    # ---- قواعد الكلمات
    elif c == "rules":
        await v_rules(ev)
    elif c == "ruleadd":
        STATE[uid] = {"step": "rule_kw"}
        await ui(ev, "🔑 ارسل الكلمات المحفزة (كل كلمة بسطر أو افصلها بفاصلة).\nمثال: الصلاة، صلاة، الصلاه\n\n(/cancel للإلغاء)")
    elif c == "rule":
        await v_rule(ev, i)
    elif c == "ruletog":
        r = q1("SELECT * FROM rules WHERE id=?", i)
        ex("UPDATE rules SET enabled=? WHERE id=?", 0 if r["enabled"] else 1, i)
        inval()
        await v_rule(ev, i)
    elif c == "rulemode":
        r = q1("SELECT * FROM rules WHERE id=?", i)
        ex("UPDATE rules SET mode=? WHERE id=?", "contains" if r["mode"] == "exact" else "exact", i)
        inval()
        await v_rule(ev, i)
    elif c == "rulekw":
        STATE[uid] = {"step": "rule_editkw", "rule": i}
        await ui(ev, "✏️ ارسل الكلمات الجديدة (تستبدل القديمة).")
    elif c == "ruledel":
        ex("DELETE FROM replies WHERE rule_id=?", i)
        ex("DELETE FROM rules WHERE id=?", i)
        inval()
        await v_rules(ev)
    elif c == "repadd":
        STATE[uid] = {"step": "rep_add", "rule": i}
        await ui(ev, "➕ ارسل نص الرد الجديد.\nتكدر تستخدم {name} لاسم الشخص.")
    elif c == "repdel":
        rep = q1("SELECT * FROM replies WHERE id=?", i)
        if rep:
            ex("DELETE FROM replies WHERE id=?", i)
            inval()
            await v_rule(ev, rep["rule_id"])
        else:
            await v_rules(ev)
    # ---- الجروبات التلقائية والمخصصة
    elif c == "aglist" and a in AG_KINDS:
        await v_aglist(ev, a)
    elif c == "agadd" and a in AG_KINDS:
        STATE[uid] = {"step": "ag_add", "kind": a}
        await ui(ev, "➕ ارسل رابط/روابط الجروبات (عامة: @يوزر أو t.me/xxx ، خاصة: t.me/+xxxx) "
                     "تكدر ترسل عدة روابط مرة وحدة.\n\nكل حساباتك راح تنضم تلقائياً (بفاصل بين كل انضمام). "
                     "الجروبات الخاصة اللي تحتاج موافقة، النشر يبدأ لحاله لما تنقبل الطلبات.\n\n(/cancel للإلغاء)")
    elif c == "agv" and i:
        await v_ag(ev, i)
    elif c == "agtog":
        g = q1("SELECT * FROM agroups WHERE id=?", i)
        if g:
            ex("UPDATE agroups SET enabled=? WHERE id=?", 0 if g["enabled"] else 1, i)
            ag_inval()
        await v_ag(ev, i)
    elif c == "agdel":
        await ui(ev, "⚠️ متأكد تحذف هذا الجروب من البوت؟", [[B("✅ نعم احذف", f"agdelok:{i}"), B("❌ لا", f"agv:{i}")]])
    elif c == "agdelok":
        g = q1("SELECT * FROM agroups WHERE id=?", i)
        if not g:
            await v_main(ev)
        else:
            ids = [x["account_id"] for x in q("SELECT account_id FROM ag_members WHERE group_id=? AND status='joined'", i)]
            ag_stop(i)
            ex("DELETE FROM ag_members WHERE group_id=?", i)
            ex("DELETE FROM agroups WHERE id=?", i)
            ag_inval()
            if S("leave_on_delete") and g["chat_id"]:
                spawn(ag_leave_all(g["chat_id"], ids))
            await v_aglist(ev, g["kind"])
    elif c == "agretry":
        ex("UPDATE ag_members SET status='pending', tries=0, err=NULL WHERE group_id=? AND status='failed'", i)
        ag_inval()
        await v_ag(ev, i)
    elif c == "aggap":
        STATE[uid] = {"step": "ag_gap", "gid": i}
        await ui(ev, "⏱ ارسل الفاصل بالثواني بين رسالة حساب والحساب اللي بعده بهذا الجروب (أقل شي 3):")
    elif c == "amlist" and a in AG_KINDS:
        await v_am(ev, a)
    elif c == "amadd" and a in AG_KINDS:
        STATE[uid] = {"step": "am_add", "kind": a}
        extra = ("\nالبوت يحتاج كليشة لكل حساب. إذا اكثر من كليشة بنفس الرسالة افصل بينهم بسطر فيه --- بس."
                 if a == "special" else "\nتكدر ترسل اكثر من كليشة بنفس الرسالة، افصل بينهم بسطر فيه --- بس.")
        await ui(ev, f"➕ ارسل الكليشة (او الكلائش) وكل ما تخلص اضغط تم.{extra}")
    elif c == "amdel":
        m = q1("SELECT kind FROM ag_msgs WHERE id=?", i)
        ex("DELETE FROM ag_msgs WHERE id=?", i)
        if m:
            await v_am(ev, m["kind"])
        else:
            await v_main(ev)
    # ---- إعدادات
    elif c == "sets":
        await v_sets(ev)
    elif c == "set" and a in SETTINGS:
        if SETTINGS[a][1] == "toggle":
            set_S(a, 0 if S(a) else 1)
            await v_sets(ev)
        else:
            STATE[uid] = {"step": "set", "key": a}
            await ui(ev, f"{SETTINGS[a][2]}\nالقيمة الحالية: {S(a)}\n\nارسل القيمة الجديدة (رقم):")


# ---------- تسجيل دخول حساب ----------
async def finish_login(ev, uid, c):
    me = await c.get_me()
    sess = c.session.save()
    await c.disconnect()
    st = STATE.pop(uid, {})
    phone = st.get("phone", me.phone or "")
    name = utils.get_display_name(me)
    old = q1("SELECT id FROM accounts WHERE user_id=?", me.id)
    if old:
        ex("UPDATE accounts SET session=?, enabled=1, name=?, phone=? WHERE id=?", sess, name, phone, old["id"])
        aid = old["id"]
    else:
        aid = ex("INSERT INTO accounts(phone,name,user_id,session) VALUES(?,?,?,?)", phone, name, me.id, sess).lastrowid
    ok = await start_runner(aid)
    await ev.respond(f"✅ تمت إضافة الحساب {name} ({phone})\n{'🟢 شغال' if ok else '⚠️ تعذر التشغيل'}",
                     buttons=[[B("📂 إضافة مجموعات", f"chats:{aid}")], [B("🏠 القائمة", "main")]])


# ---------- استيراد تلقائي من بوت Strat (جلسة مستقلة لكل رقم) ----------
async def import_strat_session(phone, session_str):
    """يسجّل جلسة مستقلة وصلت من Strat كحساب جديد (جهاز ثانٍ منفصل)، أو
    يحدّث حساباً موجوداً بنفس الرقم إن وُجد."""
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await c.connect()
        if not await c.is_user_authorized():
            log.warning("strat session for %s not authorized", phone)
            return False
        me = await c.get_me()
        name = utils.get_display_name(me)
    except Exception as e:
        log.warning("strat session for %s failed: %s", phone, e)
        return False
    finally:
        try:
            await c.disconnect()
        except Exception:
            pass

    old = q1("SELECT id FROM accounts WHERE user_id=? OR phone=?", me.id, phone)
    if old:
        ex("UPDATE accounts SET session=?, enabled=1, name=?, phone=? WHERE id=?",
           session_str, name, phone, old["id"])
        aid = old["id"]
    else:
        aid = ex("INSERT INTO accounts(phone,name,user_id,session) VALUES(?,?,?,?)",
                  phone, name, me.id, session_str).lastrowid
    ok = await start_runner(aid)
    await notify(f"🔗 استُلمت جلسة مستقلة من Strat للرقم {phone} ({name})\n"
                 f"{'🟢 شغال' if ok else '⚠️ تعذر التشغيل'}")
    return True


async def strat_poll_once():
    import json as _json
    import urllib.request as _ur
    import urllib.error as _ue

    url, key, _poll = strat_cfg()
    req = _ur.Request(
        f"{url}/partner/external-sessions",
        headers={"X-Partner-Key": key},
    )
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: _ur.urlopen(req, timeout=20).read())
    except _ue.HTTPError as e:
        if e.code != 403:
            log.warning("strat poll http error: %s", e)
        else:
            log.error("strat poll forbidden — تحقق من STRAT_API_KEY")
        return
    except Exception as e:
        log.warning("strat poll failed: %s", e)
        return

    try:
        data = _json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("strat poll bad json: %s", e)
        return

    for item in data.get("sessions") or []:
        phone = (item.get("phone_number") or "").strip()
        sess = item.get("session_string") or ""
        if not phone or not sess:
            continue
        try:
            await import_strat_session(phone, sess)
        except Exception as e:
            log.warning("import_strat_session(%s) failed: %s", phone, e)


async def strat_poll_loop():
    while True:
        url, key, poll = strat_cfg()
        if not (url and key):
            await asyncio.sleep(30)
            continue
        try:
            await strat_poll_once()
        except Exception as e:
            log.warning("strat_poll_loop: %s", e)
        await asyncio.sleep(poll)


def parse_ref(ref):
    ref = ref.strip()
    m = (re.search(r"(?:t|telegram)\.me/(?:joinchat/|\+)([\w-]+)", ref)
         or re.search(r"tg://join\?invite=([\w-]+)", ref))
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t|telegram)\.me/c/(\d+)", ref)
    if m:
        return "id", int("-100" + m.group(1))
    m = re.search(r"tg://resolve\?domain=(\w+)", ref)
    if m:
        return "user", m.group(1)
    t = num(ref)
    if re.fullmatch(r"-?\d{5,}", t):
        return "id", int(t)
    u = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/", "", ref).lstrip("@").split("/")[0].split("?")[0]
    if re.fullmatch(r"[A-Za-z]\w{3,31}", u):
        return "user", u
    return None, None


async def list_dialogs(r):
    return [d async for d in r.client.iter_dialogs()]


async def free_room(r, need, channels_only=True):
    """يغادر القنوات/المجموعات القديمة غير المضافة بالبوت (الأقدم نشاطاً أولاً)"""
    managed = managed_ids(r.id)
    ds = [d for d in await list_dialogs(r)
          if (isinstance(d.entity, types.Channel) if channels_only else (d.is_group or d.is_channel))]
    cand = sorted([d for d in ds if d.id not in managed], key=lambda d: d.date.timestamp() if d.date else 0)
    left = 0
    for d in cand[:need]:
        try:
            await r.client.delete_dialog(d.entity)
            left += 1
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.5, 3))
    if left:
        await notify(f"🧹 الحساب {r.phone} غادر {left} قناة/مجموعة غير مضافة بالبوت لتوفير مكان.")
    return left


async def ensure_room(r):
    n = sum(1 for d in await list_dialogs(r) if isinstance(d.entity, types.Channel))
    over = n - S("max_dialogs")
    if over >= 0:
        await free_room(r, over + 10)


async def leave_chat(r, chat_id):
    try:
        await r.client.delete_dialog(chat_id)
    except Exception as e:
        log.info("leave %s: %s", chat_id, type(e).__name__)


async def find_by_id(r, pid):
    s_ = str(abs(pid))
    raws = {abs(pid)}
    if s_.startswith("100") and len(s_) > 9:
        raws.add(int(s_[3:]))
    async for d in r.client.iter_dialogs():
        if isinstance(d.entity, (types.Chat, types.Channel)) and d.entity.id in raws:
            return d.entity
    return None


async def join_invite(r, h):
    c = r.client
    chat = None
    for attempt in (1, 2):
        try:
            upd = await c(functions.messages.ImportChatInviteRequest(h))
            chat = upd.chats[0] if upd.chats else None
            break
        except UserAlreadyParticipantError:
            info = await c(functions.messages.CheckChatInviteRequest(h))
            chat = getattr(info, "chat", None)
            break
        except Exception as e:
            nm = type(e).__name__
            if nm == "ChannelsTooMuchError" and attempt == 1:
                await free_room(r, 10)
                continue
            if nm == "InviteRequestSentError":
                raise JoinRequestSent("انرسل طلب انضمام للإدارة. بعد القبول اضفها بالرابط/اليوزر/الايدي.")
            if nm in ("InviteHashExpiredError", "InviteHashInvalidError"):
                raise ValueError("رابط الدعوة منتهي أو غلط")
            raise
    if not chat:
        raise ValueError("ما كدرت أحدد المجموعة من الرابط")
    return chat


async def join_public(r, ent):
    if not isinstance(ent, (types.Channel, types.Chat)):
        raise ValueError("هذا يوزر شخص/بوت مو مجموعة أو قناة")
    if isinstance(ent, types.Chat):
        return ent
    for attempt in (1, 2):
        try:
            await r.client(functions.channels.JoinChannelRequest(ent))
            break
        except UserAlreadyParticipantError:
            break
        except Exception as e:
            nm = type(e).__name__
            if nm == "ChannelsTooMuchError" and attempt == 1:
                await free_room(r, 10)
                continue
            if nm == "InviteRequestSentError":
                raise JoinRequestSent("انرسل طلب انضمام للإدارة. بعد القبول اضفها من جديد.")
            try:
                await r.client.get_permissions(ent, "me")  # يمكن منضم مسبقاً
                break
            except Exception:
                raise e
    return ent


async def resolve_chat(r, ref):
    """يقبل: رابط دعوة خاص، رابط/يوزر عام، أو ايدي (إذا الحساب منضم مسبقاً)"""
    kind, val = parse_ref(ref)
    if kind is None:
        raise ValueError("صيغة غير مفهومة (رابط أو @يوزر أو ايدي)")
    if kind == "id":
        ent = await find_by_id(r, val)
        if not ent:
            raise ValueError("بالايدي لازم الحساب يكون منضم مسبقاً، وهذا مو منضم. استخدم الرابط أو اليوزر.")
        return ent
    await ensure_room(r)
    if kind == "invite":
        return await join_invite(r, val)
    return await join_public(r, await r.client.get_entity(val))


def err_text(e):
    if type(e).__name__ == "FloodWaitError":
        return f"تيليجرام حاظر الانضمام مؤقتاً، انتظر {getattr(e, 'seconds', '?')} ثانية"
    return str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {str(e)[:80]}"


async def on_text(ev):
    uid = ev.sender_id
    txt = (ev.raw_text or "").strip()
    cmd = txt.split()[0].lower().split("@")[0] if txt.startswith("/") else ""
    if cmd in ("/start", "/menu", "/help"):
        clear_state(uid)
        return await v_main(ev)
    if cmd == "/cancel":
        clear_state(uid)
        return await ev.respond("تم الإلغاء.", buttons=main_menu())
    if cmd == "/status":
        return await ev.respond(stats_text()[:4000])
    if cmd in ("/block", "/unblock"):
        ids = [int(x) for x in re.findall(r"\d+", num(txt))]
        for x in ids:
            if cmd == "/block":
                ex("INSERT OR IGNORE INTO blocked(user_id) VALUES(?)", x)
            else:
                ex("DELETE FROM blocked WHERE user_id=?", x)
        return await ev.respond(f"{'🚫 تم حظر' if cmd == '/block' else '✅ تم فك حظر'} {len(ids)} شخص.")
    if cmd == "/blocked":
        ids = [str(x["user_id"]) for x in q("SELECT user_id FROM blocked")]
        return await ev.respond("🚫 المحظورين:\n" + ("\n".join(ids) if ids else "لا أحد"))
    if cmd == "/check":
        lines = []
        for a in q("SELECT * FROM accounts"):
            r = RUNNERS.get(a["id"])
            if not r:
                lines.append(f"{a['phone']}: 🔴 غير شغال")
                continue
            try:
                await asyncio.wait_for(r.client.get_me(), 15)
                extra = f" ⏳ موقوف {int(r.paused_until - time.time())}ث" if time.time() < r.paused_until else ""
                lines.append(f"{a['phone']}: ✅ متصل{extra}")
            except Exception as e:
                lines.append(f"{a['phone']}: ❌ {type(e).__name__}")
        return await ev.respond("🩺 فحص الحسابات:\n" + ("\n".join(lines) or "ماكو حسابات"))
    if cmd == "/backup":
        tmp = os.path.join(HERE, "backup_autoposter.db")
        dst = sqlite3.connect(tmp)
        db.backup(dst)
        dst.close()
        await bot.send_file(uid, tmp, caption="💾 نسخة احتياطية (فيها جلسات الحسابات، لا تشاركها مع أحد!)")
        return os.remove(tmp)

    st = STATE.get(uid)
    if not st:
        return await ev.respond("اكتب /start لفتح القائمة.")
    step = st["step"]

    if step == "add_phone":
        phone = "+" + re.sub(r"\D", "", num(txt))
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        try:
            sent = await c.send_code_request(phone)
        except Exception as e:
            await c.disconnect()
            STATE.pop(uid, None)
            return await ev.respond(f"❌ {type(e).__name__}: {str(e)[:100]}", buttons=main_menu())
        STATE[uid] = {"step": "add_code", "client": c, "phone": phone, "hash": sent.phone_code_hash}
        return await ev.respond("📩 وصل كود لتيليجرام الحساب.\nاكتبه مع فواصل بين الأرقام مثل: 1-2-3-4-5\n"
                                "(إذا انكتب متصل تيليجرام يلغيه)")
    if step == "add_code":
        c = st["client"]
        try:
            await c.sign_in(st["phone"], re.sub(r"\D", "", num(txt)), phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            st["step"] = "add_pass"
            return await ev.respond("🔐 الحساب عليه تحقق بخطوتين. ارسل كلمة السر:")
        except PhoneCodeInvalidError:
            return await ev.respond("❌ الكود غلط، جرب مرة ثانية.")
        except PhoneCodeExpiredError:
            clear_state(uid)
            return await ev.respond("⌛ الكود انتهى، ابدأ من جديد.", buttons=main_menu())
        except Exception as e:
            clear_state(uid)
            return await ev.respond(f"❌ {type(e).__name__}", buttons=main_menu())
        return await finish_login(ev, uid, c)
    if step == "add_pass":
        c = st["client"]
        try:
            await ev.delete()
        except Exception:
            pass
        try:
            await c.sign_in(password=txt)
        except PasswordHashInvalidError:
            return await ev.respond("❌ كلمة السر غلط، جرب مرة ثانية.")
        except Exception as e:
            clear_state(uid)
            return await ev.respond(f"❌ {type(e).__name__}", buttons=main_menu())
        return await finish_login(ev, uid, c)

    if step == "chat_add":
        r = RUNNERS.get(st["acc"])
        if not r:
            clear_state(uid)
            return await ev.respond("❌ الحساب مو شغال حالياً.", buttons=main_menu())
        refs = [x for x in re.split(r"[\s,،]+", txt) if x][:20]
        await ev.respond(f"⏳ جاري المعالجة ({len(refs)})...")
        lines = []
        r.manual_busy += 1
        for n, ref in enumerate(refs):
            try:
                chat = await asyncio.wait_for(resolve_chat(r, ref), 120)
                ex("INSERT OR IGNORE INTO chats(account_id,chat_id,title) VALUES(?,?,?)",
                   st["acc"], utils.get_peer_id(chat), utils.get_display_name(chat))
                warn = ""
                if isinstance(chat, types.Channel) and chat.broadcast and not chat.megagroup:
                    warn = " ⚠️ قناة بث: لازم الحساب يكون مشرف حتى ينشر"
                lines.append(f"✅ {utils.get_display_name(chat)}{warn}")
            except asyncio.TimeoutError:
                lines.append(f"❌ {ref}: انتهت المهلة (تيليجرام بطيء أو الحساب عليه ضغط)، جرب بعد دقيقة")
            except Exception as e:
                lines.append(f"❌ {ref}: {err_text(e)}")
            if n < len(refs) - 1:
                await asyncio.sleep(random.uniform(2, 4))
        r.manual_busy = max(0, r.manual_busy - 1)
        STATE.pop(uid, None)
        return await ev.respond("\n".join(lines),
                                buttons=[[B("📂 المجموعات", f"chats:{st['acc']}")], [B("🏠 القائمة", "main")]])

    if step == "pm_reply":
        r = RUNNERS.get(st["acc"])
        c = q1("SELECT * FROM pm_contacts WHERE account_id=? AND user_id=?", st["acc"], st["to"])
        if not r or not c:
            clear_state(uid)
            return await ev.respond("❌ الحساب مو شغال أو الشخص مو محفوظ.", buttons=main_menu())
        try:
            await r.client.send_message(types.InputPeerUser(c["user_id"], c["access_hash"]), txt, parse_mode=None)
        except Exception as e:
            return await ev.respond(f"❌ {type(e).__name__}: {str(e)[:100]}")
        STATE.pop(uid, None)
        return await ev.respond("✅ انرسل الرد من الحساب.",
                                buttons=[[B("↩️ رد ثاني", f"pmr:{st['acc']}:{st['to']}")]])

    if step == "msg_add":
        ex("INSERT INTO messages(text) VALUES(?)", txt)
        STATE.pop(uid, None)
        return await ev.respond("✅ انضافت الرسالة.", buttons=[[B("📨 الرسائل", "msgs")], [B("🏠 القائمة", "main")]])

    if step == "rule_kw":
        if not split_kw(txt):
            return await ev.respond("ارسل كلمة وحدة على الأقل.")
        st.update(step="rule_reply", kw=txt)
        return await ev.respond("💬 ممتاز، هسه ارسل نص الرد (تكدر تضيف ردود ثانية بعدين):")
    if step == "rule_reply":
        rid = ex("INSERT INTO rules(keywords) VALUES(?)", st["kw"]).lastrowid
        ex("INSERT INTO replies(rule_id,text) VALUES(?,?)", rid, txt)
        inval()
        STATE.pop(uid, None)
        return await v_rule(ev, rid)
    if step == "rule_editkw":
        ex("UPDATE rules SET keywords=? WHERE id=?", txt, st["rule"])
        inval()
        STATE.pop(uid, None)
        return await v_rule(ev, st["rule"])
    if step == "rep_add":
        ex("INSERT INTO replies(rule_id,text) VALUES(?,?)", st["rule"], txt)
        inval()
        STATE.pop(uid, None)
        return await v_rule(ev, st["rule"])

    if step == "ag_add":
        kind = st["kind"]
        refs = [x for x in re.split(r"[\s,،]+", txt) if x][:50]
        lines = []
        for ref in refs:
            k, val = parse_ref(ref)
            if k not in ("invite", "user"):
                lines.append(f"❌ {ref}: لازم رابط دعوة أو @يوزر")
                continue
            h, u = (val, None) if k == "invite" else (None, val)
            if q1("SELECT id FROM agroups WHERE hash=? OR lower(uname)=?", h or "~", (u or "~").lower()):
                lines.append(f"⚠️ {ref}: مضاف من قبل")
                continue
            gid = ex("INSERT INTO agroups(kind,link,hash,uname,access,title) VALUES(?,?,?,?,?,?)",
                     kind, ref, h, u, "private" if h else "public", ref).lastrowid
            ag_sync_group(gid)
            ag_start(gid)
            lines.append(f"✅ {ref} ({'خاص' if h else 'عام'})")
        STATE.pop(uid, None)
        lines.append("\nالحسابات راح تنضم تباعاً. تابع الحالة من صفحة الجروب.")
        return await ev.respond("\n".join(lines), buttons=[[B("📂 الجروبات", f"aglist:{kind}")], [B("🏠 القائمة", "main")]])

    if step == "ag_gap":
        try:
            v = int(float(num(txt)))
        except ValueError:
            return await ev.respond("❌ ارسل رقم صحيح بالثواني.")
        ex("UPDATE agroups SET gap_s=? WHERE id=?", max(3, v), st["gid"])
        STATE.pop(uid, None)
        return await v_ag(ev, st["gid"])

    if step == "am_add":
        kind = st["kind"]
        parts = [p.strip() for p in re.split(r"\n\s*-{3,}\s*\n", txt) if p.strip()]
        for p in parts:
            ex("INSERT INTO ag_msgs(kind,text) VALUES(?,?)", kind, p)
        total = q1("SELECT COUNT(*) c FROM ag_msgs WHERE kind=?", kind)["c"]
        note = ""
        if kind == "special":
            n = q1("SELECT COUNT(*) c FROM accounts WHERE enabled=1")["c"]
            note = f"\nالمطلوب {n} | عندك {total}" + (" ✅" if total >= n else f" (ناقص {n - total})")
        return await ev.respond(f"✅ انضافت {len(parts)} كليشة.{note}\nكمل ارسال أو اضغط تم.",
                                buttons=[[B("✅ تم", f"amlist:{kind}")]])

    if step == "set":
        try:
            v = float(num(txt))
        except ValueError:
            return await ev.respond("❌ ارسل رقم صحيح.")
        k = st["key"]
        set_S(k, v if SETTINGS[k][1] == "float" else int(v))
        STATE.pop(uid, None)
        return await v_sets(ev)

    if step == "stratset":
        field = st["field"]
        if field == "poll":
            try:
                int(num(txt))
            except ValueError:
                return await ev.respond("❌ ارسل رقم صحيح بالثواني.")
            set_T("strat_poll_seconds", int(num(txt)))
        elif field == "url":
            set_T("strat_api_url", txt.strip().rstrip("/"))
        elif field == "key":
            set_T("strat_api_key", txt.strip())
        STATE.pop(uid, None)
        return await v_strat(ev)


# ================================== التشغيل ==================================
async def main():
    global bot
    bot = TelegramClient(os.path.join(HERE, "bot_session"), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    bot.add_event_handler(admin_only(on_cb), events.CallbackQuery())
    bot.add_event_handler(admin_only(on_text), events.NewMessage(incoming=True, func=lambda e: e.is_private))
    cleanup()
    for a in q("SELECT id FROM accounts WHERE enabled=1"):
        await start_runner(a["id"])
    for g in q("SELECT id FROM agroups"):
        ag_start(g["id"])
    asyncio.create_task(report_loop())
    asyncio.create_task(strat_poll_loop())
    await notify(f"🟢 البوت اشتغل. حسابات شغالة: {len(RUNNERS)}\nاكتب /start")
    log.info("bot ready")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
