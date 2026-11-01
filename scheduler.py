import random
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database as db
from userbot import ub_manager
from config import FAILURES_BEFORE_COMA, COMA_HOURS

class SchedulerManager:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.bundle_state = {}

    def start(self):
        self.scheduler.start()
        ub_manager.scheduler = self

    async def _tick(self, bundle_id):
        bundle = await db.get_bundle(bundle_id)
        if not bundle or not bundle["is_active"]:
            return

        now = datetime.now()

        # ---- التحقق من الغيبوبة ----
        coma_until = bundle.get("coma_until")
        if coma_until:
            try:
                until_dt = datetime.fromisoformat(coma_until)
                if now < until_dt:
                    return  # لا يزال في غيبوبة
                # انتهت الغيبوبة → مسحها وتصفير العدادات
                await db.clear_bundle_coma(bundle_id)
                await db.reset_bundle_failures(bundle_id)
                print(f"[Scheduler] Bundle {bundle_id} exited coma")
            except Exception:
                pass

        # ---- التحقق من FloodWait ----
        flood_until = bundle.get("flood_until")
        if flood_until:
            try:
                until_dt = datetime.fromisoformat(flood_until)
                if now < until_dt:
                    return  # لا يزال في flood
                await db.clear_bundle_flood(bundle_id)
            except Exception:
                pass

        # ---- اختيار الكليشة ----
        state = self.bundle_state.get(bundle_id, "user")
        if state == "user":
            cliche = await db.get_cliche(bundle["cliche_id"])
            text = cliche["text"] if cliche else None
            self.bundle_state[bundle_id] = "admin"
        else:
            adms = await db.get_all_admin_cliches()
            text = random.choice(adms)["text"] if adms else None
            self.bundle_state[bundle_id] = "user"

        if not text:
            return

        grp = await db.get_group(bundle["group_id"])
        if not grp:
            return

        # ---- الإرسال ----
        status, extra = await ub_manager.send_with_reply(
            bundle["session_id"], grp["group_id"], text
        )

        if status == "ok":
            await db.reset_bundle_failures(bundle_id)
        elif status == "flood":
            await db.set_bundle_flood(bundle_id, int(extra) + 1)
            print(f"[Scheduler] Bundle {bundle_id} flood for {extra}s")
        elif status == "no_target":
            # لا يوجد عضو بشري للرد عليه — لا نحسبها فشلًا
            pass
        elif status in ("restricted", "error"):
            count = await db.increment_bundle_failure(bundle_id)
            print(f"[Scheduler] Bundle {bundle_id} failure #{count} ({status})")
            if count >= FAILURES_BEFORE_COMA:
                await db.set_bundle_coma(bundle_id, hours=COMA_HOURS)
                await db.reset_bundle_failures(bundle_id)
                print(f"[Scheduler] Bundle {bundle_id} entered coma for {COMA_HOURS}h")

    def schedule_bundle(self, bundle_id, interval):
        job_id = f"bundle_{bundle_id}"
        half = max(int(interval) // 2, 5)
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self.scheduler.add_job(
            self._tick, "interval", seconds=half,
            args=[bundle_id], id=job_id, replace_existing=True
        )
        self.bundle_state[bundle_id] = "user"

    def unschedule_bundle(self, bundle_id):
        job_id = f"bundle_{bundle_id}"
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self.bundle_state.pop(bundle_id, None)

    async def reload_all(self):
        bundles = await db.get_active_bundles()
        for b in bundles:
            self.schedule_bundle(b["id"], b["interval"])
        print(f"[Scheduler] Reloaded {len(bundles)} bundles")

scheduler_mgr = SchedulerManager()
