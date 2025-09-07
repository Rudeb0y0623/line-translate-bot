import os, hmac, hashlib, base64, json, requests, re, csv, random, time
from flask import Flask, request

# ====== 変換系 ======
from pykakasi import kakasi
from sudachipy import dictionary, tokenizer as sudachi_tokenizer


# =========================
# 環境変数
# =========================
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
DEEPL_API_KEY = os.environ["DEEPL_API_KEY"]

# クイズCSVの場所（既定: data/ 配下）
DATA_DIR = os.getenv("QUIZ_DATA_DIR", "data")

app = Flask(__name__)


# =========================
# 署名検証
# =========================
def verify_signature(body: bytes, signature: str) -> bool:
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# =========================
# DeepL 翻訳
# =========================
def deepl_translate(text: str, target_lang: str) -> str:
    url = "https://api-free.deepl.com/v2/translate"
    data = {"auth_key": DEEPL_API_KEY, "text": text, "target_lang": target_lang}
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()["translations"][0]["text"]


# =========================
# 言語判定（超簡易）→ 翻訳
# =========================
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


# =========================
# かな/ローマ字変換（記号や括弧はそのまま）
# =========================
_sudachi = dictionary.Dictionary().create()
_SPLIT = sudachi_tokenizer.Tokenizer.SplitMode.C
_katakana_to_hira = str.maketrans({chr(k): chr(k - 0x60) for k in range(ord("ァ"), ord("ヶ") + 1)})

_kakasi_roma = kakasi()
_kakasi_roma.setMode("H", "a")
_converter_roma = _kakasi_roma.getConverter()

# --- 数→漢数字（価格用） ---
_DIG = "零一二三四五六七八九"
_UNIT1 = ["", "十", "百", "千"]
_UNIT4 = ["", "万", "億", "兆"]

def _four_digits_to_kanji(n: int) -> str:
    s = ""
    for i, u in enumerate(_UNIT1[::-1]):
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
    def yen_symbol_sub(m):   return f"{num_to_kanji(_digits_to_int(m.group(2)))}円"
    def yen_after_sub(m):    return f"{num_to_kanji(_digits_to_int(m.group(1)))}円"
    def vnd_prefix_sub(m):   return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def vnd_after_sub(m):    return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def dong_prefix(m):      return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    def dong_after(m):       return f"{num_to_kanji(_digits_to_int(m.group(1)))}ドン"
    text = _price_patterns[0].sub(yen_symbol_sub, text)
    text = _price_patterns[1].sub(yen_after_sub, text)
    text = _price_patterns[2].sub(vnd_prefix_sub, text)
    text = _price_patterns[3].sub(vnd_after_sub, text)
    text = _price_patterns[4].sub(dong_prefix, text)
    text = _price_patterns[5].sub(dong_after, text)
    return text

def to_hiragana(text: str, spaced: bool = False) -> str:
    text = convert_prices_to_kanji(text)
    tokens = _sudachi.tokenize(text, _SPLIT)
    words = []
    for t in tokens:
        pos0 = t.part_of_speech()[0]
        surf = t.surface()

        # 記号・括弧・句読点はそのまま
        if pos0 in ["記号", "補助記号"]:
            words.append(surf)
            continue
        # 英数字はそのまま
        if re.fullmatch(r"[0-9A-Za-z]+", surf):
            words.append(surf)
            continue

        yomi = t.reading_form()
        hira = surf if yomi == "*" else yomi.translate(_katakana_to_hira)
        words.append(hira)

    return " ".join(words) if spaced else "".join(words)

def to_romaji(text: str, spaced: bool = False) -> str:
    hira = to_hiragana(text, spaced=True)
    parts = [p for p in hira.split(" ") if p]
    roma_parts = [_converter_roma.do(p) for p in parts]
    return " ".join(roma_parts) if spaced else "".join(roma_parts)


