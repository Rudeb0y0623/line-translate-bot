# app.py ーー 翻訳専用（記号・スペース維持版）

import os
import hmac
import hashlib
import base64
import json
import re
import requests
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

# ====== かな/ローマ字変換：Sudachi + kakasi 準備 ======
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

_kakasi_roma = kakasi()
_kakasi_roma.setMode("H", "a")  # ひらがな→ローマ字
_converter_roma = _kakasi_roma.getConverter()

# ======（任意）価格だけ漢数字化ユーティリティ ======
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
    def vnd_prefix_sub(m): return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def vnd_after_sub(m):  return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def dong_prefix(m):    return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def dong_after(m):     return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    text = _price_patterns[0].sub(yen_symbol_sub, text)
    text = _price_patterns[1].sub(yen_after_sub, text)
    text = _price_patterns[2].sub(vnd_prefix_sub, text)
    text = _price_patterns[3].sub(vnd_after_sub, text)
    text = _price_patterns[4].sub(dong_prefix, text)
    text = _price_patterns[5].sub(dong_after, text)
    return text

# ====== ここが肝：記号・空白は原文のまま保つ ======
_WHITESPACE_RE = re.compile(r"^[\s\u3000]+$")  # 半角/全角の空白のみ

def to_hiragana(text: str, spaced: bool = False) -> str:
    # 金額だけ漢数字にしたい場合は残す。不要なら次行をコメントアウト
    text = convert_prices_to_kanji(text)

    tokens = _sudachi.tokenize(text, _SPLIT)
    out = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]
        surf = t.surface()

        # 空白はそのまま（半角/全角）
        if _WHITESPACE_RE.match(surf):
            out.append(surf)
            continue

        # 記号はそのまま
        if pos0 in ("記号", "補助記号"):
            out.append(surf)
            continue

        # 英数字はそのまま
        if re.fullmatch(r"[0-9A-Za-z]+", surf):
            out.append(surf)
            continue

        # 読みが取れない語はそのまま
        yomi = t.reading_form()
        if yomi == "*":
            out.append(surf)
            continue

        # カタカナ読み → ひらがなへ
        hira = yomi.translate(_katakana_to_hira)
        out.append(hira)

    joined = " ".join(out) if spaced else "".join(out)
    # 連続する半角スペースは 1 個へ（元の空白を潰さない）
    return re.sub(r"[ \t]{2,}", " ", joined)

def to_romaji(text: str, spaced: bool = False) -> str:
    # 記号・空白を保持したまま、単語ごとひらがな化
    hira_spaced = to_hiragana(text, spaced=True)
    parts = []
    for token in hira_spaced.split(" "):
        if token == "":
            continue
        # ひらがなのみ → ローマ字化。それ以外（記号・英数字・混在）は原文
        if re.fullmatch(r"[ぁ-んー]+", token):
            parts.append(_converter_roma.do(token) or token)
        else:
            parts.append(token)

    return " ".join(parts) if spaced else "".join(parts)

# ====== LINE 返信 ======
def reply_message(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}],
    }
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=headers,
        data=json.dumps(body),
        timeout=15,
    )

# ====== 状態管理（ひらがな/ローマ字 ON/OFF） ======
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
    if t == "/status":
        return ("status", None)
    m = re.match(r"^/(hira|h)\s+(on|off)$", t)
    if m:
        return ("hira", m.group(2) == "on")
    m = re.match(r"^/(romaji|r)\s+(on|off)$", t)
    if m:
        return ("romaji", m.group(2) == "on")
    return (None, None)

# ====== ルート ======
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

# 先頭に [JP→VN] 等が付いている場合は剥がす
TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|VN|JA|VI)\s*[\-→]\s*(?:JP|VN|JA|VI)\s*\]\s*', re.IGNORECASE)

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data() or b""
    if (not signature) and (not body):
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

        # チャット単位の設定キー
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")

        # コマンド
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
            reply_message(
                ev["replyToken"],
                f"現在の設定\n- ひらがな: {'ON' if s['show_hira'] else 'OFF'}\n- ローマ字: {'ON' if s['show_romaji'] else 'OFF'}",
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
                lines.append(f"\n(ひらがな) {to_hiragana(translated, spaced=True)}")
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
