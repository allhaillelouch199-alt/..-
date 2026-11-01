from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from config import OWNER_ID
from userbot import ub_manager
from scheduler import scheduler_mgr

router = Router()

# ---------------- FSM States ----------------
class AddNumber(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()

class AddGroup(StatesGroup):
    waiting_session = State()
    waiting_link = State()

class AddCliche(StatesGroup):
    waiting_text = State()

class CreateBundle(StatesGroup):
    select_session = State()
    select_group = State()
    select_cliche = State()
    waiting_interval = State()

class OwnerFlow(StatesGroup):
    block_wait = State()
    unblock_wait = State()
    add_admin_cliche = State()
    add_forced_channel = State()
    set_limits_wait = State()

# ---------------- Menu ----------------
def user_menu(user_id):
    kb = [
        [InlineKeyboardButton(text="📱 إضافة رقم", callback_data="add_number")],
        [InlineKeyboardButton(text="👥 إضافة كروب", callback_data="add_group")],
        [InlineKeyboardButton(text="💬 إضافة كليشة", callback_data="add_cliche")],
        [InlineKeyboardButton(text="📦 إنشاء مجموعة", callback_data="add_bundle")],
        [InlineKeyboardButton(text="📋 مجموعاتي", callback_data="my_bundles")],
    ]
    if user_id == OWNER_ID:
        kb.append([InlineKeyboardButton(text="⚙️ لوحة المالك", callback_data="owner_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def owner_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 حظر عضو/بوت", callback_data="own_block")],
        [InlineKeyboardButton(text="✅ إلغاء حظر", callback_data="own_unblock")],
        [InlineKeyboardButton(text="💬 إضافة كليشة للمالك", callback_data="own_cliche")],
        [InlineKeyboardButton(text="📢 إضافة قناة إجبارية", callback_data="own_forced")],
        [InlineKeyboardButton(text="📊 تحديد حدود عضو", callback_data="own_limits")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_main")],
    ])

async def check_access(user_id):
    if await db.is_blocked(user_id):
        return False
    await db.ensure_user(user_id)
    return True

# ---------------- /start ----------------
@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if not await check_access(msg.from_user.id):
        await msg.answer("🚫 أنت محظور من استخدام البوت.")
        return
    await msg.answer("👋 مرحبًا بك!\nاختر من القائمة:", reply_markup=user_menu(msg.from_user.id))

@router.callback_query(F.data == "back_main")
async def cb_back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("اختر من القائمة:", reply_markup=user_menu(cb.from_user.id))
    await cb.answer()

# ---------------- Add Number ----------------
@router.callback_query(F.data == "add_number")
async def cb_add_number(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer("محظور", show_alert=True); return
    lim = await db.get_limits(cb.from_user.id)
    sess = await db.get_user_sessions(cb.from_user.id)
    if len(sess) >= lim["max_numbers"]:
        await cb.message.answer(f"⚠️ وصلت للحد الأقصى ({lim['max_numbers']}) من الأرقام.")
        await cb.answer(); return
    await cb.message.answer("📱 أرسل رقمك بصيغة دولية (مثال: +964xxxxxxxxx):")
    await state.set_state(AddNumber.waiting_phone)
    await cb.answer()

@router.message(AddNumber.waiting_phone)
async def process_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    status, client = await ub_manager.start_login(msg.from_user.id, phone)
    if status == "code_sent":
        await msg.answer("✅ تم إرسال كود التحقق.\n📩 أرسل الكود الآن:")
        await state.set_state(AddNumber.waiting_code)
    elif status == "already_authorized":
        await msg.answer("⚠️ هذا الرقم مسجل بالفعل.")
        await state.clear()
    else:
        await msg.answer(f"❌ خطأ: {status}")
        await state.clear()

@router.message(AddNumber.waiting_code)
async def process_code(msg: Message, state: FSMContext):
    status, data = await ub_manager.verify_code(msg.from_user.id, msg.text.strip())
    if status == "success":
        await msg.answer("✅ تم تسجيل الدخول!", reply_markup=user_menu(msg.from_user.id))
        await state.clear()
    elif status == "password_needed":
        await msg.answer("🔐 الحساب محمي بـ 2FA. أرسل كلمة المرور:")
        await state.set_state(AddNumber.waiting_password)
    else:
        await msg.answer(f"❌ فشل: {status}\nحاول مجددًا /start")

@router.message(AddNumber.waiting_password)
async def process_password(msg: Message, state: FSMContext):
    status, _ = await ub_manager.verify_password(msg.from_user.id, msg.text.strip())
    if status == "success":
        await msg.answer("✅ تم تسجيل الدخول!", reply_markup=user_menu(msg.from_user.id))
    else:
        await msg.answer(f"❌ فشل: {status}")
    await state.clear()

# ---------------- Add Group ----------------
@router.callback_query(F.data == "add_group")
async def cb_add_group(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer(); return
    lim = await db.get_limits(cb.from_user.id)
    gs = await db.get_user_groups(cb.from_user.id)
    if len(gs) >= lim["max_groups"]:
        await cb.message.answer(f"⚠️ وصلت للحد الأقصى ({lim['max_groups']}) من الكروبات.")
        await cb.answer(); return
    sessions = await db.get_user_sessions(cb.from_user.id)
    if not sessions:
        await cb.message.answer("⚠️ أضف رقم أولاً.")
        await cb.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s["phone"], callback_data=f"grpsess_{s['id']}")]
        for s in sessions
    ])
    await cb.message.answer("📱 اختر الرقم:", reply_markup=kb)
    await state.set_state(AddGroup.waiting_session)
    await cb.answer()

