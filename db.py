# -*- coding: utf-8 -*-
"""
db.py
طبقة قاعدة البيانات (PostgreSQL) — بوت حراسة قنوات متعددة (multi-channel) — النسخة الكاملة.

يضيف على النسخة السابقة:
- ban_reason في members وevents_log (قسم 5: سجل سبب الحظر).
- جدول watch_period لمنطق "فترة المراقبة" 3 أيام (قسم 4).
- جدول notification_prefs لإيقاف/تشغيل إشعار الحظر لكل قناة (تصحيح المستخدم).
- دوال بحث عبر كل القنوات وإحصائيات إجمالية (قسم 9 — صلاحيات المطوّر).
- دالة لتصدير بيانات المحظورين (قسم 7).

كل جدول مرتبط بـ channel_id لضمان عزل تام بين القنوات.
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# قيم ban_reason الممكنة — موحّدة في مكان واحد عشان تُستخدم بنفس النص في كل مكان
BAN_REASON_QUICK_LEAVE = "quick_leave"      # غادر قبل اكتمال 7 أيام من انضمامه -> حظر فوري
BAN_REASON_WATCH_EXPIRED = "watch_expired"  # غادر بعد 7 أيام، ولم يعد خلال مهلة المراقبة (3 أيام)
BAN_REASON_MANUAL = "manual"                # حظر يدوي عبر /ban_id أو من خلال بحث المطوّر

BAN_REASON_LABELS_AR = {
    BAN_REASON_QUICK_LEAVE: "مغادرة سريعة (أقل من 7 أيام)",
    BAN_REASON_WATCH_EXPIRED: "انتهاء فترة المراقبة (3 أيام بدون عودة)",
    BAN_REASON_MANUAL: "حظر يدوي",
}


def _get_connection():
    """Railway بيدي DATABASE_URL بصيغة postgres:// و psycopg2 محتاج postgresql://."""
    url = DATABASE_URL
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


@contextmanager
def get_cursor(commit=False):
    conn = _get_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """إنشاء كل الجداول لو مش موجودة، وإضافة الأعمدة الجديدة لو الجدول قديم. آمن للتنفيذ عدة مرات."""
    with get_cursor(commit=True) as cur:

        # القنوات المربوطة بالبوت
        cur.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id            BIGINT PRIMARY KEY,
                owner_user_id         BIGINT NOT NULL,
                title                 TEXT,
                welcome_photo_file_id TEXT,
                linked_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
                active                BOOLEAN NOT NULL DEFAULT TRUE
            );
        """)

        # الأعضاء — مفتاح مركب (channel_id, user_id) لعزل تام
        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                channel_id   BIGINT NOT NULL,
                user_id      BIGINT NOT NULL,
                username     TEXT,
                full_name    TEXT,
                join_date    TIMESTAMPTZ,
                leave_date   TIMESTAMPTZ,
                status       TEXT NOT NULL DEFAULT 'active',
                -- status: active / left / banned / unknown / watching
                ban_date     TIMESTAMPTZ,
                ban_reason   TEXT,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (channel_id, user_id)
            );
        """)
        # عمود ban_reason ممكن يكون مش موجود لو الجدول اتعمل من نسخة أقدم - نضيفه بأمان
        cur.execute("ALTER TABLE members ADD COLUMN IF NOT EXISTS ban_reason TEXT;")

        # سجل الأحداث
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events_log (
                id           SERIAL PRIMARY KEY,
                channel_id   BIGINT NOT NULL,
                user_id      BIGINT NOT NULL,
                event_type   TEXT NOT NULL,
                -- event_type: join / leave / ban / unban / watch_start / watch_return
                ban_reason   TEXT,
                event_time   TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("ALTER TABLE events_log ADD COLUMN IF NOT EXISTS ban_reason TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_channel_time ON events_log (channel_id, event_time);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events_log (event_type);")

        # القائمة المحصّنة من الحظر — مفتاح مركب
        cur.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                channel_id   BIGINT NOT NULL,
                user_id      BIGINT NOT NULL,
                username     TEXT,
                added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (channel_id, user_id)
            );
        """)

        # فترة المراقبة (قسم 4) — عضو غادر بعد 7 أيام أو أكتر من انضمامه، عنده
        # مهلة 3 أيام يرجع فيها قبل ما يتحظر تلقائياً. الـ JobQueue بتفحص الجدول
        # ده كل ساعة وتحظر أي صف انتهت مهلته.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watch_period (
                channel_id        BIGINT NOT NULL,
                user_id           BIGINT NOT NULL,
                watch_started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                watch_expires_at  TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (channel_id, user_id)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_expires ON watch_period (watch_expires_at);")

        # تفضيلات الإشعار لكل قناة (تصحيح المستخدم: ممكن مالك القناة يوقف إشعار
        # الحظر تحديداً لقناته، عن طريق زرار تحت كل إشعار حظر يوصله).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notification_prefs (
                channel_id           BIGINT PRIMARY KEY,
                ban_notifications_on BOOLEAN NOT NULL DEFAULT TRUE
            );
        """)

        # إعدادات عامة على مستوى البوت كله (key/value)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)

    logger.info("Database initialized (tables ensured).")


