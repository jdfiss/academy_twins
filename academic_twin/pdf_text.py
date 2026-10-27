"""從 PDF 取出逐頁文字，用來核對 AI 宣稱的原文與頁碼。（A 組）

只用標準函式庫，支援常見的文字型 PDF：
- 本專案虛構儀表板（Edge／Chrome 以 Skia 輸出：Type0／Identity-H 子集字型、ToUnicode、FlateDecode）；
- Word／Excel 匯出的公開規章（另含物件串流 ObjStm、WinAnsi 單位元組字型）。

取出的文字是「近似」：依文字矩陣的 y 座標變化插入換行、同一行內換位置時插入空白，不保證閱讀順序
與版面完全一致，所以核對一律在 NFKC 正規化並去除空白後比對（見 normalize）。
無法取出文字時（加密、掃描影像、不支援的壓縮或字型編碼）回報 reason，呼叫端應顯示「無法自動核對」，
不能當成原文不符。核對結果只是提示，原文仍須承辦人對照文件確認。
"""
from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass, field

# ---------- 物件剖析 ----------
WHITESPACE = b" \t\r\n\x0c\x00"
DELIMS = b"()<>[]{}/%"


class Name(str):
    """PDF 名稱物件（/Font 之類），與一般字串區分。"""


class Op(str):
    """內容串流的運算子或關鍵字（BT、Tj、obj、R…）。"""


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0


@dataclass
class Stream:
    dict: dict
    raw: bytes


class PdfError(Exception):
    pass


class Lexer:
    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def _skip(self) -> None:
        data, n = self.data, len(self.data)
        while self.pos < n:
            c = data[self.pos]
            if c in WHITESPACE:
                self.pos += 1
            elif c == 0x25:  # % 註解
                while self.pos < n and data[self.pos] not in b"\r\n":
                    self.pos += 1
            else:
                break

    def token(self):
        """回傳下一個基本記號；陣列與字典的括號以 Op 表示。檔尾回傳 None。"""
        self._skip()
        data, n = self.data, len(self.data)
        if self.pos >= n:
            return None
        c = data[self.pos]
        if c == 0x2F:  # /
            start = self.pos = self.pos + 1
            while self.pos < n and data[self.pos] not in WHITESPACE and data[self.pos] not in DELIMS:
                self.pos += 1
            raw = data[start:self.pos]
            return Name(re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), raw)
                        .decode("latin-1"))
        if c == 0x28:  # (
            return self._literal()
        if c == 0x3C:  # <
            if data[self.pos + 1:self.pos + 2] == b"<":
                self.pos += 2
                return Op("<<")
            end = data.find(b">", self.pos)
            if end < 0:
                raise PdfError("十六進位字串未結束")
            hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data[self.pos + 1:end])
            self.pos = end + 1
            if len(hexs) % 2:
                hexs += b"0"
            return bytes.fromhex(hexs.decode("ascii"))
        if c == 0x3E:  # >
            if data[self.pos + 1:self.pos + 2] == b">":
                self.pos += 2
                return Op(">>")
            self.pos += 1
            return Op(">")
        if c in b"[]{}":
            self.pos += 1
            return Op(chr(c))
        if c == 0x29:  # 不成對的 )，略過
            self.pos += 1
            return self.token()
        start = self.pos
        while self.pos < n and data[self.pos] not in WHITESPACE and data[self.pos] not in DELIMS:
            self.pos += 1
        word = data[start:self.pos]
        if re.fullmatch(rb"[+-]?\d+", word):
            return int(word)
        if re.fullmatch(rb"[+-]?(\d+\.\d*|\.\d+|\d+\.)", word):
            return float(word)
        return Op(word.decode("latin-1"))

    def _literal(self) -> bytes:
        data, n = self.data, len(self.data)
        self.pos += 1
        out = bytearray()
        depth = 1
        escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
        while self.pos < n:
            c = data[self.pos]
            self.pos += 1
            if c == 0x5C:  # 反斜線
                if self.pos >= n:
                    break
                e = data[self.pos]
                self.pos += 1
                if e in escapes:
                    out.append(escapes[e])
                elif 0x30 <= e <= 0x37:
                    digits = bytes([e])
                    while len(digits) < 3 and self.pos < n and 0x30 <= data[self.pos] <= 0x37:
                        digits += bytes([data[self.pos]])
                        self.pos += 1
                    out.append(int(digits, 8) & 0xFF)
                elif e == 0x0D:  # 行接續
                    if data[self.pos:self.pos + 1] == b"\n":
                        self.pos += 1
                elif e != 0x0A:
                    out.append(e)
            elif c == 0x28:
                depth += 1
                out.append(c)
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    break
                out.append(c)
            else:
                out.append(c)
        return bytes(out)

    def object(self, tok=None):
        """讀一個完整物件（含陣列、字典、間接參照 N G R）。"""
        tok = self.token() if tok is None else tok
        if isinstance(tok, Op):
            if tok == "[":
                items = []
                while True:
                    t = self.token()
                    if t is None or t == "]":
                        return self._refs(items)
                    items.append(self.object(t))
            if tok == "<<":
                items = []
                while True:
                    t = self.token()
                    if t is None or t == ">>":
                        break
                    items.append(self.object(t))
                items = self._refs(items)
                return {items[i]: items[i + 1] for i in range(0, len(items) - 1, 2) if isinstance(items[i], Name)}
        return tok

    @staticmethod
    def _refs(items: list) -> list:
        """把 [... 12 0 R ...] 合併成 Ref。"""
        out = []
        for item in items:
            if (item == "R" and isinstance(item, Op) and len(out) >= 2 and type(out[-1]) is int
                    and type(out[-2]) is int):
                gen, num = out.pop(), out.pop()
                out.append(Ref(num, gen))
            else:
                out.append(item)
        return out


OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class Document:
    def __init__(self, data: bytes):
        if not data.startswith(b"%PDF"):
            raise PdfError("不是 PDF 檔案")
        self.data = data
        self.objects: dict[int, object] = {}
        self.trailer: dict = {}
        self._scan()

    def _scan(self) -> None:
        """依序掃描檔案中的間接物件，不依賴 xref（後出現的同編號物件覆蓋前者，符合增量更新）。"""
        data, pos = self.data, 0
        while True:
            m = OBJ_RE.search(data, pos)
            if not m:
                break
            lex = Lexer(data, m.end())
            try:
                value = lex.object()
                after = lex.token()
                if after == "stream" and isinstance(value, dict):
                    start = lex.pos
                    if data[start:start + 2] == b"\r\n":
                        start += 2
                    elif data[start:start + 1] in (b"\n", b"\r"):
                        start += 1
                    length = value.get("Length")
                    end = start + length if type(length) is int else -1
                    if end < 0 or data.find(b"endstream", end, end + 32) < 0:
                        end = data.find(b"endstream", start)
                        if end < 0:
                            raise PdfError("串流未結束")
                    value = Stream(value, data[start:end])
                    pos = end
                else:
                    pos = lex.pos
            except (PdfError, ValueError, IndexError):
                pos = m.end()
                continue
            self.objects[int(m.group(1))] = value
        for m in re.finditer(rb"trailer\s*<<", data):
            lex = Lexer(data, m.end() - 2)
            try:
                self.trailer.update(lex.object())
            except (PdfError, ValueError, IndexError):
                pass
        for value in list(self.objects.values()):
            if isinstance(value, Stream) and value.dict.get("Type") == "XRef":
                self.trailer = {**value.dict, **self.trailer}
        for num, value in list(self.objects.items()):
            if isinstance(value, Stream) and value.dict.get("Type") == "ObjStm":
                self._objstm(value)

    def _objstm(self, stream: Stream) -> None:
        try:
            body = self.stream_data(stream)
            n, first = int(self.get(stream.dict["N"])), int(self.get(stream.dict["First"]))
            lex = Lexer(body)
            header = [lex.token() for _ in range(2 * n)]
            for i in range(n):
                num, off = header[2 * i], header[2 * i + 1]
                if num not in self.objects:  # 直接物件優先
                    self.objects[num] = Lexer(body, first + off).object()
        except (PdfError, ValueError, IndexError, KeyError, TypeError, zlib.error):
            pass

    def get(self, value):
        seen = 0
        while isinstance(value, Ref):
            value = self.objects.get(value.num)
            seen += 1
            if seen > 32:
                return None
        return value

    def stream_data(self, stream: Stream) -> bytes:
        filters = self.get(stream.dict.get("Filter"))
        filters = [] if filters is None else filters if isinstance(filters, list) else [filters]
        data = stream.raw
        for f in filters:
            f = self.get(f)
            if f in ("FlateDecode", "Fl"):
                data = _inflate(data)
            else:
                raise PdfError(f"不支援的壓縮格式 {f}")
        return data

    def pages(self) -> list[dict]:
        """依頁面樹順序回傳頁面字典，並展開可繼承的 Resources。"""
        root = self.get(self.trailer.get("Root"))
        if not isinstance(root, dict):
            root = next((v for v in self.objects.values() if isinstance(v, dict) and v.get("Type") == "Catalog"), None)
        if not isinstance(root, dict):
            raise PdfError("找不到文件目錄")
        out: list[dict] = []
        seen: set[int] = set()

        def walk(node_ref, inherited):
            if isinstance(node_ref, Ref):
                if node_ref.num in seen:
                    return
                seen.add(node_ref.num)
            node = self.get(node_ref)
            if not isinstance(node, dict):
                return
            res = node.get("Resources", inherited)
            if node.get("Type") == "Pages" or "Kids" in node:
                for kid in self.get(node.get("Kids")) or []:
                    walk(kid, res)
            else:
                out.append({**node, "Resources": res})

        walk(root.get("Pages"), None)
        return out