# =========================
# LINE 返信
# =========================
def reply_message(reply_token: str, text: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    body = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    requests.post("https://api.line.me/v2/bot/message/reply", headers=headers, data=json.dumps(body), timeout=15)


# =========================
# 状態管理
# =========================
state = {}
DEFAULTS = {
    "show_hira": True,
    "show_romaji": True,
    "quiz_mode": None,     # None / reading / meaning / grammar
    "quiz_last": None      # {"mode":..., "question":..., "choices":[...], "answer_index":int, "explain":...}
}

def get_state(chat_id: str):
    if chat_id not in state:
        state[chat_id] = DEFAULTS.copy()
    return state[chat_id]

def set_state(chat_id: str, **kwargs):
    s = get_state(chat_id)
    s.update({k: v for k, v in kwargs.items() if k in s})
    state[chat_id] = s
    return s


# =========================
# クイズ（CSV読み込み）
# =========================
def _load_csv(path, expect_cols):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            fixed = {k: row.get(k, "") for k in expect_cols}
            rows.append(fixed)
    return rows

# Reading / Meaning ・・・ id,question_vi,kanji,choices,answer,explanation_vi
READING = _load_csv(os.path.join(DATA_DIR, "Reading.csv"),
                    ["id","question_vi","kanji","choices","answer","explanation_vi"])
MEANING = _load_csv(os.path.join(DATA_DIR, "Meaning.csv"),
                    ["id","question_vi","kanji","choices","answer","explanation_vi"])
# Grammar ・・・ id,question_vi,sentence,choices,answer,explanation_vi
GRAMMAR = _load_csv(os.path.join(DATA_DIR, "Grammar.csv"),
                    ["id","question_vi","sentence","choices","answer","explanation_vi"])

def _split_choices(choices_str):
    # "1) あ;2) い;3) う;4) え" → ["あ","い","う","え"]
    parts = [p.strip() for p in choices_str.split(";") if p.strip()]
    cleaned = [re.sub(r"^\d+\)\s*", "", p) for p in parts]
    return cleaned[:4]

def _pick_random(rows):
    return random.choice(rows) if rows else None

def _make_quiz_message_header(mode):
    if mode == "reading": return "📝 N5クイズ（読み）"
    if mode == "meaning": return "📝 N5クイズ（意味）"
    if mode == "grammar": return "📝 N5クイズ（文法）"
    return "📝 N5クイズ"

def _format_choices_for_line(choices):
    return "\n".join([f"{i+1}) {c}" for i,c in enumerate(choices)])

def start_quiz(chat_id, mode):
    set_state(chat_id, quiz_mode=mode, quiz_last=None)
    return next_quiz(chat_id)

def stop_quiz(chat_id):
    set_state(chat_id, quiz_mode=None, quiz_last=None)
    return "🏁 N5クイズを終了しました。おつかれさま！"

def next_quiz(chat_id):
    s = get_state(chat_id)
    mode = s["quiz_mode"]
    if mode not in ("reading","meaning","grammar"):
        return "まだクイズを開始していません。/quiz reading | /quiz meaning | /quiz grammar"

    if mode == "reading":
        row = _pick_random(READING)
        if not row: return "読みの問題がありません。"
        question = f"{row['question_vi']}\n「{row['kanji']}」"
    elif mode == "meaning":
        row = _pick_random(MEANING)
        if not row: return "意味の問題がありません。"
        question = f"{row['question_vi']}\n「{row['kanji']}」"
    else:  # grammar
        row = _pick_random(GRAMMAR)
        if not row: return "文法の問題がありません。"
        question = f"{row['question_vi']}\n{row['sentence']}"

    choices = _split_choices(row["choices"])
    # 正解番号（1-4）
    try:
        correct_index_original = int(str(row["answer"]).strip()) - 1
    except:
        correct_index_original = 0
    if not (0 <= correct_index_original < len(choices)):
        correct_index_original = 0

    # シャッフルして新しいインデックスを求める
    indexed = list(enumerate(choices))  # [(0,"a"),(1,"b"),...]
    random.shuffle(indexed)
    shuffled = [c for _, c in indexed]
    # 元の正解がどこへ行ったか
    new_answer_index = next((i for i,(orig_idx,_) in enumerate(indexed) if orig_idx == correct_index_original), 0)

    explain = (row.get("explanation_vi") or "").strip()

    # 状態保存
    s["quiz_last"] = {
        "mode": mode,
        "question": question,
        "choices": shuffled,
        "answer_index": new_answer_index,  # 0-based
        "explain": explain
    }
    set_state(chat_id, **s)

    header = _make_quiz_message_header(mode)
    msg = (
        f"{header}\n{question}\n\n"
        f"{_format_choices_for_line(shuffled)}\n\n"
        "👉 1〜4 の数字で回答してください。"
    )
    return msg

def judge_and_next(chat_id, user_text):
    m = re.fullmatch(r"\s*([1-4])\s*", user_text)
    if not m:
        return None  # 数字じゃない → クイズ文脈ではない
    sel = int(m.group(1)) - 1  # 0-based

    s = get_state(chat_id)
    last = s.get("quiz_last")
    if not last:
        return "直前の問題が見つかりません。/quiz で開始してください。"

    answer_index = last["answer_index"]
    choices = last["choices"]
    explain = last["explain"]

    correct = (sel == answer_index)
    head = "✅ 正解！" if correct else "❌ 不正解…"
    correct_text = f"{answer_index+1}) {choices[answer_index]}"

    body = head + "\n" + f"正解：{correct_text}"
    if explain:
        body += f"\n解説：{explain}"

    # 次の問題を自動で
    nxt = next_quiz(chat_id)
    return body + "\n\n" + nxt


# =========================
# コマンド解析
# =========================
def parse_command(text: str):
    t = text.strip()
    tl = t.lower()

    # 設定表示
    if tl == "/status":
        return ("status", None)

    # ひらがな on/off
    m = re.match(r"^/(hira|h)\s+(on|off)$", tl)
    if m:
        return ("hira", m.group(2) == "on")

    # ローマ字 on/off
    m = re.match(r"^/(romaji|r)\s+(on|off)$", tl)
    if m:
        return ("romaji", m.group(2) == "on")

    # クイズ開始（カテゴリ）
    if tl.startswith("/quiz"):
        parts = tl.split()
        if len(parts) == 1:
            return ("quiz_help", None)
        cat = parts[1]
        if cat in ("reading","meaning","grammar"):
            return ("quiz_start", cat)
        if cat in ("stop","end","finish"):
            return ("quiz_stop", None)
        return ("quiz_help", None)

    # 日本語UIで開始したい場合の別名（任意）
    if t in ("N5読み", "N5 読み", "N5読み方"):   return ("quiz_start", "reading")
    if t in ("N5意味", "N5 意味"):             return ("quiz_start", "meaning")
    if t in ("N5文法", "N5 文法"):             return ("quiz_start", "grammar")
    if t in ("N5終了", "N5 終了"):             return ("quiz_stop", None)

    return (None, None)


# =========================
# ルート
# =========================
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

TAG_PREFIX_RE = re.compile(r'^\[\s*(?:JP|VN|JA|VI)\s*[\-→]\s*(?:JP|VN|JA|VI)\s*\]\s*', re.IGNORECASE)

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

        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId") or src.get("userId")
        s = get_state(chat_id)

        # ---- コマンド処理 ----
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
            reply_message(
                ev["replyToken"],
                "現在の設定\n"
                f"- ひらがな: {'ON' if s['show_hira'] else 'OFF'}\n"
                f"- ローマ字: {'ON' if s['show_romaji'] else 'OFF'}\n"
                f"- クイズ: {s['quiz_mode'] or 'OFF'}"
            )
            continue

        if cmd == "quiz_help":
            reply_message(
                ev["replyToken"],
                "クイズの使い方:\n"
                "/quiz reading ・・・ 読み（よみ）\n"
                "/quiz meaning ・・・ 意味（いみ）\n"
                "/quiz grammar ・・・ 文法（ぶんぽう）\n"
                "/quiz stop ・・・ 終了\n"
                "回答は 1〜4 の数字を送ってください。"
            )
            continue

        if cmd == "quiz_start":
            mode = val  # "reading" | "meaning" | "grammar"
            msgtxt = start_quiz(chat_id, mode)
            reply_message(ev["replyToken"], msgtxt)
            continue

        if cmd == "quiz_stop":
            msgtxt = stop_quiz(chat_id)
            reply_message(ev["replyToken"], msgtxt)
            continue

        # クイズ中なら回答（数字）判定を先に
        if s.get("quiz_mode"):
            judged = judge_and_next(chat_id, text)
            if judged is not None:
                reply_message(ev["replyToken"], judged)
                continue

        # ---- 翻訳（通常動作） ----
        src_lang, translated = guess_and_translate(text)

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
