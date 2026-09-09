import os
import hmac
import hashlib
import base64
import sqlite3
from datetime import datetime

import requests
from flask import Flask, request, abort

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "signup.db")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_MEMBER_PROFILE_URL = "https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        title TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS signups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        person_name TEXT NOT NULL,
        line_user_id TEXT,
        signup_type TEXT NOT NULL,
        proxy_by_user_id TEXT,
        proxy_by_name TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(event_id, person_name),
        FOREIGN KEY(event_id) REFERENCES events(id)
    );
    """)
    conn.commit()
    conn.close()


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str):
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}]
    }
    r = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
    r.raise_for_status()


def get_member_name(group_id: str, user_id: str) -> str:
    if not group_id or not user_id:
        return "未知使用者"
    url = LINE_MEMBER_PROFILE_URL.format(group_id=group_id, user_id=user_id)
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=10)
    if r.ok:
        return r.json().get("displayName", "未知使用者")
    return "未知使用者"


def create_event(group_id: str, title: str):
    conn = db()
    cur = conn.execute(
        """
        INSERT INTO events(group_id, title, active, created_at)
        VALUES (?, ?, 1, ?)
        """,
        (group_id, title, datetime.now().isoformat(timespec="seconds"))
    )
    conn.commit()
    event_id = cur.lastrowid
    conn.close()
    return event_id


def list_active_events(group_id: str):
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM events
        WHERE group_id = ? AND active = 1
        ORDER BY id ASC
        """,
        (group_id,)
    ).fetchall()
    conn.close()
    return rows


def get_event_by_number(group_id: str, number: int):
    events = list_active_events(group_id)
    if number < 1 or number > len(events):
        return None
    return events[number - 1]


def list_signups(event_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM signups WHERE event_id = ? ORDER BY id ASC",
        (event_id,)
    ).fetchall()
    conn.close()
    return rows


def add_signup(event_id: int, person_name: str, signup_type: str,
               line_user_id=None, proxy_by_user_id=None, proxy_by_name=None):
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO signups(
                event_id, person_name, line_user_id, signup_type,
                proxy_by_user_id, proxy_by_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, person_name, line_user_id, signup_type,
                proxy_by_user_id, proxy_by_name,
                datetime.now().isoformat(timespec="seconds")
            )
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_signup(event_id: int, person_name: str):
    conn = db()
    cur = conn.execute(
        "DELETE FROM signups WHERE event_id = ? AND person_name = ?",
        (event_id, person_name)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def remove_self_signup(event_id: int, user_id: str):
    conn = db()
    cur = conn.execute(
        """
        DELETE FROM signups
        WHERE event_id = ? AND line_user_id = ? AND signup_type = 'self'
        """,
        (event_id, user_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def close_event(group_id: str, number: int):
    ev = get_event_by_number(group_id, number)
    if not ev:
        return None
    conn = db()
    conn.execute("UPDATE events SET active = 0 WHERE id = ?", (ev["id"],))
    conn.commit()
    conn.close()
    return ev


def event_list_text(group_id: str):
    events = list_active_events(group_id)
    if not events:
        return "目前沒有進行中的活動。"
    lines = ["📌 目前進行中的活動：", ""]
    for i, ev in enumerate(events, 1):
        count = len(list_signups(ev["id"]))
        lines.append(f"{i}. {ev['title']}（{count} 人）")
    lines.append("")
    lines.append("例如：報名 1／代報 1 王小明／名單 1")
    return "\n".join(lines)


def parse_number(parts, index=1):
    try:
        return int(parts[index])
    except (ValueError, IndexError):
        return None


HELP_TEXT = """【活動報名 Bot｜多活動版】

建立活動：
開活動 9/20 新民班

查看目前活動：
活動

本人報名：
報名 1

代報：
代報 1 王小明

取消自己的報名：
取消報名 1

取消指定姓名：
取消 1 王小明

查看名單：
名單 1

結束活動：
結束 1
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

    parts = text.split()

    if parts and parts[0] == "報名":
        num = parse_number(parts)
        if num is None:
            reply(reply_token, "請輸入活動編號，例如：報名 1")
            return
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        ok = add_signup(ev["id"], user_name, "self", line_user_id=user_id)
        reply(
            reply_token,
            f"✅ {user_name} 已報名「{ev['title']}」。"
            if ok else f"{user_name} 已經在「{ev['title']}」名單裡了。"
        )
        return

    if parts and parts[0] == "代報":
        num = parse_number(parts)
        if num is None or len(parts) < 3:
            reply(reply_token, "格式：代報 活動編號 姓名\n例如：代報 1 王小明")
            return
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        person_name = " ".join(parts[2:]).strip()
        ok = add_signup(
            ev["id"], person_name, "proxy",
            proxy_by_user_id=user_id, proxy_by_name=user_name
        )
        reply(
            reply_token,
            f"✅ 已代報：{person_name}\n活動：{ev['title']}\n代報人：{user_name}"
            if ok else f"{person_name} 已經在「{ev['title']}」名單裡了。"
        )
        return

    if parts and parts[0] == "取消報名":
        num = parse_number(parts)
        if num is None:
            reply(reply_token, "請輸入活動編號，例如：取消報名 1")
            return
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        ok = remove_self_signup(ev["id"], user_id)
        reply(
            reply_token,
            f"✅ 已取消你在「{ev['title']}」的報名。"
            if ok else f"找不到你在「{ev['title']}」的本人報名紀錄。"
        )
        return

    if parts and parts[0] == "取消":
        num = parse_number(parts)
        if num is None or len(parts) < 3:
            reply(reply_token, "格式：取消 活動編號 姓名\n例如：取消 1 王小明")
            return
        ev = get_event_by_number(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        person_name = " ".join(parts[2:]).strip()
        ok = remove_signup(ev["id"], person_name)
        reply(
            reply_token,
            f"✅ 已從「{ev['title']}」取消：{person_name}"
            if ok else f"「{ev['title']}」名單中找不到：{person_name}"
        )
        return

    if parts and parts[0] == "名單":
        num = parse_number(parts)
        if num is None:
            reply(reply_token, "請輸入活動編號，例如：名單 1")
            return
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

    if parts and parts[0] == "結束":
        num = parse_number(parts)
        if num is None:
            reply(reply_token, "請輸入活動編號，例如：結束 1")
            return
        ev = close_event(group_id, num)
        if not ev:
            reply(reply_token, "找不到這個活動編號，請先輸入「活動」查看。")
            return
        reply(reply_token, f"✅ 已結束活動：{ev['title']}\n\n" + event_list_text(group_id))
        return


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not verify_signature(body, signature):
        abort(400)

    payload = request.get_json(silent=True) or {}
    for event in payload.get("events", []):
        if (
            event.get("type") == "message"
            and event.get("message", {}).get("type") == "text"
        ):
            handle_group_text(event)
    return "OK"


@app.route("/", methods=["GET"])
def health():
    return "LINE signup bot is running."


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
