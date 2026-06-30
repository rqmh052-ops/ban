# -*- coding: utf-8 -*-
"""
main.py — بوت حراسة قنوات متعددة (Multi-Channel Guard Bot) — النسخة الكاملة.

يطبّق كل أقسام البرومبت النهائي:
- ربط أي عدد من القنوات عن طريق KeyboardButtonRequestChat.
- حظر فوري لمن يغادر خلال أول 7 أيام من انضمامه (قسم 4، حالة أ).
- فترة مراقبة 3 أيام لمن يغادر بعد 7 أيام أو أكثر، حظر تلقائي بعدها إن لم يعد (قسم 4، حالة ب).
- سجل سبب الحظر لكل عملية حظر (قسم 5).
- إشعار حظر مفصّل (بالاسم والسبب) لمالك القناة، مع زر إيقاف الإشعارات (تصحيح المستخدم).
- تصدير قائمة المحظورين (قسم 7).
- حماية معدل الحظر التلقائي: 8 حظر/دقيقة لكل قناة (قسم 8، مبسّط لرقم ثابت حسب طلب المستخدم).
- صلاحيات مطوّر شاملة: تحكم كامل بكل قناة، بث جماعي بمعاينة وتأكيد، بحث عن مستخدم
  عبر كل القنوات مع حظر مباشر (قسم 9).
"""

import os
import io
import csv
import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ChatAdministratorRights,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError, Conflict

import db

# ---------------------------------------------------------------------------
# إعدادات أساسية وعالمية
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# OWNER_ID = السوبر أدمن العالمي الوحيد (صاحب البوت) — له صلاحية على كل القنوات
# بالإضافة لصلاحيات حصرية إضافية (قسم 9).
_owner_id_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(_owner_id_raw) if _owner_id_raw.isdigit() else None

# الحالات اللي بنعتبرها "عضو نشط فعليا" داخل القناة
ACTIVE_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
LEFT_STATUSES = {ChatMemberStatus.LEFT}
KICKED_STATUSES = {ChatMemberStatus.BANNED}

# عدد العناصر في كل صفحة للقوائم المختلفة (pagination حقيقي)
CHANNELS_PER_PAGE = 8
BANNED_PER_PAGE = 10
WHITELIST_PER_PAGE = 10

DEFAULT_WELCOME_PHOTO_KEY = "default_welcome_photo_file_id"
LINK_CHANNEL_REQUEST_ID = 1001

# --- قسم 4: نظام حماية مدة العضوية ---
QUICK_LEAVE_THRESHOLD_DAYS = 7   # أقل من 7 أيام من الانضمام = حظر فوري
WATCH_PERIOD_DAYS = 3            # مهلة العودة بعد المغادرة لمن قعد 7 أيام أو أكثر
WATCH_CHECK_INTERVAL_SECONDS = 3600  # الـ JobQueue بتفحص فترات المراقبة المنتهية كل ساعة

# --- قسم 8: حماية معدل الحظر التلقائي ---
# ✅ حسب توضيح المستخدم: رقم ثابت لكل القنوات (8 حظر/دقيقة)، بدون تدرج بالحجم،
# ويُحسب على الحظر التلقائي اللي البوت نفّذه بنفسه فقط (فوري أو بعد فترة المراقبة) —
# لا يشمل /ban_id اليدوي ولا أي حظر يدوي آخر.
AUTO_BAN_RATE_LIMIT_COUNT = 8
AUTO_BAN_RATE_LIMIT_WINDOW_SECONDS = 60

# عداد in-memory لكل قناة: deque من timestamps لآخر حظورات تلقائية (مش يدوية).
# بيتصفّر تلقائياً لأننا بنشيل أي timestamp أقدم من النافذة الزمنية مع كل فحص.
_auto_ban_timestamps: dict[int, deque] = defaultdict(deque)
# قنوات موقوف فيها الحظر التلقائي مؤقتاً بسبب تجاوز المعدل (channel_id -> سبب نصي للعرض)
_rate_limited_channels: set[int] = set()


# ---------------------------------------------------------------------------
# دوال الصلاحيات
# ---------------------------------------------------------------------------

def is_super_owner(user_id: int) -> bool:
    """هل ده الأدمن الأعلى (صاحب البوت)؟ له صلاحية على كل القنوات + صلاحيات حصرية."""
    if OWNER_ID is None:
        return False
    return user_id == OWNER_ID


def can_manage_channel(user_id: int, channel_id: int) -> bool:
    """
    هل المستخدم ده يقدر يدير القناة دي من اللوحة؟
    - السوبر أونر: يقدر يدير أي قناة (قسم 9.أ).
    - مالك القناة (اللي ضافها): يقدر يدير قناته بس.
    - أي حد تاني: لأ.
    """
    if is_super_owner(user_id):
        return True
    ch = db.get_channel(channel_id)
    return ch is not None and ch["active"] and ch["owner_user_id"] == user_id


def get_accessible_channels(user_id: int):
    """القنوات اللي المستخدم ده يقدر يديرها من اللوحة."""
    if is_super_owner(user_id):
        return db.get_all_active_channels()
    return db.get_channels_owned_by(user_id)


# ---------------------------------------------------------------------------
# دوال تنسيق النصوص
# ---------------------------------------------------------------------------

_MDV2_SPECIAL = r'([_*\[\]()~`>#+\-=|{}.!\\])'


def escape_md(text) -> str:
    """Escape آمن لأي نص قبل ما يتحط في رسالة MarkdownV2."""
    if text is None:
        return ""
    return re.sub(_MDV2_SPECIAL, r'\\\1', str(text))


def format_user_label(username, full_name, user_id) -> str:
    if username:
        return f"@{username}"
    if full_name and full_name.strip():
        return full_name.strip()
    return f"ID:{user_id}"


def fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def channel_display_name(channel_row) -> str:
    if channel_row and channel_row.get("title"):
        return channel_row["title"]
    return str(channel_row["channel_id"]) if channel_row else "—"


def ban_reason_label(reason: str) -> str:
    return db.BAN_REASON_LABELS_AR.get(reason, reason or "غير محدد")


# ---------------------------------------------------------------------------
# دوال بناء callback_data بصيغة موحدة بفاصل واضح "|"
# ---------------------------------------------------------------------------

def build_cb(action: str, *parts) -> str:
    segments = [action] + [str(p) for p in parts]
    data = "|".join(segments)
    if len(data.encode("utf-8")) > 64:
        logger.warning(f"callback_data تجاوز 64 بايت: {data}")
    return data


def parse_cb(data: str):
    return data.split("|")


# ---------------------------------------------------------------------------
# حماية معدل الحظر التلقائي (قسم 8)
# ---------------------------------------------------------------------------

def _prune_old_timestamps(channel_id: int):
    """يشيل أي timestamp أقدم من نافذة الدقيقة من عداد القناة دي — هذا هو "التصفير" التلقائي."""
    now = datetime.now(timezone.utc).timestamp()
    dq = _auto_ban_timestamps[channel_id]
    while dq and (now - dq[0]) > AUTO_BAN_RATE_LIMIT_WINDOW_SECONDS:
        dq.popleft()


def is_channel_rate_limited(channel_id: int) -> bool:
    return channel_id in _rate_limited_channels


def record_auto_ban_and_check_limit(channel_id: int) -> bool:
    """
    يسجل عملية حظر تلقائي جديدة (فوري أو بعد فترة مراقبة — وليس /ban_id اليدوي)
    ويرجع True لو القناة تجاوزت الحد دلوقتي (يعني لازم نوقف الحظر التلقائي فيها).
    """
    _prune_old_timestamps(channel_id)
    now = datetime.now(timezone.utc).timestamp()
    _auto_ban_timestamps[channel_id].append(now)

    if len(_auto_ban_timestamps[channel_id]) >= AUTO_BAN_RATE_LIMIT_COUNT:
        if channel_id not in _rate_limited_channels:
            _rate_limited_channels.add(channel_id)
            return True  # أول لحظة تجاوز — هنبعت التنبيه
    return False


def clear_rate_limit(channel_id: int):
    """فك إيقاف الحظر التلقائي يدوياً لقناة معينة (زرار في اللوحة)."""
    _rate_limited_channels.discard(channel_id)
    _auto_ban_timestamps[channel_id].clear()


