#!/usr/bin/env python3
"""
Build the proposal PDF: fills proposal.html with the details below and the
chosen conversation excerpts, then prints it with headless Chrome.

  python3 proposal/build.py
"""
import html
import json
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

with open(os.path.join(HERE, "details.json")) as f:
    D = json.load(f)


def excerpt_html(lines):
    out = []
    for e in lines:
        role = e["role"]
        out.append(
            f'    <div class="line {role}"><div class="who">{role.upper()}<span>{e["at"]}</span></div>'
            f'<div class="said">{html.escape(e["text"])}</div></div>')
    return "\n".join(out)


page = open(os.path.join(HERE, "proposal.html")).read()
fills = dict(D["fields"], EXCERPTS=excerpt_html(D["excerpts"]))
for key, value in fills.items():
    page = page.replace("{{" + key + "}}", value)
missing = [k for k in ("ARTIST", "YEAR", "CONTACT", "VIDEO_URL", "REPO_URL") if "{{" + k + "}}" in page]
assert not missing, f"fill these in proposal/details.json: {missing}"

with tempfile.NamedTemporaryFile("w", suffix=".html", dir=HERE, delete=False) as tmp:
    tmp.write(page)
out = os.path.join(HERE, "Interminable-Nothingness-proposal.pdf")
try:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--allow-file-access-from-files", f"--print-to-pdf={out}", "file://" + tmp.name],
                   check=True, capture_output=True)
finally:
    os.unlink(tmp.name)
print("wrote", out)