def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        d = zlib.decompressobj()
        out = d.decompress(data)  # 尾端損毀時保留已解出的部分
        if not out:
            raise
        return out


# ---------- 字型與文字解碼 ----------
REPLACEMENT = "�"
GLYPH_NAMES = {"space": " ", "hyphen": "-", "period": ".", "comma": ",", "colon": ":", "semicolon": ";",
               "slash": "/", "parenleft": "(", "parenright": ")", "percent": "%", "ampersand": "&",
               "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
               "seven": "7", "eight": "8", "nine": "9", "quotesingle": "'", "quotedbl": '"', "plus": "+",
               "equal": "=", "underscore": "_", "bullet": "•", "endash": "–", "emdash": "—"}


class Font:
    def __init__(self, doc: Document, font: dict):
        self.two_byte = font.get("Subtype") == "Type0"
        self.cmap: dict[int, str] = {}
        self.utf16 = False
        self.simple: dict[int, str] = {}
        self.widths: dict[int, float] = {}
        self.default_width = 1000.0 if self.two_byte else 500.0
        self._widths(doc, font)
        encoding = doc.get(font.get("Encoding"))
        tounicode = doc.get(font.get("ToUnicode"))
        if isinstance(tounicode, Stream):
            try:
                self.cmap = parse_cmap(doc.stream_data(tounicode))
            except (PdfError, zlib.error):
                self.cmap = {}
        if self.two_byte:
            if isinstance(encoding, str) and re.search(r"UCS2|UTF16", encoding):
                self.utf16 = True
        else:
            base = encoding.get("BaseEncoding") if isinstance(encoding, dict) else encoding
            codec = "cp1252" if base in (None, "WinAnsiEncoding", "StandardEncoding") else "mac_roman" \
                if base == "MacRomanEncoding" else "latin-1"
            for code in range(256):
                self.simple[code] = bytes([code]).decode(codec, errors="replace")
            if isinstance(encoding, dict):
                code = 0
                for item in doc.get(encoding.get("Differences")) or []:
                    item = doc.get(item)
                    if type(item) is int:
                        code = item
                    elif isinstance(item, Name):
                        self.simple[code] = _glyph(item)
                        code += 1

    def _widths(self, doc: Document, font: dict) -> None:
        """字寬（千分之一字級）只用來判斷字間是否有空隙，取不到時用預設值。"""
        try:
            if self.two_byte:
                desc = doc.get((doc.get(font.get("DescendantFonts")) or [None])[0]) or {}
                self.default_width = float(doc.get(desc.get("DW")) or 1000)
                w = doc.get(desc.get("W")) or []
                i = 0
                while i + 1 < len(w):
                    first, nxt = doc.get(w[i]), doc.get(w[i + 1])
                    if isinstance(nxt, list):
                        for k, v in enumerate(nxt):
                            self.widths[first + k] = float(doc.get(v))
                        i += 2
                    else:
                        for code in range(first, int(nxt) + 1):
                            self.widths[code] = float(doc.get(w[i + 2]))
                        i += 3
            else:
                first = doc.get(font.get("FirstChar")) or 0
                for k, v in enumerate(doc.get(font.get("Widths")) or []):
                    self.widths[first + k] = float(doc.get(v))
        except (TypeError, ValueError, IndexError, AttributeError):
            pass

    def advance(self, raw: bytes) -> float:
        codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]             if self.two_byte else list(raw)
        return sum(self.widths.get(c, self.default_width) for c in codes) / 1000

    def decode(self, raw: bytes) -> tuple[str, int, int]:
        """回傳（文字, 字元數, 無法解碼數）。"""
        if self.two_byte:
            codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
        else:
            codes = list(raw)
        out, bad = [], 0
        for code in codes:
            if code in self.cmap:
                ch = self.cmap[code]
            elif self.utf16:
                ch = chr(code) if not 0xD800 <= code < 0xE000 else REPLACEMENT
            elif not self.two_byte and not self.cmap and code in self.simple:
                ch = self.simple[code]
            elif not self.two_byte and code in self.simple and 0x20 <= code < 0x7F:
                ch = self.simple[code]  # ToUnicode 漏列的 ASCII，退回基本編碼
            else:
                ch = REPLACEMENT
            if REPLACEMENT in ch:
                bad += 1
            out.append(ch)
        return "".join(out), len(codes), bad


