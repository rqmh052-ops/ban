# -*- coding: utf-8 -*-
"""
db.py
طبقة قاعدة البيانات (PostgreSQL) — بوت حراسة قنوات متعددة (multi-channel).
كل جدول مرتبط بـ channel_id لضمان عزل تام بين القنوات.
"""

import os
import logging
from datetime import datetime, timezone
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


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
    """إنشاء كل الجداول لو مش موجودة. آمن للتنفيذ عدة مرات."""
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
                -- status: active / left / banned / unknown
                ban_date     TIMESTAMPTZ,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (channel_id, user_id)
            );
        """)

        # سجل الأحداث
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events_log (
                id           SERIAL PRIMARY KEY,
                channel_id   BIGINT NOT NULL,
                user_id      BIGINT NOT NULL,
                event_type   TEXT NOT NULL,
                -- event_type: join / leave / ban / unban
                event_time   TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
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


# ---------------------------------------------------------------------------
# الأعضاء
# ---------------------------------------------------------------------------

def upsert_member_join(channel_id: int, user_id: int, username: str, full_name: str):
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO members (channel_id, user_id, username, full_name, join_date, status)
            VALUES (%s, %s, %s, %s, %s, 'active')
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                username   = EXCLUDED.username,
                full_name  = EXCLUDED.full_name,
                join_date  = EXCLUDED.join_date,
                leave_date = NULL,
                status     = 'active'
        """, (channel_id, user_id, username, full_name, now))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'join', %s)
        """, (channel_id, user_id, now))


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


def mark_member_banned(channel_id: int, user_id: int):
    """نستخدم upsert هنا لأن /ban_id ممكن يحظر حد مش مسجل أصلاً في members."""
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO members (channel_id, user_id, status, ban_date)
            VALUES (%s, %s, 'banned', %s)
            ON CONFLICT (channel_id, user_id) DO UPDATE SET
                status   = 'banned',
                ban_date = EXCLUDED.ban_date
        """, (channel_id, user_id, now))
        cur.execute("""
            INSERT INTO events_log (channel_id, user_id, event_type, event_time)
            VALUES (%s, %s, 'ban', %s)
        """, (channel_id, user_id, now))


def mark_member_unbanned(channel_id: int, user_id: int):
    """
    ✅ إصلاح: الحالة بترجع لـ 'unknown' مش 'left' الغلط في النسخة القديمة،
    لأن فك الحظر معناه إننا فعلياً مش عارفين هو عضو نشط ولا لأ دلوقتي،
    لحد ما يدخل تاني (upsert_member_join هتظبطها وقتها).
    """
    now = datetime.now(timezone.utc)
    with get_cursor(commit=True) as cur:
        cur.execute("""
            UPDATE members SET status = 'unknown'
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
                COUNT(*) FILTER (WHERE status = 'active')  AS active_count,
                COUNT(*) FILTER (WHERE status = 'left')    AS left_count,
                COUNT(*) FILTER (WHERE status = 'banned')  AS banned_count,
                COUNT(*)                                   AS total_tracked
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
            SELECT e.event_type, e.event_time, m.username, m.full_name, e.user_id
            FROM events_log e
            LEFT JOIN members m ON m.channel_id = e.channel_id AND m.user_id = e.user_id
            WHERE e.channel_id = %s
            ORDER BY e.event_time DESC
            LIMIT %s
        """, (channel_id, limit))
        return cur.fetchall()


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
