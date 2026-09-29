// IPI Guard Chat - 画面側の処理
// 検出した文言は必ず textContent で表示し、HTMLとして解釈させない。
"use strict";

const log = document.getElementById("log");
const fileInput = document.getElementById("file");
const form = document.getElementById("composer");
const q = document.getElementById("q");
const drop = document.getElementById("drop");
let last = null; // 直近の検査結果（追加質問に使う）

const VERDICT = {
  allow:      { mark: "許可",   title: "Copilotに読ませても問題は見つかりませんでした" },
  review:     { mark: "要確認", title: "人の目で確認してから使ってください" },
  quarantine: { mark: "隔離",   title: "Copilotに読ませないでください" },
};

const WHY = {
  hidden_attr:   "Wordの「隠し文字」は画面にも印刷にも出ませんが、Copilotは本文として読み込みます。",
  white_text:    "白い文字は背景に溶けて人には見えませんが、AIには普通の文字として読まれます。",
  tiny_text:     "極端に小さい文字は人が気づけない一方、AIはそのまま読み込みます。",
  off_slide:     "スライドの外に置かれた図形は発表時に表示されませんが、ファイルの中身としては読まれます。",
  hidden_slide:  "非表示スライドはスライドショーでは飛ばされますが、AIは内容を読み込みます。",
  hidden_sheet:  "非表示シートは開いても見えませんが、AIはデータとして読み込みます。",
  comment:       "コメント欄は本文ではないため見落とされがちですが、AIの入力になります。",
  notes:         "発表者ノートは聴衆に見えない場所ですが、AIは読み込みます。",
  metadata:      "文書プロパティ（作成者・件名・説明など）は普段目にしない場所に文章を隠せます。",
  alt_text:      "画像の代替テキストは画面に出ませんが、AIは画像の説明として読みます。",
  svg_comment:   "SVG画像内のコメントは表示されませんが、AIが読むと指示として働いた実例があります。",
  svg_metadata:  "SVG画像のメタデータや説明タグは表示されませんが、AIには読まれます。",
  svg_invisible: "透明・非表示に設定された文字は見えませんが、AIには読まれます。",
  html_comment:  "HTMLやMarkdownのコメントは表示されませんが、AIには本文と同じように渡ります。",
  css_hidden:    "CSSで非表示にした文字は画面に出ませんが、AIには読まれます。",
  invisible_chr: "ゼロ幅文字やUnicodeタグ文字は、目に見えない形で文章を埋め込む手口に使われます。",
  visible:       "見えている本文に、AIへの命令と読める強い表現が含まれています。",
};

const ACTION = {
  allow: ["通常どおり利用できます。", "ただし未知の手口は検出できないため、Copilotの回答に不自然なリンクや宛先が出たら使わないでください。"],
  review: ["検出箇所を開いて、業務上必要な記載かどうかを確認してください。", "意図が分からない記載があれば送付元に問い合わせ、削除した版をもらうのが確実です。", "判断に迷う場合は、業務標準化チームに相談してください。"],
  quarantine: ["このファイルはCopilotのナレッジやチャットに入れないでください。", "送付元に「隠れた記載がある」ことを伝え、差し替えを依頼してください。", "すでにCopilotに読ませていた場合は、その会話の回答に含まれるリンクを開かず、業務標準化チームに連絡してください。"],
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function post(role, build) {
  const wrap = el("div", "msg " + role);
  const b = el("div", "bubble");
  if (role === "bot") b.append(el("div", "who", "IPI Guard"));
  build(b);
  wrap.append(b);
  log.append(wrap);
  log.scrollTop = log.scrollHeight;
  return b;
}

function quick(b, items) {
  const box = el("div", "quick");
  for (const [label, fn] of items) {
    const btn = el("button", null, label);
    btn.type = "button";
    btn.addEventListener("click", () => { post("user", u => u.append(el("p", null, label))); fn(); });
    box.append(btn);
  }
  b.append(box);
}

function fmtSize(n) {
  return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
}

function welcome() {
  post("bot", b => {
    b.append(el("p", null, "社外から受け取った文書を、Copilotに読ませる前に検査します。📎から添付するか、画面にドラッグしてください。"));
    b.append(el("p", "note", "対応形式：Word・PowerPoint・Excel・SVG・HTML・Markdown・テキスト（PDFは次のバージョンで対応予定）"));
    if (BROWSER_MODE) b.append(el("p", "note", "このページでは、判定をすべてブラウザの中で行います。添付したファイルはどこにも送信されません。"));
  });
}

async function scan(file) {
  post("user", b => {
    const chip = el("div", "chip-file");
    const ext = (file.name.split(".").pop() || "?").toUpperCase().slice(0, 5);
    chip.append(el("span", "ext", ext), el("span", null, file.name), el("span", "size", fmtSize(file.size)));
    b.append(chip);
  });
  const wait = post("bot", b => b.append(el("p", "busy", BROWSER_MODE && !engine ? "判定エンジンを準備しています（初回は数秒〜十数秒かかります）" : "検査しています")));
  try {
    const data = BROWSER_MODE ? await scanInBrowser(file) : await scanOnServer(file);
    wait.parentElement.remove();
    last = data;
    render(data);
  } catch (err) {
    wait.parentElement.remove();
    post("bot", b => b.append(el("p", null, err.message)));
  }
}

// ---- 判定の実行先 --------------------------------------------------------------
// localhost で開いたときは server.py に送る。GitHub Pages などで開いたときは、
// 同じ detector.py をブラウザ内の Python（Pyodide）で実行する。どちらもファイルは外部に送信しない。
const BROWSER_MODE = !["127.0.0.1", "localhost"].includes(location.hostname);
const PYODIDE_URL = "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/";
const MAX_BYTES = 20 * 1024 * 1024;
let engine = null;
let enginePromise = null;

async function scanOnServer(file) {
  let res;
  try {
    res = await fetch("api/scan", {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" },
      body: file,
    });
  } catch {
    throw new Error("検査サーバーに接続できません。server.py が起動しているか確認してください。");
  }
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || "検査できませんでした。");
  return data;
}