@router.callback_query(F.data.startswith("grpsess_"), AddGroup.waiting_session)
async def cb_group_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split("_")[1])
    await state.update_data(grp_session=sid)
    await cb.message.answer("🔗 أرسل رابط الكروب أو @username أو ID:")
    await state.set_state(AddGroup.waiting_link)
    await cb.answer()

@router.message(AddGroup.waiting_link)
async def process_group_link(msg: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("grp_session")
    result = await ub_manager.resolve_group(sid, msg.text.strip())
    if not result:
        await msg.answer("❌ لم أتمكن من الوصول للكروب.")
        await state.clear(); return
    gid, title = result
    await db.add_group(msg.from_user.id, gid, title)
    await msg.answer(f"✅ تم حفظ الكروب: {title}", reply_markup=user_menu(msg.from_user.id))
    await state.clear()

# ---------------- Add Cliche ----------------
@router.callback_query(F.data == "add_cliche")
async def cb_add_cliche(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer(); return
    lim = await db.get_limits(cb.from_user.id)
    cs = await db.get_user_cliches(cb.from_user.id)
    if len(cs) >= lim["max_cliches"]:
        await cb.message.answer(f"⚠️ وصلت للحد الأقصى ({lim['max_cliches']}) من الكليشات.")
        await cb.answer(); return
    await cb.message.answer("💬 أرسل نص الكليشة:")
    await state.set_state(AddCliche.waiting_text)
    await cb.answer()

@router.message(AddCliche.waiting_text)
async def process_cliche(msg: Message, state: FSMContext):
    await db.add_cliche(msg.from_user.id, msg.text)
    await msg.answer("✅ تم حفظ الكليشة.", reply_markup=user_menu(msg.from_user.id))
    await state.clear()

# ---------------- Create Bundle ----------------
@router.callback_query(F.data == "add_bundle")
async def cb_add_bundle(cb: CallbackQuery, state: FSMContext):
    if not await check_access(cb.from_user.id):
        await cb.answer(); return
    sessions = await db.get_user_sessions(cb.from_user.id)
    if not sessions:
        await cb.message.answer("⚠️ أضف رقم أولاً.")
        await cb.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=s["phone"], callback_data=f"bsess_{s['id']}")]
        for s in sessions
    ])
    await cb.message.answer("📱 اختر الرقم:", reply_markup=kb)
    await state.set_state(CreateBundle.select_session)
    await cb.answer()

@router.callback_query(F.data.startswith("bsess_"), CreateBundle.select_session)
async def cb_bundle_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split("_")[1])
    await state.update_data(b_session=sid)
    groups = await db.get_user_groups(cb.from_user.id)
    if not groups:
        await cb.message.answer("⚠️ أضف كروب أولاً.")
        await cb.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=g["group_title"] or str(g["group_id"]),
                              callback_data=f"bgrp_{g['id']}")]
        for g in groups
    ])
    await cb.message.answer("👥 اختر الكروب:", reply_markup=kb)
    await state.set_state(CreateBundle.select_group)
    await cb.answer()

@router.callback_query(F.data.startswith("bgrp_"), CreateBundle.select_group)
async def cb_bundle_group(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split("_")[1])
    await state.update_data(b_group=gid)
    cs = await db.get_user_cliches(cb.from_user.id)
    if not cs:
        await cb.message.answer("⚠️ أضف كليشة أولاً.")
        await cb.answer(); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=(c["text"][:40] or "..."), callback_data=f"bcli_{c['id']}")]
        for c in cs
    ])
    await cb.message.answer("💬 اختر الكليشة:", reply_markup=kb)
    await state.set_state(CreateBundle.select_cliche)
    await cb.answer()

@router.callback_query(F.data.startswith("bcli_"), CreateBundle.select_cliche)
async def cb_bundle_cliche(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split("_")[1])
    await state.update_data(b_cliche=cid)
    await cb.message.answer("⏱️ أرسل التوقيت بالثواني (مثال: 60):")
    await state.set_state(CreateBundle.waiting_interval)
    await cb.answer()