# ---------------------------------------------------------------------------
# القنوات
# ---------------------------------------------------------------------------

def upsert_channel(channel_id: int, owner_user_id: int, title: str = None):
    """تسجيل قناة جديدة أو إعادة تفعيلها لو كانت متوقفة (re-promotion)."""
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO channels (channel_id, owner_user_id, title, active)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (channel_id) DO UPDATE SET
                owner_user_id = EXCLUDED.owner_user_id,
                title         = COALESCE(EXCLUDED.title, channels.title),
                active        = TRUE
        """, (channel_id, owner_user_id, title))
        # نتأكد إن صف تفضيلات الإشعار موجود لها (افتراضياً مفعّل)
        cur.execute("""
            INSERT INTO notification_prefs (channel_id, ban_notifications_on)
            VALUES (%s, TRUE)
            ON CONFLICT (channel_id) DO NOTHING
        """, (channel_id,))


def deactivate_channel(channel_id: int):
    """البوت اتنزل من أدمن أو اتطرد — نوقف تتبع القناة دي."""
    with get_cursor(commit=True) as cur:
        cur.execute("UPDATE channels SET active = FALSE WHERE channel_id = %s", (channel_id,))


def is_channel_active(channel_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute("SELECT active FROM channels WHERE channel_id = %s", (channel_id,))
        row = cur.fetchone()
        return bool(row and row["active"])


def get_channel(channel_id: int):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM channels WHERE channel_id = %s", (channel_id,))
        return cur.fetchone()


def get_all_active_channels():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM channels WHERE active = TRUE ORDER BY linked_at DESC")
        return cur.fetchall()


def get_channels_owned_by(owner_user_id: int):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM channels WHERE owner_user_id = %s AND active = TRUE ORDER BY linked_at DESC",
            (owner_user_id,)
        )
        return cur.fetchall()


def set_welcome_photo(channel_id: int, file_id: str):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE channels SET welcome_photo_file_id = %s WHERE channel_id = %s",
            (file_id, channel_id)
        )


def get_total_channels_count() -> int:
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM channels WHERE active = TRUE")
        return cur.fetchone()["cnt"]


# ---------------------------------------------------------------------------
# تفضيلات الإشعار لكل قناة
# ---------------------------------------------------------------------------

def are_ban_notifications_on(channel_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "SELECT ban_notifications_on FROM notification_prefs WHERE channel_id = %s",
            (channel_id,)
        )
        row = cur.fetchone()
        # افتراضياً مفعّلة لو الصف مش موجود لأي سبب
        return True if row is None else bool(row["ban_notifications_on"])


def set_ban_notifications(channel_id: int, enabled: bool):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO notification_prefs (channel_id, ban_notifications_on)
            VALUES (%s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET ban_notifications_on = EXCLUDED.ban_notifications_on
        """, (channel_id, enabled))


