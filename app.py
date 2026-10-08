import os
import hmac
import hashlib
import base64
import json
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from flask import Flask, request, abort, jsonify, Response

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
LIFF_ID = os.environ["LIFF_ID"]
ADMIN_USER_IDS = {x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
DM_BUCKET = os.environ.get("DM_BUCKET", "event-dm")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_MEMBER_PROFILE_URL = "https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"


DB_POOL = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=DATABASE_URL)


def db():
    return DB_POOL.getconn()


def release_db(conn):
    if conn:
        DB_POOL.putconn(conn)


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS line_events (
        id SERIAL PRIMARY KEY,
        group_id TEXT NOT NULL,
        title TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP NOT NULL
    );
    """)
    # 既有 line_events 也自動補上活動詳細資料欄位
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS event_date DATE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS location TEXT")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS description TEXT")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS dm_image_url TEXT")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS registration_deadline DATE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'general'")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS registration_force_open BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS registration_manual_closed BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS auto_publish_list BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS list_published_at TIMESTAMP NULL")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS reopened_after_close BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS deadline_list_published_at TIMESTAMP NULL")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS relay_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS relay_label TEXT")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS relay_mode TEXT NOT NULL DEFAULT 'text'")
    cur.execute("ALTER TABLE line_events ADD COLUMN IF NOT EXISTS purchase_catalog TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS line_signups (
        id SERIAL PRIMARY KEY,
        event_id INTEGER NOT NULL REFERENCES line_events(id) ON DELETE CASCADE,
        person_name TEXT NOT NULL,
        line_user_id TEXT,
        signup_type TEXT NOT NULL,
        proxy_by_user_id TEXT,
        proxy_by_name TEXT,
        created_at TIMESTAMP NOT NULL,
        UNIQUE(event_id, person_name)
    );
    """)
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS attendance_option TEXT")
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS dharma_role TEXT")
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS day1_group TEXT")
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS day2_group TEXT")
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS relay_items TEXT")
    cur.execute("ALTER TABLE line_signups ADD COLUMN IF NOT EXISTS leader_name TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS line_group_settings (
        group_id TEXT PRIMARY KEY,
        disabled BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );
    """)

    conn.commit()
    cur.close()
    release_db(conn)


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str):
    r = requests.post(
        LINE_REPLY_URL,
        headers={
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
        timeout=10,
    )
    r.raise_for_status()


def reply_flex(reply_token: str, alt_text: str, contents: dict):
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "flex",
            "altText": alt_text,
            "contents": contents
        }]
    }
    r = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
    r.raise_for_status()


def sign_group(group_id: str) -> str:
    return hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        group_id.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def valid_group_signature(group_id: str, sig: str) -> bool:
    if not group_id or not sig:
        return False
    return hmac.compare_digest(sign_group(group_id), sig)


def is_admin(user_id: str) -> bool:
    return bool(user_id and user_id in ADMIN_USER_IDS)


def group_entry_disabled(group_id: str) -> bool:
    if not group_id:
        return False
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT disabled FROM line_group_settings WHERE group_id=%s", (group_id,))
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return bool(row and row[0])


def set_group_entry_disabled(group_id: str, disabled: bool):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO line_group_settings(group_id, disabled, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (group_id)
        DO UPDATE SET disabled=EXCLUDED.disabled, updated_at=NOW()
        """,
        (group_id, bool(disabled)),
    )
    conn.commit()
    cur.close()
    release_db(conn)


def registration_is_open(ev) -> bool:
    """Manual close wins. Otherwise admin reopen overrides the deadline."""
    if ev.get("registration_manual_closed"):
        return False
    if ev.get("registration_force_open"):
        return True
    deadline = ev.get("registration_deadline")
    if not deadline:
        return True
    today_tw = datetime.now(ZoneInfo("Asia/Taipei")).date()
    return today_tw <= deadline


def make_liff_url(group_id: str) -> str:
    q = urlencode({"g": group_id, "sig": sign_group(group_id)})
    return f"https://liff.line.me/{LIFF_ID}?{q}"


def make_entry_flex(group_id: str) -> dict:
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "活動報名入口", "weight": "bold", "size": "xl"},
                {"type": "text", "text": "查看活動、本人報名、代人報名與報名名單。", "size": "sm", "color": "#666666", "wrap": True}
            ]
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [{
                "type": "button",
                "style": "primary",
                "action": {"type": "uri", "label": "開啟報名頁", "uri": make_liff_url(group_id)}
            }]
        }
    }


def get_member_name(group_id: str, user_id: str) -> str:
    if not group_id or not user_id:
        return "未知使用者"

    r = requests.get(
        LINE_MEMBER_PROFILE_URL.format(group_id=group_id, user_id=user_id),
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        timeout=10,
    )
    if r.ok:
        return r.json().get("displayName", "未知使用者")
    return "未知使用者"


def create_event(
    group_id: str,
    title: str,
    event_date=None,
    location=None,
    description=None,
    dm_image_url=None,
    registration_deadline=None,
    event_type="general",
    relay_enabled=False,
    relay_label=None,
    relay_mode="text",
    purchase_catalog=None,
):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO line_events(
            group_id, title, active, created_at,
            event_date, location, description, dm_image_url, registration_deadline, event_type,
            relay_enabled, relay_label, relay_mode, purchase_catalog
        )
        VALUES (%s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            group_id,
            title,
            datetime.now(),
            event_date or None,
            location or None,
            description or None,
            dm_image_url or None,
            registration_deadline or None,
            event_type or "general",
            bool(relay_enabled) if (event_type or "general") == "general" else False,
            (relay_label or "接龍項目").strip() if bool(relay_enabled) and (event_type or "general") == "general" else None,
            (relay_mode if relay_mode in {"text","fixed_purchase","custom_purchase","purchase"} else "text") if bool(relay_enabled) and (event_type or "general") == "general" else "text",
            json.dumps(parse_purchase_catalog(purchase_catalog), ensure_ascii=False)
            if bool(relay_enabled) and (event_type or "general") == "general" and relay_mode == "fixed_purchase"
            else None,
        ),
    )
    event_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    release_db(conn)
    return event_id


def upload_dm_to_supabase(file_storage):
    """將 DM 圖片上傳到 Supabase Storage 的公開 bucket。"""
    if not file_storage or not file_storage.filename:
        return None

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("尚未設定 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")

    content_type = file_storage.mimetype or ""
    if not content_type.startswith("image/"):
        raise ValueError("DM 只能上傳圖片檔")

    data = file_storage.read()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("DM 圖片請控制在 5MB 以內")

    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    ext = ext_map.get(content_type, ".jpg")
    object_name = f"{uuid.uuid4().hex}{ext}"

    upload_url = f"{SUPABASE_URL}/storage/v1/object/{DM_BUCKET}/{object_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Content-Type": content_type,
        "x-upsert": "false",
    }

    r = requests.post(upload_url, headers=headers, data=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"DM 上傳失敗：{r.status_code} {r.text[:200]}")

    return f"{SUPABASE_URL}/storage/v1/object/public/{DM_BUCKET}/{object_name}"


def list_active_events(group_id: str):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT e.*,
               COUNT(s.id)::int AS signup_count,
               COUNT(s.id) FILTER (
                   WHERE s.dharma_role = 'student'
                      OR (s.dharma_role IS NULL AND s.attendance_option IS NOT NULL)
               )::int AS student_count
        FROM line_events e
        LEFT JOIN line_signups s ON s.event_id = e.id
        WHERE e.group_id = %s AND e.active = TRUE
        GROUP BY e.id
        ORDER BY e.id ASC
        """,
        (group_id,),
    )
    rows = cur.fetchall()
    cur.close()
    release_db(conn)
    return rows

def get_event_by_number(group_id: str, number: int):
    events = list_active_events(group_id)
    if 1 <= number <= len(events):
        return events[number - 1]
    return None


def get_event_by_id(group_id: str, event_id: int):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM line_events WHERE id = %s AND group_id = %s AND active = TRUE",
        (event_id, group_id)
    )
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return row


def list_signups(event_id: int):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM line_signups WHERE event_id = %s ORDER BY id ASC", (event_id,))
    rows = cur.fetchall()
    cur.close()
    release_db(conn)
    return rows


def get_signup_count(event_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM line_signups WHERE event_id=%s", (event_id,))
    count = cur.fetchone()[0]
    cur.close()
    release_db(conn)
    return count


def add_signup(event_id, person_name, signup_type, line_user_id=None,
               proxy_by_user_id=None, proxy_by_name=None,
               attendance_option=None, dharma_role=None,
               day1_group=None, day2_group=None, relay_items=None, leader_name=None):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO line_signups (
                event_id, person_name, line_user_id, signup_type,
                proxy_by_user_id, proxy_by_name, created_at,
                attendance_option, dharma_role, day1_group, day2_group, relay_items, leader_name
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            event_id, person_name, line_user_id, signup_type,
            proxy_by_user_id, proxy_by_name, datetime.now(),
            attendance_option, dharma_role, day1_group, day2_group,
            json.dumps(relay_items or [], ensure_ascii=False),
            leader_name,
        ))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        release_db(conn)



def parse_relay_items(value):
    if not value:
        return []
    data = value
    if not isinstance(value, list):
        try:
            data = json.loads(value)
        except Exception:
            data = None
    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("item", "")).strip()
                try:
                    unit_price = float(item.get("unit_price", 0) or 0)
                    qty = float(item.get("qty", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if name and unit_price >= 0 and qty > 0:
                    result.append({
                        "item": name,
                        "unit_price": unit_price,
                        "qty": qty,
                        "subtotal": round(unit_price * qty, 2),
                    })
            else:
                s = str(item).strip()
                if s:
                    result.append(s)
        return result
    return [x.strip() for x in re.split(r"[、,，\n]+", str(value)) if x.strip()]


def clean_relay_items(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        s = str(item).strip()
        if s and s not in result:
            result.append(s)
    return result[:20]


def clean_purchase_items(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("item", "")).strip()
        try:
            unit_price = float(item.get("unit_price", 0) or 0)
            qty = float(item.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not name or unit_price < 0 or qty <= 0:
            continue
        result.append({
            "item": name,
            "unit_price": round(unit_price, 2),
            "qty": round(qty, 2),
            "subtotal": round(unit_price * qty, 2),
        })
    return result


def parse_purchase_catalog(value):
    if not value:
        return []
    data = value
    if not isinstance(value, list):
        try:
            data = json.loads(value)
        except Exception:
            data = []
    if not isinstance(data, list):
        return []
    result, seen = [], set()
    for row in data[:30]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("item", "")).strip()
        try:
            price = float(row.get("unit_price", 0) or 0)
        except (TypeError, ValueError):
            continue
        key = name.casefold()
        if not name or price < 0 or key in seen:
            continue
        seen.add(key)
        result.append({"item": name, "unit_price": round(price, 2)})
    return result


def clean_fixed_purchase_items(ev, value):
    catalog = parse_purchase_catalog(ev.get("purchase_catalog"))
    allowed = {x["item"]: x["unit_price"] for x in catalog}
    if not isinstance(value, list):
        return []
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        name = str(row.get("item", "")).strip()
        if name not in allowed:
            continue
        try:
            qty = float(row.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        price = allowed[name]
        result.append({
            "item": name,
            "unit_price": price,
            "qty": round(qty, 2),
            "subtotal": round(price * qty, 2),
        })
    return result


def clean_relay_for_event(ev, value):
    mode = ev.get("relay_mode") or "text"
    if mode == "fixed_purchase":
        return clean_fixed_purchase_items(ev, value)
    if mode in {"custom_purchase", "purchase"}:
        return clean_purchase_items(value)
    return clean_relay_items(value)


def remove_signup(event_id: int, person_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM line_signups WHERE event_id = %s AND person_name = %s",
        (event_id, person_name),
    )
    ok = cur.rowcount > 0
    conn.commit()
    cur.close()
    release_db(conn)
    return ok


def remove_self_signup(event_id: int, user_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM line_signups
        WHERE event_id = %s AND line_user_id = %s AND signup_type = 'self'
        """,
        (event_id, user_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    cur.close()
    release_db(conn)
    return ok


def get_signup_by_id(event_id: int, signup_id: int):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM line_signups WHERE id=%s AND event_id=%s",
        (signup_id, event_id),
    )
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return row


