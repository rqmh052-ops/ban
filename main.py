# -*- coding: utf-8 -*-
"""
main.py — بوت حراسة قنوات متعددة (Multi-Channel Guard Bot)

- ربط أي عدد من القنوات عن طريق KeyboardButtonRequestChat (اختيار من قائمة قنواتك).
- حظر فوري لأي عضو يغادر القناة (إلا الأدمنز والقائمة المحصّنة).
- عزل تام بين القنوات: كل قناة لها أعضاؤها وسجلها وقائمتها المحصّنة الخاصة.
- نظام صلاحيات: OWNER_ID (سوبر أدمن عالمي) + مالك كل قناة (من ضافها).
- صورة ترحيب قابلة للتغيير لكل قناة من لوحة الإدارة.
"""

import os
import logging
import re

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ChatAdministratorRights,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
from telegram.error import TelegramError

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

# OWNER_ID = السوبر أدمن العالمي الوحيد (صاحب البوت) — له صلاحية على كل القنوات.
# لازم يكون رقم صحيح. لو غير موجود/غير صالح، هيتحول لـ None ويتفحص بدقة في main().
_owner_id_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(_owner_id_raw) if _owner_id_raw.isdigit() else None

# الحالات اللي بنعتبرها "عضو نشط فعليا" داخل القناة
ACTIVE_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
# الحالة اللي بنعتبرها "غادر بنفسه"
LEFT_STATUSES = {ChatMemberStatus.LEFT}
# الحالة اللي معناها "اتطرد/اتحظر بالفعل بمعرفة حد تاني"
KICKED_STATUSES = {ChatMemberStatus.KICKED}

# عدد العناصر في كل صفحة للقوائم المختلفة (pagination حقيقي)
CHANNELS_PER_PAGE = 8
BANNED_PER_PAGE = 10
WHITELIST_PER_PAGE = 10

# مفتاح الإعداد العام لصورة الترحيب الافتراضية (لو قناة معينة لسه محددتش صورتها)
DEFAULT_WELCOME_PHOTO_KEY = "default_welcome_photo_file_id"

# مفتاح request_id الثابت المستخدم في زرار KeyboardButtonRequestChat
LINK_CHANNEL_REQUEST_ID = 1001


# ---------------------------------------------------------------------------
# دوال الصلاحيات
# ---------------------------------------------------------------------------

def is_super_owner(user_id: int) -> bool:
    """هل ده الأدمن الأعلى (صاحب البوت)؟ له صلاحية على كل القنوات."""
    if OWNER_ID is None:
        return False
    return user_id == OWNER_ID


