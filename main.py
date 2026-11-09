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
}


def S(key):
    r = q1("SELECT value FROM settings WHERE key=?", key)
    v = r["value"] if r else SETTINGS[key][0]
    return float(v) if SETTINGS[key][1] == "float" else int(float(v))


def set_S(key, val):
    ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", key, str(val))


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


def remember(acc, chat, u):
    if getattr(u, "bot", False) or getattr(u, "deleted", False):
        return
    h = getattr(u, "access_hash", None)
    if not h or getattr(u, "min", False):
        return
    name = " ".join(x for x in (u.first_name, u.last_name) if x) or "عضو"
    ex("INSERT OR REPLACE INTO users(account_id,chat_id,user_id,access_hash,name,seen) VALUES(?,?,?,?,?,?)",
       acc, chat, u.id, h, name, now())


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
        self.paused_until = 0.0
        self.tasks = []
        self.refreshed = {}
        self.started = time.time()
        self.last_kw = {}


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
    r.client.add_event_handler(lambda ev: on_incoming(r, ev), events.NewMessage(incoming=True))
    RUNNERS[acc_id] = r
    r.tasks = [asyncio.create_task(broadcast_loop(r)), asyncio.create_task(backlog_loop(r))]
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
    async with r.lock:
        lo, hi = sorted(gap or (S("delay_min"), S("delay_max")))
        wait = r.last + random.uniform(lo, hi) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await r.client.send_message(ch["chat_id"], text, reply_to=reply_to,
                                        formatting_entities=entities, parse_mode=None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            r.last = time.time()
            await on_send_error(r, ch, kind, e)
            return False
        r.last = time.time()
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
    async for m in r.client.iter_messages(ch["chat_id"], limit=limit):
        uid = m.sender_id
        if not uid or uid < 0 or uid in own_ids() or uid in pending:
            continue
        s = m.sender
        if isinstance(s, types.User) and not s.min:
            remember(r.id, ch["chat_id"], s)
        elif not (isinstance(s, types.User) and s.bot):
            pending[uid] = m.id
    items = list(pending.items())
    for i in range(0, len(items), 50):
        try:
            res = await r.client(functions.users.GetUsersRequest(
                [types.InputUserFromMessage(peer, mid, uid) for uid, mid in items[i:i + 50]]))
            for u in res:
                if isinstance(u, types.User):
                    remember(r.id, ch["chat_id"], u)
        except Exception:
            pass


async def refresh_members(r, ch):
    key = ch["chat_id"]
    if time.time() - r.refreshed.get(key, 0) < 1800:
        return
    r.refreshed[key] = time.time()
    try:
        async for u in r.client.iter_participants(ch["chat_id"], limit=10000):
            remember(r.id, ch["chat_id"], u)
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
        for u in chunk:
            # عند الفشل نعيد المحاولة بعد 30 دقيقة بدل ما نعلق
            ts = t if ok else t - cd + 1800
            ex("INSERT OR REPLACE INTO cooldowns(chat_id,user_id,kind,ts) VALUES(?,?,?,?)",
               ch["chat_id"], u["user_id"], "tag", ts)


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
async def on_incoming(r, ev):
    try:
        await handle_incoming(r, ev)
    except Exception:
        log.exception("incoming")


async def handle_incoming(r, ev):
    if ev.is_private:
        return await forward_pm(r, ev)
    if not ev.sender_id or not ev.message:
        return
    ch = q1("SELECT * FROM chats WHERE account_id=? AND chat_id=? AND enabled=1", r.id, ev.chat_id)
    if not ch:
        return
    uid = ev.sender_id
    if uid in own_ids():
        return
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
    ok = await safe_send(r, ch, text, "kw", reply_to=msg_id, gap=(0.5, 2))
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


# ================================ الإحصائيات ================================
def counts(since, acc=None):
    sql = "SELECT kind, ok, COUNT(*) c, COALESCE(SUM(n),0) n FROM log WHERE ts>=?"
    args = [since]
    if acc:
        sql += " AND account_id=?"
        args.append(acc)
    d = {"tag": 0, "tagp": 0, "kw": 0, "fail": 0}
    for r in q(sql + " GROUP BY kind, ok", *args):
        if r["ok"]:
            if r["kind"] == "tag":
                d["tag"] += r["c"]
                d["tagp"] += r["n"]
            else:
                d["kw"] += r["c"]
        else:
            d["fail"] += r["c"]
    return d


def fmt(d):
    return f"📨 نشر: {d['tag']} رسالة ({d['tagp']} تاك) | 💬 ردود: {d['kw']} | ❌ فشل: {d['fail']}"


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


def clear_state(uid):
    st = STATE.pop(uid, None)
    if st and st.get("client"):
        asyncio.create_task(st["client"].disconnect())


def admin_only(f):
    async def w(ev):
        if ev.sender_id not in ADMIN_IDS:
            return
        try:
            await f(ev)
        except MessageNotModifiedError:
            pass
        except Exception:
            log.exception("ui error")
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
            [B("⚙️ الإعدادات", "sets"), B(f"{p} تشغيل/إيقاف الكل", "power")]]


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
    managed = {c["chat_id"] for c in q("SELECT chat_id FROM chats WHERE account_id=?", r.id)}
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
                raise ValueError("انرسل طلب انضمام للإدارة. بعد القبول اضفها بالرابط/اليوزر/الايدي.")
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
                raise ValueError("انرسل طلب انضمام للإدارة. بعد القبول اضفها من جديد.")
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
        for n, ref in enumerate(refs):
            try:
                chat = await resolve_chat(r, ref)
                ex("INSERT OR IGNORE INTO chats(account_id,chat_id,title) VALUES(?,?,?)",
                   st["acc"], utils.get_peer_id(chat), utils.get_display_name(chat))
                warn = ""
                if isinstance(chat, types.Channel) and chat.broadcast and not chat.megagroup:
                    warn = " ⚠️ قناة بث: لازم الحساب يكون مشرف حتى ينشر"
                lines.append(f"✅ {utils.get_display_name(chat)}{warn}")
            except Exception as e:
                lines.append(f"❌ {ref}: {err_text(e)}")
            if n < len(refs) - 1:
                await asyncio.sleep(random.uniform(2, 4))
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

    if step == "set":
        try:
            v = float(num(txt))
        except ValueError:
            return await ev.respond("❌ ارسل رقم صحيح.")
        k = st["key"]
        set_S(k, v if SETTINGS[k][1] == "float" else int(v))
        STATE.pop(uid, None)
        return await v_sets(ev)


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
    asyncio.create_task(report_loop())
    await notify(f"🟢 البوت اشتغل. حسابات شغالة: {len(RUNNERS)}\nاكتب /start")
    log.info("bot ready")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