# ---------------------------------------------------------------------------
# إشعار الحظر لمالك القناة (مفصّل: بالاسم والسبب + زر إيقاف الإشعارات)
# ---------------------------------------------------------------------------

async def notify_owner_of_ban(context: ContextTypes.DEFAULT_TYPE, channel_id: int,
                               username, full_name, user_id: int, reason: str):
    """
    ✅ تصحيح المستخدم: الإشعار يتضمن اسم/معرف المحظور والسبب (فوري/مراقبة/يدوي)،
    وتحته زر مضمّن لإيقاف هذا النوع من الإشعارات لهذه القناة تحديداً.
    يصل فقط لمالك القناة (الشخص اللي أضاف البوت فيها)، وليس للمطوّر إلا لو هو نفسه المالك.
    """
    if not db.are_ban_notifications_on(channel_id):
        return

    ch = db.get_channel(channel_id)
    if ch is None:
        return
    owner_id = ch["owner_user_id"]

    label = escape_md(format_user_label(username, full_name, user_id))
    chname = escape_md(channel_display_name(ch))
    reason_text = escape_md(ban_reason_label(reason))

    text = (
        f"🚫 *تم حظر عضو في قناتك* {chname}\n\n"
        f"👤 الشخص: {label} \\(`{user_id}`\\)\n"
        f"📋 السبب: {reason_text}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔕 إيقاف إشعارات الحظر لهذه القناة", callback_data=build_cb("bannotif_off", channel_id))
    ]])

    try:
        await context.bot.send_message(
            owner_id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2
        )
    except TelegramError as e:
        logger.warning(f"فشل إشعار حظر لمالك القناة {owner_id} (قناة {channel_id}): {e}")


