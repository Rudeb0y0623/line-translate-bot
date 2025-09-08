import os, hmac, hashlib, base64, json, requests, re
from flask import Flask, request
from pykakasi import kakasi
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

# ========= 環境変数 =========
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

app = Flask(__name__)

# ========= 署名検証 =========
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")

# ========= DeepL 翻訳 =========
def deepl_translate(text: str, target_lang: str) -> str:
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]

# ========= 言語判定（超簡易）→ 翻訳 =========
VI_CHARS = set(
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
def guess_and_translate(text: str):
    is_vi = any(c in VI_CHARS for c in text)
    if is_vi:
        return "VI", deepl_translate(text, "JA")   # VI → JA
    else:
        return "JA", deepl_translate(text, "VI")   # JA → VI

# ========= 形態素・かな/ローマ字変換 =========
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

# pykakasi（かな/カナ/漢字→ローマ字）
_kakasi_roma = kakasi()
_kakasi_roma.setMode("H", "a")  # Hiragana -> roman
_kakasi_roma.setMode("K", "a")  # Katakana -> roman
_kakasi_roma.setMode("J", "a")  # Kanji -> roman
_romaji_conv = _kakasi_roma.getConverter()

# --- 数→漢数字（価格用） ---
_DIG = "零一二三四五六七八九"
_UNIT1 = ["", "十", "百", "千"]
_UNIT4 = ["", "万", "億", "兆"]

def _four_digits_to_kanji(n: int) -> str:
    s = ""
    for i, u in enumerate(_UNIT1[::-1]):  # 千百十一
        d = (n // (10 ** (3 - i))) % 10
        if d == 0:
            continue
        s += ("" if (u and d == 1) else _DIG[d]) + u
    return s or _DIG[0]

def num_to_kanji(num: int) -> str:
    if num == 0:
        return _DIG[0]
    parts = []
    i = 0
    while num > 0 and i < len(_UNIT4):
        n = num % 10000
        if n:
            parts.append(_four_digits_to_kanji(n) + _UNIT4[i])
        num //= 10000
        i += 1
    return "".join(reversed(parts)) or _DIG[0]

# 価格（円/ドン/VND）のみ漢数字化
_price_patterns = [
    re.compile(r"(¥)\s*(\d{1,3}(?:,\d{3})+|\d+)"),
    re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*円"),
    re.compile(r"(?:VND|vnd)\s*(\d{1,3}(?:[.,]\d{3})+|\d+)"),
    re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:VND|vnd)"),
    re.compile(r"[₫đ]\s*(\d{1,3}(?:[.,]\d{3})+|\d+)"),
    re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s*[₫đ]"),
]
def _digits_to_int(s: str) -> int:
    return int(re.sub(r"[^\d]", "", s))

def convert_prices_to_kanji(text: str) -> str:
    def yen_symbol_sub(m): return f"{num_to_kanji(_digits_to_int(m.group(2)))}円"
    def yen_after_sub(m):  return f"{num_to_kanji(_digits_to_int(m.group(1)))}円"
    def vnd_prefix(m):    return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def vnd_after(m):     return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    text = _price_patterns[0].sub(yen_symbol_sub, text)
    text = _price_patterns[1].sub(yen_after_sub, text)
    text = _price_patterns[2].sub(vnd_prefix, text)
    text = _price_patterns[3].sub(vnd_after, text)
    text = _price_patterns[4].sub(vnd_prefix, text)
    text = _price_patterns[5].sub(vnd_after, text)
    return text

# かな化（記号・空白はそのまま。spaced=True で語間スペース）
def to_hiragana(text: str, spaced: bool = False) -> str:
    text = convert_prices_to_kanji(text)
    tokens = _sudachi.tokenize(text, _SPLIT)
    parts = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]
        surf = t.surface()

        # 記号・未知語・英数はそのまま
        if pos0 in ["記号", "補助記号", "空白", "未知語"] or re.fullmatch(r"[0-9A-Za-z]+", surf):
            parts.append(surf)
            continue

        yomi = t.reading_form()
        hira = surf if yomi == "*" else yomi.translate(_katakana_to_hira)
        parts.append(hira)

    return " ".join(parts) if spaced else "".join(parts)

# 👉 記号が "kigou" にならないローマ字変換
#    かな/漢字だけをローマ字化し、記号・空白・数字・英字はそのまま残す
_JP_WORD_RE = re.compile(r'^[\u3040-\u30ff\u3400-\u9fff\u3005\u30fc]+$')  # 々・ー を含む
def to_romaji(text: str, spaced: bool = False) -> str:
    hira = to_hiragana(text, spaced=True)
    out = []
    for tok in hira.split(" "):
        if not tok:
            out.append("")
            continue
        if _JP_WORD_RE.fullmatch(tok):
            out.append(_romaji_conv.do(tok))
        else:
            out.append(tok)  # 記号・空白・数字・英字はそのまま
    roma = " ".join(out)
    return roma if spaced else roma.replace(" ", "")

# ========= LINE 返信 =========
def reply_message(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply",
                  headers=headers, data=json.dumps(body), timeout=15)

# ========= 状態管理 =========
state = {}
DEFAULTS = {"show_hira": True, "show_romaji": True}

def get_state(chat_id: str):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id: str, **kwargs):
    s = get_state(chat_id)
    s.update({k: v for k, v in kwargs.items() if k in s})
    state[chat_id] = s
    return s

def parse_command(text: str):
    t = text.strip().lower()
    if t == "/status": return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m: return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m: return ("romaji", m.group(2) == "on")
    return (None, None)

# ========= ルート =========
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

# ユーザーが頭に付ける [JP→VN] / [VN→JP] を除去
TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|JA|VN|VI)\s*[\-→]\s*(?:JP|JA|VN|VI)\s*\]\s*', re.IGNORECASE)

# ========= Webhook =========
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""
    if (not signature) and (not body):
        return "OK", 200
    if not verify_signature(body, signature):
        return "bad signature", 400

    data = request.get_json(silent=True) or {}
    for ev in data.get("events", []):
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = TAG_PREFIX_RE.sub("", msg.get("text") or "", count=1)

        # チャット単位の設定
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")

        # コマンド
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
        s = get_state(chat_id)

        lines = []
        if src_lang == "VI":
            lines.append("[VN→JP]")
            lines.append(translated)
            if s["show_hira"]:
                lines.append(f"\n(hiragana) {to_hiragana(translated, spaced=True)}")
            if s["show_romaji"]:
                lines.append(f"(romaji) {to_romaji(translated, spaced=True)}")
        else:
            lines.append("[JP→VN]")
            lines.append(translated)

        reply_message(ev["replyToken"], "\n".join(lines))

    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
