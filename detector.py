"""IPI Guard Chat - 判定エンジン（MVP・Python標準ライブラリのみ）

既存の IPI Guard 本体に差し替える場合は、scan_bytes() の中身を
IPI Guard の抽出器・採点器の呼び出しに置き換える（戻り値の形だけ合わせる）。
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_ZIP_ENTRIES = 2000
MAX_UNZIPPED = 200 * 1024 * 1024  # zip爆弾対策

# ---- 経路の定義（重み = 経路倍率） -----------------------------------------
PATHS = {
    "hidden_attr":   ("隠し文字属性", 3.0),
    "white_text":    ("白色の文字", 3.0),
    "tiny_text":     ("極小フォント", 3.0),
    "off_slide":     ("スライド外のオブジェクト", 3.0),
    "hidden_slide":  ("非表示スライド", 2.0),
    "hidden_sheet":  ("非表示シート", 2.5),
    "comment":       ("コメント", 2.0),
    "notes":         ("発表者ノート", 1.5),
    "metadata":      ("文書プロパティ", 2.0),
    "alt_text":      ("画像の代替テキスト", 2.0),
    "svg_comment":   ("SVGのコメント", 3.0),
    "svg_metadata":  ("SVGのメタデータ・説明", 3.0),
    "svg_invisible": ("SVGの不可視テキスト", 3.0),
    "html_comment":  ("HTML/Markdownのコメント", 2.5),
    "css_hidden":    ("CSSで隠された文字", 3.0),
    "invisible_chr": ("不可視文字（ゼロ幅・タグ文字）", 3.5),
    "visible":       ("本文（見えている文字）", 1.0),
}

# 人の目にほぼ触れない経路（ここで指示が見つかった場合だけ「隔離」にする）
STRONG_HIDDEN = {"hidden_attr", "white_text", "tiny_text", "off_slide", "hidden_slide", "hidden_sheet",
                 "svg_comment", "svg_metadata", "svg_invisible", "html_comment", "css_hidden", "invisible_chr"}
# 文書プロパティ・代替テキストは文章があること自体は普通。ただし指示が書かれていれば隔離の対象にする
QUARANTINE_PATHS = STRONG_HIDDEN | {"metadata", "alt_text"}

# ---- 「指示らしさ」の判定パターン（日英） -----------------------------------
PATTERNS = [
    # 偽装型：業務連絡に見せかけ、目立つ命令語を避けて「要約する側」の振る舞いを変えようとする手口
    (r"(要約|回答|返答|出力|まとめ|翻訳)(を)?(する|作成する|行う|生成する)?(場合|際|とき|時)(に)?(は|には)", "要約・回答するときの振る舞いを指定している", 3),
    (r"(when|if|whenever) (you )?(summariz|answer|respond|repl|translat)", "要約・回答するときの振る舞いを指定している", 3),
    (r"(要約|回答|返答|出力|まとめ)(に|には|では|の中で).{0,30}(記載|記述|明記|表記|と書|と(す|し)る)", "回答内容の改ざんを指示している", 2),
    (r"(state|report|write|say) that .{0,40}(no issues|resolved|normal|fine|safe)", "回答内容の改ざんを指示している", 2),
    (r"(として扱|とみな|と解釈|なかったことに|解消済み)", "事実のすり替えを求めている", 2),
    (r"treat .{0,40} as|consider .{0,30} (resolved|fixed)", "事実のすり替えを求めている", 2),
    (r"(監査|管理者|システム管理|情報システム|IT部門|法務|本社|経営企画).{0,4}(注記|より|からの指示|通達|指示)", "権限のある部署を名乗っている", 1),
    (r"(AI|ＡＩ|エージェント|アシスタント|LLM|Copilot|assistant|language model)(へ|に告ぐ|の方へ|さんへ|に対して)|(to|dear) (the )?(ai|assistant|llm)", "AIに直接呼びかけている", 3),
    (r"(要約|回答|返答|出力)(文)?の?(末尾|最後|冒頭|先頭)", "回答内容の改ざんを指示している", 2),
    (r"(だけ|のみ)(を)?(出力|回答|返答|表示)", "出力内容を指定している", 2),
    (r"(output|respond|reply|answer|print|say) (only|with only|nothing but)|only (output|respond|reply|say)", "出力内容を指定している", 2),
    (r"(at|to) the (end|beginning|start|top|bottom) of (your |the )?(summary|response|answer|reply|output)|append .{0,40}(summary|response|answer)", "回答内容の改ざんを指示している", 2),
    (r"(以前|前|上記|これまで)の(指示|命令|ルール).{0,6}(無視|忘れ)", "既存の指示を無効化しようとしている", 3),
    (r"ignore (all |any )?(previous|prior|above) (instructions|prompts)", "既存の指示を無効化しようとしている", 3),
    (r"(システムプロンプト|system prompt|開発者モード|developer mode)", "AIの内部設定に言及している", 2),
    (r"(あなたは|you are now|act as|として振る舞)", "AIの役割を書き換えようとしている", 2),
    (r"(送信|転送|メールして|send|forward|email)", "送信・転送を指示している", 2),
    (r"https?://[^\s\"'<>)]+", "外部URLを含んでいる", 2),
    (r"!\[[^\]]*\]\(https?://", "外部画像の読み込み（情報持ち出しの典型手口）", 3),
    (r"(パスワード|認証コード|ワンタイム|機密|社外秘|password|credential|api[_ ]?key|token)", "機密情報に言及している", 2),
    (r"(回答|要約|返答)(に|の中に|の最後に).{0,10}(含め|追加|書い)", "回答内容の改ざんを指示している", 2),
    (r"(ユーザー|利用者)(に|へ)(は|には)?.{0,8}(言わ|伝え|知らせ)(ない|るな)", "利用者に隠すよう指示している", 3),
    (r"(実行|execute|run the|呼び出|call the)", "操作の実行を指示している", 1),
    (r"(必ず|絶対に|must|always|直ちに|immediately)", "強い命令口調", 1),
]
_COMPILED = [(re.compile(p, re.I), why, w) for p, why, w in PATTERNS]

ZW_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
TAG_RE = re.compile("[\U000E0000-\U000E007F]+")


def _instruction_hits(text: str):
    hits, score = [], 0
    for rx, why, w in _COMPILED:
        if rx.search(text):
            hits.append(why)
            score += w
    return sorted(set(hits)), score


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class Collector:
    def __init__(self):
        self.findings = []
        self.visible = []

    def add(self, path: str, location: str, text: str, also=()):
        text = _clean(text)
        if not text:
            return
        self.findings.append({"path": path, "location": location, "text": text[:1500], "also": list(also)})

    def add_visible(self, location: str, text: str):
        text = _clean(text)
        if text:
            self.visible.append((location, text))


def _parse(data: bytes):
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data:
        raise ValueError("DOCTYPE/ENTITY を含むXMLは解析しません")
    return ET.fromstring(data)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el, name):
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _is_whiteish(hexv: str | None) -> bool:
    if not hexv or not re.fullmatch(r"[0-9A-Fa-f]{6}", hexv):
        return False
    r, g, b = (int(hexv[i:i + 2], 16) for i in (0, 2, 4))
    return min(r, g, b) >= 240


# ---- Word -------------------------------------------------------------------
def _scan_docx(z: zipfile.ZipFile, c: Collector):
    parts = [n for n in z.namelist() if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$", n)]
    for name in parts:
        root = _parse(z.read(name))
        for r in root.iter():
            if _local(r.tag) != "r":
                continue
            text = "".join(t.text or "" for t in r.iter() if _local(t.tag) in ("t", "delText"))
            if not text.strip():
                continue
            rpr = next((x for x in r if _local(x.tag) == "rPr"), None)
            flags = []
            if rpr is not None:
                for p in rpr:
                    tag = _local(p.tag)
                    if tag in ("vanish", "specVanish") and _attr(p, "val") not in ("0", "false"):
                        flags.append("hidden_attr")
                    elif tag == "color" and _is_whiteish(_attr(p, "val")):
                        flags.append("white_text")
                    elif tag == "sz" and (_attr(p, "val") or "99").isdigit() and int(_attr(p, "val")) <= 4:
                        flags.append("tiny_text")
            # 同じ文字に複数の隠し方が使われていても1件として数える（二重計上しない）
            order = ["hidden_attr", "white_text", "tiny_text"]
            flags = sorted(set(flags), key=order.index)
            flagged = bool(flags)
            if flags:
                c.add(flags[0], name, text, also=flags[1:])
            if not flagged:
                c.add_visible(name, text)
        for el in root.iter():
            if _local(el.tag) == "docPr":
                c.add("alt_text", name, " ".join(filter(None, [_attr(el, "title"), _attr(el, "descr")])))
    if "word/comments.xml" in z.namelist():
        root = _parse(z.read("word/comments.xml"))
        for cm in root.iter():
            if _local(cm.tag) == "comment":
                c.add("comment", "word/comments.xml", "".join(t.text or "" for t in cm.iter() if _local(t.tag) == "t"))


# ---- PowerPoint -------------------------------------------------------------
SCHEME_GUESS = {"bg1": "FFFFFF", "lt1": "FFFFFF", "bg2": "E7E6E6", "lt2": "E7E6E6",
                "tx1": "000000", "dk1": "000000", "tx2": "44546A", "dk2": "44546A"}


def _fill_color(sppr):
    """spPr / bgPr 直下の塗りつぶし色を返す（塗りなし=None、判別不能な色付き='COLORED'）"""
    if sppr is None:
        return None
    for ch in sppr:
        tag = _local(ch.tag)
        if tag == "noFill":
            return None
        if tag == "solidFill":
            for c in ch:
                t = _local(c.tag)
                if t == "srgbClr":
                    return (_attr(c, "val") or "").upper()
                if t == "schemeClr":
                    return SCHEME_GUESS.get(_attr(c, "val"), "COLORED")
                return "COLORED"
        if tag in ("gradFill", "pattFill", "blipFill"):
            return "COLORED"  # グラデーション・画像背景は白文字が見える前提で扱う
    return None


def _is_dark(color) -> bool:
    if color is None:
        return False
    if color == "COLORED":
        return True
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        return False
    r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) < 186  # 白文字が読める程度に暗いか


def _slide_bg(z, names, slide_name):
    """スライド → レイアウト → マスターの順に背景色を探す"""
    part = slide_name
    for _ in range(3):
        if part not in names:
            break
        root = _parse(z.read(part))
        for el in root.iter():
            if _local(el.tag) == "bgPr":
                return _fill_color(el)
            if _local(el.tag) == "bgRef":
                return "COLORED" if any(_local(c.tag) in ("srgbClr", "schemeClr") and
                                        (_attr(c, "val") or "").lower() not in ("bg1", "lt1", "ffffff")
                                        for c in el) else "FFFFFF"
        rels = part.rsplit("/", 1)[0] + "/_rels/" + part.rsplit("/", 1)[1] + ".rels"
        nxt = None
        if rels in names:
            for r in _parse(z.read(rels)):
                if (_attr(r, "Type") or "").endswith(("/slideLayout", "/slideMaster")):
                    nxt = "ppt/" + (_attr(r, "Target") or "").replace("../", "")
        if not nxt:
            break
        part = nxt
    return "FFFFFF"


def _bbox(sp):
    off = next((e for e in sp.iter() if _local(e.tag) == "off"), None)
    ext = next((e for e in sp.iter() if _local(e.tag) == "ext" and _attr(e, "cx")), None)
    if off is None or ext is None:
        return None
    x, y = int(_attr(off, "x") or 0), int(_attr(off, "y") or 0)
    return x, y, x + int(_attr(ext, "cx") or 0), y + int(_attr(ext, "cy") or 0)


def _scan_pptx(z: zipfile.ZipFile, c: Collector):
    names = z.namelist()
    cx = cy = None
    if "ppt/presentation.xml" in names:
        for el in _parse(z.read("ppt/presentation.xml")).iter():
            if _local(el.tag) == "sldSz":
                cx, cy = int(_attr(el, "cx") or 0), int(_attr(el, "cy") or 0)
    for name in sorted(n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)):
        root = _parse(z.read(name))
        label = name.split("/")[-1].replace(".xml", "")
        if _attr(root, "show") == "0":
            c.add("hidden_slide", label, " ".join(t.text or "" for t in root.iter() if _local(t.tag) == "t"))
            continue
        slide_bg = _slide_bg(z, names, name)
        below = []  # 描画順に、塗りつぶしのある図形を記録（後ろの図形ほど手前）
        for sp in root.iter():
            if _local(sp.tag) not in ("sp", "pic"):
                continue
            box = _bbox(sp)
            sppr = next((e for e in sp if _local(e.tag) == "spPr"), None)
            own_fill = "COLORED" if _local(sp.tag) == "pic" else _fill_color(sppr)
            sp_text = " ".join(t.text or "" for t in sp.iter() if _local(t.tag) == "t")
            for e in sp.iter():
                if _local(e.tag) == "cNvPr":
                    c.add("alt_text", label, " ".join(filter(None, [_attr(e, "title"), _attr(e, "descr")])))
            if box and cx and cy:
                x0, y0, x1, y1 = box
                if x0 >= cx or y0 >= cy or x1 <= 0 or y1 <= 0:
                    c.add("off_slide", label, sp_text)
                    continue
            # この図形の文字の背景色：自分の塗り → 真下で重なる図形 → スライド背景
            back = own_fill
            if back is None and box:
                mx, my = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                for bb, col in reversed(below):
                    if bb[0] <= mx <= bb[2] and bb[1] <= my <= bb[3]:
                        back = col
                        break
            if back is None:
                back = slide_bg
            if box and own_fill is not None:
                below.append((box, own_fill))
            for r in sp.iter():
                if _local(r.tag) != "r":
                    continue
                text = "".join(t.text or "" for t in r.iter() if _local(t.tag) == "t")
                rpr = next((x for x in r if _local(x.tag) == "rPr"), None)
                sz = int(_attr(rpr, "sz") or 1800) if rpr is not None else 1800
                white = rpr is not None and any(_local(e.tag) == "srgbClr" and _is_whiteish(_attr(e, "val")) for e in rpr.iter())
                if sz <= 200:
                    c.add("tiny_text", label, text)
                elif white and not _is_dark(back):
                    c.add("white_text", label, text)  # 明るい背景の上の白文字＝見えない
                else:
                    c.add_visible(label, text)
    for name in sorted(n for n in names if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n)):
        c.add("notes", name.split("/")[-1].replace(".xml", ""),
              " ".join(t.text or "" for t in _parse(z.read(name)).iter() if _local(t.tag) == "t"))
    for name in names:
        if re.match(r"ppt/comments/.*\.xml$", name):
            c.add("comment", name, " ".join(t.text or "" for t in _parse(z.read(name)).iter() if _local(t.tag) == "text"))


# ---- Excel ------------------------------------------------------------------
def _scan_xlsx(z: zipfile.ZipFile, c: Collector):
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in _parse(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for r in _parse(z.read("xl/_rels/workbook.xml.rels")):
            rels[_attr(r, "Id")] = "xl/" + (_attr(r, "Target") or "").lstrip("/").removeprefix("xl/")
    if "xl/workbook.xml" in names:
        for sh in _parse(z.read("xl/workbook.xml")).iter():
            if _local(sh.tag) != "sheet":
                continue
            state, sname = _attr(sh, "state"), _attr(sh, "name")
            target = rels.get(_attr(sh, "id"))
            if not target or target not in names:
                continue
            texts = []
            for cell in _parse(z.read(target)).iter():
                if _local(cell.tag) != "c":
                    continue
                v = next((x for x in cell.iter() if _local(x.tag) == "v"), None)
                if _attr(cell, "t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    texts.append(shared[int(v.text)])
                elif _attr(cell, "t") == "inlineStr":
                    texts.append("".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t"))
            joined = " ".join(texts)
            if state in ("hidden", "veryHidden"):
                c.add("hidden_sheet", f"シート「{sname}」({state})", joined)
            else:
                c.add_visible(f"シート「{sname}」", joined)
    for name in names:
        if re.match(r"xl/comments\d*\.xml$", name):
            for cm in _parse(z.read(name)).iter():
                if _local(cm.tag) == "comment":
                    c.add("comment", name, "".join(t.text or "" for t in cm.iter() if _local(t.tag) == "t"))


def _scan_docprops(z: zipfile.ZipFile, c: Collector):
    for name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        if name in z.namelist():
            root = _parse(z.read(name))
            vals = [el.text for el in root.iter() if el.text and len(el.text.strip()) > 12
                    and _local(el.tag) not in ("created", "modified", "Application", "AppVersion", "Template", "TotalTime")]
            c.add("metadata", name, " / ".join(vals))


# ---- SVG / HTML / テキスト -----------------------------------------------------
def _scan_svg(text: str, c: Collector, where="SVG"):
    for m in re.finditer(r"<!--(.*?)-->", text, re.S):
        c.add("svg_comment", where, m.group(1))
    for tag in ("metadata", "desc", "title"):
        for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S | re.I):
            c.add("svg_metadata", f"{where} <{tag}>", re.sub(r"<[^>]+>", " ", m.group(1)))
    for m in re.finditer(r"<text([^>]*)>(.*?)</text>", text, re.S | re.I):
        attrs, body = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if re.search(r"(opacity\s*[:=]\s*[\"']?0(\.0+)?[\"';\s]|display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*[:=]\s*[\"']?0(px)?[\"';\s]|fill\s*[:=]\s*[\"']?(none|#fff(fff)?|white)\b)", attrs, re.I):
            c.add("svg_invisible", where, body)
        else:
            c.add_visible(where, body)


def _scan_markup(text: str, c: Collector, where: str):
    for m in re.finditer(r"<!--(.*?)-->", text, re.S):
        c.add("html_comment", where, m.group(1))
    for m in re.finditer(r"^\[//\]:\s*#\s*\((.*)\)\s*$", text, re.M):
        c.add("html_comment", where + "（Markdownコメント）", m.group(1))
    for m in re.finditer(r"<(\w+)[^>]*style\s*=\s*[\"']([^\"']*)[\"'][^>]*>(.*?)</\1>", text, re.S | re.I):
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(\.0+)?\s*(;|$)|color\s*:\s*(#fff(fff)?|white)\b", m.group(2), re.I):
            c.add("css_hidden", where, re.sub(r"<[^>]+>", " ", m.group(3)))
    stripped = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    c.add_visible(where, re.sub(r"<[^>]+>", " ", stripped))


def _scan_invisible_chars(raw: str, c: Collector, where: str):
    for m in TAG_RE.finditer(raw):
        decoded = "".join(chr(ord(ch) - 0xE0000) for ch in m.group(0) if 0x20 <= ord(ch) - 0xE0000 < 0x7F)
        c.add("invisible_chr", where + "（Unicodeタグ文字を復号）", decoded or "（復号不可のタグ文字）")
    zw = ZW_RE.findall(raw)
    if len(zw) >= 3:
        c.add("invisible_chr", where, f"ゼロ幅文字・方向制御文字が {len(zw)} 個含まれています")


# ---- 入口 --------------------------------------------------------------------
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp932", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def scan_bytes(filename: str, data: bytes) -> dict:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    c = Collector()
    notes = []
    try:
        if ext in ("docx", "docm", "pptx", "pptm", "xlsx", "xlsm"):
            z = zipfile.ZipFile(io.BytesIO(data))
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
                raise ValueError("展開後のサイズが大きすぎるため解析を中止しました（zip爆弾の可能性）")
            {"d": _scan_docx, "p": _scan_pptx, "x": _scan_xlsx}[ext[0]](z, c)
            _scan_docprops(z, c)
            for n in z.namelist():
                if n.lower().endswith(".svg"):
                    _scan_svg(_decode(z.read(n)), c, where=f"埋め込みSVG {n}")
            if ext.endswith("m"):
                notes.append("マクロ有効形式です。マクロの中身は検査対象外なので、開く前に別途確認してください。")
        elif ext == "svg":
            _scan_svg(_decode(data), c)
        elif ext in ("html", "htm", "md", "markdown"):
            _scan_markup(_decode(data), c, ext.upper())
        elif ext in ("txt", "csv", "json", "xml", "log", ""):
            c.add_visible("本文", _decode(data))
        elif ext == "pdf":
            return _result(filename, c, ["PDFはMVP版では未対応です。次のバージョンで対応予定です。"], unsupported=True)
        else:
            return _result(filename, c, [f".{ext} 形式は未対応です。"], unsupported=True)
        # すべての抽出テキストに対して不可視文字を確認
        for f in list(c.findings):
            _scan_invisible_chars(f["text"], c, f["location"])
        for loc, t in c.visible:
            _scan_invisible_chars(t, c, loc)
        if ext in ("txt", "csv", "json", "xml", "log", "", "html", "htm", "md", "markdown", "svg"):
            _scan_invisible_chars(_decode(data), c, "ファイル全体")
    except zipfile.BadZipFile:
        notes.append("Office形式として開けませんでした。ファイルが壊れているか、拡張子が実際の形式と異なります。")
        return _result(filename, c, notes, error=True)
    except (ValueError, ET.ParseError) as e:
        notes.append(f"解析を中止しました：{e}")
        return _result(filename, c, notes, error=True)
    return _result(filename, c, notes)


def _result(filename, c: Collector, notes, unsupported=False, error=False) -> dict:
    items, seen = [], set()
    for f in c.findings:
        key = (f["path"], f["text"])
        if key in seen:
            continue
        seen.add(key)
        label, weight = PATHS[f["path"]]
        if f.get("also"):
            label += "（" + "・".join(PATHS[a][0] for a in f["also"]) + "も併用）"
        reasons, inst = _instruction_hits(f["text"])
        score = weight * (1 + inst) if inst else weight * 0.5
        items.append({**{k: v for k, v in f.items() if k != "also"}, "path_label": label, "reasons": reasons, "score": round(score, 1)})
    for loc, t in c.visible:
        reasons, inst = _instruction_hits(t)
        if inst >= 4:  # 見えている本文は「強い指示」だけ拾う
            items.append({"path": "visible", "path_label": PATHS["visible"][0], "location": loc,
                          "text": t[:1500], "reasons": reasons, "score": round(inst * 1.0, 1)})
    items.sort(key=lambda x: -x["score"])
    # 経路ごとに最も重い1件だけを合計（小さな検出の積み上げで隔離にならないように）
    per_path = {}
    for i in items:
        per_path[i["path"]] = max(per_path.get(i["path"], 0), i["score"])
    total = round(sum(per_path.values()), 1)
    strong = [i for i in items if i["path"] in STRONG_HIDDEN]
    q_items = [i for i in items if i["path"] in QUARANTINE_PATHS]
    q_total = sum(v for k, v in per_path.items() if k in QUARANTINE_PATHS)
    if unsupported or error:
        verdict = "review"
    elif any(i["score"] >= 8 for i in q_items) or q_total >= 15:
        verdict = "quarantine"  # 隔離は「人に見えない場所に、指示らしい文章」がある場合だけ
    elif total >= 3 or strong:
        verdict = "review"      # 見えている本文・ノート・コメントだけなら最大でも要確認
    else:
        verdict = "allow"
    return {"filename": filename, "verdict": verdict, "score": total, "findings": items[:50],
            "notes": notes, "unsupported": unsupported, "error": error}
