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
# ひらがなは Sudachi（文脈で読み）＋ 記号/絵文字そのまま ＋ 価格だけ数→漢数字
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

# --- 数→漢数字ユーティリティ ---
_DIG = "零一二三四五六七八九"
_UNIT1 = ["", "十", "百", "千"]
_UNIT4 = ["", "万", "億", "兆"]

def _four_digits_to_kanji(n: int) -> str:
    assert 0 <= n <= 9999
    s = ""
    for i, u in enumerate(_UNIT1[::-1]):  # 千百十一
        d = (n // (10 ** (3 - i))) % 10
        if d == 0: continue
        s += ("" if (u and d == 1) else _DIG[d]) + u
    return s or _DIG[0]

def num_to_kanji(num: int) -> str:
    if num == 0: return _DIG[0]
    parts, i = [], 0
    while num > 0 and i < len(_UNIT4):
        n = num % 10000
        if n:
            head = _four_digits_to_kanji(n)
            if head != _DIG[0]:
                parts.append(head + _UNIT4[i])
        num //= 10000; i += 1
    return "".join(reversed(parts)) or _DIG[0]

# --- 価格だけ（円/ドン/VND）数値→漢数字にする ---
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
    def yen_symbol_sub(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(2)))
        return f"{num_kanji}円"
    def yen_after_sub(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(1)))
        return f"{num_kanji}円"
    def vnd_prefix_sub(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(1)))
        return f"{num_kanji}ドン"
    def vnd_after_sub(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(1)))
        return f"{num_kanji}ドン"
    def dong_prefix(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(1)))
        return f"{num_kanji}ドン"
    def dong_after(m):
        num_kanji = num_to_kanji(_digits_to_int(m.group(1)))
        return f"{num_kanji}ドン"

    text = _price_patterns[0].sub(yen_symbol_sub, text)
    text = _price_patterns[1].sub(yen_after_sub, text)
    text = _price_patterns[2].sub(vnd_prefix_sub, text)
    text = _price_patterns[3].sub(vnd_after_sub, text)
    text = _price_patterns[4].sub(dong_prefix, text)
    text = _price_patterns[5].sub(dong_after, text)
    return text

def to_hiragana(text: str) -> str:
    # 1) 価格だけ漢数字化（電話番号などの数字は触らない）
    text = convert_prices_to_kanji(text)

    # 2) Sudachiで文脈読みを取得。記号・絵文字・英数はそのまま残す
    tokens = _sudachi.tokenize(text, _SPLIT)
    result = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]
        surf = t.surface()
        if pos0 in ["記号", "補助記号", "未知語"] or surf.isalnum():
            result.append(surf)
            continue
        yomi = t.reading_form()
        result.append((surf if yomi == "*" else yomi.translate(_katakana_to_hira)))
    return "".join(result)

# --- ローマ字は「ひらがな化 → ローマ字」に一本化（誤読防止） ---
_kakasi_roma = kakasi()
_kakasi_roma.setMode("H", "a")  # ひらがな→ローマ字
_converter_roma = _kakasi_roma.getConverter()

def to_romaji(text: str) -> str:
    hira = to_hiragana(text)
    return _converter_roma.do(hira)

# ====== LINE 返信 ======
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
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

# ====== 方向タグ除去（先頭の [JP→VN]/[VN→JP] と旧 [JA→VI]/[VI→JA] を剥がす） ======
TAG_PREFIX_RE = re.compile(
    r'^\[\s*(?:JP|VN|JA|VI)\s*[\-→]\s*(?:JP|VN|JA|VI)\s*\]\s*',
    flags=re.IGNORECASE
)

# ====== Webhook ======
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

        # グループ招待などの非メッセージイベントは無視
        if etype == "join":
            # reply_message(ev["replyToken"], "招待ありがとう！JP⇔VN翻訳を始めます。")
            continue
        if etype != "message":
            continue

        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = (msg.get("text") or "").strip()
        text = TAG_PREFIX_RE.sub("", text, count=1)  # 先頭の方向タグを剥がす

        # チャットID（個人/グループ/ルーム）
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")

        # コマンド（/hira, /romaji, /status）
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
