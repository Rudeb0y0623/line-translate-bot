import os, hmac, hashlib, base64, json, requests, re, random, csv
from flask import Flask, request
from pykakasi import kakasi
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

# ====== 環境変数 ======
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

app = Flask(__name__)

# ====== 署名検証 ======
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ====== DeepL 翻訳 ======
def deepl_translate(text: str, target_lang: str) -> str:
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]

# ====== 言語判定 ======
def guess_and_translate(text: str):
    vi_chars = set("ăâđêôơưÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊ..."
                   "ỳýỷỹỵ")
    is_vi = any(c in vi_chars for c in text.lower())
    if is_vi:
        return "VI", deepl_translate(text, "JA")
    else:
        return "JA", deepl_translate(text, "VI")

# ====== CSV読み込み ======
DATA_DIR = "data"
quiz_data = {
    "grammar": [],
    "meaning": [],
    "reading": []
}
for qtype in quiz_data.keys():
    path = os.path.join(DATA_DIR, f"{qtype.capitalize()}.csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            quiz_data[qtype] = list(reader)

# ====== クイズ状態管理 ======
quiz_state = {}

def start_quiz(chat_id, qtype):
    if not quiz_data[qtype]:
        reply_message(chat_id, "問題がありません。")
        return
    quiz_state[chat_id] = {"type": qtype, "index": 0, "active": True}
    send_question(chat_id)

def send_question(chat_id):
    state = quiz_state.get(chat_id)
    if not state or not state["active"]:
        return
    qtype = state["type"]
    data = quiz_data[qtype]
    if state["index"] >= len(data):
        reply_message(chat_id, f"✅ N5 {qtype} クイズを終了しました。おつかれさま！")
        quiz_state[chat_id]["active"] = False
        return
    q = data[state["index"]]
    text = f"問題: {q['sentence']}\n{q['choices']}"
    quiz_state[chat_id]["current"] = q
    reply_message(chat_id, text)

def check_answer(chat_id, ans):
    state = quiz_state.get(chat_id)
    if not state or not state.get("current"):
        return
    q = state["current"]
    if ans == q["answer"]:
        msg = f"⭕ 正解！ {q['explanation_vi']}"
    else:
        msg = f"❌ 不正解。正解は {q['answer']} です。\n{q['explanation_vi']}"
    reply_message(chat_id, msg)
    state["index"] += 1
    send_question(chat_id)

# ====== LINE返信 ======
def reply_message(reply_token, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, data=json.dumps(body), timeout=15)

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""
    if not signature or not verify_signature(body, signature): return "bad signature", 400
    data = request.get_json(silent=True) or {}

    for ev in data.get("events", []):
        if ev.get("type") != "message": continue
        msg = ev.get("message", {})
        if msg.get("type") != "text": continue

        text = msg.get("text").strip()
        chat_id = ev["replyToken"]  # 本来は groupId/roomId/userId を使う

        # クイズ中なら回答処理
        if chat_id in quiz_state and quiz_state[chat_id]["active"]:
            if text in ["1", "2", "3", "4"]:
                check_answer(chat_id, text)
                continue
            if text.lower() == "/quiz stop":
                quiz_state[chat_id]["active"] = False
                reply_message(ev["replyToken"], "N5クイズを終了しました。")
                continue

        # コマンド判定
        if text.lower().startswith("/quiz"):
            if "grammar" in text:
                start_quiz(chat_id, "grammar")
            elif "meaning" in text:
                start_quiz(chat_id, "meaning")
            elif "reading" in text:
                start_quiz(chat_id, "reading")
            continue

        # 通常翻訳
        lang, translated = guess_and_translate(text)
        reply_message(ev["replyToken"], f"[{lang}→] {translated}")

    return "OK", 200

@app.route("/", methods=["GET"])
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