# ---------------------------------------------------------------------------
# الأعضاء
# ---------------------------------------------------------------------------

def upsert_member_join(channel_id: int, user_id: int, username: str, full_name: str):
    """
    تسجيل/تحديث عضو عند الانضمام (أو العودة بعد مغادرة).
    ✅ join_date بيتحدّث في كل مرة (حتى لو كان موجود قبل كده) — وهذا مقصود:
    أي عودة = عداد السبعة أيام يبدأ من جديد بالكامل من تاريخ العودة هذا
    (هذا القرار المؤكد صراحة بخصوص نظام مدة العضوية في القسم 4).
    """
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO members (channel_id, user_id, username, full_name, join_date, status, ban_reason)
            VALUES (%s, %s, %s, %s, %s, 'active', NULL)
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                username   = EXCLUDED.username,
                full_name  = EXCLUDED.full_name,
                join_date  = EXCLUDED.join_date,
                leave_date = NULL,
                status     = 'active',
                ban_reason = NULL
        """, (channel_id, user_id, username, full_name, now))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'join', %s)
        """, (channel_id, user_id, now))
    # أي عودة تلغي فترة مراقبة سابقة كانت قايمة لنفس الشخص في نفس القناة
    clear_watch_period(channel_id, user_id)


def mark_member_left(channel_id: int, user_id: int):
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            UPDATE members SET status = 'left', leave_date = %s
            WHERE channel_id = %s AND user_id = %s
        """, (now, channel_id, user_id))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'leave', %s)
        """, (channel_id, user_id, now))


def mark_member_banned(channel_id: int, user_id: int, reason: str):
    """نستخدم upsert هنا لأن /ban_id ممكن يحظر حد مش مسجل أصلاً في members."""
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO members (channel_id, user_id, status, ban_date, ban_reason)
            VALUES (%s, %s, 'banned', %s, %s)
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                status     = 'banned',
                ban_date   = EXCLUDED.ban_date,
                ban_reason = EXCLUDED.ban_reason
        """, (channel_id, user_id, now, reason))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, ban_reason, event_time)
            VALUES (%s, %s, 'ban', %s, %s)
        """, (channel_id, user_id, reason, now))
    clear_watch_period(channel_id, user_id)


def mark_member_unbanned(channel_id: int, user_id: int):
    """الحالة بترجع لـ 'unknown' لحد ما يدخل تاني (upsert_member_join هتظبطها وقتها)."""
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            UPDATE members SET status = 'unknown', ban_reason = NULL
            WHERE channel_id = %s AND user_id = %s
        """, (channel_id, user_id))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'unban', %s)
        """, (channel_id, user_id, now))


def get_member(channel_id: int, user_id: int):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM members WHERE channel_id = %s AND user_id = %s",
            (channel_id, user_id)
        )
        return cur.fetchone()


def get_banned_members(channel_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM members
            WHERE channel_id = %s AND status = 'banned'
            ORDER BY ban_date DESC
        """, (channel_id,))
        return cur.fetchall()


def count_banned_members(channel_id: int) -> int:
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM members WHERE channel_id = %s AND status = 'banned'",
            (channel_id,)
        )
        return cur.fetchone()["cnt"]


# ---------------------------------------------------------------------------
# فترة المراقبة (قسم 4)
# ---------------------------------------------------------------------------

def start_watch_period(channel_id: int, user_id: int, watch_days: int = 3):
    """تسجيل بداية فترة مراقبة 3 أيام لعضو غادر بعد 7 أيام أو أكثر من انضمامه."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=watch_days)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO watch_period (channel_id, user_id, watch_started_at, watch_expires_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                watch_started_at = EXCLUDED.watch_started_at,
                watch_expires_at = EXCLUDED.watch_expires_at
        """, (channel_id, user_id, now, expires))
        cur.execute("""
            UPDATE members SET status = 'watching' WHERE channel_id = %s AND user_id = %s
        """, (channel_id, user_id))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'watch_start', %s)
        """, (channel_id, user_id, now))