def _glyph(name: str) -> str:
    if name in GLYPH_NAMES:
        return GLYPH_NAMES[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4})", name) or re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    return chr(int(m.group(1), 16)) if m else REPLACEMENT


def _utf16(raw: bytes) -> str:
    return raw.decode("utf-16-be", errors="replace") if len(raw) % 2 == 0 else REPLACEMENT


def parse_cmap(data: bytes) -> dict[int, str]:
    """解析 ToUnicode CMap 的 bfchar 與 bfrange（含陣列形式）。"""
    cmap: dict[int, str] = {}
    lex = Lexer(data)
    toks = []
    while True:
        t = lex.token()
        if t is None:
            break
        toks.append(t)
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "beginbfchar":
            i += 1
            while i + 1 < len(toks) and toks[i] != "endbfchar":
                src, dst = toks[i], toks[i + 1]
                if isinstance(src, bytes) and isinstance(dst, bytes):
                    cmap[int.from_bytes(src, "big")] = _utf16(dst)
                i += 2
        elif t == "beginbfrange":
            i += 1
            while i + 2 < len(toks) and toks[i] != "endbfrange":
                lo, hi, dst = toks[i], toks[i + 1], toks[i + 2]
                if dst == "[":  # 陣列形式
                    j = i + 3
                    items = []
                    while j < len(toks) and toks[j] != "]":
                        items.append(toks[j])
                        j += 1
                    if isinstance(lo, bytes):
                        start = int.from_bytes(lo, "big")
                        for k, item in enumerate(items):
                            if isinstance(item, bytes):
                                cmap[start + k] = _utf16(item)
                    i = j + 1
                    continue
                if isinstance(lo, bytes) and isinstance(hi, bytes) and isinstance(dst, bytes):
                    a, b = int.from_bytes(lo, "big"), int.from_bytes(hi, "big")
                    base = int.from_bytes(dst, "big")
                    width = len(dst)
                    for k in range(min(b - a + 1, 65536)):
                        cmap[a + k] = _utf16((base + k).to_bytes(width, "big"))
                i += 3
        i += 1
    return cmap


