# app.py
import os, hmac, hashlib, base64, json, requests, re
from flask import Flask, request

# 日本語→読み/ローマ字
from sudachipy import dictionary, tokenizer as sudachi_tokenizer
from pykakasi import kakasi

# ========= 環境変数 =========
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

app = Flask(__name__)

# ========= 署名検証 =========
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

# ========= DeepL 翻訳 =========
def deepl_translate(text: str, target_lang: str) -> str:
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]

# ========= 言語推定（簡易）→ 翻訳 =========
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
        return "VI", deepl_translate(text, "JA")     # VI → JA
    else:
        return "JA", deepl_translate(text, "VI")     # JA → VI

# ========= かな/ローマ字変換 =========
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
# カタカナ→ひらがな
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

# pykakasi（ローマ字）
_kakasi = kakasi()
_kakasi.setMode("H", "a")   # ひらがな→ローマ字
_kakasi.setMode("K", "a")   # カタカナ→ローマ字
_kakasi.setMode("J", "a")   # 漢字→ローマ字
_romaji_conv = _kakasi.getConverter()

def _tokenize_to_hiragana_tokens(text: str):
    """
    Sudachi で分かち書き。記号・空白は「そのまま残す」＝『記号』という文字列は挿入しない。
    """
    tokens = _sudachi.tokenize(text, _SPLIT)
    result = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]   # 品詞大分類
        surf = t.surface()

        # 数字・英字はそのまま
        if re.fullmatch(r"[0-9A-Za-z]+", surf):
            result.append(surf)
            continue

        # 記号・補助記号・空白はそのまま（← ここで『記号』という語を入れない）
        if pos0 in ("記号", "補助記号"):
            result.append(surf)
            continue

        yomi = t.reading_form()
        hira = surf if (yomi == "*" or not yomi) else yomi.translate(_katakana_to_hira)
        result.append(hira)
    return result

def to_hiragana(text: str, spaced: bool = False) -> str:
    parts = _tokenize_to_hiragana_tokens(text)
    if spaced:
        # 全角スペースは半角に統一
        return " ".join(parts).replace("　", " ")
    return "".join(parts).replace("　", " ")

def to_romaji(text: str, spaced: bool = False) -> str:
    # ひらがなトークン列をベースにローマ字化（スペースはそのまま）
    hira = to_hiragana(text, spaced=True)
    roma = " ".join(_romaji_conv.do(p) for p in hira.split(" "))
    return roma if spaced else roma.replace(" ", "")

# ========= LINE 返信 =========
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply",
                  headers=headers, data=json.dumps(body), timeout=15)

# ========= 状態管理（ひらがな/ローマ字の独立ON/OFF） =========
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

# コマンド解析：スペース区切りで複数コマンド同時可（/h on /r off など）
def parse_commands(text: str):
    tokens = text.strip().split()
    cmds = []
    i = 0
    while i < len(tokens):
        t = tokens[i].lower()
        if t in ("/hira", "/h", "/romaji", "/r"):
            # 次トークンが on/off なら取得
            val = None
            if i + 1 < len(tokens) and tokens[i+1].lower() in ("on", "off"):
                val = (tokens[i+1].lower() == "on")
                i += 1
            cmds.append((t[1], val))  # 'h' or 'r'
        elif t == "/status":
            cmds.append(("status", None))
        i += 1
    return cmds

# ========= ルート =========
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|JA|VN|VI)\s*[\-→]\s*(?:JP|JA|VN|VI)\s*\]\s*', re.IGNORECASE)

# ========= Webhook =========
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

        # タグ [JP→VN] 等を先頭から除去
        text = TAG_PREFIX_RE.sub("", msg.get("text") or "", count=1)

        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")
        s = get_state(chat_id)

        # ----- コマンド処理（複数可）-----
        executed_command = False
        for c, val in parse_commands(text):
            executed_command = True
            if c == "h":
                if val is None:
                    # 値未指定ならトグル
                    val = not s["show_hira"]
                set_state(chat_id, show_hira=val)
                reply_message(
                    ev["replyToken"],
                    f"Đã {'bật' if val else 'tắt'} hiển thị Hiragana."
                )
            elif c == "r":
                if val is None:
                    val = not s["show_romaji"]
                set_state(chat_id, show_romaji=val)
                reply_message(
                    ev["replyToken"],
                    f"Đã {'bật' if val else 'tắt'} hiển thị Romaji."
                )
            elif c == "status":
                s = get_state(chat_id)
                reply_message(
                    ev["replyToken"],
                    "Cài đặt hiện tại\n"
                    f"- Hiragana: {'ON' if s['show_hira'] else 'OFF'}\n"
                    f"- Romaji: {'ON' if s['show_romaji'] else 'OFF'}"
                )
        if executed_command:
            # コマンドだった場合はここで次イベントへ（翻訳はしない）
            continue

        # ----- 翻訳 -----
        src_lang, translated = guess_and_translate(text)
        s = get_state(chat_id)

        lines = []
        if src_lang == "VI":
            # VI→JA（読み/ローマ字は日本語文に対してだけ付与）
            lines.append("[VN→JP]")
            lines.append(translated)
            if s["show_hira"]:
                lines.append(f"\n(hiragana) {to_hiragana(translated, spaced=True)}")
            if s["show_romaji"]:
                lines.append(f"(romaji) {to_romaji(translated, spaced=True)}")
        else:
            # JA→VI（読み付与はしない）
            lines.append("[JP→VN]")
            lines.append(translated)

        reply_message(ev["replyToken"], "\n".join(lines))

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