function loadEngine() {
  if (enginePromise) return enginePromise;
  enginePromise = (async () => {
    await new Promise((resolve, reject) => {
      const sc = document.createElement("script");
      sc.src = PYODIDE_URL + "pyodide.js";
      sc.onload = resolve;
      sc.onerror = () => reject(new Error("判定エンジンを読み込めませんでした。ネットワーク接続を確認してください。"));
      document.head.append(sc);
    });
    const py = await loadPyodide({ indexURL: PYODIDE_URL });
    const src = await (await fetch("detector.py", { cache: "no-store" })).text();
    py.runPython(src);  // Python版と同じ判定エンジンをそのまま読み込む
    engine = py;
    return py;
  })();
  enginePromise.catch(() => { enginePromise = null; });
  return enginePromise;
}

async function scanInBrowser(file) {
  if (file.size > MAX_BYTES) throw new Error("20MBを超えるファイルは検査できません");
  const py = await loadEngine();
  const bytes = new Uint8Array(await file.arrayBuffer());
  py.globals.set("_fname", file.name);
  py.globals.set("_fdata", bytes);
  try {
    const out = py.runPython("import json\njson.dumps(scan_bytes(_fname, bytes(_fdata.to_py())), ensure_ascii=False)");
    return JSON.parse(out);
  } catch {
    throw new Error("検査中にエラーが発生しました。");
  } finally {
    py.globals.delete("_fname");
    py.globals.delete("_fdata");
  }
}

function render(d) {
  const v = VERDICT[d.verdict];
  post("bot", b => {
    const head = el("div", "head");
    head.append(el("div", "stamp " + d.verdict, v.mark));
    const t = el("div");
    t.append(el("h2", null, v.title), el("div", "score", `リスクスコア ${d.score}　検出 ${d.findings.length} 件`));
    head.append(t);
    b.append(head);

    if (d.findings.length) {
      b.append(el("p", null, summaryLine(d)));
      const list = el("div", "finds");
      d.findings.forEach((f, i) => {
        const det = el("details");
        if (i === 0 && d.verdict !== "allow") det.open = true;
        const s = el("summary");
        s.append(el("span", "path", f.path_label), el("span", "loc", f.location),
                 el("span", "pts" + (f.score >= 8 ? " hot" : ""), f.score.toFixed(1)));
        det.append(s);
        if (f.reasons.length) {
          const r = el("div", "reasons");
          f.reasons.forEach(x => r.append(el("span", null, x)));
          det.append(r);
        }
        det.append(el("div", "excerpt", f.text));
        list.append(det);
      });
      b.append(list);
    } else if (!d.unsupported && !d.error) {
      b.append(el("p", null, "隠れた場所に文章は見つかりませんでした。"));
    }
    d.notes.forEach(n => b.append(el("p", "note", n)));

    const opts = [];
    if (d.findings.length) opts.push(["なぜ危険？", explainWhy]);
    opts.push(["どう対処する？", explainAction], ["別のファイルを調べる", () => fileInput.click()]);
    quick(b, opts);
  });
}

function summaryLine(d) {
  const kinds = [...new Set(d.findings.map(f => f.path_label))];
  const inst = d.findings.filter(f => f.reasons.length).length;
  let s = `${kinds.slice(0, 3).join("・")}${kinds.length > 3 ? " など" : ""}に文章が見つかりました。`;
  if (inst) s += `そのうち ${inst} 件は、AIへの指示と読める表現を含んでいます。`;
  return s;
}

function explainWhy() {
  if (!last) return post("bot", b => b.append(el("p", null, "まだファイルを検査していません。📎から添付してください。")));
  post("bot", b => {
    const kinds = [...new Set(last.findings.map(f => f.path))];
    if (!kinds.length) return b.append(el("p", null, "今回のファイルでは、危険と判断した箇所はありませんでした。"));
    kinds.forEach(k => b.append(el("p", null, WHY[k])));
    b.append(el("p", "note", "人には見えず、AIだけが読む場所に指示を書き込む手口を「間接プロンプトインジェクション」と呼びます。Copilotが指示に従うと、社内情報を外部に送ったり、回答をすり替えたりするおそれがあります。"));
  });
}

function explainAction() {
  const v = last ? last.verdict : "allow";
  post("bot", b => ACTION[v].forEach(t => b.append(el("p", null, t))));
}

// ---- 入力 ----
fileInput.addEventListener("change", () => { if (fileInput.files[0]) scan(fileInput.files[0]); fileInput.value = ""; });
form.addEventListener("submit", e => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  q.value = "";
  post("user", b => b.append(el("p", null, text)));
  if (/なぜ|理由|どうして|危険/.test(text)) explainWhy();
  else if (/対処|どうすれば|どうする|対応|削除/.test(text)) explainAction();
  else post("bot", b => b.append(el("p", null, "MVP版では「なぜ危険？」「どう対処する？」の質問に答えられます。ファイルの検査は📎から添付してください。")));
});

let depth = 0;
addEventListener("dragenter", e => { e.preventDefault(); depth++; drop.hidden = false; });
addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; drop.hidden = true; } });
addEventListener("dragover", e => e.preventDefault());
addEventListener("drop", e => {
  e.preventDefault(); depth = 0; drop.hidden = true;
  const f = e.dataTransfer.files[0];
  if (f) scan(f);
});

welcome();
if (BROWSER_MODE) loadEngine().catch(() => {});  // 先に読み込みを始めておく
