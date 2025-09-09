import os, hmac, hashlib, base64, json, requests, re
from flask import Flask, request
from pykakasi import kakasi
from sudachipy import dictionary, tokenizer as sudachi_tokenizer

# ========= 環境変数 =========
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]
OCRSPACE_API_KEY = os.environ.get("OCRSPACE_API_KEY", "")  # ← 追加（必須）

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

# ========= 言語判定 =========
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
        return "VI", deepl_translate(text, "JA")
    else:
        return "JA", deepl_translate(text, "VI")

# ========= 形態素・かな/ローマ字 =========
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

_kakasi = kakasi()
_kakasi.setMode("H", "a")  # ひらがな→ローマ字
_romaji_conv = _kakasi.getConverter()

# ===== 価格を漢数字に（必要箇所のみ） =====
_DIG = "零一二三四五六七八九"
_UNIT1 = ["", "十", "百", "千"]
_UNIT4 = ["", "万", "億", "兆"]
def _four_digits_to_kanji(n: int) -> str:
    s = ""
    for i, u in enumerate(_UNIT1[::-1]):
        d = (n // (10 ** (3 - i))) % 10
        if d == 0: continue
        s += ("" if (u and d == 1) else _DIG[d]) + u
    return s or _DIG[0]
def num_to_kanji(num: int) -> str:
    if num == 0: return _DIG[0]
    parts = []; i = 0
    while num > 0 and i < len(_UNIT4):
        n = num % 10000
        if n: parts.append(_four_digits_to_kanji(n) + _UNIT4[i])
        num //= 10000; i += 1
    return "".join(reversed(parts)) or _DIG[0]
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
    def yen_symbol(m): return f"{num_to_kanji(_digits_to_int(m.group(2)))}円"
    def yen_after(m):  return f"{num_to_kanji(_digits_to_int(m.group(1)))}円"
    def vnd_pre(m):    return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def vnd_post(m):   return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    text = _price_patterns[0].sub(yen_symbol, text)
    text = _price_patterns[1].sub(yen_after, text)
    text = _price_patterns[2].sub(vnd_pre, text)
    text = _price_patterns[3].sub(vnd_post, text)
    text = _price_patterns[4].sub(vnd_pre, text)
    text = _price_patterns[5].sub(vnd_post, text)
    return text

# ====== “きごう完全封じ” かな・ローマ字 ======
def _is_whitespace(s: str) -> bool:
    return bool(s) and all(ch.isspace() for ch in s)

def _token_to_hira(t) -> str:
    pos0 = t.part_of_speech()[0]
    surf = t.surface()
    if _is_whitespace(surf):  # 空白は捨てる
        return ""
    if pos0 in ("記号", "補助記号"):  # 記号はそのまま
        return surf
    if re.fullmatch(r"[0-9A-Za-z]+", surf):  # 英数字はそのまま
        return surf
    yomi = t.reading_form()
    return surf if yomi == "*" else yomi.translate(_katakana_to_hira)

def to_hiragana(text: str, spaced: bool = False) -> str:
    text = convert_prices_to_kanji(text).replace("\u3000", " ")
    tokens = list(_sudachi.tokenize(text, _SPLIT))
    chunks = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _is_whitespace(t.surface()):
            i += 1; continue
        merged = _token_to_hira(t)
        j = i + 1
        while j < len(tokens):
            tj = tokens[j]
            if _is_whitespace(tj.surface()):
                j += 1; continue
            if tj.part_of_speech()[0] == "助動詞":
                merged += _token_to_hira(tj); j += 1
            else:
                break
        if merged:
            chunks.append(merged)
        i = j
    if spaced:
        out = " ".join(chunks)
        out = re.sub(r"\s+", " ", out).strip()
        out = re.sub(r"\s+([、。！？!?])", r"\1", out)
        return out
    return "".join(chunks)

_HIRA_CHAR = re.compile(r"[\u3041-\u3096\u309D\u309E\u30FC]")  # ひらがな＋長音
def to_romaji(text: str, spaced: bool = False) -> str:
    # 1文字単位で変換して「ひらがな混入」を完全排除
    hira_spaced = to_hiragana(text, spaced=True)
    out_parts = []
    for tok in hira_spaced.split(" "):
        buf = []
        for ch in tok:
            if _HIRA_CHAR.fullmatch(ch):
                buf.append(_romaji_conv.do(ch))
            else:
                buf.append(ch)
        out_parts.append("".join(buf))
    out = " ".join(out_parts)
    out = re.sub(r"\s+([,.\u3001\u3002!?])", r"\1", out)
    return out if spaced else out.replace(" ", "")

# ========= LINE 返信 =========
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
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
    for k, v in kwargs.items():
        if k in s and isinstance(v, bool):
            s[k] = v
    state[chat_id] = s
    return s
def parse_command(text: str):
    t = (text or "").strip().lower()
    if t == "/status": return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m: return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m: return ("romaji", m.group(2) == "on")
    return (None, None)

# ========= ヘルス =========
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|JA|VN|VI)\s*[\-→]\s*(?:JP|JA|VN|VI)\s*\]\s*', re.IGNORECASE)

