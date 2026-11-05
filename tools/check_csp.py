"""The progress pages carry no inline script, handler, style attribute or javascript: URL.

    python tools/check_csp.py

The server sends `script-src 'self'; style-src 'self'` on everything under
/stack/ (server/app.py, STACK_CSP). That only holds if the files themselves
never need the exceptions, so this reads every file under server/web/stack/
and fails on:

  * a <script> element with a body (a src= script is fine),
  * an on*= event-handler attribute in HTML,
  * a style= attribute in HTML (a style attribute written by script is allowed
    by `style-src-attr` and is not looked for here),
  * a <style> element, and
  * a javascript: URL anywhere.

The same four things tests/security-headers.tftest.hcl guards in Rapid Stacker
Collab. Vendored third-party files (js/vendor/) are skipped: they are served
unmodified, and the policy is checked against them at run time.

No test framework, for the same reason as the other checks here.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "server" / "web" / "stack"

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)
STYLE_EL = re.compile(r"<style\b", re.I)
# An attribute name starting "on", preceded by whitespace or a quote, inside a tag.
HANDLER = re.compile(r"""<[a-zA-Z][^<>]*?[\s"'/]on[a-z]+\s*=""", re.S)
STYLE_ATTR = re.compile(r"""<[a-zA-Z][^<>]*?[\s"'/]style\s*=""", re.S)
JS_URL = re.compile(r"""javascript\s*:""", re.I)


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def check_html(path, text):
    found = []
    for m in SCRIPT.finditer(text):
        if m.group(2).strip():
            found.append(f"{path.name}:{line_of(text, m.start())} inline <script> body")
        elif "src=" not in m.group(1).lower():
            found.append(f"{path.name}:{line_of(text, m.start())} <script> with neither src nor body")
    for m in STYLE_EL.finditer(text):
        found.append(f"{path.name}:{line_of(text, m.start())} <style> element")
    for m in HANDLER.finditer(text):
        found.append(f"{path.name}:{line_of(text, m.start())} on*= attribute")
    for m in STYLE_ATTR.finditer(text):
        found.append(f"{path.name}:{line_of(text, m.start())} style= attribute")
    return found


def scan(root):
    """Every problem under `root`, as readable lines."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "vendor" in path.relative_to(root).parts:
            continue
        if path.suffix.lower() not in (".html", ".htm", ".js", ".css", ".svg"):
            continue
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root)
        if path.suffix.lower() in (".html", ".htm"):
            found += [f"{rel}: {m}" for m in check_html(path, text)]
        if path.suffix.lower() == ".svg" and re.search(r"<script\\b", text, re.I):
            found.append(f"{rel}: <script> in an SVG")
        for m in JS_URL.finditer(text):
            found.append(f"{rel}:{line_of(text, m.start())} javascript: URL")
    return found


# ------------------------------------------------ the checker catches what it should
bad = Path(__file__).parent  # only used for its type; the samples are text
samples = {
    "an inline script": '<script>alert(1)</script>',
    "an onclick attribute": '<a href="/x" onclick="go()">x</a>',
    "a style attribute": '<p class="a" style="margin:0">x</p>',
    "a style element": '<style>p{}</style>',
    "a javascript: URL": '<a href="javascript:void(0)">x</a>',
}
for name, text in samples.items():
    hits = check_html(Path("sample.html"), text) + (["js"] if JS_URL.search(text) else [])
    case(f"the check flags {name}", bool(hits))
clean = '<link rel="stylesheet" href="/a.css"><script type="module" src="/a.js"></script><p class="a">on</p><div id="one" data-onward="1">x</div>'
case("...and passes a page with none of them", not check_html(Path("sample.html"), clean), str(check_html(Path("sample.html"), clean)))

# ------------------------------------------------------------- the real files
case("the pages are there", WEB.is_dir() and (WEB / "project.html").is_file())
files = [p for p in WEB.rglob("*") if p.is_file()] if WEB.is_dir() else []
case("...with their scripts and styles", any(p.suffix == ".js" for p in files) and any(p.suffix == ".css" for p in files),
     f"{len(files)} files")
problems = scan(WEB) if WEB.is_dir() else ["no web/stack directory"]
for line in problems:
    print(f"      {line}")
case("no inline script, handler, style attribute or javascript: URL under server/web/stack/", not problems,
     f"{len(problems)} found" if problems else "")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
