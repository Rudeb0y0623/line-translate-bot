# app.py
import os, hmac, hashlib, base64, json, re, requests
from flask import Flask, request

# ------- Kana/Romaji tools -------
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
    # target_lang: "JA" / "VI"
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]

# ====== 言語判定（超簡易）→ 翻訳 ======
def guess_and_translate(text: str):
    vi_chars = set(
        "ăâđêôơưÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆ"
        "ÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢ"
        "ÙÚỦŨỤƯỪỨỬỮỰ"
        "ỲÝỶỸỴàáảãạăằắẳẵặâầấẩẫậđ"
        "èéẻẽẹêềếểễệ"
        "ìíỉĩị"
        "òóỏõọôồốổỗộơờớởỡợ"
        "ùúủũụưừứửữự"
        "ỳýỷỹỵ"
    )
    is_vi = any(c in vi_chars for c in text)
    if is_vi:
        return "VI", deepl_translate(text, "JA")  # VI → JA
    else:
        return "JA", deepl_translate(text, "VI")  # JA → VI

# ====== かな/ローマ字変換 ======
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
# カタカナ→ひらがな
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

# pykakasi: ひらがな→ローマ字
_kakasi_roma = kakasi()
_kakasi_roma.setMode("H", "a")  # Hiragana to ascii romaji
_converter_roma = _kakasi_roma.getConverter()

# Sudachiの「記号」を“きごう”などに置換しない：文字はそのまま出す
def to_hiragana(text: str, spaced: bool = False) -> str:
    tokens = _sudachi.tokenize(text, _SPLIT)
    out = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]  # 名詞/動詞/記号 など
        surf = t.surface()

        # 英数・記号・空白はそのまま
        if pos0 in ["記号", "補助記号"] or re.fullmatch(r"[0-9A-Za-z]+", surf):
            out.append(surf)
            continue

        yomi = t.reading_form()
        hira = surf if yomi == "*" else yomi.translate(_katakana_to_hira)
        out.append(hira)

    return " ".join(out) if spaced else "".join(out)

def to_romaji(text: str, spaced: bool = False) -> str:
    hira_sp = to_hiragana(text, spaced=True)
    parts = [p for p in hira_sp.split(" ") if p]
    roma_parts = [_converter_roma.do(p) for p in parts]
    return " ".join(roma_parts) if spaced else "".join(roma_parts)

# ====== LINE 返信 ======
def reply_message(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply",
                  headers=headers, data=json.dumps(body), timeout=15)

# ====== 状態管理（メモリ） ======
state = {}
DEFAULTS = {"show_hira": True, "show_romaji": True}

def get_state(chat_id: str):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id: str, **kwargs):
    s = get_state(chat_id)
    for k, v in kwargs.items():
        if k in s:
            s[k] = v
    state[chat_id] = s
    return s

# ====== コマンド ======
def parse_command(text: str):
    t = text.strip().lower()
    if t == "/status": return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m: return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m: return ("romaji", m.group(2) == "on")
    return (None, None)

# ====== 健康チェック ======
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

# “[JP→VN] …” などのタグを先頭に書かれても除去
TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|VN|JA|VI)\s*[\-→]\s*(?:JP|VN|JA|VI)\s*\]\s*', re.IGNORECASE)

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""
    if (not signature) and (not body):  # verify ping対策
        return "OK", 200
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

        text = TAG_PREFIX_RE.sub("", msg.get("text") or "", count=1)

        # チャット単位の状態
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")
        s = get_state(chat_id)

        # コマンド処理
        cmd, val = parse_command(text)
        if cmd == "hira":
            set_state(chat_id, show_hira=val)
            reply_message(ev["replyToken"], f"Đã {'bật' if val else 'tắt'} hiển thị Hiragana.")
            continue
        if cmd == "romaji":
            set_state(chat_id, show_romaji=val)
            reply_message(ev["replyToken"], f"Đã {'bật' if val else 'tắt'} hiển thị Romaji.")
            continue
        if cmd == "status":
            s = get_state(chat_id)
            reply_message(
                ev["replyToken"],
                f"Cài đặt hiện tại\n- Hiragana: {'ON' if s['show_hira'] else 'OFF'}\n- Romaji: {'ON' if s['show_romaji'] else 'OFF'}"
            )
            continue

        # 翻訳
        src_lang, translated = guess_and_translate(text)

        lines = []
        if src_lang == "VI":
            lines.append("[VN→JP]")
            lines.append(translated)
            if s["show_hira"]:
                lines.append(f"(hiragana) {to_hiragana(translated, spaced=True)}")
            if s["show_romaji"]:
                lines.append(f"(romaji) {to_romaji(translated, spaced=True)}")
        else:
            lines.append("[JP→VN]")
            lines.append(translated)
            # JP→VN 側は指示がない限りふりがな等は付けない

        reply_message(ev["replyToken"], "\n".join(lines))

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
