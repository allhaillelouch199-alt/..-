import asyncio
import os
import re
import random
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelsTooMuchError,
)
from telethon.tl.functions.channels import LeaveChannelRequest

from config import API_ID, API_HASH, SESSIONS_DIR, CHANNEL_LIMIT_LEAVE_COUNT
import database as db

CHANNEL_REGEX = re.compile(
    r"(?:@([\w\d_]{4,})|(?:https?://)?t\.me/(?:joinchat/|\+)?([\w\d_-]{4,})|"
    r"telegram\.me/(?:joinchat/|\+)?([\w\d_-]{4,}))"
)

class LoginSession:
    def __init__(self, session_name, phone, client):
        self.session_name = session_name
        self.phone = phone
        self.client = client

class UserbotManager:
    def __init__(self):
        self.clients = {}
        self.login_sessions = {}
        self.scheduler = None

    # ---------- Login flow ----------
    async def start_login(self, user_id, phone):
        session_name = f"u{user_id}_{int(asyncio.get_event_loop().time())}"
        path = os.path.join(SESSIONS_DIR, session_name)
        client = TelegramClient(path, API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            return ("already_authorized", client)
        try:
            await client.send_code_request(phone)
        except Exception as e:
            await client.disconnect()
            return (f"error:{e}", None)
        self.login_sessions[user_id] = LoginSession(session_name, phone, client)
        return ("code_sent", client)

    async def verify_code(self, user_id, code):
        ls = self.login_sessions.get(user_id)
        if not ls:
            return ("no_login", None)
        try:
            await ls.client.sign_in(ls.phone, code)
            return await self._finalize_login(user_id, ls)
        except SessionPasswordNeededError:
            return ("password_needed", ls.client)
        except Exception as e:
            return (f"error:{e}", ls.client)

    async def verify_password(self, user_id, password):
        ls = self.login_sessions.get(user_id)
        if not ls:
            return ("no_login", None)
        try:
            await ls.client.sign_in(password=password)
            return await self._finalize_login(user_id, ls)
        except Exception as e:
            return (f"error:{e}", ls.client)

    async def _finalize_login(self, user_id, ls):
        sid = await db.add_session(user_id, ls.phone, ls.session_name)
        self.clients[sid] = ls.client
        self._register_listeners(sid, ls.client)
        del self.login_sessions[user_id]
        return ("success", sid)

    # ---------- Load existing sessions ----------
    async def load_all_sessions(self):
        sessions = await db.get_all_active_sessions()
        for s in sessions:
            path = os.path.join(SESSIONS_DIR, s["session_name"])
            if not os.path.exists(path + ".session"):
                continue
            try:
                client = TelegramClient(path, API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    self.clients[s["id"]] = client
                    self._register_listeners(s["id"], client)
                    print(f"[Userbot] Loaded session {s['id']} ({s['phone']})")
                else:
                    await client.disconnect()
            except Exception as e:
                print(f"[Userbot] Failed to load {s['id']}: {e}")

    def get_client(self, session_id):
        return self.clients.get(session_id)

    # ---------- Channel tracking listeners ----------
    def _register_listeners(self, session_id, client):
        @client.on(events.NewMessage())
        async def _on_msg(event):
            try:
                await self._handle_possible_channel(session_id, event)
            except Exception as e:
                print(f"[Userbot listen] {e}")

    async def _handle_possible_channel(self, session_id, event):
        chat_id = event.chat_id
        if not chat_id:
            return
        bundle = await db.get_bundle_by_session(session_id)
        if not bundle:
            return
        groups = await db._fetchall(
            "SELECT * FROM groups_tbl WHERE id=?", (bundle["group_id"],)
        )
        if not groups:
            return
        target_group_id = groups[0]["group_id"]
        if chat_id != target_group_id:
            return

        # تجاهل الرسائل المرسلة من البوتات أو المحظورين
        try:
            sender = await event.get_sender()
            if sender and getattr(sender, "bot", False):
                return
            if sender and await db.is_blocked(sender.id):
                return
        except Exception:
            pass

        text = event.raw_text or ""
        matches = CHANNEL_REGEX.findall(text)
        if not matches:
            return

        client = self.get_client(session_id)
        if not client:
            return

        for m in matches:
            # m = (username, hash1, hash2)
            username, h1, h2 = m[0], m[1], m[2]
            if username:
                link = "@" + username
            elif h1:
                if ("+" + h1) in text or ("joinchat/" + h1) in text:
                    link = "https://t.me/+" + h1
                else:
                    link = h1
            elif h2:
                if ("+" + h2) in text or ("joinchat/" + h2) in text:
                    link = "https://t.me/+" + h2
                else:
                    link = h2
            else:
                continue

            await self._join_and_schedule_leave(session_id, client, chat_id, link)

    async def _join_and_schedule_leave(self, session_id, client, chat_id, link):
        joined = False
        try:
            await client.join_chat(link)
            joined = True
        except ChannelsTooMuchError:
            print(f"[Join] Channel limit reached. Leaving {CHANNEL_LIMIT_LEAVE_COUNT} channels...")
            await self._leave_oldest_channels(client, count=CHANNEL_LIMIT_LEAVE_COUNT)
            try:
                await client.join_chat(link)
                joined = True
            except Exception as e:
                print(f"[Join retry] {link}: {e}")
        except FloodWaitError as e:
            print(f"[Join] FloodWait {e.seconds}s")
            return
        except Exception as e:
            print(f"[Join] {link}: {e}")

        if not joined:
            return

        if self.scheduler:
            job_id = f"leave_{session_id}_{chat_id}_{link}"
            from apscheduler.triggers.date import DateTrigger
            self.scheduler.scheduler.add_job(
                self._leave_channel,
                trigger=DateTrigger(run_date=datetime.now() + timedelta(minutes=5)),
                args=[session_id, link],
                id=job_id,
                replace_existing=True,
            )

    async def _leave_oldest_channels(self, client, count=200):
        """مغادرة أقدم القنوات (broadcast channels فقط، لا المجموعات)."""
        try:
            dialogs = await client.get_dialogs(limit=None)
            channels = [d for d in dialogs if d.is_channel and not d.is_group]
            fallback = datetime.min.replace(tzinfo=timezone.utc)
            channels.sort(key=lambda d: d.date or fallback)
            to_leave = channels[:count]
            left = 0
            for d in to_leave:
                try:
                    await client(LeaveChannelRequest(d.entity))
                    left += 1
                except Exception as e:
                    print(f"[LeaveOld] {getattr(d, 'name', d.id)}: {e}")
            print(f"[LeaveOld] Successfully left {left}/{len(to_leave)} channels")
        except Exception as e:
            print(f"[LeaveOld] Error: {e}")

    async def _leave_channel(self, session_id, link):
        client = self.get_client(session_id)
        if not client:
            return
        try:
            await client.leave_chat(link)
            print(f"[Leave] {link}")
        except Exception as e:
            print(f"[Leave] {link}: {e}")

    # ---------- Message sending with reply ----------
    async def send_with_reply(self, session_id, group_id, text):
        """
        Returns: (status, extra)
          - ("ok", None)
          - ("flood", seconds)
          - ("no_target", None)
          - ("restricted", None)
          - ("error", str)
        """
        client = self.get_client(session_id)
        if not client:
            return ("error", "no_client")

        reply_to = await self._find_reply_target(client, group_id)
        if reply_to is None:
            return ("no_target", None)

        try:
            await client.send_message(group_id, text, reply_to=reply_to)
            return ("ok", None)
        except FloodWaitError as e:
            return ("flood", e.seconds)
        except (ChatWriteForbiddenError, UserBannedInChannelError) as e:
            print(f"[Send] Restricted in {group_id}: {e}")
            return ("restricted", None)
        except Exception as e:
            print(f"[Send] {e}")
            return ("error", str(e))

    async def _find_reply_target(self, client, group_id):
        """يجد رسالة عضو بشري (غير محظور، غير بوت، ليس الحساب نفسه) ليرد عليها."""
        try:
            messages = await client.get_messages(group_id, limit=30)
            for msg in messages:
                # تجاهل الرسائل الصادرة من الحساب نفسه
                if getattr(msg, "out", False):
                    continue
                if not msg.from_id:
                    continue
                try:
                    sender = await msg.get_sender()
                except Exception:
                    continue
                if not sender:
                    continue
                # تجاهل البوتات
                if getattr(sender, "bot", False):
                    continue
                # تجاهل المحظورين
                if await db.is_blocked(sender.id):
                    continue
                return msg.id
        except Exception:
            pass
        return None

    async def resolve_group(self, session_id, link):
        client = self.get_client(session_id)
        if not client:
            return None
        try:
            if link.startswith("https://t.me/") or link.startswith("t.me/"):
                link = "@" + link.split("/")[-1]
            if link.startswith("@"):
                ent = await client.get_entity(link)
            else:
                ent = await client.get_entity(int(link))
            title = getattr(ent, "title", str(ent.id))
            return (ent.id, title)
        except Exception as e:
            print(f"[Resolve] {e}")
            return None

ub_manager = UserbotManager()
