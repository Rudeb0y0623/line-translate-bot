import os, hmac, hashlib, base64, json, requests, re
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

# ====== 言語判定（超簡易）→ 翻訳 ======
def guess_and_translate(text: str):
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
        return "VI", deepl_translate(text, "JA")  # VI → JA
    else:
        return "JA", deepl_translate(text, "VI")  # JA → VI

# ====== かな/ローマ字変換 ======
# ローマ字は pykakasi
_kakasi_roma = kakasi()
_kakasi_roma.setMode("J", "a"); _kakasi_roma.setMode("K", "a"); _kakasi_roma.setMode("H", "a")
_converter_roma = _kakasi_roma.getConverter()
def to_romaji(text: str) -> str:
    return _converter_roma.do(text)

# ひらがなは Sudachi（文脈で読みを出す）
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

def to_hiragana(text: str) -> str:
    tokens = _sudachi.tokenize(text, _SPLIT)
    katakana = "".join(t.reading_form() if t.reading_form() != "*" else t.surface() for t in tokens)
    return katakana.translate(_katakana_to_hira)

# ====== LINE 返信 ======
def reply_message(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, data=json.dumps(body), timeout=15)

# ====== 表示設定（チャット単位） ======
state = {}  # {chat_id: {"show_hira": bool, "show_romaji": bool}}
DEFAULTS = {"show_hira": True, "show_romaji": True}

def get_state(chat_id: str):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id: str, **kwargs):
    s = get_state(chat_id); s.update({k: v for k, v in kwargs.items() if k in s}); state[chat_id] = s; return s

def parse_command(text: str):
    t = text.strip().lower()
    if t == "/status": return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m: return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m: return ("romaji", m.group(2) == "on")
    return (None, None)

# ====== ルート（ヘルスチェック） ======
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

# ====== Webhook ======
# 先頭の方向タグを除去するための正規表現（JP/VN と JA/VI の旧表記も対応）
TAG_PREFIX_RE = re.compile(
    r'^\[\s*(?:JP|VN|JA|VI)\s*[\-→]\s*(?:JP|VN|JA|VI)\s*\]\s*',
    flags=re.IGNORECASE
)

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""

    # Verifyボタン等の空POSTは200で返す
    if (not signature) and (not body):
        return "OK", 200

    if not signature or not verify_signature(body, signature):
        return "bad signature", 400

    data = request.get_json(silent=True) or {}
    events = data.get("events", [])

    for ev in events:
        etype = ev.get("type")
        # グループ招待などの非メッセージイベントは無視（必要なら返信してもOK）
        if etype == "join":
            # reply_message(ev["replyToken"], "招待ありがとう！JP⇔VN翻訳を始めます。")
            continue
        if etype != "message":
            continue

        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = (msg.get("text") or "").strip()

        # 先頭に [JP→VN] / [VN→JP] / [JA→VI] / [VI→JA] が付いていたら剥がす
        text = TAG_PREFIX_RE.sub("", text, count=1)

        # チャットID（個人/グループ/ルーム）
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")

        # コマンド対応（/hira, /romaji, /status）
        cmd, val = parse_command(text)
        if cmd == "hira":
            set_state(chat_id, show_hira=val)
            reply_message(ev["replyToken"], f"ひらがな表示を {'ON' if val else 'OFF'} にしました。")
            continue
        if cmd == "romaji":
            set_state(chat_id, show_romaji=val)
            reply_message(ev["replyToken"], f"ローマ字表示を {'ON' if val else 'OFF'} にしました。")
            continue
        if cmd == "status":
            s = get_state(chat_id)
            reply_message(ev["replyToken"], f"現在の設定\n- ひらがな: {'ON' if s['show_hira'] else 'OFF'}\n- ローマ字: {'ON' if s['show_romaji'] else 'OFF'}")
            continue

        # 翻訳
        src_lang, translated = guess_and_translate(text)

        # 出力（表示ラベルは JP/VN）
        s = get_state(chat_id)
        lines = []
        if src_lang == "VI":
            lines.append("[VN→JP]")
            lines.append(translated)
            if s["show_hira"]:
                lines.append(f"\n(ひらがな) {to_hiragana(translated)}")
            if s["show_romaji"]:
                lines.append(f"(romaji) {to_romaji(translated)}")
        else:
            lines.append("[JP→VN]")
            lines.append(translated)

        reply_message(ev["replyToken"], "\n".join(lines))

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
