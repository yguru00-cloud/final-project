"""Build the self-contained report.html and slides.html from docs/ (images embedded, numbers filled in).

Run after any app change:  python tools/measure_runs.py   (optional, refreshes docs/evidence.json)
                           python tools/build_docs.py
"""

import base64
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def embed_images(text: str) -> str:
    def repl(m: re.Match) -> str:
        path = DOCS / m.group(1)
        mime = "image/png" if path.suffix == ".png" else "image/jpeg"
        return f'src="data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"'
    return re.sub(r'src="(img/[^"]+)"', repl, text)


def impact_rows(ev: dict) -> str:
    rows = []
    for i, r in enumerate(ev["runs"], 1):
        b = r["best"]
        if not r["list"].isascii():  # the deliverables are in English
            r = {**r, "list": "Same items as #1, typed in another language (input-language test)"}
        elif r.get("input_note"):
            r = {**r, "list": r["list"] + " (typed in another language; shown translated)"}
        plan = f"{b['day']} · {' + '.join(b['stores'])} · {b['mode']}"
        rows.append(
            f"<tr><td>#{i} {html.escape(r['list'])}</td><td>{html.escape(plan)}</td>"
            f"<td class='n'>${b['total']:.2f}</td><td class='n'>${r['baseline_total']:.2f}</td>"
            f"<td class='n'>${(r['savings'] or 0):.2f}</td></tr>")
    return "\n  ".join(rows)


def main() -> None:
    ev = json.loads((DOCS / "evidence.json").read_text(encoding="utf-8"))
    tests = (DOCS / "test_safety_output.txt").read_text(encoding="utf-8-sig").strip()
    fills = {"IMPACT_ROWS": impact_rows(ev), "TEST_SAFETY_OUTPUT": html.escape(tests)}
    for src, out in (("report_src.html", "report.html"), ("slides_src.html", "slides.html")):
        p = DOCS / src
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for k, v in fills.items():
            text = text.replace(k, v)
        text = embed_images(text)
        (ROOT / out).write_text(text, encoding="utf-8")
        left = [k for k in ("REPO_URL_PENDING", "USER_TEST_PENDING") if k in text]
        print(f"wrote {out}: {len(text) / 1024:.0f} KB" + (f"  (still pending: {', '.join(left)})" if left else ""))


if __name__ == "__main__":
    main()