def can_manage_channel(user_id: int, channel_id: int) -> bool:
    """
    هل المستخدم ده يقدر يدير القناة دي من اللوحة؟
    - السوبر أونر: يقدر يدير أي قناة.
    - مالك القناة (اللي ضافها): يقدر يدير قناته بس.
    - أي حد تاني (حتى لو أدمن في تيليجرام نفسها): لأ.
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

# الحروف الخاصة في MarkdownV2 اللي لازم نعملها escape
_MDV2_SPECIAL = r'([_*\[\]()~`>#+\-=|{}.!\\])'


def escape_md(text) -> str:
    """Escape آمن لأي نص قبل ما يتحط في رسالة MarkdownV2."""
    if text is None:
        return ""
    return re.sub(_MDV2_SPECIAL, r'\\\1', str(text))


def format_user_label(username, full_name, user_id) -> str:
    """تنسيق اسم العضو للعرض — بدون أي markdown، الـ escape بيحصل وقت الإدراج في النص النهائي."""
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
    """اسم القناة للعرض — العنوان لو موجود، وإلا الـ ID."""
    if channel_row and channel_row.get("title"):
        return channel_row["title"]
    return str(channel_row["channel_id"]) if channel_row else "—"


# ---------------------------------------------------------------------------
# دوال بناء callback_data بصيغة موحدة بفاصل واضح "|"
# ---------------------------------------------------------------------------
#
# ✅ مهم: نستخدم "|" كفاصل صريح بين الأجزاء (مش "_") لتجنب أي لبس عند الفصل،
# لأن أسماء الأفعال أو القيم ممكن تحتوي "_" بداخلها أصلاً.
# الصيغة العامة: action|param1|param2|...
# مثال: unban|-1001234567890|987654321

def build_cb(action: str, *parts) -> str:
    """بناء callback_data بصيغة موحدة. كل القيم لازم تتحول لـ str أولاً."""
    segments = [action] + [str(p) for p in parts]
    data = "|".join(segments)
    if len(data.encode("utf-8")) > 64:
        # حماية وقت التطوير — تليجرام بيرفض أي callback_data أطول من 64 بايت
        logger.warning(f"callback_data تجاوز 64 بايت: {data}")
    return data


def parse_cb(data: str):
    """فصل callback_data لقائمة أجزاء. الجزء الأول دايماً اسم الفعل (action)."""
    return data.split("|")


# ---------------------------------------------------------------------------
# المعالج الأساسي: تتبع دخول وخروج الأعضاء + الحظر الفوري
# ---------------------------------------------------------------------------

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    بيتنفذ مع كل تحديث في حالة عضوية أي شخص داخل أي قناة البوت موجود فيها.
    هنا قلب منطق الحظر الفوري.
    """
    result = update.chat_member
    if result is None:
        return

    channel_id = result.chat.id

    # تجاهل أي قناة مش مسجلة/مفعّلة في البوت (حماية من تفاعل خارج النطاق)
    if not db.is_channel_active(channel_id):
        return

    user = result.new_chat_member.user
    # ✅ الإصلاح الحرج الأول: نستخدم old_status الجاهز في الـ update نفسه
    # بدل عمل استعلام جديد لتيليجرام بعد المغادرة (اللي بيرجّع نتيجة غير موثوقة
    # لأن تيليجرام بينزّل صلاحيات الأدمن فوراً وقت المغادرة).
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    user_id = user.id
    username = user.username
    full_name = (f"{user.first_name or ''} {user.last_name or ''}").strip()

    # حالة 1: انضمام جديد فعلي
    if old_status not in ACTIVE_STATUSES and new_status in ACTIVE_STATUSES:
        db.upsert_member_join(channel_id, user_id, username, full_name)
        logger.info(f"[{channel_id}] انضمام جديد: {format_user_label(username, full_name, user_id)}")
        return

    # حالة 2: عضو غادر بنفسه (member/admin/owner -> left)
    if old_status in ACTIVE_STATUSES and new_status in LEFT_STATUSES:
        db.mark_member_left(channel_id, user_id)
        logger.info(f"[{channel_id}] مغادرة: {format_user_label(username, full_name, user_id)}")

        # --- منطق الحظر الفوري ---

        # 1) لو كان أدمن/مالك قبل المغادرة مباشرة (من old_status نفسه) -> تجاهل
        #    ✅ هذا هو الإصلاح: نعتمد على الحالة "قبل" المغادرة المؤكدة من الـ update،
        #    مش على استعلام "بعد" المغادرة اللي ممكن يرجع نتيجة غلط.
        if old_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            logger.info(f"[{channel_id}] تم تجاهل حظر {user_id} — كان أدمن/مالك قبل المغادرة مباشرة.")
            return

        # 2) لو موجود في القائمة المحصّنة لهذه القناة -> تجاهل
        if db.is_whitelisted(channel_id, user_id):
            logger.info(f"[{channel_id}] تم تجاهل حظر {user_id} — في القائمة المحصّنة.")
            return

        # 3) غير كده -> حظر فوري ونهائي (بدون تفرقة بين سبب الحظر، حسب الطلب)
        try:
            await context.bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            db.mark_member_banned(channel_id, user_id)
            logger.info(f"[{channel_id}] تم حظر {format_user_label(username, full_name, user_id)} فورا بعد المغادرة.")
        except TelegramError as e:
            logger.error(f"[{channel_id}] فشل حظر {user_id}: {e}")
        return

    # حالة 3: عضو اتطرد بالفعل (kicked) بمعرفة حد تاني أو البوت نفسه — بنسجل بس
    if new_status in KICKED_STATUSES and old_status not in KICKED_STATUSES:
        db.mark_member_banned(channel_id, user_id)
        logger.info(f"[{channel_id}] تسجيل حظر/طرد: {format_user_label(username, full_name, user_id)}")
        return