@router.message(CreateBundle.waiting_interval)
async def cb_bundle_interval(msg: Message, state: FSMContext):
    try:
        interval = int(msg.text.strip())
        if interval < 10:
            raise ValueError
    except ValueError:
        await msg.answer("❌ أرسل رقمًا صحيحًا (>= 10).")
        return
    data = await state.get_data()
    existing = await db.get_bundle_by_session(data["b_session"])
    if existing:
        await msg.answer("⚠️ هذا الرقم مستخدم في مجموعة أخرى بالفعل.")
        await state.clear(); return
    bid = await db.add_bundle(
        msg.from_user.id, data["b_session"], data["b_group"], data["b_cliche"], interval
    )
    scheduler_mgr.schedule_bundle(bid, interval)
    await msg.answer(
        f"✅ تم إنشاء المجموعة #{bid}!\n"
        f"⏱️ كل {interval//2} ثانية سيتم إرسال رسالة (بالتبديل).",
        reply_markup=user_menu(msg.from_user.id)
    )
    await state.clear()

# ---------------- My bundles ----------------
@router.callback_query(F.data == "my_bundles")
async def cb_my_bundles(cb: CallbackQuery):
    bundles = await db.get_user_bundles(cb.from_user.id)
    if not bundles:
        await cb.message.answer("📭 لا توجد مجموعات.")
        await cb.answer(); return
    text = "📋 مجموعاتك:\n\n"
    for b in bundles:
        status = "🟢" if b["is_active"] else "🔴"
        if b.get("coma_until"):
            status = "💤 غيبوبة"
        elif b.get("flood_until"):
            status = "🌊 انتظار"
        text += (f"{status} #{b['id']} | رقم #{b['session_id']} | كروب #{b['group_id']} "
                 f"| كليشة #{b['cliche_id']} | ⏱️ {b['interval']}ث\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 حذف #{b['id']}", callback_data=f"delb_{b['id']}")]
        for b in bundles
    ])
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("delb_"))
async def cb_del_bundle(cb: CallbackQuery):
    bid = int(cb.data.split("_")[1])
    scheduler_mgr.unschedule_bundle(bid)
    await db.delete_bundle(bid)
    await cb.message.edit_text(f"✅ تم حذف المجموعة #{bid}.")
    await cb.answer()

# ---------------- Owner Panel ----------------
@router.callback_query(F.data == "owner_menu")
async def cb_owner_menu(cb: CallbackQuery):
    if cb.from_user.id != OWNER_ID:
        await cb.answer("للمالك فقط.", show_alert=True); return
    await cb.message.edit_text("⚙️ لوحة المالك:", reply_markup=owner_menu())
    await cb.answer()

@router.callback_query(F.data == "own_block")
async def cb_own_block(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل user_id / bot_id للحظر:")
    await state.set_state(OwnerFlow.block_wait)
    await cb.answer()

@router.message(OwnerFlow.block_wait)
async def proc_block(msg: Message, state: FSMContext):
    try:
        eid = int(msg.text.strip())
        await db.block_entity(eid, "user")
        await msg.answer(f"✅ تم حظر {eid}.")
    except ValueError:
        await msg.answer("❌ أرسل ID رقمي.")
    await state.clear()

@router.callback_query(F.data == "own_unblock")
async def cb_own_unblock(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل ID لإلغاء الحظر:")
    await state.set_state(OwnerFlow.unblock_wait)
    await cb.answer()

@router.message(OwnerFlow.unblock_wait)
async def proc_unblock(msg: Message, state: FSMContext):
    try:
        eid = int(msg.text.strip())
        await db.unblock_entity(eid)
        await msg.answer(f"✅ تم إلغاء حظر {eid}.")
    except ValueError:
        await msg.answer("❌ ID غير صحيح.")
    await state.clear()

@router.callback_query(F.data == "own_cliche")
async def cb_own_cliche(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل نص كليشة المالك:")
    await state.set_state(OwnerFlow.add_admin_cliche)
    await cb.answer()

@router.message(OwnerFlow.add_admin_cliche)
async def proc_admin_cliche(msg: Message, state: FSMContext):
    await db.add_admin_cliche(msg.text)
    await msg.answer("✅ تم حفظ كليشة المالك.")
    await state.clear()

@router.callback_query(F.data == "own_forced")
async def cb_own_forced(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل @username القناة الإجبارية:")
    await state.set_state(OwnerFlow.add_forced_channel)
    await cb.answer()

@router.message(OwnerFlow.add_forced_channel)
async def proc_forced(msg: Message, state: FSMContext):
    link = msg.text.strip()
    cid = link.lstrip("@")
    await db.add_forced_channel(cid, link)
    await msg.answer(f"✅ تم إضافة القناة: {link}")
    await state.clear()

@router.callback_query(F.data == "own_limits")
async def cb_own_limits(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != OWNER_ID: await cb.answer(); return
    await cb.message.answer("أرسل: user_id max_numbers max_groups max_cliches")
    await state.set_state(OwnerFlow.set_limits_wait)
    await cb.answer()

@router.message(OwnerFlow.set_limits_wait)
async def proc_limits(msg: Message, state: FSMContext):
    parts = msg.text.split()
    if len(parts) != 4:
        await msg.answer("❌ صيغة غير صحيحة.")
        return
    try:
        uid, mn, mg, mc = map(int, parts)
        await db.set_limits(uid, mn, mg, mc)
        await msg.answer(f"✅ تم تحديث حدود {uid}.")
    except ValueError:
        await msg.answer("❌ قيم غير رقمية.")
    await state.clear()
