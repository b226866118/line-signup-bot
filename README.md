# LINE 群組活動報名 Bot — MVP

這是一個最小可用版，專門解決 LINE 群組人工接龍洗版問題。

## 功能

在 LINE 群組輸入：

- `開活動 9/20 茶會`：建立新的活動（同群組上一個活動會自動結束）
- `報名`：本人報名，自動取得 LINE 顯示名稱
- `代報 王小明`：幫不在群組裡的新人或親友報名
- `取消報名`：取消自己的本人報名
- `取消 王小明`：取消指定姓名
- `名單`：列出最新名單
- `活動`：查看目前活動
- `說明`：顯示指令

Bot 不會對一般聊天內容回覆，因此不會反過來造成洗版。

## 1. 建立 LINE Official Account / Messaging API

需要建立 LINE Official Account 與 Messaging API channel。

在 LINE Developers Console 中：

1. 取得 `Channel secret`
2. 取得 `Channel access token`
3. 開啟 **Allow bot to join group chats**
4. 設定 Webhook URL，例如：
   `https://你的網域/callback`
5. 啟用 Webhook

注意：同一個 LINE 群組同時只能加入一個 LINE Official Account Bot。

## 2. 本機安裝

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

設定環境變數：

```bash
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
```

然後：

```bash
python app.py
```

預設監聽 8080 port。

LINE webhook 必須能從網際網路以 HTTPS 存取，所以正式使用時需部署到 Render、Railway、Fly.io、Cloud Run 等服務，或開發階段使用 tunnel。

## 3. 群組中的使用方式

例如：

```text
甲：開活動 9/20 茶會
Bot：📌 已建立活動：9/20 茶會

乙：報名
Bot：✅ 乙 已報名「9/20 茶會」。

丙：代報 王小明
Bot：✅ 已代報：王小明
     代報人：丙

丙：代報 李小美
Bot：✅ 已代報：李小美
     代報人：丙

甲：名單
Bot：
📋 9/20 茶會
目前共 3 人

1. 乙
2. 王小明（丙 代報）
3. 李小美（丙 代報）
```

## 目前 MVP 的限制

1. `取消 姓名` 暫時任何群組成員都可操作。正式版應加「主辦人權限」。
2. SQLite 適合測試或單機部署。正式多人使用可改 PostgreSQL / Supabase。
3. 目前用文字指令，尚未使用 Flex Message 按鈕。
4. 代報只填姓名；下一版可加入：
   - 新人 / 道親
   - 葷 / 素
   - 聯絡電話
   - 接送
   - 佛堂 / 區別
   - 備註
5. 名單很多人時，應改成分頁或網頁檢視，避免單則訊息過長。

## 建議第二版

最適合實際群組使用的是：

- 活動建立後只出現一張 Flex Message 卡片
- 「本人報名」
- 「代人報名」
- 「查看名單」
- 「取消」
- 代人報名時打開 LIFF 小表單
- 群組只顯示簡短結果，例如「王小明已加入，目前 18 人」
- 完整名單只在需要時查看

這樣才會真正達成「不洗版」。