# ---------- 內容串流 ----------
def _mul(a, b):
    """3x3 仿射矩陣（以 6 數表示）相乘 a × b。"""
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _PageText:
    def __init__(self, doc: Document):
        self.doc = doc
        self.parts: list[str] = []
        self.chars = self.bad = 0
        self.last: tuple[float, float] | None = None
        self.fonts: dict[int, Font] = {}

    def font(self, resources: dict, name: str) -> Font | None:
        fonts = self.doc.get((resources or {}).get("Font")) or {}
        ref = fonts.get(name)
        obj = self.doc.get(ref)
        if not isinstance(obj, dict):
            return None
        key = ref.num if isinstance(ref, Ref) else id(obj)
        if key not in self.fonts:
            self.fonts[key] = Font(self.doc, obj)
        return self.fonts[key]

    def emit(self, text: str, tm, ctm, size: float, advance: float) -> None:
        """依起點與上一段終點的位置，插入換行（換行）或空白（同行有空隙）。"""
        start = _mul(tm, ctm)
        scale = abs(size) * (abs(start[0]) + abs(start[1]) or 1.0)
        if self.last is not None:
            dx, dy = start[4] - self.last[0], start[5] - self.last[1]
            if abs(dy) > scale * 0.5:
                self.parts.append("\n")
            elif abs(dx) > scale * 0.25:
                self.parts.append(" ")
        self.parts.append(text)
        end = _mul(_mul((1, 0, 0, 1, advance * size, 0), tm), ctm)
        self.last = (end[4], end[5])

    def run(self, content: bytes, resources: dict, ctm=IDENTITY, depth: int = 0) -> None:
        lex = Lexer(content)
        stack: list = []
        gstack: list = []
        tm = lm = IDENTITY
        font: Font | None = None
        size, leading = 1.0, 0.0
        while True:
            tok = lex.token()
            if tok is None:
                break
            if not isinstance(tok, Op) or tok in ("[", "<<"):
                stack.append(lex.object(tok))
                continue
            op = str(tok)
            args, stack = stack, []
            try:
                if op == "BI":  # 內嵌影像：跳到 EI
                    end = re.compile(rb"\sEI[\s]").search(content, lex.pos)
                    lex.pos = end.end() if end else len(content)
                elif op == "q":
                    gstack.append(ctm)
                elif op == "Q":
                    ctm = gstack.pop() if gstack else ctm
                elif op == "cm" and len(args) == 6:
                    ctm = _mul(tuple(map(float, args)), ctm)
                elif op == "BT":
                    tm = lm = IDENTITY
                elif op == "Tf" and len(args) == 2:
                    font = self.font(resources, args[0])
                    size = float(args[1])
                elif op == "TL" and args:
                    leading = float(args[0])
                elif op in ("Td", "TD") and len(args) == 2:
                    if op == "TD":
                        leading = -float(args[1])
                    lm = tm = _mul((1, 0, 0, 1, float(args[0]), float(args[1])), lm)
                elif op == "Tm" and len(args) == 6:
                    lm = tm = tuple(map(float, args))
                elif op in ("T*", "'", '"'):
                    lm = tm = _mul((1, 0, 0, 1, 0, -leading), lm)
                if op in ("Tj", "'", '"') and args and isinstance(args[-1], bytes):
                    tm = self.show([args[-1]], font, tm, ctm, size)
                elif op == "TJ" and args and isinstance(args[0], list):
                    tm = self.show(args[0], font, tm, ctm, size)
                elif op == "Do" and args and depth < 8:
                    self.xobject(resources, args[0], ctm, depth)
            except (ValueError, TypeError, IndexError):
                continue

    def show(self, items: list, font: Font | None, tm, ctm, size: float):
        """輸出一段文字，回傳前進後的文字矩陣。"""
        text, advance = [], 0.0
        for item in items:
            if isinstance(item, bytes):
                if font is None:
                    self.chars += len(item)
                    self.bad += len(item)
                    continue
                s, n, bad = font.decode(item)
                self.chars += n
                self.bad += bad
                text.append(s)
                advance += font.advance(item)
            elif isinstance(item, (int, float)):
                advance -= item / 1000
                if item < -250:
                    text.append(" ")  # 明顯的字距調整視為字間空白
        if text:
            self.emit("".join(text), tm, ctm, size, advance)
        return _mul((1, 0, 0, 1, advance * size, 0), tm)

    def xobject(self, resources: dict, name: str, ctm, depth: int) -> None:
        xobjs = self.doc.get((resources or {}).get("XObject")) or {}
        obj = self.doc.get(xobjs.get(name))
        if isinstance(obj, Stream) and obj.dict.get("Subtype") == "Form":
            matrix = self.doc.get(obj.dict.get("Matrix"))
            inner = _mul(tuple(map(float, matrix)), ctm) if isinstance(matrix, list) and len(matrix) == 6 else ctm
            res = self.doc.get(obj.dict.get("Resources")) or resources
            self.run(self.doc.stream_data(obj), res, inner, depth + 1)


