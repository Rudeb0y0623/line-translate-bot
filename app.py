import os, hmac, hashlib, base64, json, requests, re
from flask import Flask, request, abort
from pykakasi import kakasi

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

app = Flask(__name__)

def verify_signature(body, signature):
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

def deepl_translate(text, target_lang):
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]

def guess_and_translate(text):
    # VI文字をざっくり検出 → 日本語へ、それ以外はベトナム語へ
    vi_chars = set("ăâđêôơưÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆ"
                   "ÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
                   "ÙÚỦŨỤƯỪỨỬỮỰ"
                   "ỲÝỶỸỴàáảãạăằắẳẵặâầấẩẫậđ"
                   "èéẻẽẹêềếểễệ"
                   "ìíỉĩị"
                   "òóỏõọôồốổỗộơờớởỡợ"
                   "ùúủũụưừứửữự"
                   "ỳýỷỹỵ")
    is_vi = any(c in vi_chars for c in text.lower())
    if is_vi:
        return "VI", deepl_translate(text, "JA")
    else:
        return "JA", deepl_translate(text, "VI")

# --- 変換器（ひらがな／ローマ字）を使い回し ---
_kakasi_hira = kakasi(); _kakasi_hira.setMode("J","H"); _kakasi_hira.setMode("K","H"); _kakasi_hira.setMode("H","H")
_converter_hira = _kakasi_hira.getConverter()

_kakasi_roma = kakasi(); _kakasi_roma.setMode("J","a"); _kakasi_roma.setMode("K","a"); _kakasi_roma.setMode("H","a")
_converter_roma = _kakasi_roma.getConverter()

def to_hiragana(text): return _converter_hira.do(text)
def to_romaji(text):   return _converter_roma.do(text)

def reply_message(reply_token, text):
    headers = {"Content-Type":"application/json","Authorization":f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages":[{"type":"text","text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, data=json.dumps(body), timeout=15)

# ===== 表示設定（チャット単位） =====
state = {}  # state[chat_id] = {"show_hira": bool, "show_romaji": bool}
DEFAULTS = {"show_hira": True, "show_romaji": True}

def get_state(chat_id):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id, **kwargs):
    s = get_state(chat_id)
    s.update({k:v for k,v in kwargs.items() if k in s})
    state[chat_id] = s
    return s

def parse_command(text):
    t = text.strip().lower()
    if t == "/status":
        return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m:
        return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m:
        return ("romaji", m.group(2) == "on")
    return (None, None)

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()
    if not verify_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])
    for ev in events:
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = msg["text"]

        # 自分の出力は再翻訳しない
        if text.startswith("[VI→JA]") or text.startswith("[JA→VI]"):
            continue

        src_info = ev.get("source", {})
        chat_id = src_info.get("groupId") or src_info.get("roomId") or src_info.get("userId")

        # コマンド処理
        cmd, val = parse_command(text)
        if cmd == "hira":
            set_state(chat_id, show_hira=val)
            reply_message(ev["replyToken"], f"ひらがな表示を {'ON' if val else 'OFF'} にしました。")
            continue
        elif cmd == "romaji":
            set_state(chat_id, show_romaji=val)
            reply_message(ev["replyToken"], f"ローマ字表示を {'ON' if val else 'OFF'} にしました。")
            continue
        elif cmd == "status":
            s = get_state(chat_id)
            reply_message(
                ev["replyToken"],
                f"現在の設定\n- ひらがな: {'ON' if s['show_hira'] else 'OFF'}\n- ローマ字: {'ON' if s['show_romaji'] else 'OFF'}"
            )
            continue

        # 翻訳（必須）
        src_lang, translated = guess_and_translate(text)

        # 出力生成
        s = get_state(chat_id)
        lines = []
        if src_lang == "VI":
            lines.append("[VI→JA]")
            lines.append(translated)
            if s["show_hira"]:
                lines.append
