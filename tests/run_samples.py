"""検体の判定が期待どおりかを確認する回帰テスト:  python tests/run_samples.py"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from detector import scan_bytes  # noqa: E402

EXPECTED = {
    "01_clean.docx": "allow",
    "02_white_text.docx": "quarantine",
    "03_hidden_attr.docx": "quarantine",
    "04_off_slide.pptx": "quarantine",
    "05_hidden_sheet.xlsx": "quarantine",
    "06_comment.svg": "quarantine",
    "07_tag_chars.md": "quarantine",
    "08_white_on_dark_box.pptx": "review",
    "09_white_on_white_slide.pptx": "quarantine",
    "10_metadata_attack.docx": "quarantine",
    "11_metadata_clean.docx": "allow",
    "12_white_and_tiny.docx": "quarantine",
}
# 手元の実資料（誤検知の回帰用）は samples/regression/ に置き、ファイル名に期待値を含める
# 例: explain-deck__review.pptx

subprocess.run([sys.executable, str(ROOT / "samples" / "make_samples.py")], check=True, capture_output=True)
cases = [(ROOT / "samples" / "out" / n, v) for n, v in EXPECTED.items()]
reg = ROOT / "samples" / "regression"
if reg.exists():
    cases += [(p, p.stem.rsplit("__", 1)[-1]) for p in sorted(reg.iterdir()) if "__" in p.stem]

ng = 0
for path, want in cases:
    got = scan_bytes(path.name, path.read_bytes())
    ok = got["verdict"] == want
    ng += not ok
    print(f"{'OK ' if ok else 'NG '} {path.name:40s} 期待={want:10s} 結果={got['verdict']:10s} スコア={got['score']}")
print(f"\n{len(cases) - ng}/{len(cases)} 件が期待どおり")
sys.exit(1 if ng else 0)
