import os
import hmac
import hashlib
import base64
import re
from datetime import datetime
from urllib.parse import urlencode

import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, abort, jsonify, Response

app = Flask(__name__)

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
LIFF_ID = os.environ["LIFF_ID"]
ADMIN_USER_IDS = {x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()}

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


def get_event_by_id(group_id: str, event_id: int):
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM line_events WHERE id = %s AND group_id = %s AND active = TRUE",
        (event_id, group_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


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
.actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}button{border:0;border-radius:10px;padding:11px 6px;font-size:15px}
.primary{background:#06c755;color:#fff}.secondary{background:#e8f1ff;color:#1769aa}.light{background:#eee;color:#333}
.msg{display:none;margin:10px 0;padding:10px;border-radius:10px}.ok{display:block;background:#e8f8ee;color:#17723b}.err{display:block;background:#fdecec;color:#a22}
dialog{width:min(92vw,520px);border:0;border-radius:16px;padding:0}.modal{padding:18px}textarea,input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px;margin:8px 0 12px}
</style>
</head>
<body><div class="wrap"><h1>活動報名</h1><div class="sub" id="who">讀取 LINE 身分中…</div><div id="msg" class="msg"></div>
<div id="adminTools" class="card" style="display:none"><div class="title">活動管理</div><div class="actions" style="grid-template-columns:1fr 1fr"><button class="primary" onclick="openCreate()">＋ 新增活動</button><button class="light" onclick="toggleCloseMode()">結束活動</button></div><div id="closeModeHint" style="display:none;color:#a22;margin-top:10px;font-size:14px">請在下方活動卡片按「結束此活動」。</div></div>
<div id="events">載入活動中…</div></div>
<dialog id="createDialog"><div class="modal"><h3>新增活動</h3><input id="newTitle" placeholder="活動名稱，例如：9/20 新民班"><button class="primary" style="width:100%" onclick="submitCreate()">建立活動</button><button class="light" style="width:100%;margin-top:8px" onclick="createDialog.close()">取消</button></div></dialog>
<dialog id="proxyDialog"><div class="modal"><h3 id="proxyTitle">代人報名</h3><textarea id="proxyNames" rows="5" placeholder="可輸入多人：王小明 李小華；也可用頓號、逗號或換行"></textarea><button class="primary" style="width:100%" onclick="submitProxy()">送出代報</button><button class="light" style="width:100%;margin-top:8px" onclick="proxyDialog.close()">取消</button></div></dialog>
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
let profile=null, proxyEventId=null, adminMode=false, closeMode=false;
function showMsg(t,ok=true){const e=document.getElementById('msg');e.className='msg '+(ok?'ok':'err');e.textContent=t;setTimeout(()=>e.style.display='none',3000)}
async function api(path,opt={}){const sep=path.includes('?')?'&':'?';const r=await fetch(path+sep+new URLSearchParams({g:groupId,sig:sig}),opt);const d=await r.json();if(!r.ok)throw new Error(d.error||'發生錯誤');return d}
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function init(){if(!groupId||!sig){document.getElementById('events').innerHTML='此連結無效，請從群組中的「報名入口」開啟。';return} await liff.init({liffId:LIFF_ID}); if(!liff.isLoggedIn()){liff.login({redirectUri:location.href});return} profile=await liff.getProfile();document.getElementById('who').textContent='你好，'+profile.displayName; try{const me=await api('/api/liff/me?user_id='+encodeURIComponent(profile.userId));adminMode=!!me.is_admin;if(adminMode)document.getElementById('adminTools').style.display='block'}catch(e){console.error(e)} loadEvents()}
async function loadEvents(){try{const d=await api('/api/liff/events');const root=document.getElementById('events'); if(!d.events.length){root.innerHTML='<div class="card">目前沒有進行中的活動。</div>';return} root.innerHTML=d.events.map(ev=>`<div class="card"><div class="title">${esc(ev.title)}</div><div class="count">目前 ${ev.count} 人報名</div><div class="actions"><button class="primary" onclick="selfSignup(${ev.id})">本人報名</button><button class="secondary" onclick="openProxy(${ev.id},'${String(ev.title).replace(/'/g,"\'")}')">代人報名</button><button class="light" onclick="showList(${ev.id},'${String(ev.title).replace(/'/g,"\'")}')">查看名單</button></div>${adminMode&&closeMode?`<button class="light" style="width:100%;margin-top:10px;color:#a22" onclick="closeEvent(${ev.id},'${String(ev.title).replace(/'/g,"\'")}')">結束此活動</button>`:''}</div>`).join('')}catch(e){document.getElementById('events').innerHTML='載入失敗：'+esc(e.message)}}
function openCreate(){document.getElementById('newTitle').value='';createDialog.showModal()}
async function submitCreate(){const title=document.getElementById('newTitle').value.trim();if(!title){showMsg('請輸入活動名稱',false);return}try{const d=await api('/api/liff/events/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,user_id:profile.userId})});createDialog.close();showMsg(d.message);loadEvents()}catch(e){showMsg(e.message,false)}}
function toggleCloseMode(){closeMode=!closeMode;document.getElementById('closeModeHint').style.display=closeMode?'block':'none';loadEvents()}
async function closeEvent(id,title){if(!confirm('確定要結束「'+title+'」嗎？'))return;try{const d=await api('/api/liff/events/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId})});showMsg(d.message);loadEvents()}catch(e){showMsg(e.message,false)}}
async function selfSignup(id){try{const d=await api('/api/liff/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:id,user_id:profile.userId,display_name:profile.displayName})});showMsg(d.message);loadEvents()}catch(e){showMsg(e.message,false)}}
function openProxy(id,title){proxyEventId=id;document.getElementById('proxyTitle').textContent='代人報名｜'+title;document.getElementById('proxyNames').value='';proxyDialog.showModal()}
async function submitProxy(){const names=document.getElementById('proxyNames').value.trim();if(!names){showMsg('請輸入姓名',false);return}try{const d=await api('/api/liff/proxy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_id:proxyEventId,names:names,user_id:profile.userId,display_name:profile.displayName})});proxyDialog.close();showMsg(d.message);loadEvents()}catch(e){showMsg(e.message,false)}}
async function showList(id,title){try{const d=await api('/api/liff/list?event_id='+id);document.getElementById('listTitle').textContent='報名名單｜'+title;document.getElementById('listBody').innerHTML=d.people.length?d.people.map((p,i)=>`${i+1}. ${esc(p)}`).join('<br>'):'目前尚無人報名';listDialog.showModal()}catch(e){showMsg(e.message,false)}}
init();
</script></body></html>"""


@app.route("/liff", methods=["GET"])
def liff_page():
    return Response(LIFF_HTML.replace("__LIFF_ID__", LIFF_ID), mimetype="text/html")


def require_group_from_request():
    group_id = request.args.get("g", "")
    sig = request.args.get("sig", "")
    if not valid_group_signature(group_id, sig):
        abort(403)
    return group_id



@app.route("/api/liff/me", methods=["GET"])
def api_liff_me():
    require_group_from_request()
    user_id = request.args.get("user_id", "")
    return jsonify({"is_admin": is_admin(user_id)})


@app.route("/api/liff/events/create", methods=["POST"])
def api_liff_create_event():
    group_id = require_group_from_request()
    data = request.get_json(force=True)
    user_id = str(data.get("user_id", "")).strip()
    title = str(data.get("title", "")).strip()
    if not is_admin(user_id):
        return jsonify({"error": "你沒有管理活動的權限"}), 403
    if not title:
        return jsonify({"error": "請輸入活動名稱"}), 400
    create_event(group_id, title)
    return jsonify({"message": f"已新增活動：{title}"})


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
    conn.close()
    return jsonify({"message": f"已結束活動：{ev['title']}"})


@app.route("/api/liff/events", methods=["GET"])
def api_liff_events():
    group_id = require_group_from_request()
    result = []
    for ev in list_active_events(group_id):
        result.append({"id": ev["id"], "title": ev["title"], "count": len(list_signups(ev["id"]))})
    return jsonify({"events": result})


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
    if not user_id or not display_name:
        return jsonify({"error": "無法取得 LINE 使用者資料"}), 400
    if not add_signup(event_id, display_name, "self", line_user_id=user_id):
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
    names = split_names(str(data.get("names", "")).strip())
    if not names:
        return jsonify({"error": "請輸入至少一個姓名"}), 400
    display_name = str(data.get("display_name", "")).strip()
    user_id = str(data.get("user_id", "")).strip()
    added, dup = 0, []
    for name in names:
        if add_signup(event_id, name, "proxy", proxy_by_user_id=user_id, proxy_by_name=display_name):
            added += 1
        else:
            dup.append(name)
    msg = f"已成功加入 {added} 人" if not dup else f"已加入 {added} 人；重複：{'、'.join(dup)}"
    return jsonify({"message": msg})


@app.route("/api/liff/list", methods=["GET"])
def api_liff_list():
    group_id = require_group_from_request()
    event_id = int(request.args.get("event_id", "0") or 0)
    ev = get_event_by_id(group_id, event_id)
    if not ev:
        return jsonify({"error": "找不到活動"}), 404
    people = []
    for row in list_signups(event_id):
        if row["signup_type"] == "proxy":
            people.append(f"{row['person_name']}（{row['proxy_by_name']} 代報）")
        else:
            people.append(row["person_name"])
    return jsonify({"people": people})


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