def clear_watch_period(channel_id: int, user_id: int):
    """إلغاء فترة مراقبة قائمة (لو العضو رجع، أو اتحظر بسبب تاني، إلخ)."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM watch_period WHERE channel_id = %s AND user_id = %s",
            (channel_id, user_id)
        )


def is_in_watch_period(channel_id: int, user_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM watch_period WHERE channel_id = %s AND user_id = %s",
            (channel_id, user_id)
        )
        return cur.fetchone() is not None


def get_expired_watch_periods():
    """
    كل صفوف فترة المراقبة اللي معادها فات — تُستخدم من الـ JobQueue (تعمل كل ساعة)
    عشان تحظر كل عضو فات معاد رجوعه ولم يرجع، بدون تحديد قناة (بتدور على كل القنوات
    دفعة واحدة، أكفأ من فحص كل قناة لوحدها).
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT wp.channel_id, wp.user_id, wp.watch_started_at, wp.watch_expires_at,
                   m.username, m.full_name
            FROM watch_period wp
            LEFT JOIN members m ON m.channel_id = wp.channel_id AND m.user_id = wp.user_id
            WHERE wp.watch_expires_at <= now()
        """)
        return cur.fetchall()


# ---------------------------------------------------------------------------
# القائمة المحصّنة (Whitelist)
# ---------------------------------------------------------------------------

def add_to_whitelist(channel_id: int, user_id: int, username: str = None):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO whitelist (channel_id, user_id, username)
            VALUES (%s, %s, %s)
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, whitelist.username)
        """, (channel_id, user_id, username))


def remove_from_whitelist(channel_id: int, user_id: int):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "DELETE FROM whitelist WHERE channel_id = %s AND user_id = %s",
            (channel_id, user_id)
        )


def is_whitelisted(channel_id: int, user_id: int) -> bool:
    with get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM whitelist WHERE channel_id = %s AND user_id = %s",
            (channel_id, user_id)
        )
        return cur.fetchone() is not None


def get_whitelist(channel_id: int):
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM whitelist WHERE channel_id = %s ORDER BY added_at DESC",
            (channel_id,)
        )
        return cur.fetchall()


def count_whitelist(channel_id: int) -> int:
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM whitelist WHERE channel_id = %s",
            (channel_id,)
        )
        return cur.fetchone()["cnt"]


# ---------------------------------------------------------------------------
# الإحصائيات (كلها مفلترة بـ channel_id)
# ---------------------------------------------------------------------------

def get_general_stats(channel_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'active')   AS active_count,
                COUNT(*) FILTER (WHERE status = 'left')     AS left_count,
                COUNT(*) FILTER (WHERE status = 'watching') AS watching_count,
                COUNT(*) FILTER (WHERE status = 'banned')   AS banned_count,
                COUNT(*)                                    AS total_tracked
            FROM members
            WHERE channel_id = %s
        """, (channel_id,))
        return cur.fetchone()


def get_today_join_leave_counts(channel_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE event_type = 'join')  AS joins_today,
                COUNT(*) FILTER (WHERE event_type = 'leave') AS leaves_today,
                COUNT(*) FILTER (WHERE event_type = 'ban')   AS bans_today
            FROM events_log
            WHERE channel_id = %s
              AND event_time >= date_trunc('day', now())
        """, (channel_id,))
        return cur.fetchone()


def get_daily_breakdown(channel_id: int, days: int = 7):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                date_trunc('day', event_time) AS day,
                COUNT(*) FILTER (WHERE event_type = 'join')  AS joins,
                COUNT(*) FILTER (WHERE event_type = 'leave') AS leaves,
                COUNT(*) FILTER (WHERE event_type = 'ban')   AS bans
            FROM events_log
            WHERE channel_id = %s
              AND event_time >= now() - (%s || ' days')::interval
            GROUP BY day
            ORDER BY day DESC
        """, (channel_id, days))
        return cur.fetchall()


def get_peak_join_hour(channel_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                EXTRACT(HOUR FROM event_time) AS hour,
                COUNT(*) AS joins_count
            FROM events_log
            WHERE channel_id = %s AND event_type = 'join'
            GROUP BY hour
            ORDER BY joins_count DESC
            LIMIT 1
        """, (channel_id,))
        return cur.fetchone()