async def notify_owner_of_rate_limit(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """تنبيه فوري لمالك القناة عند تجاوز معدل الحظر التلقائي (قسم 8)."""
    ch = db.get_channel(channel_id)
    if ch is None:
        return
    owner_id = ch["owner_user_id"]
    chname = escape_md(channel_display_name(ch))

    text = (
        f"⚠️ *تنبيه أمان* — قناتك {chname}\n\n"
        f"تم رصد {AUTO_BAN_RATE_LIMIT_COUNT} عمليات حظر تلقائي خلال دقيقة واحدة\\.\n"
        "تم *إيقاف الحظر التلقائي مؤقتاً* في هذه القناة تحديداً لمنع أي خطأ مفاجئ، "
        "والقنوات الأخرى وباقي البوت يعملان بشكل طبيعي\\.\n\n"
        "يمكنك إعادة التفعيل يدوياً من لوحة القناة\\."
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ إعادة تفعيل الحظر التلقائي الآن", callback_data=build_cb("ratelimit_clear", channel_id))
    ]])

    try:
        await context.bot.send_message(owner_id, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e:
        logger.warning(f"فشل تنبيه تجاوز المعدل لمالك القناة {owner_id} (قناة {channel_id}): {e}")

    # السوبر أونر كمان لازم يكون عارف، لأنه مسؤول عن كل القنوات (قسم 9.أ) — إلا لو هو نفسه المالك
    if OWNER_ID and owner_id != OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ تجاوز معدل حظر في قناة `{channel_id}` \\({chname}\\) — تم الإيقاف المؤقت تلقائياً\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except TelegramError as e:
            logger.warning(f"فشل تنبيه السوبر أونر بتجاوز المعدل: {e}")


# ---------------------------------------------------------------------------
# تنفيذ الحظر الموحّد — نقطة واحدة تستخدمها كل المسارات الثلاثة
# (فوري / بعد فترة المراقبة / يدوي) عشان نضمن إن كل حظر بيتسجل بنفس الطريقة
# بالظبط (DB + rate limit + إشعار)، بدل تكرار نفس الخطوات في 3 أماكن مختلفة.
# ---------------------------------------------------------------------------

async def execute_ban(context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int,
                       username, full_name, reason: str, is_automatic: bool):
    """
    ينفذ الحظر فعلياً عبر تيليجرام، يسجله في قاعدة البيانات بسببه، يبعت إشعار
    لمالك القناة، وفي حالة الحظر التلقائي بس — يفحص حد معدل الحظر (قسم 8).
    """
    try:
        await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
    except TelegramError as e:
        logger.error(f"[{channel_id}] فشل تنفيذ حظر {user_id} (سبب: {reason}): {e}")
        return False

    db.mark_member_banned(channel_id, user_id, reason)
    logger.info(f"[{channel_id}] تم حظر {format_user_label(username, full_name, user_id)} — السبب: {reason}")

    await notify_owner_of_ban(context, channel_id, username, full_name, user_id, reason)

    if is_automatic:
        exceeded = record_auto_ban_and_check_limit(channel_id)
        if exceeded:
            await notify_owner_of_rate_limit(context, channel_id)

    return True


# ---------------------------------------------------------------------------
# المعالج الأساسي: تتبع دخول وخروج الأعضاء + منطق الحظر (فوري أو مراقبة)
# ---------------------------------------------------------------------------

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    بيتنفذ مع كل تحديث في حالة عضوية أي شخص داخل أي قناة البوت موجود فيها.
    هنا قلب منطق الحظر — مع تطبيق قسم 4 (مدة العضوية) كاملاً.
    """
    result = update.chat_member
    if result is None:
        return

    channel_id = result.chat.id

    if not db.is_channel_active(channel_id):
        return

    user = result.new_chat_member.user
    # ✅ نستخدم old_status الجاهز في الـ update نفسه (إصلاح حظر الأدمن بالغلط)
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    user_id = user.id
    username = user.username
    full_name = (f"{user.first_name or ''} {user.last_name or ''}").strip()

    # حالة 1: انضمام جديد فعلي (أو عودة بعد مغادرة)
    if old_status not in ACTIVE_STATUSES and new_status in ACTIVE_STATUSES:
        db.upsert_member_join(channel_id, user_id, username, full_name)
        logger.info(f"[{channel_id}] انضمام/عودة: {format_user_label(username, full_name, user_id)}")
        return

    # حالة 2: عضو غادر بنفسه (member/admin/owner -> left)
    if old_status in ACTIVE_STATUSES and new_status in LEFT_STATUSES:
        # نجيب بيانات العضو قبل ما نحدّث حالته لـ left، عشان نعرف تاريخ انضمامه الأصلي
        member_row = db.get_member(channel_id, user_id)
        db.mark_member_left(channel_id, user_id)
        logger.info(f"[{channel_id}] مغادرة: {format_user_label(username, full_name, user_id)}")

        # 1) لو كان أدمن/مالك قبل المغادرة مباشرة -> تجاهل تماماً (لا حظر ولا مراقبة)
        if old_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            logger.info(f"[{channel_id}] تم تجاهل {user_id} — كان أدمن/مالك قبل المغادرة مباشرة.")
            return

        # 2) لو موجود في القائمة المحصّنة -> تجاهل تماماً
        if db.is_whitelisted(channel_id, user_id):
            logger.info(f"[{channel_id}] تم تجاهل {user_id} — في القائمة المحصّنة.")
            return

        # --- قسم 4: تحديد المسار حسب مدة العضوية ---
        # لو القناة موقوف فيها الحظر التلقائي مؤقتاً (تجاوز معدل)، منوقفش تسجيل
        # المغادرة ولا منعمل مراقبة، لكن مفيش حظر تلقائي خالص لحد ما يتفك يدوياً.
        if is_channel_rate_limited(channel_id):
            logger.info(f"[{channel_id}] الحظر التلقائي موقوف مؤقتاً (تجاوز معدل) — تم تجاهل حظر {user_id}.")
            return

        join_date = member_row["join_date"] if member_row else None
        membership_days = None
        if join_date is not None:
            now = datetime.now(timezone.utc)
            membership_days = (now - join_date).total_seconds() / 86400.0

        if membership_days is not None and membership_days >= QUICK_LEAVE_THRESHOLD_DAYS:
            # حالة ب: قعد 7 أيام أو أكتر -> فترة مراقبة 3 أيام بدل حظر فوري
            db.start_watch_period(channel_id, user_id, watch_days=WATCH_PERIOD_DAYS)
            logger.info(
                f"[{channel_id}] {user_id} دخل فترة مراقبة {WATCH_PERIOD_DAYS} أيام "
                f"(قعد {membership_days:.1f} يوم قبل المغادرة)."
            )
            return
        else:
            # حالة أ: قعد أقل من 7 أيام (أو تاريخ انضمامه غير معروف لأي سبب) -> حظر فوري
            await execute_ban(
                context, channel_id, user_id, username, full_name,
                reason=db.BAN_REASON_QUICK_LEAVE, is_automatic=True,
            )
        return

    # حالة 3: عضو اتطرد بالفعل (kicked) بمعرفة حد تاني أو البوت نفسه — بنسجل بس،
    # ده مش حظر بقرار البوت فمنسجلوش كـ ban_reason معروف ولا بيدخل rate limit.
    if new_status in KICKED_STATUSES and old_status not in KICKED_STATUSES:
        # ⚠️ لو البوت هو اللي حظر العضو ده للتو (عبر execute_ban)، تيليجرام بيبعت
        # update تاني لنفس الحدث (status -> kicked) بيوصل هنا كمان. لازم نتجاهله
        # وإلا هيكتب فوق ban_reason الصحيح (quick_leave/watch_expired) بـ "manual"
        # ويضيف حدث حظر مكرر في events_log. نتأكد إن العضو مش متسجل "banned" بالفعل.
        member_row = db.get_member(channel_id, user_id)
        if member_row and member_row["status"] == "banned":
            return
        db.mark_member_banned(channel_id, user_id, reason=db.BAN_REASON_MANUAL)
        logger.info(f"[{channel_id}] تسجيل حظر/طرد خارجي: {format_user_label(username, full_name, user_id)}")
        return


# ---------------------------------------------------------------------------
# Job مجدولة: فحص فترات المراقبة المنتهية كل ساعة (قسم 4)
# ---------------------------------------------------------------------------

async def check_expired_watch_periods_job(context: ContextTypes.DEFAULT_TYPE):
    """
    تعمل كل ساعة (WATCH_CHECK_INTERVAL_SECONDS). تدوّر على كل فترات المراقبة
    اللي انتهت مهلتها عبر كل القنوات دفعة واحدة، وتحظر كل عضو لم يعد خلالها.
    """
    expired = db.get_expired_watch_periods()
    if not expired:
        return

    logger.info(f"فحص فترات المراقبة المنتهية: وجد {len(expired)} حالة.")

    for row in expired:
        channel_id = row["channel_id"]
        user_id = row["user_id"]

        # القناة ممكن تكون اتلغى ربطها أو موقوفة في rate limit من وقت ما بدأت المراقبة
        if not db.is_channel_active(channel_id):
            db.clear_watch_period(channel_id, user_id)
            continue
        if is_channel_rate_limited(channel_id):
            # نسيبها في الجدول لحد ما يتفك الإيقاف — هتتحظر في الفحص التالي
            continue

        ok = await execute_ban(
            context, channel_id, user_id, row["username"], row["full_name"],
            reason=db.BAN_REASON_WATCH_EXPIRED, is_automatic=True,
        )
        if ok:
            db.clear_watch_period(channel_id, user_id)
        # لو فشل الحظر (مثلاً البوت فقد صلاحياته في القناة)، نسيب الصف في الجدول
        # عمداً عشان نعاود المحاولة في الفحص التالي بدل ما العضو يفلت تماماً.


# ---------------------------------------------------------------------------
# تتبع حالة البوت نفسه (my_chat_member) — اكتشاف الترقية لأدمن في قناة جديدة
# ---------------------------------------------------------------------------

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يتنفذ لما تتغير صلاحيات البوت نفسه في أي شات."""
    result = update.my_chat_member
    if result is None:
        return

    chat = result.chat
    if chat.type != ChatType.CHANNEL:
        return  # القنوات فقط — مش جروبات ولا سوبر جروبات

    new_status = result.new_chat_member.status
    channel_id = chat.id

    if new_status == ChatMemberStatus.ADMINISTRATOR:
        owner_id = context.bot_data.pop(f"_linking_user_{channel_id}", None)
        if owner_id is None:
            owner_id = OWNER_ID

        db.upsert_channel(channel_id, owner_id, chat.title)
        logger.info(f"تم ربط قناة جديدة: {channel_id} ({chat.title}) — المالك: {owner_id}")

        await send_welcome_to_channel(context, channel_id)
        await send_link_notifications(context, channel_id, owner_id, chat.title)

    elif new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        db.deactivate_channel(channel_id)
        logger.info(f"تم إلغاء تفعيل قناة: {channel_id} (البوت لم يعد أدمن)")


async def send_welcome_to_channel(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """رسالة ترحيب داخل القناة نفسها + صورة (لو محددة لهذه القناة أو صورة افتراضية)."""
    ch = db.get_channel(channel_id)
    photo_id = (ch["welcome_photo_file_id"] if ch else None) or db.get_setting(DEFAULT_WELCOME_PHOTO_KEY)

    caption = (
        "🛡️ *تم تفعيل نظام الحماية على هذه القناة\\.*\n\n"
        "⚠️ أي عضو يغادر القناة سيتم حظره \\(فوراً أو بعد فترة مراقبة قصيرة حسب مدة عضويته\\)\\."
    )

    try:
        if photo_id:
            await context.bot.send_photo(
                chat_id=channel_id, photo=photo_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await context.bot.send_message(chat_id=channel_id, text=caption, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e:
        logger.warning(f"فشل إرسال رسالة الترحيب في القناة {channel_id}: {e}")


async def send_link_notifications(context: ContextTypes.DEFAULT_TYPE, channel_id: int, owner_id: int, title: str):
    """إشعارين عند ربط قناة جديدة: لمالك القناة، وللسوبر أونر (إلا لو هو نفسه المالك)."""
    safe_title = escape_md(title or str(channel_id))

    try:
        await context.bot.send_message(
            owner_id,
            f"✅ تم تفعيل البوت بنجاح على قناة *{safe_title}*\\!\n\n"
            "أي عضو يغادر القناة من الآن سيخضع لنظام الحماية التلقائي\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramError as e:
        logger.warning(f"فشل إشعار المالك {owner_id}: {e}")

    if OWNER_ID and owner_id != OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"📢 مستخدم `{owner_id}` أضاف البوت كأدمن على قناة *{safe_title}*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except TelegramError as e:
            logger.warning(f"فشل إشعار السوبر أونر: {e}")


# ---------------------------------------------------------------------------
# زرار ربط قناة جديدة (KeyboardButtonRequestChat)
# ---------------------------------------------------------------------------

def build_link_channel_keyboard() -> ReplyKeyboardMarkup:
    required_rights = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_post_messages=True,
        can_edit_messages=False,
        can_pin_messages=False,
    )
    btn = KeyboardButton(
        text="📡 اختر قناتك لتفعيل الحماية",
        request_chat=KeyboardButtonRequestChat(
            request_id=LINK_CHANNEL_REQUEST_ID,
            chat_is_channel=True,
            user_administrator_rights=required_rights,
            bot_administrator_rights=required_rights,
            bot_is_member=False,
        ),
    )
    return ReplyKeyboardMarkup([[btn]], resize_keyboard=True, one_time_keyboard=True)


async def cmd_link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/link أو زرار 'ربط قناة جديدة' — يفتح كيبورد اختيار القناة."""
    await update.message.reply_text(
        "➕ *ربط قناة جديدة*\n\n"
        "اضغط الزرار تحت واختر القناة التي تريد تفعيل الحماية عليها من قائمة قنواتك:",
        reply_markup=build_link_channel_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يتنفذ فور ما المستخدم يختار قناة من الكيبورد ويأكد ترقية البوت."""
    msg = update.message
    if not msg or not msg.chat_shared:
        return

    if msg.chat_shared.request_id != LINK_CHANNEL_REQUEST_ID:
        return

    requested_channel_id = msg.chat_shared.chat_id
    requesting_user_id = msg.from_user.id

    context.bot_data[f"_linking_user_{requested_channel_id}"] = requesting_user_id

    await msg.reply_text(
        "⏳ تم استلام اختيارك\\. بمجرد قبول صلاحيات الإدارة سيتفعل البوت تلقائياً على القناة\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ReplyKeyboardRemove(),
    )


# ---------------------------------------------------------------------------
# /start — نقطة الدخول الرئيسية للوحة
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start أو /menu — بيفتح اللوحة الرئيسية. يشتغل في الخاص فقط."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    user_id = update.effective_user.id
    channels = get_accessible_channels(user_id)

    if not channels and not is_super_owner(user_id):
        await update.message.reply_text(
            "🛡️ *أهلاً بك في بوت حراسة القنوات\\!*\n\n"
            "لا توجد قنوات مربوطة بحسابك حتى الآن\\.\n"
            "اضغط الزرار تحت لربط أول قناة:",
            reply_markup=build_link_channel_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await update.message.reply_text(
        "🛡️ *لوحة تحكم حارس القنوات*\n\nاختار من الأزرار تحت:",
        reply_markup=build_main_menu_keyboard(user_id),
        parse_mode=ParseMode.MARKDOWN,
    )


def build_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """
    القائمة الرئيسية. لغير السوبر أونر: قنواته بس + ربط قناة جديدة.
    للسوبر أونر (قسم 9): كل ما سبق + قسم "صلاحيات المطوّر" كامل ومنفصل تماماً —
    لا يظهر أي أثر له لأي مالك قناة عادي (مبدأ السرية المؤكد في القسم 9).
    """
    keyboard = []
    channels = get_accessible_channels(user_id)

    if len(channels) == 1:
        ch = channels[0]
        keyboard.append([
            InlineKeyboardButton(f"📡 {channel_display_name(ch)}", callback_data=build_cb("chmenu", ch["channel_id"]))
        ])
    elif len(channels) > 1:
        label = "📡 قنواتي" if not is_super_owner(user_id) else f"📡 قنواتي ({len(channels)})"
        keyboard.append([InlineKeyboardButton(label, callback_data=build_cb("chlist", "mine", 0))])

    keyboard.append([InlineKeyboardButton("➕ ربط قناة جديدة", callback_data=build_cb("linkchannel"))])

    if is_super_owner(user_id):
        all_count = len(db.get_all_active_channels())
        keyboard.append([
            InlineKeyboardButton(f"🗂️ كل القنوات ({all_count})", callback_data=build_cb("chlist", "all", 0))
        ])
        keyboard.append([
            InlineKeyboardButton("👑 لوحة المطوّر (صلاحيات حصرية)", callback_data=build_cb("devpanel"))
        ])

    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# لوحة المطوّر الحصرية (قسم 9) — لا تظهر إلا لـ OWNER_ID
# ---------------------------------------------------------------------------

def build_dev_panel_keyboard() -> InlineKeyboardMarkup:
    """
    قسم 9 بالكامل:
    - أ) التحكم الكامل بكل قناة متاح أصلاً عبر "كل القنوات" في القائمة الرئيسية.
    - ب) بث إعلانات جماعي.
    - ج) البحث عن مستخدم عبر كل القنوات (مع حظر مباشر).
    - د) صورة الترحيب الافتراضية على مستوى البوت كله (إعداد عام يخص المطوّر).
    """
    keyboard = [
        [InlineKeyboardButton("📢 بث إعلان جماعي لكل القنوات", callback_data=build_cb("broadcast_start"))],
        [InlineKeyboardButton("🔍 بحث عن مستخدم عبر كل القنوات", callback_data=build_cb("globalsearch_start"))],
        [InlineKeyboardButton("🖼️ صورة الترحيب الافتراضية", callback_data=build_cb("setdefphoto"))],
        [InlineKeyboardButton("📊 إحصائيات البوت الإجمالية", callback_data=build_cb("botwidestats"))],
        [InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("menu"))],
    ]
    return InlineKeyboardMarkup(keyboard)


async def show_dev_panel(query, user_id: int):
    if not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return
    await query.edit_message_text(
        "👑 *لوحة المطوّر — صلاحيات حصرية*\n\n"
        "هذا القسم لا يظهر لأي مالك قناة آخر مهما كانت صلاحياته\\.",
        reply_markup=build_dev_panel_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def show_bot_wide_stats(query, user_id: int):
    if not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return
    s = db.get_bot_wide_stats()
    text = (
        "📊 *إحصائيات البوت الإجمالية* \\(كل القنوات\\)\n\n"
        f"📡 عدد القنوات النشطة: *{s['channels_count']}*\n"
        f"👥 إجمالي الأعضاء النشطين: *{s['active_members_count']}*\n"
        f"🚫 إجمالي المحظورين: *{s['banned_total']}*"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("devpanel"))]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)


# أعلام "انتظار إدخال" المتنافسة في user_data — كل واحد منها بيخلي الرسالة/الصورة
# الجاية تتفسر كإدخال لـ flow معين. لازم تتمسح كلها قبل ما نبدأ flow جديد، وإلا لو
# المستخدم بدأ flow وسابه نص، وبدأ flow تاني، هيتفسر إدخاله الجديد حسب ترتيب
# الفحص في handle_private_text/handle_private_photo مش حسب آخر زرار ضغطه فعلاً.
_AWAITING_FLAG_KEYS = (
    "_awaiting_wladd_for",
    "_awaiting_local_search_for",
    "_awaiting_global_search",
    "_awaiting_broadcast_content",
    "_awaiting_welcome_photo_for",
    "_awaiting_default_welcome_photo",
)


def _clear_awaiting_flags(context):
    for key in _AWAITING_FLAG_KEYS:
        context.user_data.pop(key, None)


# --- ب) بث إعلانات جماعي — يتطلب معاينة وتأكيد صريح من المطوّر قبل الإرسال ---

async def start_broadcast_flow(query, context, user_id: int):
    if not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return
    _clear_awaiting_flags(context)
    context.user_data["_awaiting_broadcast_content"] = True
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data=build_cb("devpanel"))]])
    await query.edit_message_text(
        "📢 *بث إعلان جماعي*\n\n"
        "أرسل الآن النص \\(ويمكن إرفاق صورة معه\\) الذي تريد بثه لكل القنوات المربوطة\\.\n"
        "سيتم عرض معاينة وعدد القنوات/الأعضاء قبل الإرسال الفعلي، ولن يُرسل شيء قبل تأكيدك مباشرة\\.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def receive_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستقبل محتوى البث (نص أو نص+صورة) من المطوّر، ويعرض معاينة + تأكيد قبل أي إرسال فعلي."""
    user_id = update.effective_user.id
    if not is_super_owner(user_id):
        return

    text = update.message.text or update.message.caption or ""
    photo_file_id = update.message.photo[-1].file_id if update.message.photo else None

    if not text and not photo_file_id:
        await update.message.reply_text("⚠️ محتاج نص أو صورة على الأقل. أرسل المحتوى تاني.")
        return

    # نخزن المحتوى مؤقتاً في bot_data (مش user_data) لأن التأكيد بيجي من زرار،
    # وbot_data أكثر أماناً هنا لأنه مش هيتمسح لو user_data اتعمله reset لأي سبب
    context.bot_data["_pending_broadcast"] = {"text": text, "photo_file_id": photo_file_id}
    context.user_data.pop("_awaiting_broadcast_content", None)

    s = db.get_bot_wide_stats()
    preview = text or "(صورة بدون نص)"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد الإرسال الآن", callback_data=build_cb("broadcast_confirm"))],
        [InlineKeyboardButton("❌ إلغاء", callback_data=build_cb("devpanel"))],
    ])

    caption = (
        f"📢 *معاينة البث الجماعي*\n\n"
        f"سيصل هذا المحتوى إلى *{s['channels_count']}* قناة "
        f"\\(بإجمالي تقديري *{s['active_members_count']}* عضو نشط\\)\\.\n\n"
        f"— — — — —\n{escape_md(preview)}\n— — — — —\n\n"
        "هذا الإجراء لا يتطلب موافقة من أصحاب القنوات\\. اضغط تأكيد للإرسال الفعلي\\."
    )

    if photo_file_id:
        await update.message.reply_photo(photo_file_id, caption=caption, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(caption, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)


async def confirm_and_send_broadcast(query, context, user_id: int):
    """التأكيد الفعلي — الإرسال الحقيقي لكل القنوات، لا يحدث إلا هنا."""
    if not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return

    pending = context.bot_data.pop("_pending_broadcast", None)
    if pending is None:
        await query.edit_message_text("⚠️ لا يوجد بث معلّق للتأكيد (ربما انتهت صلاحيته). ابدأ من جديد.")
        return

    await query.edit_message_text("⏳ جاري الإرسال لكل القنوات...")

    channels = db.get_all_active_channels()
    sent, failed = 0, 0
    for ch in channels:
        try:
            if pending["photo_file_id"]:
                await context.bot.send_photo(
                    chat_id=ch["channel_id"], photo=pending["photo_file_id"], caption=pending["text"] or None
                )
            else:
                await context.bot.send_message(chat_id=ch["channel_id"], text=pending["text"])
            sent += 1
        except TelegramError as e:
            failed += 1
            logger.warning(f"فشل بث الإعلان لقناة {ch['channel_id']}: {e}")

    await context.bot.send_message(
        user_id,
        f"✅ تم الانتهاء من البث.\n\nنجح: {sent}\nفشل: {failed}",
    )


# --- ج) بحث عن مستخدم عبر كل القنوات — مع إمكانية حظر مباشر ---

async def start_global_search_flow(query, context, user_id: int):
    if not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return
    _clear_awaiting_flags(context)
    context.user_data["_awaiting_global_search"] = True
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data=build_cb("devpanel"))]])
    await query.edit_message_text(
        "🔍 *بحث عن مستخدم عبر كل القنوات*\n\n"
        "أرسل الآن الـ *USER ID* الرقمي، أو *username* \\(بدون @\\) للشخص الذي تريد البحث عنه:",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def receive_global_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    قسم 9.ج/د: يقبل إما USER ID رقمي أو username (بحرف أو بدون @)، ويبحث عبر
    كل القنوات المربوطة. البحث بالـ username يحتاج أولاً تحديد الـ user_id
    المطابق له من أي قناة موجود فيها، ثم نكمل البحث الشامل بنفس الـ ID.
    """
    user_id = update.effective_user.id
    if not is_super_owner(user_id):
        return

    text = (update.message.text or "").strip().lstrip("@")
    context.user_data.pop("_awaiting_global_search", None)

    if text.isdigit():
        target_id = int(text)
    else:
        # بحث بالـ username: نلاقي أول تطابق عبر كل القنوات عشان نطلع الـ user_id بتاعه
        found_id = None
        for ch in db.get_all_active_channels():
            m = db.find_user_by_username_in_channel(ch["channel_id"], text)
            if m:
                found_id = m["user_id"]
                break
        if found_id is None:
            await update.message.reply_text(f"🔍 لم يتم العثور على @{text} في أي قناة مربوطة.")
            return
        target_id = found_id

    results = db.find_user_across_channels(target_id)

    if not results:
        await update.message.reply_text(f"🔍 لم يتم العثور على `{target_id}` في أي قناة مربوطة.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"🔍 *نتائج البحث عن* `{target_id}`\n"]
    keyboard = []
    status_labels = {
        "active": "نشط ✅", "left": "غادر 🚪", "watching": "تحت المراقبة ⏳",
        "banned": "محظور 🚫", "unknown": "غير معروف ❔",
    }
    for r in results:
        label = format_user_label(r["username"], r["full_name"], target_id)
        chname = channel_display_name({"title": r["title"], "channel_id": r["channel_id"]})
        status_text = status_labels.get(r["status"], r["status"])
        lines.append(f"📡 *{escape_md(chname)}*: {status_text} — {escape_md(label)}")

        if r["status"] != "banned":
            keyboard.append([
                InlineKeyboardButton(
                    f"🚫 حظر في {chname}",
                    callback_data=build_cb("devban", r["channel_id"], target_id)
                )
            ])

    keyboard.append([InlineKeyboardButton("⬅️ رجوع للوحة المطوّر", callback_data=build_cb("devpanel"))])

    await update.message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2
    )


async def do_dev_ban(query, context, channel_id: int, target_id: int):
    """حظر مباشر من نتيجة البحث الشامل — حظر يدوي (ليس له علاقة بمنطق المراقبة أو rate limit)."""
    ok = await execute_ban(context, channel_id, target_id, None, None, reason=db.BAN_REASON_MANUAL, is_automatic=False)
    if ok:
        await query.answer("تم الحظر ✅", show_alert=True)
    else:
        await query.answer("فشل الحظر — راجع السجلات.", show_alert=True)


# --- بحث محلي داخل قناة واحدة (متاح لمالك القناة العادي، قسم 9.د) ---

async def start_local_search_flow(query, context, user_id: int, channel_id: int):
    if not can_manage_channel(user_id, channel_id):
        await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
        return
    _clear_awaiting_flags(context)
    context.user_data["_awaiting_local_search_for"] = channel_id
    keyboard = build_back_to_channel_button(channel_id)
    await query.edit_message_text(
        "🔎 *بحث عن عضو في هذه القناة*\n\n"
        "أرسل الآن الـ *USER ID* الرقمي، أو *username* \\(بدون @\\):",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def receive_local_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    user_id = update.effective_user.id
    if not can_manage_channel(user_id, channel_id):
        await update.message.reply_text("غير مصرح لك بإدارة هذه القناة.")
        return

    text = (update.message.text or "").strip().lstrip("@")

    if text.isdigit():
        member = db.get_member(channel_id, int(text))
    else:
        member = db.find_user_by_username_in_channel(channel_id, text)

    if member is None:
        await update.message.reply_text(
            f"🔍 لم يتم العثور على \"{text}\" في هذه القناة.",
            reply_markup=build_back_to_channel_button(channel_id),
        )
        return

    status_labels = {
        "active": "نشط ✅", "left": "غادر 🚪", "watching": "تحت المراقبة ⏳",
        "banned": "محظور 🚫", "unknown": "غير معروف ❔",
    }
    label = format_user_label(member["username"], member["full_name"], member["user_id"])
    status_text = status_labels.get(member["status"], member["status"])

    lines = [
        f"🔎 *نتيجة البحث*\n",
        f"👤 {escape_md(label)} \\(`{member['user_id']}`\\)",
        f"📌 الحالة: {status_text}",
    ]
    if member["status"] == "banned" and member["ban_reason"]:
        lines.append(f"📋 سبب الحظر: {escape_md(ban_reason_label(member['ban_reason']))}")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=build_back_to_channel_button(channel_id),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# معالج ضغطات الأزرار — Dispatcher مركزي واحد
# ---------------------------------------------------------------------------

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    parts = parse_cb(query.data)
    action = parts[0]

    # ===== القائمة الرئيسية =====
    if action == "menu":
        await query.edit_message_text(
            "🛡️ *لوحة تحكم حارس القنوات*\n\nاختار من الأزرار تحت:",
            reply_markup=build_main_menu_keyboard(user_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ===== ربط قناة جديدة (من جوه اللوحة) =====
    if action == "linkchannel":
        await context.bot.send_message(
            user_id,
            "➕ *ربط قناة جديدة*\n\nاضغط الزرار تحت واختر القناة:",
            reply_markup=build_link_channel_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ===== قائمة قنوات (mine / all) مع pagination =====
    if action == "chlist":
        scope = parts[1]
        page = int(parts[2])
        await show_channels_list(query, context, scope, page, user_id)
        return

    # ===== فتح قائمة قناة محددة =====
    if action == "chmenu":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await open_channel_menu(query, context, channel_id)
        return

    # ===== الإحصائيات =====
    if action == "stats":
        stat_type = parts[1]
        channel_id = int(parts[2])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await show_stats(query, stat_type, channel_id)
        return

    # ===== إدارة المحظورين =====
    if action == "banned":
        channel_id = int(parts[1])
        page = int(parts[2])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await show_banned_management(query, channel_id, page)
        return

    if action == "unban":
        channel_id = int(parts[1])
        target_id = int(parts[2])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await do_unban(query, context, channel_id, target_id)
        return

    # ===== تصدير قائمة المحظورين (قسم 7) =====
    if action == "export_banned":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await export_banned_list(query, context, channel_id)
        return

    # ===== القائمة المحصّنة =====
    if action == "wl":
        channel_id = int(parts[1])
        page = int(parts[2])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        await show_whitelist_management(query, context, channel_id, page)
        return

    if action == "wlremove":
        channel_id = int(parts[1])
        target_id = int(parts[2])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        db.remove_from_whitelist(channel_id, target_id)
        await query.answer("تم الحذف من القائمة المحصّنة ✅", show_alert=True)
        await show_whitelist_management(query, context, channel_id, 0)
        return

    if action == "wladd":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        _clear_awaiting_flags(context)
        context.user_data["_awaiting_wladd_for"] = channel_id
        await context.bot.send_message(
            user_id,
            "✏️ أرسل الآن الـ *USER ID* (رقمي) للشخص الذي تريد إضافته للقائمة المحصّنة:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ===== صورة الترحيب الخاصة بقناة =====
    if action == "setphoto":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        _clear_awaiting_flags(context)
        context.user_data["_awaiting_welcome_photo_for"] = channel_id
        await context.bot.send_message(user_id, "📸 أرسل الآن الصورة التي تريد تعيينها كصورة ترحيب لهذه القناة:")
        return

    # ===== صورة الترحيب الافتراضية (سوبر أونر فقط) =====
    if action == "setdefphoto":
        if not is_super_owner(user_id):
            await query.answer("غير مصرح.", show_alert=True)
            return
        _clear_awaiting_flags(context)
        context.user_data["_awaiting_default_welcome_photo"] = True
        await context.bot.send_message(user_id, "📸 أرسل الآن الصورة الافتراضية لكل القنوات التي لم تحدد صورة خاصة بها:")
        return

    # ===== إلغاء ربط قناة =====
    if action == "unlink":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        ch = db.get_channel(channel_id)
        name = escape_md(channel_display_name(ch))
        keyboard = [[
            InlineKeyboardButton("✅ نعم، إلغاء الربط", callback_data=build_cb("unlinkconfirm", channel_id)),
            InlineKeyboardButton("❌ لا، رجوع", callback_data=build_cb("chmenu", channel_id)),
        ]]
        await query.edit_message_text(
            f"⚠️ هل أنت متأكد من إلغاء ربط قناة *{name}*؟\n\n"
            "لن يتم حذف أي بيانات مخزّنة، ويمكنك إعادة ربطها لاحقاً\\.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if action == "unlinkconfirm":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        db.deactivate_channel(channel_id)
        await query.answer("تم إلغاء ربط القناة ✅", show_alert=True)
        await query.edit_message_text(
            "🛡️ *لوحة تحكم حارس القنوات*\n\nاختار من الأزرار تحت:",
            reply_markup=build_main_menu_keyboard(user_id),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ===== إيقاف إشعارات الحظر لقناة معينة (من زر تحت إشعار الحظر نفسه) =====
    if action == "bannotif_off":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        db.set_ban_notifications(channel_id, False)
        await query.answer("تم إيقاف إشعارات الحظر لهذه القناة ✅", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    # ===== إعادة تفعيل الحظر التلقائي يدوياً بعد إيقاف بسبب تجاوز معدل (قسم 8) =====
    if action == "ratelimit_clear":
        channel_id = int(parts[1])
        if not can_manage_channel(user_id, channel_id):
            await query.answer("غير مصرح لك بإدارة هذه القناة.", show_alert=True)
            return
        clear_rate_limit(channel_id)
        await query.answer("تم تفعيل الحظر التلقائي مرة أخرى ✅", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    # ===== لوحة المطوّر الحصرية (قسم 9) =====
    if action == "devpanel":
        await show_dev_panel(query, user_id)
        return

    if action == "botwidestats":
        await show_bot_wide_stats(query, user_id)
        return

    if action == "broadcast_start":
        await start_broadcast_flow(query, context, user_id)
        return

    if action == "broadcast_confirm":
        await confirm_and_send_broadcast(query, context, user_id)
        return

    if action == "globalsearch_start":
        await start_global_search_flow(query, context, user_id)
        return

    if action == "devban":
        channel_id = int(parts[1])
        target_id = int(parts[2])
        if not is_super_owner(user_id):
            await query.answer("غير مصرح.", show_alert=True)
            return
        await do_dev_ban(query, context, channel_id, target_id)
        return

    if action == "localsearch_start":
        channel_id = int(parts[1])
        await start_local_search_flow(query, context, user_id, channel_id)
        return


# ---------------------------------------------------------------------------
# عرض قائمة القنوات (مع pagination)
# ---------------------------------------------------------------------------

async def show_channels_list(query, context, scope: str, page: int, user_id: int):
    if scope == "all" and not is_super_owner(user_id):
        await query.answer("غير مصرح.", show_alert=True)
        return

    if scope == "all":
        channels = db.get_all_active_channels()
        title = "🗂️ كل القنوات المربوطة"
    else:
        channels = db.get_channels_owned_by(user_id) if not is_super_owner(user_id) else get_accessible_channels(user_id)
        title = "📡 قنواتي"

    if not channels:
        await query.edit_message_text(
            f"{title}\n\nلا توجد قنوات حتى الآن\\.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ ربط قناة جديدة", callback_data=build_cb("linkchannel"))],
                [InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("menu"))],
            ]),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    total = len(channels)
    start = page * CHANNELS_PER_PAGE
    end = start + CHANNELS_PER_PAGE
    page_items = channels[start:end]

    keyboard = []
    for ch in page_items:
        banned_count = db.count_banned_members(ch["channel_id"])
        flag = " ⏸️" if is_channel_rate_limited(ch["channel_id"]) else ""
        label = f"📡 {channel_display_name(ch)} — 🚫{banned_count}{flag}"
        keyboard.append([InlineKeyboardButton(label, callback_data=build_cb("chmenu", ch["channel_id"]))])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=build_cb("chlist", scope, page - 1)))
    if end < total:
        nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=build_cb("chlist", scope, page + 1)))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("menu"))])

    text = f"{title} ({total})\n\nصفحة {page + 1} من {((total - 1) // CHANNELS_PER_PAGE) + 1}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ---------------------------------------------------------------------------
# قائمة قناة محددة
# ---------------------------------------------------------------------------

async def open_channel_menu(query, context, channel_id: int):
    context.user_data["_active_channel_id"] = channel_id
    await render_channel_menu(query, channel_id)


async def render_channel_menu(query, channel_id: int):
    ch = db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("⚠️ القناة غير موجودة أو ألغيت.")
        return

    name = escape_md(channel_display_name(ch))
    text = f"📡 *{name}*\n\nاختر القسم الذي تريد إدارته:"

    if is_channel_rate_limited(channel_id):
        text += "\n\n⏸️ _الحظر التلقائي متوقف مؤقتاً \\(تجاوز معدل\\) — اضغط الإحصائيات لإعادة التفعيل\\._"

    keyboard = [
        [InlineKeyboardButton("📊 الإحصائيات العامة", callback_data=build_cb("stats", "general", channel_id))],
        [InlineKeyboardButton("📅 تقرير اليوم", callback_data=build_cb("stats", "today", channel_id))],
        [InlineKeyboardButton("📈 آخر 7 أيام", callback_data=build_cb("stats", "week", channel_id))],
        [InlineKeyboardButton("⏰ أعلى فترة انضمام", callback_data=build_cb("stats", "peak", channel_id))],
        [InlineKeyboardButton("🕒 آخر الأحداث", callback_data=build_cb("stats", "recent", channel_id))],
        [InlineKeyboardButton("🚫 إدارة المحظورين", callback_data=build_cb("banned", channel_id, 0))],
        [InlineKeyboardButton("📤 تصدير قائمة المحظورين", callback_data=build_cb("export_banned", channel_id))],
        [InlineKeyboardButton("✅ القائمة المحصّنة", callback_data=build_cb("wl", channel_id, 0))],
        [InlineKeyboardButton("🔎 بحث عن عضو في هذه القناة", callback_data=build_cb("localsearch_start", channel_id))],
        [InlineKeyboardButton("📸 تعيين صورة الترحيب", callback_data=build_cb("setphoto", channel_id))],
    ]
    if is_channel_rate_limited(channel_id):
        keyboard.append([InlineKeyboardButton("✅ إعادة تفعيل الحظر التلقائي", callback_data=build_cb("ratelimit_clear", channel_id))])
    keyboard.append([InlineKeyboardButton("🔕 إلغاء ربط القناة", callback_data=build_cb("unlink", channel_id))])
    keyboard.append([InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("menu"))])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)


def build_back_to_channel_button(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ رجوع لقائمة القناة", callback_data=build_cb("chmenu", channel_id))
    ]])


# ---------------------------------------------------------------------------
# الإحصائيات
# ---------------------------------------------------------------------------

async def show_stats(query, stat_type: str, channel_id: int):
    back = build_back_to_channel_button(channel_id)

    if stat_type == "general":
        s = db.get_general_stats(channel_id)
        text = (
            "📊 *الإحصائيات العامة*\n"
            "_(من لحظة ربط القناة بالبوت)_\n\n"
            f"👥 الأعضاء النشطين حالياً: *{s['active_count']}*\n"
            f"🚪 غادروا (غير محظورين): *{s['left_count']}*\n"
            f"⏳ تحت فترة المراقبة: *{s['watching_count']}*\n"
            f"🚫 محظورين: *{s['banned_count']}*\n"
            f"📋 إجمالي اللي مر عليهم البوت: *{s['total_tracked']}*"
        )
        await query.edit_message_text(text, reply_markup=back, parse_mode=ParseMode.MARKDOWN)
        return

    if stat_type == "today":
        s = db.get_today_join_leave_counts(channel_id)
        text = (
            "📅 *تقرير اليوم*\n\n"
            f"➕ انضموا اليوم: *{s['joins_today']}*\n"
            f"➖ غادروا اليوم: *{s['leaves_today']}*\n"
            f"🚫 اتحظروا اليوم: *{s['bans_today']}*"
        )
        await query.edit_message_text(text, reply_markup=back, parse_mode=ParseMode.MARKDOWN)
        return

    if stat_type == "week":
        rows = db.get_daily_breakdown(channel_id, days=7)
        if not rows:
            text = "📈 *آخر 7 أيام*\n\nلا توجد بيانات كافية لسه\\."
            await query.edit_message_text(text, reply_markup=back, parse_mode=ParseMode.MARKDOWN_V2)
            return
        lines = ["📈 *آخر 7 أيام*\n"]
        for r in rows:
            day_str = r["day"].strftime("%Y-%m-%d")
            lines.append(f"`{day_str}` | ➕{r['joins']} | ➖{r['leaves']} | 🚫{r['bans']}")
        await query.edit_message_text("\n".join(lines), reply_markup=back, parse_mode=ParseMode.MARKDOWN)
        return

    if stat_type == "peak":
        peak_hour = db.get_peak_join_hour(channel_id)
        peak_day = db.get_peak_join_day_of_week(channel_id)
        lines = ["⏰ *أعلى فترة انضمام*\n"]
        if peak_hour:
            h = int(peak_hour["hour"])
            lines.append(
                f"🕐 أكتر ساعة بينضم فيها أعضاء: *من {h:02d}:00 إلى {(h + 1) % 24:02d}:00* (UTC) "
                f"— بمعدل {peak_hour['joins_count']} انضمام."
            )
        else:
            lines.append("لا توجد بيانات انضمام كافية لسه.")
        if peak_day:
            lines.append(f"📆 أكتر يوم في الأسبوع: *{peak_day['day_name']}* ({peak_day['joins_count']} انضمام).")
        await query.edit_message_text("\n".join(lines), reply_markup=back, parse_mode=ParseMode.MARKDOWN)
        return

    if stat_type == "recent":
        events = db.get_recent_events(channel_id, limit=15)
        if not events:
            text = "🕒 *آخر الأحداث*\n\nلا توجد أحداث مسجلة لسه\\."
            await query.edit_message_text(text, reply_markup=back, parse_mode=ParseMode.MARKDOWN_V2)
            return
        icons = {"join": "➕", "leave": "➖", "ban": "🚫", "unban": "✅", "watch_start": "⏳"}
        lines = ["🕒 *آخر 15 حدث*\n"]
        for e in events:
            icon = icons.get(e["event_type"], "•")
            label = escape_md(format_user_label(e["username"], e["full_name"], e["user_id"]))
            extra = f" \\({escape_md(ban_reason_label(e['ban_reason']))}\\)" if e["event_type"] == "ban" and e["ban_reason"] else ""
            lines.append(f"{icon} {label}{extra} — `{fmt_dt(e['event_time'])}`")
        await query.edit_message_text("\n".join(lines), reply_markup=back, parse_mode=ParseMode.MARKDOWN_V2)
        return


# ---------------------------------------------------------------------------
# إدارة المحظورين (مع pagination) + تصدير (قسم 7)
# ---------------------------------------------------------------------------

async def show_banned_management(query, channel_id: int, page: int):
    banned = db.get_banned_members(channel_id)
    back = build_back_to_channel_button(channel_id)

    if not banned:
        await query.edit_message_text(
            "🚫 *إدارة المحظورين*\n\nمفيش حد محظور حالياً\\.",
            reply_markup=back,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    total = len(banned)
    start = page * BANNED_PER_PAGE
    end = start + BANNED_PER_PAGE
    page_items = banned[start:end]

    keyboard = []
    for m in page_items:
        label = format_user_label(m["username"], m["full_name"], m["user_id"])
        reason_short = ban_reason_label(m["ban_reason"])
        keyboard.append([
            InlineKeyboardButton(f"✅ فك حظر {label} ({reason_short})", callback_data=build_cb("unban", channel_id, m["user_id"]))
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=build_cb("banned", channel_id, page - 1)))
    if end < total:
        nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=build_cb("banned", channel_id, page + 1)))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("📤 تصدير القائمة كاملة", callback_data=build_cb("export_banned", channel_id))])
    keyboard.append([InlineKeyboardButton("⬅️ رجوع لقائمة القناة", callback_data=build_cb("chmenu", channel_id))])

    text = f"🚫 *إدارة المحظورين* ({total} محظور — صفحة {page + 1})\n\nاضغط لفك حظر أي حد:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


async def do_unban(query, context: ContextTypes.DEFAULT_TYPE, channel_id: int, target_id: int):
    try:
        await context.bot.unban_chat_member(chat_id=channel_id, user_id=target_id, only_if_banned=True)
        db.mark_member_unbanned(channel_id, target_id)
        await query.answer("تم فك الحظر ✅", show_alert=True)
    except TelegramError as e:
        logger.error(f"فشل فك حظر {target_id} من {channel_id}: {e}")
        await query.answer(f"فشل فك الحظر: {e}", show_alert=True)
    await show_banned_management(query, channel_id, 0)


async def export_banned_list(query, context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """
    قسم 7: يولّد ملف CSV فيه (تاريخ الحظر، username، user_id، السبب) ويبعته
    كملف لمالك القناة (نفس الشخص اللي ضغط الزرار، بما إن الصلاحية اتفحصت أصلاً).
    """
    rows = db.get_banned_export_rows(channel_id)
    ch = db.get_channel(channel_id)
    chname = channel_display_name(ch)

    if not rows:
        await query.answer("لا يوجد محظورين لتصديرهم.", show_alert=True)
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "ban_date_utc", "ban_reason"])
    for r in rows:
        writer.writerow([
            r["user_id"],
            r["username"] or "",
            fmt_dt(r["ban_date"]),
            ban_reason_label(r["ban_reason"]),
        ])
    buf.seek(0)
    data_bytes = buf.getvalue().encode("utf-8-sig")  # BOM عشان Excel يفتح العربي صح

    file_name = f"banned_{channel_id}.csv"
    await context.bot.send_document(
        chat_id=query.from_user.id,
        document=InputFile(io.BytesIO(data_bytes), filename=file_name),
        caption=f"📤 قائمة المحظورين المصدّرة — {chname} ({len(rows)} محظور)",
    )
    await query.answer("تم إرسال الملف ✅", show_alert=True)


# ---------------------------------------------------------------------------
# إدارة القائمة المحصّنة (Whitelist) — مع pagination وزرار إضافة مباشر
# ---------------------------------------------------------------------------

async def show_whitelist_management(query, context, channel_id: int, page: int):
    context.user_data["_active_channel_id"] = channel_id

    wl = db.get_whitelist(channel_id)
    total = len(wl)

    lines = [
        "✅ *القائمة المحصّنة من الحظر*",
        "_الأدمنز محصّنون تلقائياً — هذه القائمة لغيرهم\\._\n",
    ]

    keyboard = []
    if not wl:
        lines.append("القائمة فارغة حالياً\\.")
    else:
        start = page * WHITELIST_PER_PAGE
        end = start + WHITELIST_PER_PAGE
        page_items = wl[start:end]
        lines.append(f"({total} شخص — صفحة {page + 1}) — اضغط لحذف:")

        for w in page_items:
            label = format_user_label(w["username"], None, w["user_id"])
            keyboard.append([
                InlineKeyboardButton(f"❌ {label}", callback_data=build_cb("wlremove", channel_id, w["user_id"]))
            ])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=build_cb("wl", channel_id, page - 1)))
        if end < total:
            nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=build_cb("wl", channel_id, page + 1)))
        if nav_row:
            keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("➕ إضافة عضو للقائمة", callback_data=build_cb("wladd", channel_id))])
    keyboard.append([InlineKeyboardButton("⬅️ رجوع لقائمة القناة", callback_data=build_cb("chmenu", channel_id))])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# استقبال الرسائل الخاصة (نصوص وصور) — للحالات اللي مستنية إدخال من المستخدم
# ---------------------------------------------------------------------------
#
# ✅ كل دالة بتتفحص أولاً هل في "حالة انتظار" خاصة بيها مسجلة في user_data،
# وبترجع فوراً (مش return بعد processing) لو مفيش، عشان الرسائل العادية
# (زي أوامر /whitelist_add و/ban_id) تكمل لباقي الـ handlers من غير تعارض.

async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلتقط الرسائل النصية في الخاص أثناء انتظار إدخال (whitelist، بحث المطوّر، بث)."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if "_awaiting_wladd_for" in context.user_data:
        channel_id = context.user_data.pop("_awaiting_wladd_for")
        if not can_manage_channel(user_id, channel_id):
            await update.message.reply_text("غير مصرح لك بإدارة هذه القناة.")
            return
        if not text.isdigit():
            await update.message.reply_text("⚠️ الرقم غير صالح. أرسل USER ID رقمي صحيح فقط.")
            context.user_data["_awaiting_wladd_for"] = channel_id
            return
        target_id = int(text)
        db.add_to_whitelist(channel_id, target_id)
        await update.message.reply_text(f"✅ تمت إضافة `{target_id}` للقائمة المحصّنة.", parse_mode=ParseMode.MARKDOWN)
        return

    if "_awaiting_local_search_for" in context.user_data:
        channel_id = context.user_data.pop("_awaiting_local_search_for")
        await receive_local_search_query(update, context, channel_id)
        return

    if context.user_data.get("_awaiting_global_search"):
        await receive_global_search_query(update, context)
        return

    if context.user_data.get("_awaiting_broadcast_content"):
        await receive_broadcast_content(update, context)
        return


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلتقط الصور في الخاص أثناء انتظار صورة ترحيب (خاصة بقناة/افتراضية) أو محتوى بث."""
    user_id = update.effective_user.id

    if "_awaiting_welcome_photo_for" in context.user_data:
        channel_id = context.user_data.pop("_awaiting_welcome_photo_for")
        if not can_manage_channel(user_id, channel_id):
            await update.message.reply_text("غير مصرح لك بإدارة هذه القناة.")
            return
        file_id = update.message.photo[-1].file_id
        db.set_welcome_photo(channel_id, file_id)
        ch = db.get_channel(channel_id)
        name = channel_display_name(ch)
        await update.message.reply_text(f"✅ تم تعيين صورة الترحيب لقناة *{escape_md(name)}* بنجاح\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if context.user_data.pop("_awaiting_default_welcome_photo", False):
        if not is_super_owner(user_id):
            await update.message.reply_text("غير مصرح.")
            return
        file_id = update.message.photo[-1].file_id
        db.set_setting(DEFAULT_WELCOME_PHOTO_KEY, file_id)
        await update.message.reply_text("✅ تم تعيين صورة الترحيب الافتراضية لكل القنوات الجديدة.")
        return

    # صورة (مع كابشن اختياري) أثناء انتظار محتوى بث جماعي — نعالجها بنفس دالة النص
    # لأن receive_broadcast_content بتدعم الحالتين (نص بس، أو نص+صورة) من خلال caption.
    if context.user_data.get("_awaiting_broadcast_content"):
        await receive_broadcast_content(update, context)
        return


# ---------------------------------------------------------------------------
# الأوامر النصية (تعمل على آخر قناة فُتحت من اللوحة — context.user_data["_active_channel_id"])
# ---------------------------------------------------------------------------

def _get_active_channel_or_warn(context, user_id: int):
    channel_id = context.user_data.get("_active_channel_id")
    if channel_id is None:
        return None, "⚠️ افتح قناة من اللوحة أولاً (عبر /start) قبل استخدام هذا الأمر."
    if not can_manage_channel(user_id, channel_id):
        return None, "غير مصرح لك بإدارة هذه القناة."
    return channel_id, None


async def cmd_whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whitelist_add USER_ID — يضيف العضو لآخر قناة مفتوحة من اللوحة."""
    user_id = update.effective_user.id
    channel_id, error = _get_active_channel_or_warn(context, user_id)
    if error:
        await update.message.reply_text(error)
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستخدام: `/whitelist_add USER_ID`", parse_mode=ParseMode.MARKDOWN)
        return

    target_id = int(context.args[0])
    db.add_to_whitelist(channel_id, target_id)
    await update.message.reply_text(f"✅ تمت إضافة `{target_id}` للقائمة المحصّنة.", parse_mode=ParseMode.MARKDOWN)


async def cmd_ban_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ban_id USER_ID — حظر يدوي مباشر في آخر قناة مفتوحة من اللوحة (لا يدخل في rate limit التلقائي)."""
    user_id = update.effective_user.id
    channel_id, error = _get_active_channel_or_warn(context, user_id)
    if error:
        await update.message.reply_text(error)
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستخدام: `/ban_id USER_ID`", parse_mode=ParseMode.MARKDOWN)
        return

    target_id = int(context.args[0])
    ok = await execute_ban(context, channel_id, target_id, None, None, reason=db.BAN_REASON_MANUAL, is_automatic=False)
    if ok:
        await update.message.reply_text(f"✅ تم حظر `{target_id}`.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("فشل الحظر — راجع السجلات.")


# ---------------------------------------------------------------------------
# Error Handler عام — يمنع توقف البوت بسبب استثناء غير متوقع في أي handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logger.error(
            "تعارض (Conflict): فيه أكتر من نسخة شغالة بنفس BOT_TOKEN في نفس الوقت "
            "(تأكد إن عدد الـ replicas في إعدادات Railway = 1 بالظبط، ومفيش نسخة تانية "
            "للبوت شغالة محلياً). التفاصيل: %s", context.error,
        )
        return
    logger.error("استثناء غير متوقع أثناء معالجة Update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# تشغيل البوت
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("لازم تضيف متغير بيئة BOT_TOKEN (توكن البوت من BotFather).")

    if OWNER_ID is None:
        raise RuntimeError("لازم تضيف متغير بيئة OWNER_ID برقم صحيح (الآيدي بتاعك على تيليجرام).")

    if not db.DATABASE_URL:
        raise RuntimeError("لازم تضيف متغير بيئة DATABASE_URL (رابط قاعدة بيانات PostgreSQL).")

    db.init_db()

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    # تتبع حالة البوت نفسه (ترقية لأدمن / تنزيل / طرد) — يجب تسجيله أولاً
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # تتبع أعضاء القنوات المربوطة (دخول/خروج) — العمود الفقري لمنطق الحظر
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # استقبال اختيار القناة من زرار KeyboardButtonRequestChat
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_chat_shared))

    # استقبال صور الترحيب/البث (في الخاص فقط) — لازم يتسجل قبل أي فلتر نصي عام
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_private_photo))

    # استقبال نصوص الإدخال (whitelist، بحث المطوّر، بث) في الخاص — بعد استبعاد الأوامر
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_private_text,
    ))

    # الأوامر
    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("link", cmd_link_channel))
    app.add_handler(CommandHandler("whitelist_add", cmd_whitelist_add))
    app.add_handler(CommandHandler("ban_id", cmd_ban_id))

    # الأزرار (Inline)
    app.add_handler(CallbackQueryHandler(handle_button))

    # Error handler عام
    app.add_error_handler(error_handler)

    # --- قسم 4: Job مجدولة لفحص فترات المراقبة المنتهية كل ساعة ---
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            check_expired_watch_periods_job,
            interval=WATCH_CHECK_INTERVAL_SECONDS,
            first=60,  # أول فحص بعد دقيقة من الإقلاع، مش فوراً، عشان البوت يكمل تهيئته الأول
            name="check_expired_watch_periods",
        )
    else:
        logger.error(
            "JobQueue غير متاحة! نظام فترة المراقبة (قسم 4) لن يعمل. "
            "تأكد من تثبيت 'python-telegram-bot[job-queue]' في requirements.txt."
        )

    logger.info("البوت بدأ التشغيل...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