def remove_owned_signup(event_id: int, signup_id: int, user_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        DELETE FROM line_signups
        WHERE id=%s
          AND event_id=%s
          AND (
                (signup_type='self' AND line_user_id=%s)
             OR (signup_type='proxy' AND proxy_by_user_id=%s)
          )
        """,
        (signup_id, event_id, user_id, user_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    cur.close()
    release_db(conn)
    return ok


def close_event(group_id: str, number: int):
    ev = get_event_by_number(group_id, number)
    if not ev:
        return None

    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE line_events SET active = FALSE WHERE id = %s", (ev["id"],))
    conn.commit()
    cur.close()
    release_db(conn)
    return ev


def event_list_text(group_id: str):
    events = list_active_events(group_id)
    if not events:
        return "目前沒有進行中的活動。"

    lines = ["📌 目前進行中的活動：", ""]
    for i, ev in enumerate(events, 1):
        if (ev.get("event_type") or "general") == "dharma":
            lines.append(f"{i}. {ev['title']}（班員 {ev.get('student_count', 0)} 人）")
        else:
            lines.append(f"{i}. {ev['title']}（{ev['signup_count']} 人）")

    lines += ["", "例如：報名1／代報1 王小明／名單1"]
    return "\n".join(lines)


def all_signup_lists_text(group_id: str):
    events = list_active_events(group_id)
    if not events:
        return "目前沒有進行中的活動。"

    sections = []
    total = 0

    for i, ev in enumerate(events, 1):
        rows = list_signups(ev["id"])
        total += len(rows)

        lines = [f"【{i}. {ev['title']}】", f"共 {len(rows)} 人"]
        if not rows:
            lines.append("目前尚無人報名")
        else:
            for j, row in enumerate(rows, 1):
                if row["signup_type"] == "proxy":
                    lines.append(f"{j}. {row['person_name']}（{row['proxy_by_name']} 代報）")
                else:
                    lines.append(f"{j}. {row['person_name']}")

        sections.append("\n".join(lines))

    return (
        f"📋 全部活動報名名單\n"
        f"目前 {len(events)} 個活動，共 {total} 筆報名\n\n"
        + "\n\n".join(sections)
    )


def match_command_number(text: str, command: str):
    m = re.fullmatch(rf"{re.escape(command)}\s*(\d+)", text)
    return int(m.group(1)) if m else None


def split_names(raw: str):
    if not raw:
        return []

    normalized = raw.replace("\u3000", " ")
    chunks = re.split(r"[、,，;；/／\n\r\t]+", normalized)

    names = []
    seen = set()

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        parts = [x.strip() for x in re.split(r"\s+", chunk) if x.strip()]
        for name in parts:
            if name not in seen:
                names.append(name)
                seen.add(name)

    return names


HELP_TEXT = """【活動報名 Bot｜Supabase 版】

建立活動：
開活動 9/20 新民班

查看目前活動：
活動

LIFF 報名入口：
報名入口

本人報名：
報名1

代報（可一次多人）：
代報1 王小明 李小華 陳大華
也可用頓號、逗號、分號、斜線或換行

取消自己的報名：
取消報名1

取消指定姓名：
取消1 王小明

查看單一活動名單：
名單1

查看全部活動名單：
全部名單

結束活動：
結束1

※ 報名／代報／取消成功時不回覆，避免洗版。
※ 活動與名單改存 Supabase/PostgreSQL，不會因 Render 休眠或重新部署而消失。
"""


def handle_group_text(event):
    source = event.get("source", {})
    group_id = source.get("groupId")
    user_id = source.get("userId")
    reply_token = event["replyToken"]
    text = event["message"]["text"].strip()

    if not group_id:
        reply(reply_token, "這個版本請在 LINE 群組裡使用。")
        return

    user_name = get_member_name(group_id, user_id)

    if text in ("說明", "help", "Help", "HELP"):
        reply(reply_token, HELP_TEXT)
        return

    if text in ("報名入口", "活動入口", "LIFF"):
        reply_flex(reply_token, "活動報名入口", make_entry_flex(group_id))
        return

    if text.startswith("開活動 "):
        title = text[len("開活動 "):].strip()
        if not title:
            reply(reply_token, "請輸入活動名稱，例如：開活動 9/20 新民班")
            return
        create_event(group_id, title)
        reply(reply_token, f"✅ 已建立活動：{title}\n\n" + event_list_text(group_id))
        return

    if text == "活動":
        reply(reply_token, event_list_text(group_id))
        return

    if text in ("全部名單", "所有名單"):
        reply(reply_token, all_signup_lists_text(group_id))
        return

    num = match_command_number(text, "報名")
    if num is not None:
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        if not add_signup(ev["id"], user_name, "self", line_user_id=user_id):
            reply(reply_token, f"{user_name} 已經在「{ev['title']}」名單裡了。")
        return

    m = re.match(r"^代報\s*(\d+)\s+(.+)$", text, flags=re.S)
    if m:
        num = int(m.group(1))
        raw_names = m.group(2).strip()
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return

        names = split_names(raw_names)
        if not names:
            reply(reply_token, "請輸入至少一個姓名。")
            return

        duplicated = []
        added_count = 0
        for person_name in names:
            if add_signup(
                ev["id"], person_name, "proxy",
                proxy_by_user_id=user_id, proxy_by_name=user_name
            ):
                added_count += 1
            else:
                duplicated.append(person_name)

        if duplicated:
            msg = f"以下 {len(duplicated)} 位已經在「{ev['title']}」名單裡：\n" + "、".join(duplicated)
            if added_count:
                msg += f"\n其餘 {added_count} 位已成功加入。"
            reply(reply_token, msg)
        return

    elif text.startswith("代報"):
        reply(reply_token, "格式：代報1 王小明 李小華 陳大華\n也可以用頓號、逗號、分號、斜線或換行。")
        return

    num = match_command_number(text, "取消報名")
    if num is not None:
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        if not remove_self_signup(ev["id"], user_id):
            reply(reply_token, f"找不到你在「{ev['title']}」的本人報名紀錄。")
        return

    m = re.match(r"^取消\s*(\d+)\s+(.+)$", text, flags=re.S)
    if m:
        num = int(m.group(1))
        person_name = m.group(2).strip()
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        if not remove_signup(ev["id"], person_name):
            reply(reply_token, f"「{ev['title']}」名單中找不到：{person_name}")
        return

    elif text.startswith("取消"):
        reply(reply_token, "格式：取消1 王小明")
        return

    num = match_command_number(text, "名單")
    if num is not None:
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        rows = list_signups(ev["id"])
        if not rows:
            reply(reply_token, f"📋 {ev['title']}\n目前還沒有人報名。")
            return

        lines = [f"📋 {ev['title']}", f"目前共 {len(rows)} 人", ""]
        for i, row in enumerate(rows, 1):
            if row["signup_type"] == "proxy":
                lines.append(f"{i}. {row['person_name']}（{row['proxy_by_name']} 代報）")
            else:
                lines.append(f"{i}. {row['person_name']}")
        reply(reply_token, "\n".join(lines))
        return

    elif text.startswith("名單"):
        reply(reply_token, "請輸入活動編號，例如：名單1")
        return

    num = match_command_number(text, "結束")
    if num is not None:
        ev = close_event(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        reply(reply_token, f"✅ 已結束活動：{ev['title']}\n\n" + event_list_text(group_id))
        return

    elif text.startswith("結束"):
        reply(reply_token, "請輸入活動編號，例如：結束1")
        return


LIFF_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>活動報名</title>
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f6f7f8;color:#222}
.wrap{max-width:720px;margin:auto;padding:18px}h1{font-size:24px;margin:4px 0}.sub{color:#777;margin:4px 0 16px}
.card{background:#fff;border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 1px 6px rgba(0,0,0,.08)}
.title{font-size:19px;font-weight:700}.count{font-size:14px;color:#666;margin:7px 0 13px}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}button{border:0;border-radius:10px;padding:11px 6px;font-size:15px}
.primary{background:#06c755;color:#fff}.secondary{background:#e8f1ff;color:#1769aa}.light{background:#eee;color:#333}
.msg{display:none;margin:10px 0;padding:10px;border-radius:10px}.ok{display:block;background:#e8f8ee;color:#17723b}.err{display:block;background:#fdecec;color:#a22}
dialog{width:min(92vw,520px);border:0;border-radius:16px;padding:0}.modal{padding:18px}.meta{font-size:14px;color:#666;line-height:1.6;margin:8px 0}.desc{font-size:14px;line-height:1.6;margin:8px 0 12px;white-space:pre-wrap}.dm{width:100%;border-radius:12px;margin:8px 0 12px;display:block}
.card-dm{max-height:360px;object-fit:contain;background:#f7f7f7;cursor:pointer}
.preview-desc{color:#555;max-height:3.4em;overflow:hidden}label{display:block;font-size:13px;color:#666;margin-top:8px}textarea,input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px}
</style>
</head>
<body><div class="wrap"><h1>活動報名</h1><div class="sub" id="who">讀取 LINE 身分中…</div><div id="msg" class="msg"></div>
<div id="groupDisabledBanner" class="card" style="display:none;background:#fff3f3;border:1px solid #f1b5b5">
<div class="title" style="color:#a22">此群組報名入口目前已停用</div>
<div style="font-size:14px;color:#666;margin-top:6px">活動與名單資料仍有保留；一般使用者目前無法操作報名功能。</div>
</div>
<div id="adminTools" class="card" style="display:none">
<div class="title">活動管理</div>
<button id="groupAccessBtn" class="light" style="width:100%;margin:10px 0 4px;color:#a22" onclick="toggleGroupAccess()">停用此群組報名入口</button>
<div class="actions" style="grid-template-columns:1fr 1fr">
<button class="primary" onclick="openCreate()">＋ 新增活動</button>
<button class="light" onclick="toggleEditMode()">編輯活動</button>
<button class="light" onclick="toggleCloseMode()">結束活動</button>
</div>
<div id="editModeHint" style="display:none;color:#1769aa;margin-top:10px;font-size:14px">請在下方活動卡片按「編輯此活動」。</div>
<div id="closeModeHint" style="display:none;color:#a22;margin-top:10px;font-size:14px">請在下方活動卡片按「結束此活動」。</div>
</div>
<div id="events">載入活動中…</div></div>
<dialog id="editDialog"><div class="modal">
<h3>編輯活動</h3>
<input type="hidden" id="editEventId">
<label>活動名稱 *</label><input id="editTitle">
<label>活動類型</label>
<select id="editType" onchange="toggleEditRelay()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"><option value="general">一般活動</option><option value="dharma">法會</option></select>
<div id="editRelayBox" style="display:none;padding:10px 12px;background:#f7f7f7;border-radius:10px;margin-bottom:12px">
<label style="margin:0"><input id="editRelayEnabled" type="checkbox" style="width:auto;margin-right:7px" onchange="toggleEditRelayLabel()">開啟接龍項目</label>
<div id="editRelayLabelBox" style="display:none">
<label>接龍類型</label>
<select id="editRelayMode" onchange="toggleEditPurchaseCatalog()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
<option value="text">一般接龍</option>
<option value="fixed_purchase">固定商品（管理員先設定商品與單價）</option>
<option value="custom_purchase">自由採購（使用者自己填商品與單價）</option>
</select>
<div id="editPurchaseCatalogBox" style="display:none;margin-top:8px">
  <div style="font-weight:600;margin-bottom:6px">商品與單價</div>
  <div id="editPurchaseCatalogRows"></div>
  <button type="button" class="secondary" style="width:100%;margin-bottom:10px" onclick="addCatalogRow('editPurchaseCatalogRows')">＋ 新增商品</button>
  <div style="font-size:12px;color:#888;margin-bottom:8px">使用者報名時只需要填數量。</div>
</div>
<label>接龍欄位名稱</label><input id="editRelayLabel" placeholder="例如：菜色、攜帶物品、商品">
</div>
</div>
<label>活動日期</label><input id="editDate" type="date">
<label>地點</label><input id="editLocation">
<label>報名截止日</label><input id="editDeadline" type="date">
<label>活動說明</label><textarea id="editDescription" rows="5"></textarea>
<label>目前 DM</label>
<img id="editCurrentDM" class="dm" style="display:none">
<div id="editNoDM" style="font-size:13px;color:#888;margin-bottom:8px">目前沒有 DM</div>
<label>更換 DM（可留空）</label><input id="editDM" type="file" accept="image/*">
<div style="font-size:12px;color:#888;margin:-4px 0 12px">不選新圖片就保留原本 DM。</div>
<button class="primary" style="width:100%" onclick="submitEdit(this)">儲存修改</button>
<button class="light" style="width:100%;margin-top:8px" onclick="editDialog.close()">取消</button>
</div></dialog>
<dialog id="createDialog"><div class="modal">
<h3>新增活動</h3>
<label>活動名稱 *</label><input id="newTitle" placeholder="例如：9/20 新民班">
<label>活動類型</label>
<select id="newType" onchange="toggleNewRelay()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"><option value="general">一般活動</option><option value="dharma">法會</option></select>
<div id="newRelayBox" style="padding:10px 12px;background:#f7f7f7;border-radius:10px;margin-bottom:12px">
<label style="margin:0"><input id="newRelayEnabled" type="checkbox" style="width:auto;margin-right:7px" onchange="toggleNewRelayLabel()">開啟接龍項目</label>
<div id="newRelayLabelBox" style="display:none">
<label>接龍類型</label>
<select id="newRelayMode" onchange="toggleNewPurchaseCatalog()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
<option value="text">一般接龍</option>
<option value="fixed_purchase">固定商品（管理員先設定商品與單價）</option>
<option value="custom_purchase">自由採購（使用者自己填商品與單價）</option>
</select>
<div id="newPurchaseCatalogBox" style="display:none;margin-top:8px">
  <div style="font-weight:600;margin-bottom:6px">商品與單價</div>
  <div id="newPurchaseCatalogRows"></div>
  <button type="button" class="secondary" style="width:100%;margin-bottom:10px" onclick="addCatalogRow('newPurchaseCatalogRows')">＋ 新增商品</button>
  <div style="font-size:12px;color:#888;margin-bottom:8px">使用者報名時只需要填數量。</div>
</div>
<label>接龍欄位名稱</label><input id="newRelayLabel" value="菜色" placeholder="例如：菜色、攜帶物品、商品">
</div>
</div>
<label>活動日期</label><input id="newDate" type="date">
<label>地點</label><input id="newLocation" placeholder="例如：崇德大樓">
<label>報名截止日</label><input id="newDeadline" type="date">
<label>活動說明</label><textarea id="newDescription" rows="5" placeholder="活動內容、集合時間、注意事項等"></textarea>
<label>DM 圖片</label><input id="newDM" type="file" accept="image/*">
<div style="font-size:12px;color:#888;margin:-4px 0 12px">可不傳；建議 JPG/PNG/WebP，5MB 以內。</div>
<button class="primary" style="width:100%" onclick="submitCreate(this)">建立活動</button>
<button class="light" style="width:100%;margin-top:8px" onclick="createDialog.close()">取消</button>
</div></dialog>
<dialog id="dharmaDialog"><div class="modal">
<h3 id="dharmaTitle">法會報名</h3>
<label>報名身分</label>
<select id="dharmaRole" onchange="toggleDharmaFields()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
<option value="student">班員</option><option value="staff">辦事人員</option>
</select>
<div id="dharmaStudentFields">
<label>班員參班方式</label>
<select id="dharmaAttendance" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
<option value="上兩天">上兩天</option><option value="第一天">第一天</option><option value="補第二天">補第二天</option><option value="加開第一天">加開第一天</option>
</select>
</div>
<div id="dharmaStaffFields" style="display:none">
<label>第一天工作（不參加可留白）</label><select id="dharmaDay1" class="dharma-group" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
<label>第二天工作（不參加可留白）</label><select id="dharmaDay2" class="dharma-group" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
<div style="font-size:12px;color:#888;margin:-4px 0 12px">至少一天要選擇組別。</div>
</div>
<button class="primary" style="width:100%" onclick="submitDharma(this)">送出報名</button>
<button class="light" style="width:100%;margin-top:8px" onclick="dharmaDialog.close()">取消</button>
</div></dialog>
<dialog id="detailDialog"><div class="modal">
<h3 id="detailTitle">活動詳情</h3>
<img id="detailDM" class="dm" style="display:none">
<div id="detailMeta" class="meta"></div>
<div id="detailDesc" class="desc"></div>
<button class="light" style="width:100%;margin-top:12px" onclick="detailDialog.close()">關閉</button>
</div></dialog>
<dialog id="signupChoiceDialog"><div class="modal">
<h3 id="signupChoiceTitle">立即報名</h3>

<div id="unifiedSelfBox">
<label style="display:flex;align-items:center;gap:8px;font-size:16px;color:#222;margin:6px 0 12px">
  <input id="unifiedSelf" type="checkbox" checked style="width:auto;margin:0" onchange="toggleUnifiedSelf()">
  我本人也要報名
</label>
</div>

<div id="unifiedDharmaOptions" style="display:none;padding:10px 12px;background:#f7f7f7;border-radius:10px;margin-bottom:12px">
  <div style="font-size:13px;color:#666;margin-bottom:6px">請先選擇報名身分；班員的參班方式會在每位班員資料中個別選擇。</div>
  <label>報名身分</label>
  <select id="unifiedDharmaRole" onchange="toggleUnifiedDharmaFields()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
    <option value="student">班員</option>
    <option value="staff">辦事人員</option>
  </select>
  <div id="unifiedStudentFields" style="display:none"></div>
  <div id="unifiedStaffFields" style="display:none">
    <label>第一天工作（不參加可留白）</label>
    <select id="unifiedDay1" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
    <label>第二天工作（不參加可留白）</label>
    <select id="unifiedDay2" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
  </div>
</div>

<div id="unifiedSelfRelay" style="display:none;padding:10px 12px;background:#f7f7f7;border-radius:10px;margin-bottom:12px">
  <div id="unifiedSelfRelayLabel" style="font-weight:600;margin-bottom:6px">我的接龍項目</div>
  <div id="unifiedSelfRelayItems"></div>
  <button class="secondary" type="button" style="width:100%" onclick="addRelayInput('unifiedSelfRelayItems')">＋ 新增一項</button>
</div>

<div style="border-top:1px solid #eee;margin:14px 0"></div>
<label id="unifiedProxySectionLabel" style="font-size:15px;color:#222">順便幫其他人報名（可留空）</label>
<div id="unifiedProxyStudentPairs" style="display:none">
  <div id="unifiedProxyStudentRows"></div>
  <button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addProxyStudentRow()">＋ 新增一位班員</button>
  <div style="font-size:12px;color:#888;margin:-2px 0 10px">每位班員都可以填自己的帶班人員。</div>
</div>

<div id="unifiedProxyStaffPairs" style="display:none">
  <div id="unifiedProxyStaffRows"></div>
  <button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addProxyStaffRow()">＋ 新增一位辦事人員</button>
  <div style="font-size:12px;color:#888;margin:-2px 0 10px">每位辦事人員可分別選第一天、第二天工作；不參加的那一天可留白。</div>
</div>

<textarea id="unifiedProxyNames" rows="4" placeholder="例如：王小明 李小華；可用空格、頓號、逗號或換行"></textarea>

<div id="unifiedGeneralProxyPeople" style="display:none">
  <div id="unifiedGeneralProxyRows"></div>
  <button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addGeneralProxyPerson()">＋ 新增一位</button>
  <div style="font-size:12px;color:#888;margin-top:2px">每一位都可以有自己的接龍內容；同一人也可以新增多個項目。</div>
</div>

<div id="unifiedProxyRelay" style="display:none"></div>

<button class="primary" style="width:100%" onclick="submitUnifiedSignup(this)">送出報名</button>
<button class="light" style="width:100%;margin-top:8px" onclick="signupChoiceDialog.close()">取消</button>
</div></dialog>
<dialog id="relaySelfDialog"><div class="modal">
<h3 id="relaySelfTitle">本人報名</h3>
<div id="relaySelfLabel" style="font-weight:600;margin:8px 0"></div>
<div id="relaySelfItems"></div>
<button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addRelayInput('relaySelfItems')">＋ 新增一項</button>
<button class="primary" style="width:100%" onclick="submitRelaySelf(this)">送出報名</button>
<button class="light" style="width:100%;margin-top:8px" onclick="relaySelfDialog.close()">取消</button>
</div></dialog>
<dialog id="relayEditDialog"><div class="modal">
<h3 id="relayEditTitle">修改接龍項目</h3>
<div id="relayEditLabel" style="font-weight:600;margin:8px 0"></div>
<div id="relayEditItems"></div>
<button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addRelayInput('relayEditItems')">＋ 新增一項</button>
<button class="primary" style="width:100%" onclick="submitRelayEdit(this)">儲存修改</button>
<button class="light" style="width:100%;margin-top:8px" onclick="relayEditDialog.close()">取消</button>
</div></dialog>
<dialog id="proxyDialog"><div class="modal"><h3 id="proxyTitle">代人報名</h3>
<div id="proxyDharmaOptions" style="display:none">
<label>報名身分</label>
<select id="proxyDharmaRole" onchange="toggleProxyDharmaFields()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
<option value="student">班員</option><option value="staff">辦事人員</option>
</select>
<div id="proxyStudentFields">
<label>班員參班方式</label><select id="proxyAttendance" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"><option value="上兩天">上兩天</option><option value="第一天">第一天</option><option value="補第二天">補第二天</option><option value="加開第一天">加開第一天</option></select>
</div>
<div id="proxyStaffFields" style="display:none">
<label>第一天工作（不參加可留白）</label><select id="proxyDay1" class="dharma-group" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
<label>第二天工作（不參加可留白）</label><select id="proxyDay2" class="dharma-group" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
<div style="font-size:12px;color:#888;margin:-4px 0 12px">這一批代報的人會套用相同的日期與組別；至少一天要選擇組別。</div>
</div>
</div>
<div id="proxyRelayOptions" style="display:none">
<div id="proxyRelayLabel" style="font-weight:600;margin:8px 0"></div>
<div id="proxyRelayItems"></div>
<button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addRelayInput('proxyRelayItems')">＋ 新增一項</button>
<div style="font-size:12px;color:#888;margin:-2px 0 12px">一次代報多人時，這批人會套用相同的接龍項目；若不同請分開代報。</div>
</div>
<textarea id="proxyNames" rows="5" placeholder="可輸入多人：王小明 李小華；也可用頓號、逗號或換行"></textarea><button class="primary" style="width:100%" onclick="submitProxy(this)">送出代報</button><button class="light" style="width:100%;margin-top:8px" onclick="proxyDialog.close()">取消</button></div></dialog>
<dialog id="adminSignupEditDialog"><div class="modal">
<h3 id="adminSignupEditTitle">編輯報名資料</h3>
<label>姓名</label><input id="adminSignupName">

<div id="adminGeneralEditBox" style="display:none">
  <div id="adminRelayEditBox" style="display:none">
    <div id="adminRelayEditLabel" style="font-weight:600;margin:8px 0"></div>
    <div id="adminRelayEditItems"></div>
    <button class="secondary" type="button" style="width:100%;margin-bottom:10px" onclick="addRelayInput('adminRelayEditItems')">＋ 新增一項</button>
  </div>
</div>

<div id="adminDharmaEditBox" style="display:none">
  <label>報名身分</label>
  <select id="adminDharmaRole" onchange="toggleAdminDharmaFields()" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
    <option value="student">班員</option>
    <option value="staff">辦事人員</option>
  </select>
  <div id="adminStudentEditFields">
    <label>班員參班方式</label>
    <select id="adminAttendance" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
      <option value="上兩天">上兩天</option>
      <option value="第一天">第一天</option>
      <option value="補第二天">補第二天</option>
      <option value="加開第一天">加開第一天</option>
    </select>
    <label>帶班人員 *</label>
    <input id="adminLeaderName" placeholder="帶班人員姓名">
  </div>
  <div id="adminStaffEditFields" style="display:none">
    <label>第一天工作（不參加可留白）</label>
    <select id="adminDay1" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
    <label>第二天工作（不參加可留白）</label>
    <select id="adminDay2" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white"></select>
  </div>
</div>

<button class="primary" style="width:100%" onclick="submitAdminSignupEdit(this)">儲存修改</button>
<button class="light" style="width:100%;margin-top:8px" onclick="adminSignupEditDialog.close()">取消</button>
</div></dialog>
<dialog id="listDialog"><div class="modal"><h3 id="listTitle">報名名單</h3><div id="listBody" style="line-height:1.8"></div><button class="light" style="width:100%;margin-top:12px" onclick="listDialog.close()">關閉</button></div></dialog>
<script>
const LIFF_ID="__LIFF_ID__";
const qs=new URLSearchParams(location.search);

function getLiffParams(){
  let g=qs.get("g");
  let s=qs.get("sig");

  if((!g||!s) && qs.get("liff.state")){
    try{
      const raw=decodeURIComponent(qs.get("liff.state"));
      const q=raw.startsWith("?") ? raw.slice(1) : raw;
      const sp=new URLSearchParams(q);
      g=g||sp.get("g");
      s=s||sp.get("sig");
    }catch(e){
      console.error("Failed to parse liff.state",e);
    }
  }

  return {g:g,s:s};
}

const lp=getLiffParams();
const groupId=lp.g, sig=lp.s;
let profile=null, proxyEventId=null, proxyEventType="general", adminMode=false, groupDisabled=false, closeMode=false, editMode=false, dharmaEventId=null;
function setBusy(btn,busy,label='處理中…'){if(!btn)return;if(busy){btn.dataset.old=btn.textContent;btn.textContent=label;btn.disabled=true;btn.style.opacity='.6'}else{btn.textContent=btn.dataset.old||btn.textContent;btn.disabled=false;btn.style.opacity='1'}}
function showMsg(t,ok=true){const e=document.getElementById('msg');e.className='msg '+(ok?'ok':'err');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',3000)}
async function api(path,opt={}){
  try{
    const sep=path.includes('?')?'&':'?';
    const g=encodeURIComponent(String(groupId||''));
    const s=encodeURIComponent(String(sig||''));
    const target=String(path)+sep+'g='+g+'&sig='+s;
    const r=await fetch(target,opt);
    let d={};
    try{d=await r.json()}catch(_){throw new Error('伺服器回應格式錯誤')}
    if(!r.ok)throw new Error(d.error||'發生錯誤');
    return d;
  }catch(e){
    if(e && e.message==='The string did not match the expected pattern.'){
      throw new Error('Safari 無法建立報名請求網址，請從 LINE 群組的「報名入口」重新開啟頁面');
    }
    throw e;
  }
}
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function init(){
  fillDharmaGroups();
  if(!groupId||!sig){
    document.getElementById('events').innerHTML='此連結無效，請從群組中的「報名入口」開啟。';
    return;
  }
  await liff.init({liffId:LIFF_ID});
  if(!liff.isLoggedIn()){
    liff.login({redirectUri:location.href});
    return;
  }
  profile=await liff.getProfile();
  document.getElementById('who').textContent='你好，'+profile.displayName+'｜管理者：檢查中';

  try{
    const me=await api('/api/liff/me?user_id='+encodeURIComponent(profile.userId));
    adminMode=!!me.is_admin;
    groupDisabled=!!me.group_disabled;
    document.getElementById('who').textContent='你好，'+profile.displayName+'｜管理者：'+(adminMode?'是':'否');

    if(groupDisabled){
      document.getElementById('groupDisabledBanner').style.display='block';
    }

    if(adminMode){
      document.getElementById('adminTools').style.display='block';
      updateGroupAccessButton();
    }

    if(groupDisabled && !adminMode){
      document.getElementById('events').innerHTML='<div class="card">此群組報名入口目前已停用。</div>';
      return;
    }
  }catch(e){
    document.getElementById('who').textContent='你好，'+profile.displayName+'｜管理者：檢查失敗';
    console.error(e);
  }
  loadEvents();
}
function updateGroupAccessButton(){
  const btn=document.getElementById('groupAccessBtn');
  if(!btn)return;
  btn.textContent=groupDisabled?'重新啟用此群組報名入口':'停用此群組報名入口';
  btn.style.color=groupDisabled?'#17723b':'#a22';
}
async function toggleGroupAccess(){
  if(!adminMode)return;
  const next=!groupDisabled;
  const wording=next?'停用':'重新啟用';
  if(!confirm('確定要'+wording+'這個群組的報名入口嗎？'))return;

  try{
    const d=await api('/api/liff/group-access',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_id:profile.userId,disabled:next})
    });
    groupDisabled=!!d.group_disabled;
    document.getElementById('groupDisabledBanner').style.display=groupDisabled?'block':'none';
    updateGroupAccessButton();
    showMsg(d.message);

    if(groupDisabled){
      document.getElementById('events').innerHTML='<div class="card">此群組報名入口目前已停用。資料仍有保留。</div>';
    }else{
      await loadEvents();
    }
  }catch(e){
    showMsg(e.message,false);
  }
}

let events=[];
let lastListPeople=[];
let adminEditEventId=0, adminEditSignupId=0, adminEditEventType='general', adminEditRelayEnabled=false, adminEditRelayLabel='接龍項目', adminEditRelayMode='text';
let relaySelfEventId=0, relayEditEventId=0, relayEditSignupId=0;

function addRelayInput(containerId,value=''){
  const root=document.getElementById(containerId);
  const row=document.createElement('div');
  row.style.cssText='display:flex;gap:7px;align-items:center;margin-bottom:7px';
  const input=document.createElement('input');
  input.className='relay-item-input'; input.value=value||''; input.placeholder='請輸入項目';
  input.style.cssText='margin:0;flex:1';
  const del=document.createElement('button');
  del.type='button'; del.className='light'; del.textContent='刪除';
  del.style.cssText='padding:10px 12px'; del.onclick=()=>row.remove();
  row.appendChild(input); row.appendChild(del); root.appendChild(row);
}
function relayValues(containerId){
  return Array.from(document.querySelectorAll('#'+containerId+' .relay-item-input')).map(x=>x.value.trim()).filter(Boolean);
}
function moneyText(v){
  const n=Number(v||0);
  return Number.isInteger(n)?String(n):n.toFixed(2).replace(/0+$/,'').replace(/\.$/,'');
}
function addCatalogRow(containerId,value={}){
  const root=document.getElementById(containerId);
  const row=document.createElement('div');
  row.className='catalog-row';
  row.style.cssText='padding:10px;border:1px solid #ddd;border-radius:10px;margin-bottom:8px;background:#fff';
  const v=(value&&typeof value==='object')?value:{};
  row.innerHTML=`
    <label style="margin-top:0">商品名稱</label>
    <input class="catalog-name" value="${esc(v.item||'')}" placeholder="例如：茶葉">
    <label>單價</label>
    <input class="catalog-price" type="number" min="0" step="1" value="${v.unit_price??''}" placeholder="0">
    <button type="button" class="light" style="width:100%;color:#a22" onclick="this.parentElement.remove()">刪除商品</button>`;
  root.appendChild(row);
}
function catalogValues(containerId){
  return Array.from(document.querySelectorAll('#'+containerId+' .catalog-row')).map(row=>({
    item:row.querySelector('.catalog-name').value.trim(),
    unit_price:Number(row.querySelector('.catalog-price').value||0)
  })).filter(x=>x.item&&x.unit_price>=0);
}
function toggleNewPurchaseCatalog(){
  const fixed=document.getElementById('newRelayMode').value==='fixed_purchase';
  document.getElementById('newPurchaseCatalogBox').style.display=fixed?'block':'none';
  const root=document.getElementById('newPurchaseCatalogRows');
  if(fixed && !root.children.length)addCatalogRow('newPurchaseCatalogRows');
}
function toggleEditPurchaseCatalog(){
  const fixed=document.getElementById('editRelayMode').value==='fixed_purchase';
  document.getElementById('editPurchaseCatalogBox').style.display=fixed?'block':'none';
}
function isPurchaseMode(mode){ return ['fixed_purchase','custom_purchase','purchase'].includes(mode); }
function addFixedPurchaseInputs(containerId,catalog=[],values=[]){
  const root=document.getElementById(containerId);
  root.innerHTML='';
  const qtyMap={};
  (values||[]).forEach(x=>{if(x&&typeof x==='object')qtyMap[x.item]=Number(x.qty||0)});
  (catalog||[]).forEach(item=>{
    const row=document.createElement('div');
    row.className='fixed-purchase-row';
    row.dataset.item=item.item;
    row.dataset.price=item.unit_price;
    row.style.cssText='padding:10px;border:1px solid #ddd;border-radius:10px;margin-bottom:8px;background:#fff';
    row.innerHTML=`
      <div style="font-weight:600">${esc(item.item)}</div>
      <div style="font-size:13px;color:#666;margin:3px 0 6px">單價：${moneyText(item.unit_price)} 元</div>
      <label>數量</label>
      <input class="fixed-purchase-qty" type="number" min="0" step="1" value="${qtyMap[item.item]||0}">
      <div class="fixed-purchase-subtotal" style="font-size:14px;color:#555">小計：0 元</div>`;
    const recalc=()=>{
      const q=Number(row.querySelector('.fixed-purchase-qty').value||0);
      row.querySelector('.fixed-purchase-subtotal').textContent='小計：'+moneyText(Number(item.unit_price||0)*q)+' 元';
    };
    row.querySelector('.fixed-purchase-qty').addEventListener('input',recalc);
    root.appendChild(row); recalc();
  });
}
function fixedPurchaseValues(containerId){
  return Array.from(document.querySelectorAll('#'+containerId+' .fixed-purchase-row')).map(row=>({
    item:row.dataset.item,
    unit_price:Number(row.dataset.price||0),
    qty:Number(row.querySelector('.fixed-purchase-qty').value||0)
  })).filter(x=>x.qty>0);
}
function addGeneralProxyPerson(name='',items=[]){
  const ev=events.find(x=>x.id===signupChoiceEventId);
  const mode=(ev&&ev.relay_mode)||'text';
  const catalog=(ev&&ev.purchase_catalog)||[];
  const root=document.getElementById('unifiedGeneralProxyRows');
  const card=document.createElement('div');
  card.className='general-proxy-person';
  card.style.cssText='padding:12px;border:1px solid #ddd;border-radius:12px;margin-bottom:10px;background:#fff';

  const personId='gp_'+Date.now()+'_'+Math.random().toString(36).slice(2,8);
  card.dataset.personId=personId;

  const label = mode==='fixed_purchase' ? '購買數量'
    : (isPurchaseMode(mode) ? '購買項目' : ((ev&&ev.relay_label)||'接龍項目'));

  card.innerHTML=`
    <label style="margin-top:0">姓名</label>
    <input class="general-proxy-name" value="${esc(name)}" placeholder="請輸入姓名">
    <div style="font-weight:600;margin:4px 0 8px">${esc(label)}</div>
    <div class="general-proxy-items"></div>
    <button type="button" class="secondary general-proxy-add-item" style="width:100%;margin-bottom:8px">＋ 新增一項</button>
    <button type="button" class="light" style="width:100%;color:#a22" onclick="this.parentElement.remove()">刪除這位</button>`;

  root.appendChild(card);
  const itemRoot=card.querySelector('.general-proxy-items');
  itemRoot.id=personId+'_items';
  const addBtn=card.querySelector('.general-proxy-add-item');

  if(mode==='fixed_purchase'){
    addBtn.style.display='none';
    addFixedPurchaseInputs(itemRoot.id,catalog,Array.isArray(items)?items:[]);
  }else{
    addBtn.style.display='block';
    addBtn.onclick=()=>addRelayInputByMode(itemRoot.id,mode);
    const initial=(Array.isArray(items)&&items.length)?items:[isPurchaseMode(mode)?{}:''];
    initial.forEach(x=>addRelayInputByMode(itemRoot.id,mode,x));
  }
}

function getGeneralProxyPeople(){
  const ev=events.find(x=>x.id===signupChoiceEventId);
  const mode=(ev&&ev.relay_mode)||'text';
  return Array.from(document.querySelectorAll('#unifiedGeneralProxyRows .general-proxy-person')).map(card=>({
    name:card.querySelector('.general-proxy-name').value.trim(),
    relay_items:relayValuesByMode(card.querySelector('.general-proxy-items').id,mode)
  })).filter(x=>x.name || (x.relay_items&&x.relay_items.length));
}
function addPurchaseInput(containerId,value={}){
  const root=document.getElementById(containerId);
  const row=document.createElement('div');
  row.className='purchase-item-row';
  row.style.cssText='padding:10px;border:1px solid #ddd;border-radius:10px;margin-bottom:8px;background:#fff';
  const item=(value&&typeof value==='object')?value:{};
  row.innerHTML=`
    <label style="margin-top:0">品項</label>
    <input class="purchase-name" value="${esc(item.item||'')}" placeholder="例如：茶葉">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
      <div><label>單價</label><input class="purchase-price" type="number" min="0" step="1" value="${item.unit_price??''}" placeholder="0"></div>
      <div><label>數量</label><input class="purchase-qty" type="number" min="0.01" step="1" value="${item.qty??1}"></div>
    </div>
    <div class="purchase-subtotal" style="font-size:14px;color:#555;margin:2px 0 8px">小計：0 元</div>
    <button type="button" class="light" style="width:100%;color:#a22" onclick="this.parentElement.remove()">刪除這項</button>`;
  const recalc=()=>{
    const p=Number(row.querySelector('.purchase-price').value||0);
    const q=Number(row.querySelector('.purchase-qty').value||0);
    row.querySelector('.purchase-subtotal').textContent='小計：'+moneyText(p*q)+' 元';
  };
  row.querySelector('.purchase-price').addEventListener('input',recalc);
  row.querySelector('.purchase-qty').addEventListener('input',recalc);
  root.appendChild(row); recalc();
}
function purchaseValues(containerId){
  return Array.from(document.querySelectorAll('#'+containerId+' .purchase-item-row')).map(row=>({
    item:row.querySelector('.purchase-name').value.trim(),
    unit_price:Number(row.querySelector('.purchase-price').value||0),
    qty:Number(row.querySelector('.purchase-qty').value||0)
  })).filter(x=>x.item&&x.unit_price>=0&&x.qty>0);
}
function addRelayInputByMode(containerId,mode,value='',catalog=[]){
  if(mode==='fixed_purchase'){
    addFixedPurchaseInputs(containerId,catalog,Array.isArray(value)?value:[]);
  }else if(mode==='custom_purchase'||mode==='purchase'){
    addPurchaseInput(containerId,value&&typeof value==='object'?value:{});
  }else{
    addRelayInput(containerId,typeof value==='string'?value:'');
  }
}
function relayValuesByMode(containerId,mode){
  if(mode==='fixed_purchase')return fixedPurchaseValues(containerId);
  if(mode==='custom_purchase'||mode==='purchase')return purchaseValues(containerId);
  return relayValues(containerId);
}
function toggleNewRelay(){
  const general=document.getElementById('newType').value==='general';
  document.getElementById('newRelayBox').style.display=general?'block':'none';
  toggleNewRelayLabel();
}
function toggleNewRelayLabel(){
  const on=document.getElementById('newType').value==='general'&&document.getElementById('newRelayEnabled').checked;
  document.getElementById('newRelayLabelBox').style.display=on?'block':'none';
  if(on)toggleNewPurchaseCatalog();
}
function toggleEditRelay(){
  const general=document.getElementById('editType').value==='general';
  document.getElementById('editRelayBox').style.display=general?'block':'none';
  toggleEditRelayLabel();
}
function toggleEditRelayLabel(){
  const on=document.getElementById('editType').value==='general'&&document.getElementById('editRelayEnabled').checked;
  document.getElementById('editRelayLabelBox').style.display=on?'block':'none';
  if(on)toggleEditPurchaseCatalog();
}

async function loadEvents(){
if(groupDisabled){
  document.getElementById('events').innerHTML='<div class="card">此群組報名入口目前已停用。資料仍有保留。</div>';
  return;
}
try{
  const d=await api('/api/liff/events');
  events=d.events || [];
  const root=document.getElementById('events');

  if(!events.length){
    root.innerHTML='<div class="card">目前沒有進行中的活動。</div>';
    return;
  }

  root.innerHTML=events.map(ev=>{
    const meta=[
      ev.event_date ? `📅 ${esc(ev.event_date)}` : '',
      ev.location ? `📍 ${esc(ev.location)}` : '',
      ev.registration_deadline ? `截止：${esc(ev.registration_deadline)}` : ''
    ].filter(Boolean).join('　');

    const safeTitle=String(ev.title).replace(/'/g,"\'");
    const typeBadge=ev.event_type==='dharma'
      ? '<div class="meta">法會｜班員／辦事人員分開報名</div>'
      : (ev.relay_enabled ? `<div class="meta">${ev.relay_mode==='fixed_purchase'?'固定商品':((ev.relay_mode==='custom_purchase'||isPurchaseMode(ev.relay_mode||'text'))?'自由採購':'接龍項目')}：${esc(ev.relay_label||'接龍項目')}</div>` : '');
    const deadlineBadge=!ev.registration_open
      ? '<div style="margin:8px 0;padding:8px 10px;border-radius:9px;background:#fdecec;color:#a22;font-weight:600">報名已截止</div>'
      : (ev.registration_force_open ? '<div style="margin:8px 0;padding:8px 10px;border-radius:9px;background:#e8f8ee;color:#17723b">管理者已重新開放報名</div>' : '');

    // DM 直接顯示在卡片上；點圖片可開啟完整詳情
    const img=ev.dm_image_url
      ? `<img class="dm card-dm" src="${esc(ev.dm_image_url)}" alt="活動DM" onclick="showDetail(${ev.id})">`
      : '';

    // 說明只顯示摘要，完整內容到「查看詳情」
    const shortDesc=ev.description
      ? `<div class="desc preview-desc">${esc(ev.description.length>55 ? ev.description.slice(0,55)+'…' : ev.description)}</div>`
      : '';

    return `<div class="card">
      <div class="title">${esc(ev.title)}</div>
      ${typeBadge}
      ${meta ? `<div class="meta">${meta}</div>` : ''}
      ${deadlineBadge}
      ${img}
      ${shortDesc}
      <div class="count">${(ev.event_type||'general')==='dharma' ? `目前班員 ${ev.count} 人` : `目前 ${ev.count} 人報名`}</div>

      <div class="actions">
        <button class="light" onclick="showDetail(${ev.id})">查看詳情</button>
        <button class="primary" ${ev.registration_open ? `onclick="openSignupChoice(${ev.id},'${safeTitle}','${ev.event_type==='dharma'?'dharma':'general'}')"` : 'disabled style="background:#bbb;color:white"'}>${ev.registration_open?'立即報名':'報名已截止'}</button>
        <button class="light" onclick="showList(${ev.id},'${safeTitle}')">查看名單</button>
      </div>

      ${adminMode
        ? `<button class="light" style="width:100%;margin-top:10px"
             onclick="toggleAutoPublish(${ev.id},${ev.auto_publish_list?'false':'true'})">${ev.auto_publish_list?'✓ 截止後自動公布名單':'截止後自動公布名單：關閉'}</button>
           ${ev.list_published_at?`<button class="light" style="width:100%;margin-top:8px" onclick="publishList(${ev.id},'${safeTitle}')">重新公布最新名單</button>`:''}`
        : ''}
      ${adminMode&&ev.registration_open
        ? `<button type="button" class="light close-registration-btn"
             data-event-id="${ev.id}"
             style="width:100%;margin-top:10px;color:#a22">關閉報名</button>`
        : ''}
      ${adminMode&&!ev.registration_open
        ? `<button class="primary" style="width:100%;margin-top:10px"
             onclick="reopenRegistration(${ev.id},'${safeTitle}')">重新開放報名</button>`
        : ''}
      ${adminMode&&editMode
        ? `<button class="secondary" style="width:100%;margin-top:10px"
             onclick="openEdit(${ev.id})">編輯此活動</button>`
        : ''}
      ${adminMode&&closeMode
        ? `<button class="light" style="width:100%;margin-top:10px;color:#a22"
             onclick="closeEvent(${ev.id},'${safeTitle}')">結束此活動</button>`
        : ''}
    </div>`;
  }).join('');

  document.querySelectorAll('.close-registration-btn').forEach(btn=>{
    btn.addEventListener('click', async function(e){
      e.preventDefault();
      e.stopPropagation();
      const id=Number(this.dataset.eventId);
      const ev=events.find(x=>x.id===id);
      if(!ev){showMsg('找不到活動資料',false);return;}
      await closeRegistration(id, ev.title || '活動');
    });
  });

}catch(e){
  document.getElementById('events').innerHTML='載入失敗：'+esc(e.message);
}}

function openCreate(){
document.getElementById('newTitle').value='';
document.getElementById('newType').value='general';
document.getElementById('newDate').value='';
document.getElementById('newLocation').value='';
document.getElementById('newDeadline').value='';
document.getElementById('newDescription').value='';
document.getElementById('newDM').value='';
createDialog.showModal()
}
async function submitCreate(btn){
const title=document.getElementById('newTitle').value.trim();
if(!title){showMsg('請輸入活動名稱',false);return}
setBusy(btn,true,'建立中…');
try{
  const fd=new FormData();
  fd.append('title',title);
  fd.append('event_type',document.getElementById('newType').value);
  fd.append('relay_enabled',document.getElementById('newRelayEnabled').checked?'1':'0');
  fd.append('relay_mode',document.getElementById('newRelayMode').value);
  fd.append('purchase_catalog',JSON.stringify(catalogValues('newPurchaseCatalogRows')));
  fd.append('relay_label',document.getElementById('newRelayLabel').value.trim());
  fd.append('event_date',document.getElementById('newDate').value);
  fd.append('location',document.getElementById('newLocation').value.trim());
  fd.append('registration_deadline',document.getElementById('newDeadline').value);
  fd.append('description',document.getElementById('newDescription').value.trim());
  fd.append('user_id',profile.userId);
  const file=document.getElementById('newDM').files[0];
  if(file)fd.append('dm',file);
  const d=await api('/api/liff/events/create',{method:'POST',body:fd});
  createDialog.close();
  showMsg(d.message);
  loadEvents();
}catch(e){showMsg(e.message,false)}
finally{setBusy(btn,false)}
}
function toggleEditMode(){
  editMode=!editMode;
  if(editMode) closeMode=false;
  document.getElementById('editModeHint').style.display=editMode?'block':'none';
  document.getElementById('closeModeHint').style.display='none';
  loadEvents();
}
function toggleCloseMode(){closeMode=!closeMode;if(closeMode)editMode=false;document.getElementById('closeModeHint').style.display=closeMode?'block':'none';document.getElementById('editModeHint').style.display='none';loadEvents()}
async function toggleAutoPublish(id,enabled){
  try{
    const d=await api('/api/liff/events/auto-publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId,enabled:enabled})});
    showMsg(d.message,true);await loadEvents()
  }catch(e){showMsg(e.message,false)}
}
async function publishList(id,title){
  if(!confirm('確定要把「'+title+'」目前最新名單發到 LINE 群組嗎？'))return;
  try{
    const d=await api('/api/liff/events/publish-list',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId})});
    showMsg(d.message,true);await loadEvents()
  }catch(e){showMsg(e.message,false)}
}
async function closeRegistration(id,title){
  const ev=events.find(x=>x.id===id);
  if(!ev){showMsg('找不到活動資料',false);return;}

  let publish=false;
  if(ev.reopened_after_close){
    publish=true;
  }else{
    const choice=window.prompt(
      '關閉「'+title+'」報名後，要不要立即公布目前名單？\n\n輸入 1：關閉並公布名單\n輸入 2：只關閉報名\n輸入其他內容或按取消：不做任何變更',
      '2'
    );
    if(choice===null)return;
    if(choice==='1') publish=true;
    else if(choice==='2') publish=false;
    else {showMsg('未關閉報名',false);return;}
  }

  try{
    showMsg('正在關閉報名…',true);
    const d=await api('/api/liff/events/close-registration',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({event_id:id,user_id:profile.userId,publish:publish})
    });
    await loadEvents();
    showMsg(d.message, !d.publish_failed);
  }catch(e){
    showMsg(e.message,false);
    // 即使公布名單失敗，也重新向後端取得真實報名狀態。
    try{ await loadEvents(); }catch(_){}
  }
}
async function reopenRegistration(id,title){
  if(!confirm('確定要重新開放「'+title+'」的報名嗎？'))return;
  try{
    const d=await api('/api/liff/events/reopen',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId})});
    showMsg(d.message,true);
    await loadEvents();
  }catch(e){showMsg(e.message,false)}
}
async function closeEvent(id,title){if(!confirm('確定要結束「'+title+'」嗎？'))return;try{const d=await api('/api/liff/events/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId})});showMsg(d.message);loadEvents()}catch(e){showMsg(e.message,false)}}
async function openEdit(id){
  try{
    const d=await api('/api/liff/event?event_id='+id);
    const ev=d.event;

    document.getElementById('editEventId').value=ev.id;
    document.getElementById('editTitle').value=ev.title||'';
    document.getElementById('editType').value=ev.event_type||'general';
    document.getElementById('editRelayEnabled').checked=!!ev.relay_enabled;
    document.getElementById('editRelayMode').value=(isPurchaseMode(ev.relay_mode||'text')?'custom_purchase':(ev.relay_mode||'text'));
    document.getElementById('editRelayLabel').value=ev.relay_label||'菜色';
    document.getElementById('editPurchaseCatalogRows').innerHTML='';
    (ev.purchase_catalog||[]).forEach(x=>addCatalogRow('editPurchaseCatalogRows',x));
    toggleEditRelay();
    document.getElementById('editDate').value=ev.event_date||'';
    document.getElementById('editLocation').value=ev.location||'';
    document.getElementById('editDeadline').value=ev.registration_deadline||'';
    document.getElementById('editDescription').value=ev.description||'';
    document.getElementById('editDM').value='';

    const img=document.getElementById('editCurrentDM');
    const no=document.getElementById('editNoDM');
    if(ev.dm_image_url){
      img.src=ev.dm_image_url;
      img.style.display='block';
      no.style.display='none';
    }else{
      img.removeAttribute('src');
      img.style.display='none';
      no.style.display='block';
    }

    editDialog.showModal();
  }catch(e){
    showMsg(e.message,false);
  }
}

async function submitEdit(btn){
  const eventId=document.getElementById('editEventId').value;
  const title=document.getElementById('editTitle').value.trim();

  if(!title){
    showMsg('請輸入活動名稱',false);
    return;
  }

  setBusy(btn,true,'儲存中…');

  try{
    const fd=new FormData();
    fd.append('event_id',eventId);
    fd.append('title',title);
    fd.append('event_type',document.getElementById('editType').value);
    fd.append('relay_enabled',document.getElementById('editRelayEnabled').checked?'1':'0');
    fd.append('relay_mode',document.getElementById('editRelayMode').value);
    fd.append('purchase_catalog',JSON.stringify(catalogValues('editPurchaseCatalogRows')));
    fd.append('relay_label',document.getElementById('editRelayLabel').value.trim());
    fd.append('event_date',document.getElementById('editDate').value);
    fd.append('location',document.getElementById('editLocation').value.trim());
    fd.append('registration_deadline',document.getElementById('editDeadline').value);
    fd.append('description',document.getElementById('editDescription').value.trim());
    fd.append('user_id',profile.userId);

    const file=document.getElementById('editDM').files[0];
    if(file) fd.append('dm',file);

    const d=await api('/api/liff/events/update',{
      method:'POST',
      body:fd
    });

    editDialog.close();
    showMsg(d.message,true);
    await loadEvents();

  }catch(e){
    showMsg(e.message,false);
  }finally{
    setBusy(btn,false);
  }
}

async function showDetail(id){
try{
  const d=await api('/api/liff/event?event_id='+id);
  const ev=d.event;
  document.getElementById('detailTitle').textContent=ev.title;
  const meta=[
    ev.event_type==='dharma' ? '類型：法會' : '',
    ev.event_date ? '📅 '+ev.event_date : '',
    ev.location ? '📍 '+ev.location : '',
    ev.registration_deadline ? '報名截止：'+ev.registration_deadline+'（當日 23:59）' : '',
    !ev.registration_open ? '⛔ 報名已截止' : (ev.registration_force_open ? '✅ 管理者已重新開放報名' : '')
  ].filter(Boolean).join('<br>');
  document.getElementById('detailMeta').innerHTML=meta||'';
  document.getElementById('detailDesc').textContent=ev.description||'目前沒有活動說明。';
  const img=document.getElementById('detailDM');
  if(ev.dm_image_url){img.src=ev.dm_image_url;img.style.display='block'}
  else{img.removeAttribute('src');img.style.display='none'}
  detailDialog.showModal()
}catch(e){showMsg(e.message,false)}
}

const DHARMA_GROUPS=['服務','文書','接待','總務','辦道','壇務','炊事'];
function fillDharmaGroups(){
  document.querySelectorAll('.dharma-group').forEach(s=>{
    s.innerHTML='<option value="">&nbsp;</option>'+DHARMA_GROUPS.map(x=>`<option value="${x}">${x}</option>`).join('');
    s.value='';
  });
}
function selectValue(id){
  const el=document.getElementById(id);
  if(!el || el.selectedIndex<0)return '';
  const opt=el.options[el.selectedIndex];
  return opt ? String(opt.value||'').trim() : '';
}
function toggleDharmaFields(){
  const staff=selectValue('dharmaRole')==='staff';
  document.getElementById('dharmaStudentFields').style.display=staff?'none':'block';
  document.getElementById('dharmaStaffFields').style.display=staff?'block':'none';
}
function toggleProxyDharmaFields(){
  const staff=selectValue('proxyDharmaRole')==='staff';
  document.getElementById('proxyStudentFields').style.display=staff?'none':'block';
  document.getElementById('proxyStaffFields').style.display=staff?'block':'none';
}
function openDharma(id,title){
  dharmaEventId=id;
  document.getElementById('dharmaTitle').textContent='法會報名｜'+title;
  document.getElementById('dharmaRole').value='staff';
  document.getElementById('dharmaDay1').selectedIndex=0;
  document.getElementById('dharmaDay2').selectedIndex=0;
  toggleDharmaFields();
  dharmaDialog.showModal();
}
async function submitDharma(btn){
  const role=selectValue('dharmaRole');
  const body={event_id:dharmaEventId,user_id:profile.userId,display_name:profile.displayName,dharma_role:role};
  if(role==='student'){
    body.attendance_option=selectValue('dharmaAttendance');
  }else{
    body.day1_group=selectValue('dharmaDay1');
    body.day2_group=selectValue('dharmaDay2');
    if(!body.day1_group&&!body.day2_group){showMsg('辦事人員至少要選擇一天的組別',false);return}
  }
  setBusy(btn,true,'送出中…');
  try{
    const d=await api('/api/liff/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    dharmaDialog.close();showMsg(d.message);await loadEvents()
  }catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}
}
let signupChoiceEventId=0, signupChoiceTitleText='', signupChoiceType='general';

function fillUnifiedGroups(){
  const opts='<option value="">&nbsp;</option>'+DHARMA_GROUPS.map(g=>`<option value="${esc(g)}">${esc(g)}</option>`).join('');
  document.getElementById('unifiedDay1').innerHTML=opts;
  document.getElementById('unifiedDay2').innerHTML=opts;
}
function addProxyStudentRow(name='',leader=''){
  const root=document.getElementById('unifiedProxyStudentRows');
  const row=document.createElement('div');
  row.className='proxy-student-row';
  row.style.cssText='padding:10px;border:1px solid #ddd;border-radius:10px;margin-bottom:8px;background:#fff';
  row.innerHTML=`
    <label style="margin-top:0">班員姓名</label>
    <input class="proxy-student-name" value="${esc(name)}" placeholder="班員姓名">
    <label>參班方式 *</label>
    <select class="proxy-student-attendance" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">
      <option value="上兩天">上兩天</option>
      <option value="第一天">第一天</option>
      <option value="補第二天">補第二天</option>
      <option value="加開第一天">加開第一天</option>
    </select>
    <label>帶班人員 *</label>
    <input class="proxy-student-leader" value="${esc(leader)}" placeholder="帶班人員姓名">
    <button type="button" class="light" style="width:100%;color:#a22" onclick="this.parentElement.remove()">刪除這位</button>`;
  root.appendChild(row);
}
function getProxyStudentPairs(){
  return Array.from(document.querySelectorAll('#unifiedProxyStudentRows .proxy-student-row'))
    .map(row=>({
      name:row.querySelector('.proxy-student-name').value.trim(),
      attendance_option:row.querySelector('.proxy-student-attendance').value.trim(),
      leader_name:row.querySelector('.proxy-student-leader').value.trim()
    }))
    .filter(x=>x.name||x.leader_name);
}
function staffGroupOptions(selected=''){
  const values=['',...DHARMA_GROUPS];
  return values.map(g=>`<option value="${esc(g)}"${g===selected?' selected':''}>${g?esc(g):'&nbsp;'}</option>`).join('');
}
function addProxyStaffRow(name='',day1='',day2=''){
  const root=document.getElementById('unifiedProxyStaffRows');
  const row=document.createElement('div');
  row.className='proxy-staff-row';
  row.style.cssText='padding:10px;border:1px solid #ddd;border-radius:10px;margin-bottom:8px;background:#fff';
  row.innerHTML=`
    <label style="margin-top:0">辦事人員姓名</label>
    <input class="proxy-staff-name" value="${esc(name)}" placeholder="姓名">
    <label>第一天工作（不參加可留白）</label>
    <select class="proxy-staff-day1" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">${staffGroupOptions(day1)}</select>
    <label>第二天工作（不參加可留白）</label>
    <select class="proxy-staff-day2" style="width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px;background:white">${staffGroupOptions(day2)}</select>
    <button type="button" class="light" style="width:100%;color:#a22" onclick="this.parentElement.remove()">刪除這位</button>`;
  root.appendChild(row);
}
function getProxyStaffPairs(){
  return Array.from(document.querySelectorAll('#unifiedProxyStaffRows .proxy-staff-row'))
    .map(row=>({
      name:row.querySelector('.proxy-staff-name').value.trim(),
      day1_group:row.querySelector('.proxy-staff-day1').value.trim(),
      day2_group:row.querySelector('.proxy-staff-day2').value.trim()
    }))
    .filter(x=>x.name||x.day1_group||x.day2_group);
}
function toggleUnifiedDharmaFields(){
  const staff=document.getElementById('unifiedDharmaRole').value==='staff';
  document.getElementById('unifiedStudentFields').style.display=staff?'none':'block';
  document.getElementById('unifiedStaffFields').style.display=staff?'block':'none';
  document.getElementById('unifiedProxyStudentPairs').style.display=staff?'none':'block';
  document.getElementById('unifiedProxyStaffPairs').style.display=staff?'block':'none';
  document.getElementById('unifiedProxyNames').style.display='none';

  const selfBox=document.getElementById('unifiedSelfBox');
  const selfCheck=document.getElementById('unifiedSelf');
  if(staff){
    selfBox.style.display='block';
    selfCheck.checked=true;
  }else{
    selfBox.style.display='none';
    selfCheck.checked=false;
  }
}
function toggleUnifiedSelf(){
  const ev=events.find(x=>x.id===signupChoiceEventId);
  const show=document.getElementById('unifiedSelf').checked && signupChoiceType==='general' && ev && ev.relay_enabled;
  document.getElementById('unifiedSelfRelay').style.display=show?'block':'none';
}
function openSignupChoice(id,title,eventType){
  signupChoiceEventId=id;
  signupChoiceTitleText=title;
  signupChoiceType=eventType||'general';

  const ev=events.find(x=>x.id===id);
  document.getElementById('signupChoiceTitle').textContent='立即報名｜'+title;
  document.getElementById('unifiedSelf').checked=true;
  document.getElementById('unifiedProxyNames').value='';
  document.getElementById('unifiedGeneralProxyRows').innerHTML='';

  const isDharma=signupChoiceType==='dharma';

  // 每次打開報名視窗都先把「一般活動 / 法會」介面狀態完整重設，
  // 避免上一次開過法會後，狀態殘留到一般活動。
  document.getElementById('unifiedDharmaOptions').style.display=isDharma?'block':'none';
  document.getElementById('unifiedProxyStudentPairs').style.display='none';
  document.getElementById('unifiedProxyStaffPairs').style.display='none';
  document.getElementById('unifiedGeneralProxyPeople').style.display='none';
  document.getElementById('unifiedSelfRelay').style.display='none';
  document.getElementById('unifiedProxyRelay').style.display='none';

  const selfBox=document.getElementById('unifiedSelfBox');
  const selfCheck=document.getElementById('unifiedSelf');

  if(isDharma){
    fillUnifiedGroups();
    document.getElementById('unifiedDharmaRole').value='student';
    document.getElementById('unifiedDay1').selectedIndex=0;
    document.getElementById('unifiedDay2').selectedIndex=0;
    document.getElementById('unifiedProxyStudentRows').innerHTML='';
    document.getElementById('unifiedProxyStaffRows').innerHTML='';
    addProxyStudentRow();
    addProxyStaffRow();
    toggleUnifiedDharmaFields();
  }else{
    // 一般活動一定恢復一般報名介面，不顯示班員/辦事人員選項。
    selfBox.style.display='block';
    selfCheck.checked=true;
    document.getElementById('unifiedProxyNames').style.display='block';
  }

  const relayOn=!isDharma && ev && ev.relay_enabled;
  document.getElementById('unifiedProxySectionLabel').textContent=relayOn?'幫其他人報名（可新增多人）':'順便幫其他人報名（可留空）';
  document.getElementById('unifiedSelfRelay').style.display=relayOn?'block':'none';
  document.getElementById('unifiedProxyRelay').style.display='none';
  document.getElementById('unifiedGeneralProxyPeople').style.display=relayOn?'block':'none';
  document.getElementById('unifiedProxyNames').style.display=(!isDharma && !relayOn)?'block':'none';
  const relayMode=(ev&&ev.relay_mode)||'text';
  const fixed=relayMode==='fixed_purchase';
  const purchase=isPurchaseMode(relayMode);
  document.getElementById('unifiedSelfRelayLabel').textContent='我的'+((ev&&ev.relay_label)||'接龍項目')+(fixed?'（請填數量）':(purchase?'（品項／單價／數量）':'（可填多項）'));
  document.getElementById('unifiedSelfRelayItems').innerHTML='';
  const selfAddBtn=document.querySelector('#unifiedSelfRelay button');
  if(selfAddBtn){
    selfAddBtn.style.display=fixed?'none':'block';
    selfAddBtn.onclick=()=>addRelayInputByMode('unifiedSelfRelayItems',relayMode);
  }
  if(relayOn){
    if(fixed){
      addFixedPurchaseInputs('unifiedSelfRelayItems',ev.purchase_catalog||[],[]);
    }else{
      addRelayInputByMode('unifiedSelfRelayItems',relayMode);
    }
    addGeneralProxyPerson();
  }

  signupChoiceDialog.showModal();
}

async function submitUnifiedSignup(btn){
  let includeSelf=document.getElementById('unifiedSelf').checked;
  if(signupChoiceType==='dharma' && selectValue('unifiedDharmaRole')==='student'){
    includeSelf=false;
    document.getElementById('unifiedSelf').checked=false;
  }
  let proxyNames=document.getElementById('unifiedProxyNames').value.trim();
  let proxyStudentPairs=[];
  let proxyStaffPairs=[];
  let generalProxyPeople=[];

  const currentEv=events.find(x=>x.id===signupChoiceEventId);
  if(signupChoiceType==='general' && currentEv && currentEv.relay_enabled){
    generalProxyPeople=getGeneralProxyPeople();
    proxyNames='';
  }

  if(signupChoiceType==='dharma'){
    const role=selectValue('unifiedDharmaRole');
    if(role==='student') proxyStudentPairs=getProxyStudentPairs();
    if(role==='staff') proxyStaffPairs=getProxyStaffPairs();
    proxyNames='';
  }

  if(!includeSelf && !proxyNames && !proxyStudentPairs.length && !proxyStaffPairs.length && !generalProxyPeople.length){
    showMsg('請勾選本人報名，或新增至少一位要代報的人員',false);
    return;
  }

  for(const p of generalProxyPeople){
    if(!p.name){
      showMsg('每一位代報人員都要填寫姓名',false);
      return;
    }
  }

  const ev=events.find(x=>x.id===signupChoiceEventId);
  const common={event_id:signupChoiceEventId,user_id:profile.userId,display_name:profile.displayName};

  if(signupChoiceType==='dharma'){
    common.dharma_role=selectValue('unifiedDharmaRole');
    if(common.dharma_role==='student'){
      for(const p of proxyStudentPairs){
        if(!p.name || !p.leader_name || !p.attendance_option){
          showMsg('每位代報班員都要填寫班員姓名、參班方式與帶班人員',false);
          return;
        }
      }
    }else{
      common.day1_group=selectValue('unifiedDay1');
      common.day2_group=selectValue('unifiedDay2');
      if(includeSelf && !common.day1_group&&!common.day2_group){
        showMsg('本人若報名辦事人員，至少要選擇一天的工作',false);
        return;
      }
      for(const p of proxyStaffPairs){
        if(!p.name){showMsg('請填寫辦事人員姓名',false);return;}
        if(!p.day1_group&&!p.day2_group){showMsg(p.name+' 至少要選擇一天的工作',false);return;}
      }
    }
  }

  setBusy(btn,true,'送出中…');
  const msgs=[], errs=[];
  try{
    if(includeSelf){
      const selfBody={...common};
      if(signupChoiceType==='general' && ev && ev.relay_enabled){
        selfBody.relay_items=relayValuesByMode('unifiedSelfRelayItems',ev.relay_mode||'text');
      }
      try{
        const d=await api('/api/liff/signup',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(selfBody)
        });
        msgs.push('本人：'+d.message);
      }catch(e){
        errs.push('本人：'+e.message);
      }
    }

    if(signupChoiceType==='general' && ev && ev.relay_enabled && generalProxyPeople.length){
      for(const p of generalProxyPeople){
        const proxyBody={...common,names:p.name,relay_items:p.relay_items};
        try{
          const d=await api('/api/liff/proxy',{
            method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(proxyBody)
          });
          msgs.push(p.name+'：'+d.message);
        }catch(e){
          errs.push(p.name+'：'+e.message);
        }
      }
    }else if(proxyNames || proxyStudentPairs.length || proxyStaffPairs.length){
      const proxyBody={...common,names:proxyNames};
      if(signupChoiceType==='dharma' && common.dharma_role==='student'){
        proxyBody.student_pairs=proxyStudentPairs;
      }
      if(signupChoiceType==='dharma' && common.dharma_role==='staff'){
        proxyBody.staff_pairs=proxyStaffPairs;
        delete proxyBody.day1_group;
        delete proxyBody.day2_group;
      }
      try{
        const d=await api('/api/liff/proxy',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(proxyBody)
        });
        msgs.push('代報：'+d.message);
      }catch(e){
        errs.push('代報：'+e.message);
      }
    }

    if(msgs.length){
      signupChoiceDialog.close();
      showMsg(msgs.join('；')+(errs.length?'；'+errs.join('；'):''),!errs.length);
      await loadEvents();
    }else{
      showMsg(errs.join('；')||'報名失敗',false);
    }
  }finally{
    setBusy(btn,false);
  }
}

function openRelaySelf(id,title){
  const ev=events.find(x=>x.id===id);
  relaySelfEventId=id;
  document.getElementById('relaySelfTitle').textContent='本人報名｜'+title;
  document.getElementById('relaySelfLabel').textContent=((ev&&ev.relay_label)||'接龍項目')+'（可填多項）';
  document.getElementById('relaySelfItems').innerHTML='';
  addRelayInput('relaySelfItems');
  relaySelfDialog.showModal();
}
async function submitRelaySelf(btn){
  setBusy(btn,true,'送出中…');
  try{
    const d=await api('/api/liff/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      event_id:relaySelfEventId,user_id:profile.userId,display_name:profile.displayName,
      relay_items:relayValues('relaySelfItems')
    })});
    relaySelfDialog.close();showMsg(d.message);await loadEvents();
  }catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}
}
async function selfSignup(id,btn){setBusy(btn,true,'送出中…');try{const d=await api('/api/liff/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId,display_name:profile.displayName})});showMsg(d.message);await loadEvents()}catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}}
function openProxy(id,title,eventType){
  proxyEventId=id;proxyEventType=eventType||'general';
  const ev=events.find(x=>x.id===id);
  document.getElementById('proxyTitle').textContent='代人報名｜'+title;
  document.getElementById('proxyNames').value='';
  document.getElementById('proxyDharmaOptions').style.display=proxyEventType==='dharma'?'block':'none';
  const relayOn=proxyEventType==='general'&&ev&&ev.relay_enabled;
  document.getElementById('proxyRelayOptions').style.display=relayOn?'block':'none';
  document.getElementById('proxyRelayLabel').textContent=((ev&&ev.relay_label)||'接龍項目')+'（可填多項）';
  document.getElementById('proxyRelayItems').innerHTML='';
  if(relayOn)addRelayInput('proxyRelayItems');
  if(proxyEventType==='dharma'){
    document.getElementById('proxyDharmaRole').value='student';
    document.getElementById('proxyDay1').selectedIndex=0;
    document.getElementById('proxyDay2').selectedIndex=0;
    toggleProxyDharmaFields();
  }
  proxyDialog.showModal()
}
async function submitProxy(btn){
  const names=document.getElementById('proxyNames').value.trim();
  if(!names){showMsg('請輸入姓名',false);return}
  const body={event_id:proxyEventId,names:names,user_id:profile.userId,display_name:profile.displayName};
  if(proxyEventType==='dharma'){
    body.dharma_role=selectValue('proxyDharmaRole');
    if(body.dharma_role==='student'){
      body.attendance_option=selectValue('proxyAttendance');
    }else{
      body.day1_group=selectValue('proxyDay1');
      body.day2_group=selectValue('proxyDay2');
      if(!body.day1_group&&!body.day2_group){showMsg('辦事人員至少要選擇一天的組別',false);return}
    }
  }else if(document.getElementById('proxyRelayOptions').style.display!=='none'){
    body.relay_items=relayValues('proxyRelayItems');
  }
  setBusy(btn,true,'送出中…');
  try{
    const d=await api('/api/liff/proxy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    proxyDialog.close();showMsg(d.message);await loadEvents()
  }catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}
}
function fillAdminDharmaGroups(){
  const opts=DHARMA_GROUPS.map(g=>`<option value="${esc(g)}">${esc(g||'不參加')}</option>`).join('');
  document.getElementById('adminDay1').innerHTML=opts;
  document.getElementById('adminDay2').innerHTML=opts;
}
function toggleAdminDharmaFields(){
  const staff=document.getElementById('adminDharmaRole').value==='staff';
  document.getElementById('adminStudentEditFields').style.display=staff?'none':'block';
  document.getElementById('adminStaffEditFields').style.display=staff?'block':'none';
}
function openAdminSignupEdit(eventId,signupId){
  if(!adminMode)return;
  const ev=events.find(x=>x.id===eventId);
  const p=lastListPeople.find(x=>x.id===signupId);
  if(!ev||!p)return;

  adminEditEventId=eventId;
  adminEditSignupId=signupId;
  adminEditEventType=ev.event_type||'general';
  adminEditRelayEnabled=!!ev.relay_enabled;
  adminEditRelayLabel=ev.relay_label||'接龍項目';
  adminEditRelayMode=ev.relay_mode==='purchase'?'custom_purchase':(ev.relay_mode||'text');

  document.getElementById('adminSignupEditTitle').textContent='編輯報名｜'+p.name;
  document.getElementById('adminSignupName').value=p.name||'';

  const isDharma=adminEditEventType==='dharma';
  document.getElementById('adminGeneralEditBox').style.display=isDharma?'none':'block';
  document.getElementById('adminDharmaEditBox').style.display=isDharma?'block':'none';

  document.getElementById('adminRelayEditBox').style.display=(!isDharma&&adminEditRelayEnabled)?'block':'none';
  document.getElementById('adminRelayEditLabel').textContent=adminEditRelayLabel+'（可填多項）';
  document.getElementById('adminRelayEditItems').innerHTML='';
  if(!isDharma&&adminEditRelayEnabled){
    const addBtn=document.querySelector('#adminRelayEditBox .secondary');
    if(addBtn){
      addBtn.style.display=adminEditRelayMode==='fixed_purchase'?'none':'block';
      addBtn.onclick=()=>addRelayInputByMode('adminRelayEditItems',adminEditRelayMode);
    }
    if(adminEditRelayMode==='fixed_purchase'){
      addFixedPurchaseInputs('adminRelayEditItems',(ev&&ev.purchase_catalog)||[],p.relay_items||[]);
    }else{
      (p.relay_items&&p.relay_items.length?p.relay_items:[isPurchaseMode(adminEditRelayMode)?{}:'']).forEach(x=>addRelayInputByMode('adminRelayEditItems',adminEditRelayMode,x));
    }
  }

  if(isDharma){
    fillAdminDharmaGroups();
    const role=p.dharma_role || (p.attendance_option?'student':'staff');
    document.getElementById('adminDharmaRole').value=role;
    document.getElementById('adminAttendance').value=p.attendance_option||'上兩天';
    document.getElementById('adminLeaderName').value=p.leader_name||'';
    document.getElementById('adminDay1').value=p.day1_group||'';
    document.getElementById('adminDay2').value=p.day2_group||'';
    toggleAdminDharmaFields();
  }

  adminSignupEditDialog.showModal();
}
async function submitAdminSignupEdit(btn){
  const name=document.getElementById('adminSignupName').value.trim();
  if(!name){showMsg('姓名不能留空',false);return}

  const body={
    event_id:adminEditEventId,
    signup_id:adminEditSignupId,
    user_id:profile.userId,
    person_name:name
  };

  if(adminEditEventType==='dharma'){
    body.dharma_role=selectValue('adminDharmaRole');
    if(body.dharma_role==='student'){
      body.attendance_option=selectValue('adminAttendance');
      body.leader_name=document.getElementById('adminLeaderName').value.trim();
      if(!body.leader_name){showMsg('請填寫帶班人員',false);return}
    }else{
      body.day1_group=selectValue('adminDay1');
      body.day2_group=selectValue('adminDay2');
      if(!body.day1_group&&!body.day2_group){
        showMsg('辦事人員至少要選擇一天的組別',false);return;
      }
    }
  }else if(adminEditRelayEnabled){
    body.relay_items=relayValuesByMode('adminRelayEditItems',adminEditRelayMode);
  }

  setBusy(btn,true,'儲存中…');
  try{
    const d=await api('/api/liff/admin-signup/update',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)
    });
    adminSignupEditDialog.close();
    showMsg(d.message);
    const ev=events.find(x=>x.id===adminEditEventId);
    await showList(adminEditEventId,ev?ev.title:'活動');
    await loadEvents();
  }catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}
}
function adminButtons(id,p,title){
  if(!adminMode)return '';
  return `<button class="secondary" style="padding:5px 9px;margin-left:8px" onclick="openAdminSignupEdit(${id},${p.id})">編輯</button>
  <button class="light" style="padding:5px 9px;margin-left:8px;color:#a22" onclick="cancelSignup(${id},${p.id},'${String(p.name).replace(/'/g,"\\'")}','${String(title).replace(/'/g,"\\'")}')">刪除</button>`;
}

function cancelBtn(id,p,title){
  if(adminMode)return '';
  return p.can_cancel?`<button class="light" style="padding:5px 9px;margin-left:8px;color:#a22" onclick="cancelSignup(${id},${p.id},'${String(p.name).replace(/'/g,"\\'")}','${String(title).replace(/'/g,"\\'")}')">取消</button>`:'';
}
function personText(p){
  const proxy=p.proxy_by_name ? `（${esc(p.proxy_by_name)} 代報）` : '';
  return `${esc(p.name)}${proxy}`;
}
function relayItemsText(p,mode='text'){
  if(isPurchaseMode(mode)){
    return (p.relay_items||[]).map(x=>`${esc(x.item||'')}｜${moneyText(x.unit_price)} × ${moneyText(x.qty)} = ${moneyText(x.subtotal??(Number(x.unit_price||0)*Number(x.qty||0)))} 元`).join('<br>');
  }
  return (p.relay_items||[]).map(esc).join('、');
}
function fixedPurchaseCompactText(p){
  const items=p.relay_items||[];
  const total=items.reduce((s,x)=>s+Number(x.subtotal??(Number(x.unit_price||0)*Number(x.qty||0))),0);
  const proxy=p.proxy_by_name?`（${esc(p.proxy_by_name)} 代報）`:'';
  if(items.length===1){
    const x=items[0];
    return `${esc(p.name)} × ${moneyText(x.qty)}｜${moneyText(total)} 元${proxy}`;
  }
  const detail=items.map(x=>`${esc(x.item||'')}×${moneyText(x.qty)}`).join('、');
  return `${esc(p.name)}｜${detail}｜${moneyText(total)} 元${proxy}`;
}
let relayEditMode='text';
function openRelayEditById(eventId,signupId,label){
  const p=lastListPeople.find(x=>x.id===signupId);
  const ev=events.find(x=>x.id===eventId);
  if(!p)return;
  relayEditEventId=eventId; relayEditSignupId=signupId;
  relayEditMode=((ev&&ev.relay_mode)==='purchase'?'custom_purchase':((ev&&ev.relay_mode)||'text'));
  document.getElementById('relayEditTitle').textContent='修改接龍｜'+p.name;
  const fixed=relayEditMode==='fixed_purchase';
  document.getElementById('relayEditLabel').textContent=(label||'接龍項目')+(fixed?'（請修改數量）':(isPurchaseMode(relayEditMode)?'（品項／單價／數量）':'（可填多項）'));
  document.getElementById('relayEditItems').innerHTML='';
  const btn=document.querySelector('#relayEditDialog .secondary');
  if(btn){
    btn.style.display=fixed?'none':'block';
    btn.onclick=()=>addRelayInputByMode('relayEditItems',relayEditMode);
  }
  if(fixed){
    addFixedPurchaseInputs('relayEditItems',(ev&&ev.purchase_catalog)||[],p.relay_items||[]);
  }else{
    (p.relay_items&&p.relay_items.length?p.relay_items:[isPurchaseMode(relayEditMode)?{}:'']).forEach(x=>addRelayInputByMode('relayEditItems',relayEditMode,x));
  }
  relayEditDialog.showModal();
}
async function submitRelayEdit(btn){
  setBusy(btn,true,'儲存中…');
  try{
    const d=await api('/api/liff/relay-items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      event_id:relayEditEventId,signup_id:relayEditSignupId,user_id:profile.userId,
      relay_items:relayValuesByMode('relayEditItems',relayEditMode)
    })});
    relayEditDialog.close();showMsg(d.message);
    const ev=events.find(x=>x.id===relayEditEventId);
    await showList(relayEditEventId,ev?ev.title:'活動');
  }catch(e){showMsg(e.message,false)}finally{setBusy(btn,false)}
}
function dharmaPersonText(p){ return personText(p); }
function dharmaListHtml(id,title,people){
  let html='';
  const students=people.filter(p=>p.dharma_role==='student'||(!p.dharma_role&&p.attendance_option));
  if(students.length){
    html+=`<h4 style="margin:8px 0">班員（共 ${students.length} 人）</h4>`;
    ['上兩天','第一天','補第二天','加開第一天'].forEach(opt=>{
      const a=students.filter(p=>p.attendance_option===opt);
      if(a.length) html+=`<div style="margin:8px 0"><b>${opt}（${a.length}）</b><br>`+a.map(p=>{const proxy=p.proxy_by_name?`（${esc(p.proxy_by_name)} 代報）`:'';const leader=p.leader_name?` <span style="color:#666">→ ${esc(p.leader_name)}</span>`:'';return `<div style="margin:6px 0">${esc(p.name)}${leader}${proxy}${adminButtons(id,p,title)}${cancelBtn(id,p,title)}</div>`}).join('')+'</div>';
    });
  }
  const staff=people.filter(p=>p.dharma_role==='staff');
  ['day1_group','day2_group'].forEach((key,idx)=>{
    const day=idx===0?'第一天':'第二天';
    const active=staff.filter(p=>p[key]);
    if(!active.length)return;
    html+=`<h4 style="margin:16px 0 6px">${day}辦事人員（${active.length}）</h4>`;
    DHARMA_GROUPS.filter(Boolean).forEach(g=>{
      const a=active.filter(p=>p[key]===g);
      if(a.length) html+=`<div style="margin:8px 0"><b>${g}（${a.length}）</b><br>`+a.map(p=>`<div style="margin:6px 0">${dharmaPersonText(p)}${adminButtons(id,p,title)}${cancelBtn(id,p,title)}</div>`).join('')+'</div>';
    });
  });
  return html||'目前尚無人報名';
}
async function showList(id,title){
  try{
    const d=await api('/api/liff/list?event_id='+id+'&user_id='+encodeURIComponent(profile.userId));
    document.getElementById('listTitle').textContent='報名名單｜'+title;
    lastListPeople=d.people||[];
    if(!d.people.length){
      document.getElementById('listBody').innerHTML='目前尚無人報名';
    }else if(d.event_type==='dharma'){
      document.getElementById('listBody').innerHTML=dharmaListHtml(id,title,d.people);
    }else{
      document.getElementById('listBody').innerHTML=d.people.map((p,i)=>{
        const b=cancelBtn(id,p,title);
        const edit=!adminMode&&d.relay_enabled&&p.can_cancel
          ? `<button class="secondary" style="padding:5px 9px;margin-left:8px" onclick="openRelayEditById(${id},${p.id},'${String(d.relay_label||'接龍項目').replace(/'/g,"\\'")}')">修改接龍</button>`:'';
        const admin=adminButtons(id,p,title);

        if(d.relay_enabled && d.relay_mode==='fixed_purchase'){
          const compact=fixedPurchaseCompactText(p);
          return `<div style="margin:10px 0;display:flex;justify-content:space-between;align-items:center;gap:8px">
            <span>${i+1}. ${compact}</span><span>${admin}${edit}${b}</span>
          </div>`;
        }

        const itemTotal=(isPurchaseMode(d.relay_mode||'text')&&p.relay_items)?p.relay_items.reduce((s,x)=>s+Number(x.subtotal??(Number(x.unit_price||0)*Number(x.qty||0))),0):0;
        const items=d.relay_enabled&&p.relay_items&&p.relay_items.length
          ? `<div style="font-size:14px;color:#555;margin:3px 0 0 18px">${esc(d.relay_label||'接龍項目')}：${relayItemsText(p,d.relay_mode||'text')}${isPurchaseMode(d.relay_mode||'text')?`<br><b>個人合計：${moneyText(itemTotal)} 元</b>`:''}</div>`:'';
        return `<div style="margin:9px 0"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><span>${i+1}. ${personText(p)}</span><span>${admin}${edit}${b}</span></div>${items}</div>`
      }).join('');
      if(d.relay_enabled&&isPurchaseMode(d.relay_mode||'text')){
        const grand=d.people.reduce((sum,p)=>sum+(p.relay_items||[]).reduce((s,x)=>s+Number(x.subtotal??(Number(x.unit_price||0)*Number(x.qty||0))),0),0);
        document.getElementById('listBody').innerHTML += `<div style="margin-top:14px;padding-top:10px;border-top:1px solid #ddd"><b>全部總金額：${moneyText(grand)} 元</b></div>`;
      }
    }
    listDialog.showModal()
  }catch(e){showMsg(e.message,false)}
}
async function cancelSignup(eventId,signupId,name,title){if(!confirm('確定要取消「'+name+'」的報名嗎？'))return;try{const d=await api('/api/liff/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:eventId,signup_id:signupId,user_id:profile.userId})});showMsg(d.message);await showList(eventId,title);await loadEvents()}catch(e){showMsg(e.message,false)}}
init();
</script></body></html>"""


@app.route("/liff", methods=["GET"])
def liff_page():
    # 不在伺服器這一層檢查 g/s。
    # LINE LIFF 有時會把原本參數放進 liff.state，再由前端解析。
    # 真正的群組簽章與停權檢查仍由 API 層執行。
    return Response(LIFF_HTML.replace("__LIFF_ID__", LIFF_ID), mimetype="text/html")



def require_group_from_request(allow_disabled=False):
    group_id = request.args.get("g", "")
    sig = request.args.get("sig", "")
    if not valid_group_signature(group_id, sig):
        abort(403)
    if not allow_disabled and group_entry_disabled(group_id):
        return abort(403, description="此群組報名入口已停用")
    return group_id



@app.route("/api/liff/me", methods=["GET"])
def api_liff_me():
    group_id = require_group_from_request(allow_disabled=True)
    user_id = request.args.get("user_id", "")
    return jsonify({
        "is_admin": is_admin(user_id),
        "group_disabled": group_entry_disabled(group_id),
    })


@app.route("/api/liff/group-access", methods=["POST"])
def api_liff_group_access():
    group_id = require_group_from_request(allow_disabled=True)
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    disabled = bool(data.get("disabled"))

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理此群組報名入口的權限"}), 403

    set_group_entry_disabled(group_id, disabled)
    return jsonify({
        "message": "已停用此群組報名入口" if disabled else "已重新啟用此群組報名入口",
        "group_disabled": disabled,
    })


@app.route("/api/liff/events/create", methods=["POST"])
def api_liff_create_event():
    group_id = require_group_from_request()

    # 此頁用 multipart/form-data，才能同時送文字與 DM 圖片
    user_id = str(request.form.get("user_id", "")).strip()
    title = str(request.form.get("title", "")).strip()
    event_type = str(request.form.get("event_type", "general")).strip() or "general"
    relay_enabled = str(request.form.get("relay_enabled", "0")).strip() in {"1","true","True","on"}
    relay_label = str(request.form.get("relay_label", "")).strip() or "接龍項目"
    relay_mode = str(request.form.get("relay_mode", "text")).strip()
    if relay_mode == "purchase":
        relay_mode = "custom_purchase"
    if relay_mode not in {"text", "fixed_purchase", "custom_purchase"}:
        relay_mode = "text"
    purchase_catalog = parse_purchase_catalog(request.form.get("purchase_catalog", "[]"))
    if relay_mode == "fixed_purchase" and relay_enabled and not purchase_catalog:
        return jsonify({"error": "固定商品模式請至少設定一項商品與單價"}), 400
    event_date = str(request.form.get("event_date", "")).strip() or None
    location = str(request.form.get("location", "")).strip() or None
    registration_deadline = str(request.form.get("registration_deadline", "")).strip() or None
    description = str(request.form.get("description", "")).strip() or None

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403

    if not title:
        return jsonify({"error": "請輸入活動名稱"}), 400

    dm_image_url = None
    dm = request.files.get("dm")

    try:
        if dm and dm.filename:
            dm_image_url = upload_dm_to_supabase(dm)

        create_event(
            group_id=group_id,
            title=title,
            event_date=event_date,
            location=location,
            description=description,
            dm_image_url=dm_image_url,
            registration_deadline=registration_deadline,
            event_type=event_type,
            relay_enabled=relay_enabled,
            relay_label=relay_label,
            relay_mode=relay_mode,
            purchase_catalog=purchase_catalog,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("Create event failed")
        return jsonify({"error": f"建立活動失敗：{str(e)}"}), 500

    return jsonify({"message": f"已新增活動：{title}"})


@app.route("/api/liff/events/update", methods=["POST"])
def api_liff_update_event():
    group_id = require_group_from_request()

    user_id = str(request.form.get("user_id", "")).strip()
    event_id = int(request.form.get("event_id", "0") or 0)
    title = str(request.form.get("title", "")).strip()
    event_type = str(request.form.get("event_type", "general")).strip() or "general"
    relay_enabled = str(request.form.get("relay_enabled", "0")).strip() in {"1","true","True","on"}
    relay_label = str(request.form.get("relay_label", "")).strip() or "接龍項目"
    relay_mode = str(request.form.get("relay_mode", "text")).strip()
    if relay_mode == "purchase":
        relay_mode = "custom_purchase"
    if relay_mode not in {"text", "fixed_purchase", "custom_purchase"}:
        relay_mode = "text"
    purchase_catalog = parse_purchase_catalog(request.form.get("purchase_catalog", "[]"))
    if relay_mode == "fixed_purchase" and relay_enabled and not purchase_catalog:
        return jsonify({"error": "固定商品模式請至少設定一項商品與單價"}), 400
    event_date = str(request.form.get("event_date", "")).strip() or None
    location = str(request.form.get("location", "")).strip() or None
    registration_deadline = str(request.form.get("registration_deadline", "")).strip() or None
    description = str(request.form.get("description", "")).strip() or None

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    if not title:
        return jsonify({"error": "請輸入活動名稱"}), 400

    dm_image_url = ev.get("dm_image_url")
    dm = request.files.get("dm")

    try:
        if dm and dm.filename:
            dm_image_url = upload_dm_to_supabase(dm)

        conn = db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE line_events
            SET title=%s,
                event_date=%s,
                location=%s,
                registration_deadline=%s,
                description=%s,
                dm_image_url=%s,
                event_type=%s,
                relay_enabled=%s,
                relay_label=%s,
                relay_mode=%s,
                purchase_catalog=%s,
                registration_force_open=FALSE,
                registration_manual_closed=FALSE
            WHERE id=%s AND group_id=%s
            """,
            (
                title,
                event_date,
                location,
                registration_deadline,
                description,
                dm_image_url,
                event_type,
                bool(relay_enabled) if event_type == 'general' else False,
                relay_label if relay_enabled and event_type == 'general' else None,
                relay_mode if relay_enabled and event_type == 'general' else 'text',
                json.dumps(purchase_catalog, ensure_ascii=False)
                if relay_enabled and event_type == 'general' and relay_mode == 'fixed_purchase'
                else None,
                event_id,
                group_id,
            ),
        )
        conn.commit()
        cur.close()
        release_db(conn)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("Update event failed")
        return jsonify({"error": f"更新活動失敗：{str(e)}"}), 500

    return jsonify({"message": f"已更新活動：{title}"})


@app.route("/api/liff/events/close-registration", methods=["POST"])
def api_liff_close_registration():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    event_id = int(data.get("event_id", 0) or 0)

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    publish = bool(data.get("publish"))
    # 截止日前：是否公布由管理員決定。
    # 截止後若曾重新開放：再次關閉時直接公布最新完整名單。
    deadline = ev.get("registration_deadline")
    if isinstance(deadline, str):
        deadline = date.fromisoformat(deadline[:10])
    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    is_after_deadline = bool(deadline and now_tw.date() > deadline)
    if is_after_deadline and ev.get("reopened_after_close"):
        publish = True

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE line_events
           SET registration_manual_closed=TRUE,
               registration_force_open=FALSE,
               reopened_after_close=FALSE
           WHERE id=%s AND group_id=%s""",
        (event_id, group_id),
    )
    conn.commit()
    cur.close()
    release_db(conn)

    if publish:
        ev = get_event_by_id(group_id, event_id)
        try:
            _publish_event_list(group_id, ev, force=True)
        except Exception as e:
            app.logger.exception("Failed to publish final list for event %s", event_id)
            # 關閉報名本身已成功；公布失敗不能讓前端誤以為整個關閉失敗。
            return jsonify({
                "message": "報名已關閉；但名單傳送到 LINE 群組失敗，請稍後按「重新公布最新名單」再試一次。",
                "registration_closed": True,
                "publish_failed": True
            }), 200
        return jsonify({
            "message": f"已關閉「{ev['title']}」並立即公布最新名單到 LINE 群組",
            "registration_closed": True,
            "publish_failed": False
        })

    return jsonify({"message": f"已關閉報名：{ev['title']}（尚未公布名單）"})


@app.route("/api/liff/events/reopen", methods=["POST"])
def api_liff_reopen_event():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    event_id = int(data.get("event_id", 0) or 0)

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE line_events
           SET registration_force_open=TRUE,
               registration_manual_closed=FALSE,
               reopened_after_close=TRUE,
               list_published_at=NULL
           WHERE id=%s AND group_id=%s""",
        (event_id, group_id),
    )
    conn.commit()
    cur.close()
    release_db(conn)
    return jsonify({"message": f"已重新開放報名：{ev['title']}"})


@app.route("/api/liff/events/close", methods=["POST"])
def api_liff_close_event():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    event_id = int(data.get("event_id", 0) or 0)
    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403
    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE line_events SET active=FALSE WHERE id=%s AND group_id=%s", (event_id, group_id))
    conn.commit()
    cur.close()
    release_db(conn)
    return jsonify({"message": f"已結束活動：{ev['title']}"})


@app.route("/api/liff/events", methods=["GET"])
def api_liff_events():
    group_id = require_group_from_request()
    result = []

    for ev in list_active_events(group_id):
        result.append({
            "id": ev["id"],
            "title": ev["title"],
            "count": (
                ev.get("student_count", 0)
                if (ev.get("event_type") or "general") == "dharma"
                else ev.get("signup_count", len(list_signups(ev["id"])))
            ),
            "student_count": ev.get("student_count", 0),
            "event_date": ev.get("event_date").isoformat() if ev.get("event_date") else None,
            "location": ev.get("location"),
            "description": ev.get("description"),
            "dm_image_url": ev.get("dm_image_url"),
            "registration_deadline": ev.get("registration_deadline").isoformat() if ev.get("registration_deadline") else None,
            "event_type": ev.get("event_type") or "general",
            "registration_open": registration_is_open(ev),
            "registration_force_open": bool(ev.get("registration_force_open")),
            "registration_manual_closed": bool(ev.get("registration_manual_closed")),
            "auto_publish_list": bool(ev.get("auto_publish_list")),
            "list_published_at": ev.get("list_published_at").isoformat() if ev.get("list_published_at") else None,
            "reopened_after_close": bool(ev.get("reopened_after_close")),
            "relay_enabled": bool(ev.get("relay_enabled")),
            "relay_label": ev.get("relay_label") or "接龍項目",
            "relay_mode": ev.get("relay_mode") or "text",
            "purchase_catalog": parse_purchase_catalog(ev.get("purchase_catalog")),
        })

    return jsonify({"events": result})


@app.route("/api/liff/event", methods=["GET"])
def api_liff_event_detail():
    group_id = require_group_from_request()
    event_id = int(request.args.get("event_id", "0") or 0)

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    return jsonify({
        "event": {
            "id": ev["id"],
            "title": ev["title"],
            "event_date": ev.get("event_date").isoformat() if ev.get("event_date") else None,
            "location": ev.get("location"),
            "description": ev.get("description"),
            "dm_image_url": ev.get("dm_image_url"),
            "registration_deadline": ev.get("registration_deadline").isoformat() if ev.get("registration_deadline") else None,
            "event_type": ev.get("event_type") or "general",
            "registration_open": registration_is_open(ev),
            "registration_force_open": bool(ev.get("registration_force_open")),
            "registration_manual_closed": bool(ev.get("registration_manual_closed")),
            "auto_publish_list": bool(ev.get("auto_publish_list")),
            "list_published_at": ev.get("list_published_at").isoformat() if ev.get("list_published_at") else None,
            "reopened_after_close": bool(ev.get("reopened_after_close")),
            "relay_enabled": bool(ev.get("relay_enabled")),
            "relay_label": ev.get("relay_label") or "接龍項目",
            "relay_mode": ev.get("relay_mode") or "text",
            "purchase_catalog": parse_purchase_catalog(ev.get("purchase_catalog")),
        }
    })


@app.route("/api/liff/signup", methods=["POST"])
def api_liff_signup():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    event_id = int(data.get("event_id", 0))
    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404
    user_id = str(data.get("user_id", "")).strip()
    display_name = str(data.get("display_name", "")).strip()
    attendance_option = str(data.get("attendance_option", "")).strip() or None
    dharma_role = str(data.get("dharma_role", "")).strip() or None
    day1_group = str(data.get("day1_group", "")).strip() or None
    day2_group = str(data.get("day2_group", "")).strip() or None
    leader_name = str(data.get("leader_name", "")).strip() or None
    relay_items = clean_relay_for_event(ev, data.get("relay_items", []))
    if not user_id or not display_name:
        return jsonify({"error": "無法取得 LINE 使用者資料"}), 400
    if not registration_is_open(ev):
        return jsonify({"error": "此活動報名已截止"}), 403
    if (ev.get("event_type") or "general") == "dharma":
        valid_groups = {"服務", "文書", "接待", "總務", "辦道", "壇務", "炊事"}
        if dharma_role == "student":
            if attendance_option not in {"上兩天", "第一天", "補第二天", "加開第一天"}:
                return jsonify({"error": "請選擇班員參班方式"}), 400
            leader_name = None
            day1_group = day2_group = None
        elif dharma_role == "staff":
            attendance_option = None
            if day1_group and day1_group not in valid_groups:
                return jsonify({"error": "第一天組別不正確"}), 400
            if day2_group and day2_group not in valid_groups:
                return jsonify({"error": "第二天組別不正確"}), 400
            if not day1_group and not day2_group:
                return jsonify({"error": "辦事人員至少要選擇一天的組別"}), 400
        else:
            return jsonify({"error": "請選擇班員或辦事人員"}), 400
    else:
        attendance_option = dharma_role = day1_group = day2_group = leader_name = None
        if not ev.get('relay_enabled'):
            relay_items = []
    if not add_signup(event_id, display_name, "self", line_user_id=user_id,
                      attendance_option=attendance_option, dharma_role=dharma_role,
                      day1_group=day1_group, day2_group=day2_group, relay_items=relay_items,
                      leader_name=leader_name):
        return jsonify({"error": f"{display_name} 已經報名過了"}), 409
    return jsonify({"message": "報名成功"})


@app.route("/api/liff/proxy", methods=["POST"])
def api_liff_proxy():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    event_id = int(data.get("event_id", 0))
    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404
    if not registration_is_open(ev):
        return jsonify({"error": "此活動報名已截止"}), 403

    display_name = str(data.get("display_name", "")).strip()
    user_id = str(data.get("user_id", "")).strip()
    attendance_option = str(data.get("attendance_option", "")).strip() or None
    dharma_role = str(data.get("dharma_role", "")).strip() or None
    day1_group = str(data.get("day1_group", "")).strip() or None
    day2_group = str(data.get("day2_group", "")).strip() or None
    relay_items = clean_relay_for_event(ev, data.get("relay_items", []))
    event_type = ev.get("event_type") or "general"
    added, dup = 0, []

    if event_type == "dharma":
        valid_groups = {"服務", "文書", "接待", "總務", "辦道", "壇務", "炊事"}
        if dharma_role == "student":
            pairs = data.get("student_pairs", [])
            if not isinstance(pairs, list):
                pairs = []
            cleaned = []
            valid_attendance = {"上兩天", "第一天", "補第二天", "加開第一天"}
            for p in pairs:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name", "")).strip()
                p_attendance = str(p.get("attendance_option", "")).strip()
                leader = str(p.get("leader_name", "")).strip()
                if not name or not leader or p_attendance not in valid_attendance:
                    return jsonify({"error": "每位班員都要填寫姓名、參班方式與帶班人員"}), 400
                cleaned.append((name, p_attendance, leader))
            if not cleaned:
                return jsonify({"error": "請輸入至少一位班員"}), 400
            for name, p_attendance, leader in cleaned:
                if add_signup(event_id, name, "proxy",
                              proxy_by_user_id=user_id, proxy_by_name=display_name,
                              attendance_option=p_attendance, dharma_role="student",
                              day1_group=None, day2_group=None, relay_items=[],
                              leader_name=leader):
                    added += 1
                else:
                    dup.append(name)
        elif dharma_role == "staff":
            pairs = data.get("staff_pairs", [])
            if not isinstance(pairs, list):
                pairs = []

            cleaned = []
            for p in pairs:
                if not isinstance(p, dict):
                    continue
                name = str(p.get("name", "")).strip()
                p_day1 = str(p.get("day1_group", "")).strip() or None
                p_day2 = str(p.get("day2_group", "")).strip() or None
                if not name:
                    return jsonify({"error": "請填寫辦事人員姓名"}), 400
                if p_day1 and p_day1 not in valid_groups:
                    return jsonify({"error": f"{name} 的第一天工作不正確"}), 400
                if p_day2 and p_day2 not in valid_groups:
                    return jsonify({"error": f"{name} 的第二天工作不正確"}), 400
                if not p_day1 and not p_day2:
                    return jsonify({"error": f"{name} 至少要選擇一天的工作"}), 400
                cleaned.append((name, p_day1, p_day2))

            # Backward-compatible fallback for older page versions.
            if not cleaned:
                names = split_names(str(data.get("names", "")).strip())
                if names:
                    if day1_group and day1_group not in valid_groups:
                        return jsonify({"error": "第一天組別不正確"}), 400
                    if day2_group and day2_group not in valid_groups:
                        return jsonify({"error": "第二天組別不正確"}), 400
                    if not day1_group and not day2_group:
                        return jsonify({"error": "辦事人員至少要選擇一天的工作"}), 400
                    cleaned = [(name, day1_group, day2_group) for name in names]

            if not cleaned:
                return jsonify({"error": "請輸入至少一位辦事人員"}), 400

            for name, p_day1, p_day2 in cleaned:
                if add_signup(event_id, name, "proxy",
                              proxy_by_user_id=user_id, proxy_by_name=display_name,
                              attendance_option=None, dharma_role="staff",
                              day1_group=p_day1, day2_group=p_day2,
                              relay_items=[], leader_name=None):
                    added += 1
                else:
                    dup.append(name)
        else:
            return jsonify({"error": "請選擇班員或辦事人員"}), 400
    else:
        names = split_names(str(data.get("names", "")).strip())
        if not names:
            return jsonify({"error": "請輸入至少一個姓名"}), 400
        if not ev.get("relay_enabled"):
            relay_items = []
        for name in names:
            if add_signup(event_id, name, "proxy",
                          proxy_by_user_id=user_id, proxy_by_name=display_name,
                          relay_items=relay_items, leader_name=None):
                added += 1
            else:
                dup.append(name)

    msg = f"已成功加入 {added} 人" if not dup else f"已加入 {added} 人；重複：{'、'.join(dup)}"
    return jsonify({"message": msg})


@app.route("/api/liff/list", methods=["GET"])
def api_liff_list():
    group_id = require_group_from_request()
    event_id = int(request.args.get("event_id", "0") or 0)
    user_id = request.args.get("user_id", "")

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    people = []
    for row in list_signups(event_id):
        if row["signup_type"] == "proxy":
            opt = f"｜{row['attendance_option']}" if row.get("attendance_option") else ""
            label = f"{row['person_name']}{opt}（{row['proxy_by_name']} 代報）"
            can_cancel = bool(
                user_id and row["proxy_by_user_id"] == user_id
            )
        else:
            opt = f"｜{row['attendance_option']}" if row.get("attendance_option") else ""
            label = f"{row['person_name']}{opt}"
            can_cancel = bool(
                user_id and row["line_user_id"] == user_id
            )

        people.append({
            "id": row["id"],
            "name": row["person_name"],
            "label": label,
            "can_cancel": can_cancel,
            "dharma_role": row.get("dharma_role"),
            "attendance_option": row.get("attendance_option"),
            "day1_group": row.get("day1_group"),
            "day2_group": row.get("day2_group"),
            "leader_name": row.get("leader_name"),
            "proxy_by_name": row.get("proxy_by_name"),
            "relay_items": parse_relay_items(row.get("relay_items")),
        })

    return jsonify({"people": people, "event_type": ev.get("event_type") or "general", "relay_enabled": bool(ev.get("relay_enabled")), "relay_label": ev.get("relay_label") or "接龍項目", "relay_mode": ev.get("relay_mode") or "text", "purchase_catalog": parse_purchase_catalog(ev.get("purchase_catalog"))})


@app.route("/api/liff/admin-signup/update", methods=["POST"])
def api_liff_admin_signup_update():
    group_id = require_group_from_request()
    data = request.get_json(force=True)

    user_id = str(data.get("user_id", "")).strip()
    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理報名資料的權限"}), 403

    event_id = int(data.get("event_id", 0) or 0)
    signup_id = int(data.get("signup_id", 0) or 0)
    person_name = str(data.get("person_name", "")).strip()
    if not person_name:
        return jsonify({"error": "姓名不能留空"}), 400

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404
    row = get_signup_by_id(event_id, signup_id)
    if not row:
        return jsonify({"error": "找不到這筆報名"}), 404

    event_type = ev.get("event_type") or "general"
    attendance_option = None
    dharma_role = None
    day1_group = None
    day2_group = None
    leader_name = None
    relay_items = []

    if event_type == "dharma":
        dharma_role = str(data.get("dharma_role", "")).strip()
        valid_groups = {"服務", "文書", "接待", "總務", "辦道", "壇務", "炊事"}
        if dharma_role == "student":
            attendance_option = str(data.get("attendance_option", "")).strip()
            leader_name = str(data.get("leader_name", "")).strip() or None
            if attendance_option not in {"上兩天", "第一天", "補第二天", "加開第一天"}:
                return jsonify({"error": "請選擇正確的班員參班方式"}), 400
            if not leader_name:
                return jsonify({"error": "班員請填寫帶班人員"}), 400
        elif dharma_role == "staff":
            day1_group = str(data.get("day1_group", "")).strip() or None
            day2_group = str(data.get("day2_group", "")).strip() or None
            if day1_group and day1_group not in valid_groups:
                return jsonify({"error": "第一天組別不正確"}), 400
            if day2_group and day2_group not in valid_groups:
                return jsonify({"error": "第二天組別不正確"}), 400
            if not day1_group and not day2_group:
                return jsonify({"error": "辦事人員至少要選擇一天的組別"}), 400
        else:
            return jsonify({"error": "請選擇班員或辦事人員"}), 400
    else:
        if ev.get("relay_enabled"):
            relay_items = clean_relay_for_event(ev, data.get("relay_items", []))

    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT id FROM line_signups WHERE event_id=%s AND person_name=%s AND id<>%s",
        (event_id, person_name, signup_id),
    )
    if cur.fetchone():
        cur.close()
        release_db(conn)
        return jsonify({"error": f"{person_name} 已經在報名名單中"}), 409

    cur = conn.cursor()
    cur.execute(
        """UPDATE line_signups
           SET person_name=%s,
               attendance_option=%s,
               dharma_role=%s,
               day1_group=%s,
               day2_group=%s,
               relay_items=%s,
               leader_name=%s
           WHERE id=%s AND event_id=%s""",
        (
            person_name,
            attendance_option,
            dharma_role,
            day1_group,
            day2_group,
            json.dumps(relay_items, ensure_ascii=False),
            leader_name,
            signup_id,
            event_id,
        ),
    )
    conn.commit()
    cur.close()
    release_db(conn)
    return jsonify({"message": f"已更新：{person_name}"})


@app.route("/api/liff/relay-items", methods=["POST"])
def api_liff_update_relay_items():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    event_id = int(data.get("event_id", 0) or 0)
    signup_id = int(data.get("signup_id", 0) or 0)
    user_id = str(data.get("user_id", "")).strip()
    ev = get_event_by_id(group_id, event_id)
    items = clean_relay_for_event(ev or {}, data.get("relay_items", []))
    if not ev or (ev.get("event_type") or "general") != "general" or not ev.get("relay_enabled"):
        return jsonify({"error": "這個活動沒有開啟接龍項目"}), 400
    if not registration_is_open(ev):
        return jsonify({"error": "報名已截止，無法修改接龍項目"}), 403

    row = get_signup_by_id(event_id, signup_id)
    if not row:
        return jsonify({"error": "找不到這筆報名"}), 404
    owned = (
        (row.get("signup_type") == "self" and row.get("line_user_id") == user_id) or
        (row.get("signup_type") == "proxy" and row.get("proxy_by_user_id") == user_id)
    )
    if not owned:
        return jsonify({"error": "你只能修改自己或自己代報的接龍項目"}), 403

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE line_signups SET relay_items=%s WHERE id=%s AND event_id=%s",
        (json.dumps(items, ensure_ascii=False), signup_id, event_id),
    )
    conn.commit()
    cur.close()
    release_db(conn)
    return jsonify({"message": "接龍項目已更新"})


@app.route("/api/liff/cancel", methods=["POST"])
def api_liff_cancel():
    group_id = require_group_from_request()
    data = request.get_json(force=True)

    event_id = int(data.get("event_id", 0) or 0)
    signup_id = int(data.get("signup_id", 0) or 0)
    user_id = str(data.get("user_id", "")).strip()

    if not user_id:
        return jsonify({"error": "無法取得 LINE 使用者資料"}), 400

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    row = get_signup_by_id(event_id, signup_id)
    if not row:
        return jsonify({"error": "找不到這筆報名"}), 404

    if is_admin(user_id):
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM line_signups WHERE id=%s AND event_id=%s", (signup_id, event_id))
        conn.commit()
        cur.close()
        release_db(conn)
    elif not remove_owned_signup(event_id, signup_id, user_id):
        return jsonify({"error": "你只能取消自己報名或自己代報的人"}), 403

    return jsonify({
        "message": f"已刪除：{row['person_name']}" if is_admin(user_id) else f"已取消：{row['person_name']}",
        "count": get_signup_count(event_id),
    })



def _signup_display_name(row):
    name = row.get("person_name") or ""
    proxy = row.get("proxy_by_name")
    return f"{name}（{proxy} 代報）" if proxy else name


def _final_list_text(group_id, ev):
    """Build the final list text sent to the LINE group."""
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT person_name, attendance_option, dharma_role, day1_group, day2_group, proxy_by_name, relay_items, leader_name
        FROM line_signups
        WHERE event_id=%s
        ORDER BY id
    """, (ev["id"],))
    rows = cur.fetchall()
    cur.close()
    release_db(conn)

    title = ev.get("title") or "活動"
    lines = [f"📋 {title}｜最終報名名單", ""]

    if (ev.get("event_type") or "general") != "dharma":
        lines.append(f"報名人數：{len(rows)} 人")
        relay_label = ev.get("relay_label") or "接龍項目"
        for i, r in enumerate(rows, 1):
            items = parse_relay_items(r.get("relay_items"))
            if ev.get("relay_enabled") and items and (ev.get("relay_mode") or "text") == "fixed_purchase":
                dict_items = [x for x in items if isinstance(x, dict)]
                person_total = sum(float(x.get("subtotal", 0) or 0) for x in dict_items)
                proxy = f"（{r.get('proxy_by_name')} 代報）" if r.get("proxy_by_name") else ""
                if len(dict_items) == 1:
                    x = dict_items[0]
                    lines.append(f"{i}. {r.get('person_name') or ''} × {x.get('qty',0):g}｜{person_total:g} 元{proxy}")
                else:
                    detail = "、".join(f"{x.get('item','')}×{x.get('qty',0):g}" for x in dict_items)
                    lines.append(f"{i}. {r.get('person_name') or ''}｜{detail}｜{person_total:g} 元{proxy}")
            elif ev.get("relay_enabled") and items and (ev.get("relay_mode") or "text") in {"custom_purchase", "purchase"}:
                lines.append(f"{i}. {_signup_display_name(r)}")
                person_total = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    subtotal = float(item.get("subtotal", 0) or 0)
                    person_total += subtotal
                    lines.append(f"　{item.get('item','')}｜{item.get('unit_price',0):g} × {item.get('qty',0):g} = {subtotal:g} 元")
                lines.append(f"　個人合計：{person_total:g} 元")
            else:
                text_items = [str(x) for x in items if not isinstance(x, dict)]
                suffix = f"｜{relay_label}：{'、'.join(text_items)}" if ev.get("relay_enabled") and text_items else ""
                lines.append(f"{i}. {_signup_display_name(r)}{suffix}")
        if ev.get("relay_enabled") and (ev.get("relay_mode") or "text") in {"fixed_purchase", "custom_purchase", "purchase"}:
            grand_total = 0
            for r in rows:
                for item in parse_relay_items(r.get("relay_items")):
                    if isinstance(item, dict):
                        grand_total += float(item.get("subtotal", 0) or 0)
            lines += ["", f"全部總金額：{grand_total:g} 元"]
    else:
        students = [r for r in rows if r.get("dharma_role") == "student" or (not r.get("dharma_role") and r.get("attendance_option"))]
        staff = [r for r in rows if r.get("dharma_role") == "staff"]

        if students:
            lines.append(f"【班員｜共 {len(students)} 人】")
            for opt in ["上兩天", "第一天", "補第二天", "加開第一天"]:
                selected = [r for r in students if r.get("attendance_option") == opt]
                if selected:
                    lines.append(f"{opt}（{len(selected)}）")
                    for r in selected:
                        leader = f" → {r.get('leader_name')}" if r.get("leader_name") else ""
                        proxy = f"（{r.get('proxy_by_name')} 代報）" if r.get("proxy_by_name") else ""
                        lines.append(f"{r.get('person_name') or ''}{leader}{proxy}")
            lines.append("")

        groups = ["服務", "文書", "接待", "總務", "辦道", "壇務", "炊事"]
        for key, day in [("day1_group", "第一天"), ("day2_group", "第二天")]:
            active = [r for r in staff if r.get(key)]
            if not active:
                continue
            lines.append(f"【{day}辦事人員】")
            for g in groups:
                names = [_signup_display_name(r) for r in active if r.get(key) == g]
                if names:
                    lines.append(f"{g}（{len(names)}）")
                    lines.extend(names)
            lines.append("")

        # 法會不再把班員與辦事人員混成一個「總報名人數」。
        # 班員人數在上方單獨統計；辦事人員則依日期與各組分開顯示。

    lines += ["", "報名已截止，如需異動請聯絡活動管理者。"]
    return "\n".join(lines)