# ---------------------------------------------------------------------------
# تتبع حالة البوت نفسه (my_chat_member) — اكتشاف الترقية لأدمن في قناة جديدة
# ---------------------------------------------------------------------------

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    بيتنفذ لما تتغير صلاحيات البوت نفسه في أي شات.
    هنا بنكتشف إن البوت بقى أدمن في قناة جديدة، أو اتنزل/اتطرد من قناة.
    """
    result = update.my_chat_member
    if result is None:
        return

    chat = result.chat
    if chat.type != ChatType.CHANNEL:
        return  # القنوات فقط — مش جروبات ولا سوبر جروبات

    new_status = result.new_chat_member.status
    channel_id = chat.id

    # البوت بقى أدمن في القناة دي
    if new_status == ChatMemberStatus.ADMINISTRATOR:
        # نجيب الـ user_id بتاع اللي رفّع البوت من context.user_data المؤقت
        # (اتسجل وقت ما هو دوس على القناة في زرار KeyboardButtonRequestChat)
        owner_id = context.bot_data.pop(f"_linking_user_{channel_id}", None)
        if owner_id is None:
            # حماية احتياطية: لو لسبب ما الـ owner_id مش متسجل (مثلاً البوت اتضاف يدوياً
            # من إعدادات تيليجرام مباشرة بدل زرار اللوحة)، نخلي السوبر أونر هو المالك المؤقت.
            owner_id = OWNER_ID

        db.upsert_channel(channel_id, owner_id, chat.title)
        logger.info(f"تم ربط قناة جديدة: {channel_id} ({chat.title}) — المالك: {owner_id}")

        await send_welcome_to_channel(context, channel_id)
        await send_link_notifications(context, channel_id, owner_id, chat.title)

    # البوت اتنزل من أدمن أو اتطرد أو سيب القناة
    elif new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        db.deactivate_channel(channel_id)
        logger.info(f"تم إلغاء تفعيل قناة: {channel_id} (البوت لم يعد أدمن)")


async def send_welcome_to_channel(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """رسالة ترحيب داخل القناة نفسها + صورة (لو محددة لهذه القناة أو صورة افتراضية)."""
    ch = db.get_channel(channel_id)
    photo_id = (ch["welcome_photo_file_id"] if ch else None) or db.get_setting(DEFAULT_WELCOME_PHOTO_KEY)

    caption = (
        "🛡️ *تم تفعيل نظام الحماية على هذه القناة\\.*\n\n"
        "⚠️ أي عضو يغادر القناة سيتم حظره فوراً بشكل تلقائي\\."
    )

    try:
        if photo_id:
            await context.bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await context.bot.send_message(
                chat_id=channel_id,
                text=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except TelegramError as e:
        logger.warning(f"فشل إرسال رسالة الترحيب في القناة {channel_id}: {e}")


async def send_link_notifications(context: ContextTypes.DEFAULT_TYPE, channel_id: int, owner_id: int, title: str):
    """
    إشعارين إضافيين عند ربط قناة جديدة (بجانب رسالة الترحيب جوه القناة):
    1) رسالة خاص لمالك القناة (تأكيد التفعيل).
    2) رسالة خاص للسوبر أونر (إلا لو هو نفسه اللي ضاف القناة، فمفيش داعي يتكرر الإشعار).
    """
    safe_title = escape_md(title or str(channel_id))

    # إشعار المالك
    try:
        await context.bot.send_message(
            owner_id,
            f"✅ تم تفعيل البوت بنجاح على قناة *{safe_title}*\\!\n\n"
            "أي عضو يغادر القناة من الآن سيتم حظره تلقائياً\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramError as e:
        logger.warning(f"فشل إشعار المالك {owner_id}: {e}")

    # إشعار السوبر أونر (لو موجود ومختلف عن المالك نفسه)
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
    """
    يبني الزرار اللي بيفتح للمستخدم قائمة قنواته (هو أدمن فيها بصلاحية حظر بالفعل)،
    ويرفّع البوت أدمن فيها تلقائياً (بعد تأكيده) بصلاحية can_restrict_members.
    """
    required_rights = ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=True,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
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
    """
    يتنفذ فور ما المستخدم يختار قناة من الكيبورد ويأكد ترقية البوت.
    تيليجرام بترسل هذه الرسالة فور التأكيد — لكن التسجيل الفعلي للقناة في قاعدة
    البيانات بيحصل في handle_my_chat_member (لما البوت فعلياً يستلم صلاحيات الأدمن).
    دور هذه الدالة هنا هو فقط: تسجيل مين هو الشخص اللي طلب الربط، عشان
    handle_my_chat_member يعرف ينسبه له كمالك، وإرسال رسالة تأكيد فورية للمستخدم.
    """
    msg = update.message
    if not msg or not msg.chat_shared:
        return

    if msg.chat_shared.request_id != LINK_CHANNEL_REQUEST_ID:
        return  # طلب من نوع تاني (احتياطي لو اتضافت ميزات تستخدم request_id مختلف مستقبلاً)

    requested_channel_id = msg.chat_shared.chat_id
    requesting_user_id = msg.from_user.id

    # نسجل مين طلب الربط — هيتقرأ في handle_my_chat_member لحظة ما البوت يترقّى فعلياً
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

    if not channels:
        # مستخدم لسه ملوش أي قناة مربوطة — نوريه بس زرار الربط
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
    """القائمة الرئيسية تختلف حسب نوع المستخدم (سوبر أونر أو مالك قنوات عادي)."""
    keyboard = []
    channels = get_accessible_channels(user_id)

    if len(channels) == 1:
        # قناة واحدة بس -> اختصار مباشر لقائمة القناة
        ch = channels[0]
        keyboard.append([
            InlineKeyboardButton(f"📡 {channel_display_name(ch)}", callback_data=build_cb("chmenu", ch["channel_id"]))
        ])
    elif len(channels) > 1:
        keyboard.append([
            InlineKeyboardButton(f"📡 قنواتي ({len(channels)})", callback_data=build_cb("chlist", "mine", 0))
        ])

    if is_super_owner(user_id):
        all_count = len(db.get_all_active_channels())
        keyboard.append([
            InlineKeyboardButton(f"🗂️ كل القنوات ({all_count})", callback_data=build_cb("chlist", "all", 0))
        ])
        keyboard.append([
            InlineKeyboardButton("🖼️ صورة الترحيب الافتراضية", callback_data=build_cb("setdefphoto"))
        ])

    keyboard.append([
        InlineKeyboardButton("➕ ربط قناة جديدة", callback_data=build_cb("linkchannel"))
    ])

    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# معالج ضغطات الأزرار — Dispatcher مركزي واحد
# ---------------------------------------------------------------------------
#
# ✅ كل القيم بتتفصل بـ "|" (build_cb/parse_cb) — مفيش اعتماد على ترتيب "_" أو
# تخمين الموضع، عشان channel_id (سالب دايماً مثل -1001234567890) وuser_id
# ميتلخبطوش مع بعض أو مع أرقام صفحات الـ pagination.

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
        scope = parts[1]          # "mine" أو "all"
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
        stat_type = parts[1]      # general/today/week/peak/recent
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
        context.user_data["_awaiting_welcome_photo_for"] = channel_id
        await context.bot.send_message(
            user_id,
            "📸 أرسل الآن الصورة التي تريد تعيينها كصورة ترحيب لهذه القناة:",
        )
        return

    # ===== صورة الترحيب الافتراضية (سوبر أونر فقط) =====
    if action == "setdefphoto":
        if not is_super_owner(user_id):
            await query.answer("غير مصرح.", show_alert=True)
            return
        context.user_data["_awaiting_default_welcome_photo"] = True
        await context.bot.send_message(
            user_id,
            "📸 أرسل الآن الصورة الافتراضية لكل القنوات التي لم تحدد صورة خاصة بها:",
        )
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
        channels = db.get_channels_owned_by(user_id)
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
        label = f"📡 {channel_display_name(ch)} — 🚫{banned_count}"
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
    """
    يفتح لوحة إدارة قناة محددة، ويسجل آخر قناة مفتوحة في user_data —
    عشان أوامر /whitelist_add و /ban_id النصية تعرف تتعامل مع القناة الصحيحة
    من غير ما تحتاج المستخدم يكتب channel_id يدوياً كل مرة.
    """
    context.user_data["_active_channel_id"] = channel_id
    await render_channel_menu(query, channel_id)


async def render_channel_menu(query, channel_id: int):
    ch = db.get_channel(channel_id)
    if not ch:
        await query.edit_message_text("⚠️ القناة غير موجودة أو ألغيت.")
        return

    name = escape_md(channel_display_name(ch))
    text = f"📡 *{name}*\n\nاختر القسم الذي تريد إدارته:"

    keyboard = [
        [InlineKeyboardButton("📊 الإحصائيات العامة", callback_data=build_cb("stats", "general", channel_id))],
        [InlineKeyboardButton("📅 تقرير اليوم", callback_data=build_cb("stats", "today", channel_id))],
        [InlineKeyboardButton("📈 آخر 7 أيام", callback_data=build_cb("stats", "week", channel_id))],
        [InlineKeyboardButton("⏰ أعلى فترة انضمام", callback_data=build_cb("stats", "peak", channel_id))],
        [InlineKeyboardButton("🕒 آخر الأحداث", callback_data=build_cb("stats", "recent", channel_id))],
        [InlineKeyboardButton("🚫 إدارة المحظورين", callback_data=build_cb("banned", channel_id, 0))],
        [InlineKeyboardButton("✅ القائمة المحصّنة", callback_data=build_cb("wl", channel_id, 0))],
        [InlineKeyboardButton("📸 تعيين صورة الترحيب", callback_data=build_cb("setphoto", channel_id))],
        [InlineKeyboardButton("🔕 إلغاء ربط القناة", callback_data=build_cb("unlink", channel_id))],
        [InlineKeyboardButton("⬅️ رجوع", callback_data=build_cb("menu"))],
    ]
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
        icons = {"join": "➕", "leave": "➖", "ban": "🚫", "unban": "✅"}
        lines = ["🕒 *آخر 15 حدث*\n"]
        for e in events:
            icon = icons.get(e["event_type"], "•")
            label = escape_md(format_user_label(e["username"], e["full_name"], e["user_id"]))
            lines.append(f"{icon} {label} — `{fmt_dt(e['event_time'])}`")
        await query.edit_message_text("\n".join(lines), reply_markup=back, parse_mode=ParseMode.MARKDOWN_V2)
        return


# ---------------------------------------------------------------------------
# إدارة المحظورين (مع pagination)
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
        keyboard.append([
            InlineKeyboardButton(f"✅ فك حظر {label}", callback_data=build_cb("unban", channel_id, m["user_id"]))
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ السابق", callback_data=build_cb("banned", channel_id, page - 1)))
    if end < total:
        nav_row.append(InlineKeyboardButton("التالي ▶️", callback_data=build_cb("banned", channel_id, page + 1)))
    if nav_row:
        keyboard.append(nav_row)

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


# ---------------------------------------------------------------------------
# إدارة القائمة المحصّنة (Whitelist) — مع pagination وزرار إضافة مباشر
# ---------------------------------------------------------------------------

async def show_whitelist_management(query, context, channel_id: int, page: int):
    # نسجل آخر قناة مفتوحة هنا كمان عشان لو المستخدم دخل مباشرة من رابط قديم
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
# ✅ نستخدم context.user_data بشكل موحّد لكل حالات "الانتظار" (مش chat_data)
# لأن البيانات دي خاصة بالمستخدم نفسه، وكل دوال العرض اللي محتاجة تتذكر
# "آخر قناة مفتوحة" بتاخد context كباراميتر صريح بدل أي حل ملتوي.

async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلتقط الرسائل النصية في الخاص أثناء انتظار إدخال (USER ID للـ whitelist مثلاً)."""
    text = (update.message.text or "").strip()

    if "_awaiting_wladd_for" in context.user_data:
        channel_id = context.user_data.pop("_awaiting_wladd_for")
        if not can_manage_channel(update.effective_user.id, channel_id):
            await update.message.reply_text("غير مصرح لك بإدارة هذه القناة.")
            return
        if not text.isdigit():
            await update.message.reply_text("⚠️ الرقم غير صالح. أرسل USER ID رقمي صحيح فقط.")
            context.user_data["_awaiting_wladd_for"] = channel_id  # نرجّعها عشان يحاول تاني
            return
        target_id = int(text)
        db.add_to_whitelist(channel_id, target_id)
        await update.message.reply_text(f"✅ تمت إضافة `{target_id}` للقائمة المحصّنة.", parse_mode=ParseMode.MARKDOWN)
        return


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يلتقط الصور في الخاص أثناء انتظار صورة ترحيب (خاصة بقناة أو افتراضية)."""
    photo = update.message.photo[-1]  # أعلى جودة متاحة
    file_id = photo.file_id

    if "_awaiting_welcome_photo_for" in context.user_data:
        channel_id = context.user_data.pop("_awaiting_welcome_photo_for")
        if not can_manage_channel(update.effective_user.id, channel_id):
            await update.message.reply_text("غير مصرح لك بإدارة هذه القناة.")
            return
        db.set_welcome_photo(channel_id, file_id)
        ch = db.get_channel(channel_id)
        name = channel_display_name(ch)
        await update.message.reply_text(f"✅ تم تعيين صورة الترحيب لقناة *{name}* بنجاح\\!", parse_mode=ParseMode.MARKDOWN_V2)
        return

    if context.user_data.pop("_awaiting_default_welcome_photo", False):
        if not is_super_owner(update.effective_user.id):
            await update.message.reply_text("غير مصرح.")
            return
        db.set_setting(DEFAULT_WELCOME_PHOTO_KEY, file_id)
        await update.message.reply_text("✅ تم تعيين صورة الترحيب الافتراضية لكل القنوات الجديدة.")
        return


# ---------------------------------------------------------------------------
# الأوامر النصية (تعمل على آخر قناة فُتحت من اللوحة — context.user_data["_active_channel_id"])
# ---------------------------------------------------------------------------

def _get_active_channel_or_warn(update_message, context, user_id: int):
    """
    يرجع channel_id لو موجود وصالح للمستخدم ده، أو يبعت تحذير ويرجع None.
    هذا يجنّب تكرار نفس الفحص في cmd_whitelist_add و cmd_ban_id.
    """
    channel_id = context.user_data.get("_active_channel_id")
    if channel_id is None:
        return None, "⚠️ افتح قناة من اللوحة أولاً (عبر /start) قبل استخدام هذا الأمر."
    if not can_manage_channel(user_id, channel_id):
        return None, "غير مصرح لك بإدارة هذه القناة."
    return channel_id, None


async def cmd_whitelist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whitelist_add USER_ID — يضيف العضو لآخر قناة مفتوحة من اللوحة."""
    user_id = update.effective_user.id
    channel_id, error = _get_active_channel_or_warn(update.message, context, user_id)
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
    """/ban_id USER_ID — حظر يدوي مباشر في آخر قناة مفتوحة من اللوحة."""
    user_id = update.effective_user.id
    channel_id, error = _get_active_channel_or_warn(update.message, context, user_id)
    if error:
        await update.message.reply_text(error)
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("الاستخدام: `/ban_id USER_ID`", parse_mode=ParseMode.MARKDOWN)
        return

    target_id = int(context.args[0])
    try:
        await context.bot.ban_chat_member(chat_id=channel_id, user_id=target_id)
        db.mark_member_banned(channel_id, target_id)
        await update.message.reply_text(f"✅ تم حظر `{target_id}`.", parse_mode=ParseMode.MARKDOWN)
    except TelegramError as e:
        await update.message.reply_text(f"فشل الحظر: {e}")


# ---------------------------------------------------------------------------
# Error Handler عام — يمنع توقف البوت بسبب استثناء غير متوقع في أي handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("استثناء غير متوقع أثناء معالجة Update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# تشغيل البوت
# ---------------------------------------------------------------------------

def main():
    # ✅ كل فحوصات متغيرات البيئة موحّدة هنا في مكان واحد، بترتيب منطقي،
    # وكل واحدة بترمي رسالة خطأ واضحة فور الإقلاع بدل فشل صامت أو غامض لاحقاً.
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
    # (لازم يتسجل قبل أي MessageHandler نصي عام لتجنب أي تضارب فلترة)
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, handle_chat_shared))

    # استقبال صور الترحيب (في الخاص فقط)
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_private_photo))

    # استقبال نصوص الإدخال (USER ID للـ whitelist) في الخاص — بعد استبعاد الأوامر
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

    logger.info("البوت بدأ التشغيل...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
