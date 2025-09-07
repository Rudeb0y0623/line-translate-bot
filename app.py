import os, hmac, hashlib, base64, json, requests, re, random, csv
from flask import Flask, request

# ====== 環境変数 ======
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

app = Flask(__name__)

# ====== 署名検証 ======
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ====== LINE返信 ======
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply",
                  headers=headers, data=json.dumps(body), timeout=15)

# ====== クイズデータ読込 ======
QUIZ_FILES = {
    "grammar": "data/Grammar.csv",
    "meaning": "data/Meaning.csv",
    "reading": "data/Reading.csv"
}
quizzes = {}

for qtype, path in QUIZ_FILES.items():
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            quizzes[qtype] = list(reader)
    except:
        quizzes[qtype] = []

# ====== 状態管理 ======
quiz_state = {}  # { chat_id: {"type": "grammar", "active": True, "index": 0, "questions": [...]} }

def start_quiz(chat_id, qtype):
    if qtype not in quizzes or not quizzes[qtype]:
        return None
    qs = random.sample(quizzes[qtype], len(quizzes[qtype]))
    quiz_state[chat_id] = {"type": qtype, "active": True, "index": 0, "questions": qs}
    return qs[0]

def get_next_question(chat_id):
    state = quiz_state.get(chat_id)
    if not state: return None
    state["index"] += 1
    if state["index"] >= len(state["questions"]):
        quiz_state[chat_id]["active"] = False
        return None
    return state["questions"][state["index"]]

def format_question(q):
    return f"問題: {q['sentence']}\n{q['choices']}"

def check_answer(chat_id, user_ans):
    state = quiz_state.get(chat_id)
    if not state: return None, None
    q = state["questions"][state["index"]]
    correct = q["answer"].strip()
    explanation = q.get("explanation_vi", "")

    if user_ans == correct:
        result = f"⭕ 正解！ 「{q['choices'].split(';')[int(correct)-1]}」 {explanation}"
    else:
        result = f"❌ 不正解。正解は {correct}) {q['choices'].split(';')[int(correct)-1]}"

    nq = get_next_question(chat_id)
    return result, nq

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
        if ev.get("type") != "message": continue
        msg = ev.get("message", {})
        if msg.get("type") != "text": continue

        text = (msg.get("text") or "").strip()
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")

        # ===== クイズ回答処理 =====
        if chat_id in quiz_state and quiz_state[chat_id]["active"]:
            if text in ["1", "2", "3", "4"]:
                result, nq = check_answer(chat_id, text)
                if result: reply_message(ev["replyToken"], result)
                if nq: reply_message(ev["replyToken"], format_question(nq))
                else: reply_message(ev["replyToken"], "✅ N5クイズを終了しました。おつかれさま！")
                continue  # ← 翻訳処理に行かないようにする！

        # ===== コマンド処理 =====
        if text.startswith("/quiz "):
            cmd = text.split(" ", 1)[1]
            if cmd in ["grammar", "meaning", "reading"]:
                q = start_quiz(chat_id, cmd)
                if q:
                    reply_message(ev["replyToken"], format_question(q))
                else:
                    reply_message(ev["replyToken"], f"{cmd}の問題がありません。")
                continue
            elif cmd == "stop":
                quiz_state[chat_id] = {"active": False}
                reply_message(ev["replyToken"], "✅ N5クイズを終了しました。おつかれさま！")
                continue

        # ===== 翻訳処理（省略可：前の翻訳コードをここに入れる） =====
        reply_message(ev["replyToken"], "[JP⇔VN 翻訳処理はここに実装]")

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