@dataclass
class PdfText:
    """逐頁文字；reason 非空表示整份無法核對（呼叫端顯示「無法自動核對」，不是原文不符）。"""
    pages: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason

    def to_dict(self) -> dict:
        return {"pages": self.pages, "reason": self.reason}

    @classmethod
    def from_dict(cls, data) -> "PdfText":
        if not isinstance(data, dict):
            return cls([], "沒有保存原文文字，無法自動核對")
        pages = data.get("pages")
        if not isinstance(pages, list) or not all(isinstance(p, str) for p in pages):
            return cls([], "保存的原文文字格式不符，無法自動核對")
        return cls(pages, str(data.get("reason") or ""))


MAX_BAD_RATIO = 0.05  # 無法解碼的字元超過此比例，視為不支援的字型編碼


def extract_pages(pdf: bytes) -> PdfText:
    """取出逐頁文字；任何失敗都回報 reason，不丟例外。"""
    try:
        doc = Document(pdf)
        if doc.trailer.get("Encrypt") is not None:
            return PdfText([], "文件已加密，無法取出文字")
        page_dicts = doc.pages()
        if not page_dicts:
            return PdfText([], "找不到頁面，可能不是支援的 PDF 結構")
        texts, chars, bad = [], 0, 0
        for page in page_dicts:
            reader = _PageText(doc)
            contents = doc.get(page.get("Contents"))
            streams = contents if isinstance(contents, list) else [contents]
            data = b"\n".join(doc.stream_data(s) for s in (doc.get(x) for x in streams) if isinstance(s, Stream))
            reader.run(data, doc.get(page.get("Resources")) or {})
            texts.append("".join(reader.parts).strip())
            chars += reader.chars
            bad += reader.bad
    except (PdfError, zlib.error, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        return PdfText([], f"無法剖析 PDF（{exc}）")
    if not any(t.strip() for t in texts):
        return PdfText([], "PDF 中沒有可取出的文字，可能是掃描影像或檔案不完整")
    if chars and bad / chars > MAX_BAD_RATIO:
        return PdfText([], f"有 {bad}/{chars} 個字元無法解碼，可能是不支援的字型編碼")
    return PdfText(texts)


# ---------- 核對 ----------
def normalize(text: str) -> str:
    """比對用：NFKC（全形半形、相容字如康熙部首）、英文不分大小寫，並去除空白與標點。

    標點不同（如漏寫冒號、全形括號）不算原文不符；文字與數字仍須逐字相同。
    """
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] not in "PZC")


MATCH, OTHER_PAGE, NOT_FOUND, UNVERIFIABLE = "match", "other_page", "not_found", "unverifiable"


