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
        signup_type TEXT NOT NULL,     -- self / proxy
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


def active_event(group_id: str):
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM events
        WHERE group_id = ? AND active = 1
        ORDER BY id DESC LIMIT 1
        """,
        (group_id,)
    ).fetchone()
    conn.close()
    return row


def create_event(group_id: str, title: str):
    conn = db()
    conn.execute("UPDATE events SET active = 0 WHERE group_id = ?", (group_id,))
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
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


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
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def list_signups(event_id: int):
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM signups
        WHERE event_id = ?
        ORDER BY id ASC
        """,
        (event_id,)
    ).fetchall()
    conn.close()
    return rows


HELP_TEXT = """【活動報名 Bot】
開活動 活動名稱
報名
代報 姓名
取消報名
取消 姓名
名單
活動
說明

例：
開活動 9/20 新民班
代報 王小明
代報 李小華
名單"""


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
            reply(reply_token, "請輸入活動名稱，例如：開活動 9/20 茶會")
            return
        create_event(group_id, title)
        reply(
            reply_token,
            f"📌 已建立活動：{title}\n\n"
            f"本人報名：輸入「報名」\n"
            f"代新人報名：輸入「代報 姓名」\n"
            f"查看：輸入「名單」"
        )
        return

    ev = active_event(group_id)
    if not ev:
        reply(reply_token, "目前沒有進行中的活動。\n請先輸入：開活動 活動名稱")
        return

    if text == "活動":
        reply(reply_token, f"目前活動：{ev['title']}")
        return

    if text == "報名":
        ok = add_signup(
            ev["id"],
            person_name=user_name,
            signup_type="self",
            line_user_id=user_id
        )
        if ok:
            reply(reply_token, f"✅ {user_name} 已報名「{ev['title']}」。")
        else:
            reply(reply_token, f"{user_name} 已經在名單裡了。")
        return

    if text.startswith("代報 "):
        person_name = text[len("代報 "):].strip()
        if not person_name:
            reply(reply_token, "請輸入姓名，例如：代報 王小明")
            return
        ok = add_signup(
            ev["id"],
            person_name=person_name,
            signup_type="proxy",
            proxy_by_user_id=user_id,
            proxy_by_name=user_name
        )
        if ok:
            reply(
                reply_token,
                f"✅ 已代報：{person_name}\n"
                f"代報人：{user_name}\n"
                f"活動：{ev['title']}"
            )
        else:
            reply(reply_token, f"{person_name} 已經在這場活動的名單裡了。")
        return

    if text == "取消報名":
        ok = remove_self_signup(ev["id"], user_id)
        reply(
            reply_token,
            "✅ 已取消你的報名。" if ok else "找不到你的本人報名紀錄。"
        )
        return

    if text.startswith("取消 "):
        person_name = text[len("取消 "):].strip()
        if not person_name:
            reply(reply_token, "請輸入姓名，例如：取消 王小明")
            return
        ok = remove_signup(ev["id"], person_name)
        reply(
            reply_token,
            f"✅ 已取消：{person_name}" if ok else f"名單中找不到：{person_name}"
        )
        return

    if text == "名單":
        rows = list_signups(ev["id"])
        if not rows:
            reply(reply_token, f"📋 {ev['title']}\n目前還沒有人報名。")
            return

        lines = [f"📋 {ev['title']}", f"目前共 {len(rows)} 人", ""]
        for i, row in enumerate(rows, 1):
            if row["signup_type"] == "proxy":
                lines.append(
                    f"{i}. {row['person_name']}（{row['proxy_by_name']} 代報）"
                )
            else:
                lines.append(f"{i}. {row['person_name']}")
        reply(reply_token, "\n".join(lines))
        return

    # 不回覆一般聊天，避免 Bot 自己變成洗版來源。


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


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
