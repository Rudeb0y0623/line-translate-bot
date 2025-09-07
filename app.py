import os, hmac, hashlib, base64, json, requests, re, csv, random
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
    vi_chars = set("ăâđêôơư...ỳýỷỹỵ")  # 省略
    is_vi = any(c in vi_chars for c in text.lower())
    if is_vi:
        return "VI", deepl_translate(text, "JA")
    else:
        return "JA", deepl_translate(text, "VI")

# ====== Quiz Loader (choicesが1列のCSV対応) ======
def load_quiz(filename):
    questions = []
    path = os.path.join("data", filename)
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("sentence"):
                continue
            # "1) で 2) に 3) から 4) とか" を分割
            choices = re.split(r"\d+\)", row["choices"])
            choices = [c.strip() for c in choices if c.strip()]
            questions.append({
                "q": row["sentence"].strip(),
                "choices": choices,
                "ans": row["answer"].strip(),
                "explanation": row.get("explanation_vi", "")
            })
    return questions

QUIZ = {
    "reading": load_quiz("Reading.csv"),
    "meaning": load_quiz("Meaning.csv"),
    "grammar": load_quiz("Grammar.csv"),
}

# ====== 状態管理 ======
state = {}
DEFAULTS = {"show_hira": True, "show_romaji": True, "quiz": None, "current": None}

def get_state(chat_id: str):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id: str, **kwargs):
    s = get_state(chat_id)
    s.update({k: v for k, v in kwargs.items() if k in s})
    state[chat_id] = s
    return s

# ====== LINE返信 ======
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, data=json.dumps(body), timeout=15)

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""
    if not signature or not verify_signature(body, signature):
        return "bad signature", 400

    data = request.get_json(silent=True) or {}
    events = data.get("events", [])

    for ev in events:
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = msg.get("text") or ""
        chat_id = ev.get("source", {}).get("groupId") or ev.get("source", {}).get("roomId") or ev.get("source", {}).get("userId")
        s = get_state(chat_id)

        # ===== クイズコマンド =====
        if text.startswith("/quiz"):
            parts = text.split()
            if len(parts) == 2 and parts[1] in QUIZ:
                quiz_type = parts[1]
                q = random.choice(QUIZ[quiz_type])
                set_state(chat_id, quiz=quiz_type, current=q)
                reply_message(ev["replyToken"],
                              f"問題: {q['q']}\n" +
                              "\n".join([f"{i+1}) {c}" for i, c in enumerate(q['choices'])]))
                continue
            elif len(parts) == 2 and parts[1] == "stop":
                set_state(chat_id, quiz=None, current=None)
                reply_message(ev["replyToken"], "✅ N5クイズを終了しました。おつかれさま！")
                continue

        # ===== クイズ進行 =====
        if s["quiz"]:
            if text.isdigit() and s.get("current"):
                q = s["current"]
                correct = q["ans"]
                if text == correct:
                    reply_message(ev["replyToken"], f"⭕ 正解！ {q['explanation']}")
                else:
                    reply_message(ev["replyToken"], f"❌ 不正解！ 正解は {correct}) {q['choices'][int(correct)-1]}\n{q['explanation']}")
                # 次の問題
                q = random.choice(QUIZ[s["quiz"]])
                set_state(chat_id, current=q)
                reply_message(ev["replyToken"],
                              f"次の問題:\n{q['q']}\n" +
                              "\n".join([f"{i+1}) {c}" for i, c in enumerate(q['choices'])]))
                continue

        # ===== 通常翻訳 =====
        src_lang, translated = guess_and_translate(text)
        lines = []
        if src_lang == "VI":
            lines.append("[VN→JP]")
            lines.append(translated)
        else:
            lines.append("[JP→VN]")
            lines.append(translated)

        reply_message(ev["replyToken"], "\n".join(lines))

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