@dataclass
class Citation:
    status: str  # match、other_page、not_found、unverifiable
    pages: list[int] = field(default_factory=list)  # match／other_page 時，原文出現的頁碼（從 1 起）
    missing: list[str] = field(default_factory=list)  # not_found 時，整份都找不到的片段
    reason: str = ""  # unverifiable 的原因
    loose: bool = False  # 只在詞的層級找得到（字詞都在，但取出的文字不相連，常見於表格）


def segments(quote: str) -> list[str]:
    """引文依換行或刪節號切段；表格的多列引文在取出的文字中不一定相鄰，逐段核對。"""
    parts = re.split(r"[\r\n]+|\.{3,}|…+", str(quote))
    return [p for p in (p.strip() for p in parts) if normalize(p)]


def _found(segment: str, page: str) -> int:
    """2：整段相連出現；1：以空白分開的每個詞都出現（不相連）；0：找不到。"""
    if normalize(segment) in page:
        return 2
    words = [normalize(w) for w in segment.split()]
    words = [w for w in words if w]
    return 1 if len(words) > 1 and all(w in page for w in words) else 0


def locate(text: PdfText, quote: str, page) -> Citation:
    """核對引文是否出現在宣稱頁（page 從 1 起）。"""
    if not text.ok:
        return Citation(UNVERIFIABLE, reason=text.reason)
    parts = segments(quote)
    if not parts:
        return Citation(UNVERIFIABLE, reason="引文空白")
    pages = [normalize(p) for p in text.pages]
    found = [[_found(s, pg) for pg in pages] for s in parts]  # found[段][頁]

    def level(i):
        return min(f[i] for f in found)

    claimed = page - 1 if type(page) is int and 1 <= page <= len(pages) else None
    if claimed is not None and level(claimed):
        return Citation(MATCH, [page], loose=level(claimed) == 1)
    hits = [i for i in range(len(pages)) if level(i)]
    if hits:
        best = max(level(i) for i in hits)
        return Citation(OTHER_PAGE, [i + 1 for i in hits if level(i) == best], loose=best == 1)
    missing = [s for s, f in zip(parts, found) if not any(f)]
    if not missing:  # 各段都找得到，但分散在不同頁
        spread = sorted({i + 1 for f in found for i, v in enumerate(f) if v})
        if claimed is not None and claimed + 1 in spread:
            return Citation(MATCH, spread, loose=True)
        return Citation(OTHER_PAGE, spread, loose=True)
    if claimed is not None and not pages[claimed]:
        return Citation(UNVERIFIABLE, reason=f"第 {page} 頁取不出文字（可能是影像），無法自動核對")
    return Citation(NOT_FOUND, missing=missing)


def describe(c: Citation, page) -> str:
    """核對結果的承辦人提示文字；完全相符時回傳空字串。"""
    if c.status == MATCH:
        return "" if not c.loose else (f"原文的字詞都在第 {page} 頁，但取出的文字不相連（可能是表格拆開或語序不同），"
                                       "請對照文件確認")
    if c.status == OTHER_PAGE:
        where = "、".join(map(str, c.pages))
        return f"原文不在宣稱的第 {page} 頁，而在第 {where} 頁，頁碼可能錯，請對照文件確認"
    if c.status == NOT_FOUND:
        shown = "、".join(f"「{s[:30]}」" for s in c.missing[:3])
        return f"原文 {shown} 在文件中找不到，可能是 AI 編造，請對照文件確認"
    return f"無法自動核對原文：{c.reason}"


def main(argv: list[str]) -> None:
    """開發用：印出逐頁文字。python -m academic_twin.pdf_text <pdf>"""
    import sys
    from pathlib import Path
    for path in argv:
        result = extract_pages(Path(path).read_bytes())
        print(f"== {path}：{len(result.pages)} 頁" + (f"；無法核對：{result.reason}" if result.reason else ""))
        for i, page in enumerate(result.pages, 1):
            print(f"-- 第 {i} 頁 --\n{page}")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