def _push_group_text(group_id, message):
    """Push immediately to the original LINE group. Retry once on transient failure."""
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": group_id,
        "messages": [{"type": "text", "text": message[:5000]}],
    }
    last_error = None
    for _ in range(2):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=20)
            if r.ok:
                return
            last_error = f"LINE push failed: {r.status_code} {r.text}"
        except requests.RequestException as e:
            last_error = f"LINE push request failed: {e}"
    raise RuntimeError(last_error or "LINE push failed")


def _publish_event_list(group_id, ev, force=False):
    if not force and ev.get("list_published_at"):
        return False
    _push_group_text(group_id, _final_list_text(group_id, ev))
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE line_events SET list_published_at=NOW() WHERE id=%s", (ev["id"],))
    conn.commit()
    cur.close()
    release_db(conn)
    return True


@app.route("/api/cron/publish-deadline-lists", methods=["GET", "POST"])
def cron_publish_deadline_lists():
    """
    Called by an external scheduler. Publishes each eligible event only once.
    Protect with CRON_SECRET in Render; scheduler sends ?secret=...
    """
    expected = os.environ.get("CRON_SECRET", "")
    supplied = request.args.get("secret", "")
    if not expected or supplied != expected:
        return jsonify({"error": "unauthorized"}), 401

    now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    conn = db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT *
        FROM line_events
        WHERE active=TRUE
          AND auto_publish_list=TRUE
          AND deadline_list_published_at IS NULL
          AND registration_deadline IS NOT NULL
        ORDER BY id
    """)
    events = cur.fetchall()
    cur.close()
    release_db(conn)

    published = []
    for ev in events:
        if group_entry_disabled(ev.get("group_id")):
            continue
        d = ev.get("registration_deadline")
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        # Deadline is inclusive through 23:59 Taiwan; publish from next day 00:00.
        if d and now_tw.date() > d:
            # 正式截止公布與截止前的手動公布分開記錄。
            _publish_event_list(ev["group_id"], ev, force=True)
            conn2 = db()
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE line_events SET deadline_list_published_at=NOW() WHERE id=%s",
                (ev["id"],),
            )
            conn2.commit()
            cur2.close()
            release_db(conn2)
            published.append(ev["id"])

    return jsonify({"ok": True, "published_event_ids": published})


@app.route("/api/liff/events/auto-publish", methods=["POST"])
def api_liff_auto_publish():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    event_id = int(data.get("event_id", 0) or 0)
    enabled = bool(data.get("enabled"))

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403

    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE line_events SET auto_publish_list=%s WHERE id=%s AND group_id=%s",
        (enabled, event_id, group_id),
    )
    conn.commit()
    cur.close()
    release_db(conn)
    return jsonify({"message": "已開啟截止後自動公布名單" if enabled else "已關閉截止後自動公布名單"})


@app.route("/api/liff/events/publish-list", methods=["POST"])
def api_liff_publish_list():
    """Admin manual/re-publish. Intended after reopen/changes."""
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    event_id = int(data.get("event_id", 0) or 0)

    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403
    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404

    try:
        _publish_event_list(group_id, ev, force=True)
    except Exception:
        app.logger.exception("Failed to manually publish list for event %s", event_id)
        return jsonify({"error": "名單傳送失敗，請稍後再試一次"}), 502
    return jsonify({"message": "已立即將最新名單公布到 LINE 群組"})


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        abort(400)

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            handle_group_text(event)

    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "LINE signup bot is running with Supabase/PostgreSQL."


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