def get_peak_join_day_of_week(channel_id: int):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                TRIM(TO_CHAR(event_time, 'Day')) AS day_name,
                COUNT(*) AS joins_count
            FROM events_log
            WHERE channel_id = %s AND event_type = 'join'
            GROUP BY day_name
            ORDER BY joins_count DESC
            LIMIT 1
        """, (channel_id,))
        return cur.fetchone()


def get_recent_events(channel_id: int, limit: int = 15):
    with get_cursor() as cur:
        cur.execute("""
            SELECT e.event_type, e.event_time, e.ban_reason, m.username, m.full_name, e.user_id
            FROM events_log e
            LEFT JOIN members m ON m.channel_id = e.channel_id AND m.user_id = e.user_id
            WHERE e.channel_id = %s
            ORDER BY e.event_time DESC
            LIMIT %s
        """, (channel_id, limit))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# تصدير المحظورين (قسم 7)
# ---------------------------------------------------------------------------

def get_banned_export_rows(channel_id: int):
    """كل بيانات المحظورين المطلوبة للتصدير: تاريخ الحظر، username، user_id، السبب."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT user_id, username, ban_date, ban_reason
            FROM members
            WHERE channel_id = %s AND status = 'banned'
            ORDER BY ban_date DESC
        """, (channel_id,))
        return cur.fetchall()


# ---------------------------------------------------------------------------
# بحث وإحصائيات عبر كل القنوات (قسم 9 — صلاحيات المطوّر الحصرية)
# ---------------------------------------------------------------------------

def find_user_across_channels(user_id: int):
    """
    يدوّر على user_id معين في members عبر كل القنوات النشطة، ويرجع حالته في
    كل قناة موجود فيها (نشط/محظور/إلخ) مع اسم القناة — يُستخدم في بحث المطوّر
    عبر كل القنوات (قسم 9.ج).
    """
    with get_cursor() as cur:
        cur.execute("""
            SELECT m.channel_id, m.status, m.username, m.full_name, m.join_date,
                   m.ban_date, m.ban_reason, c.title
            FROM members m
            JOIN channels c ON c.channel_id = m.channel_id
            WHERE m.user_id = %s AND c.active = TRUE
            ORDER BY c.linked_at DESC
        """, (user_id,))
        return cur.fetchall()


def find_user_by_username_in_channel(channel_id: int, username: str):
    """بحث بالـ username (بدون @) عن عضو داخل قناة محددة (قسم 9.د)."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM members
            WHERE channel_id = %s AND LOWER(username) = LOWER(%s)
        """, (channel_id, username))
        return cur.fetchone()


def get_bot_wide_stats():
    """إحصائيات إجمالية على مستوى البوت كله (لمعاينة البث الجماعي مثلاً)."""
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM channels WHERE active = TRUE")
        channels_count = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM members m
            JOIN channels c ON c.channel_id = m.channel_id
            WHERE c.active = TRUE AND m.status = 'active'
        """)
        active_members_count = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM members m
            JOIN channels c ON c.channel_id = m.channel_id
            WHERE c.active = TRUE AND m.status = 'banned'
        """)
        banned_total = cur.fetchone()["cnt"]
        return {
            "channels_count": channels_count,
            "active_members_count": active_members_count,
            "banned_total": banned_total,
        }


# ---------------------------------------------------------------------------
# إعدادات عامة (key/value) — مثلاً صورة ترحيب افتراضية على مستوى البوت
# ---------------------------------------------------------------------------

def set_setting(key: str, value: str):
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO bot_settings (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (key, value))


def get_setting(key: str):
    with get_cursor() as cur:
        cur.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None
