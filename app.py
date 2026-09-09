import os
import hmac
import hashlib
import base64
import re
from datetime import datetime

import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, abort

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_MEMBER_PROFILE_URL = "https://api.line.me/v2/bot/group/{group_id}/member/{user_id}"


def db():
    return psycopg2.connect(DATABASE_URL)


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
    conn.commit()
    cur.close()
    conn.close()


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


def create_event(group_id: str, title: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO line_events(group_id, title, active, created_at)
        VALUES (%s, %s, TRUE, %s)
        RETURNING id
        """,
        (group_id, title, datetime.now()),
    )
    event_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return event_id


def list_active_events(group_id: str):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT * FROM line_events
        WHERE group_id = %s AND active = TRUE
        ORDER BY id ASC
        """,
        (group_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_event_by_number(group_id: str, number: int):
    events = list_active_events(group_id)
    if 1 <= number <= len(events):
        return events[number - 1]
    return None


def list_signups(event_id: int):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM line_signups WHERE event_id = %s ORDER BY id ASC", (event_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def add_signup(event_id: int, person_name: str, signup_type: str,
               line_user_id=None, proxy_by_user_id=None, proxy_by_name=None):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO line_signups(
                event_id, person_name, line_user_id, signup_type,
                proxy_by_user_id, proxy_by_name, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id, person_name, line_user_id, signup_type,
                proxy_by_user_id, proxy_by_name, datetime.now(),
            ),
        )
        conn.commit()
        return True
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


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
    conn.close()
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
    conn.close()
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