# ========= OCR.space 画像→文字 =========
def ocrspace_parse_image(image_bytes: bytes) -> str:
    if not OCRSPACE_API_KEY:
        return ""
    url = "https://api.ocr.space/parse/image"
    files = {"file": ("image.jpg", image_bytes)}
    data = {
        "apikey": OCRSPACE_API_KEY,
        "language": "jpn,eng,vie",  # 日本語・英語・ベトナム語
        "isOverlayRequired": False,
        "OCREngine": 2,   # 現行エンジン
        "scale": True,
        "detectOrientation": True,
    }
    r = requests.post(url, files=files, data=data, timeout=60)
    r.raise_for_status()
    jr = r.json()
    if not jr.get("IsErroredOnProcessing") and jr.get("ParsedResults"):
        text = jr["ParsedResults"][0].get("ParsedText", "")
        # OCRは改行が多く混ざるので軽く整える（空行圧縮）
        text = re.sub(r"\r", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text
    return ""

# ========= LINEの画像データ取得 =========
def get_line_message_content(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.content

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
        mtype = msg.get("type")

        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")
        s = get_state(chat_id)

        # ===== 画像 → OCR → 翻訳 =====
        if mtype == "image":
            try:
                img_bytes = get_line_message_content(msg["id"])
                ocr_text = ocrspace_parse_image(img_bytes)
            except Exception:
                ocr_text = ""

            if not ocr_text.strip():
                reply_message(ev["replyToken"], "[画像OCR] 文字が見つかりませんでした。")
                continue

            src_lang, translated = guess_and_translate(ocr_text)
            out = []
            out.append("[OCR原文]")
            out.append(ocr_text[:1500])  # 長すぎる原文は少し抑制
            if src_lang == "VI":
                out.append("\n[VN→JP]")
                out.append(translated)
                if s["show_hira"]:
                    out.append(f"\n(hiragana) {to_hiragana(translated, spaced=True)}")
                if s["show_romaji"]:
                    out.append(f"(romaji) {to_romaji(translated, spaced=True)}")
            else:
                out.append("\n[JP→VN]")
                out.append(translated)
            reply_message(ev["replyToken"], "\n".join(out))
            continue

        # ===== テキスト =====
        if mtype != "text":
            continue

        text = TAG_PREFIX_RE.sub("", msg.get("text") or "", count=1)

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
            reply_message(ev["replyToken"],
                          f"Cài đặt hiện tại\n- Hiragana: {'ON' if s['show_hira'] else 'OFF'}\n- Romaji: {'ON' if s['show_romaji'] else 'OFF'}")
            continue

        src_lang, translated = guess_and_translate(text)
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
