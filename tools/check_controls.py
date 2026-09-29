"""Cross-check every control in the interface against the code behind it.

    python tools/check_controls.py

Two failures this catches, both of which have actually happened here:

  * **A control the code talks to that is not in the page.** `$('btnFoo')`
    returns null, `.addEventListener` on it throws inside a handler nobody
    catches, and the button silently does nothing. That is exactly how the
    guider's Connect button came to do nothing at all — a null dereference two
    lines before the request was sent, swallowed as an unhandled rejection.
  * **A control in the page that nothing listens to.** Dead buttons: they look
    live, they depress, and nothing happens.

Static rather than clicked, on purpose. Clicking every button in a program that
drives a telescope means slewing it, and most of these need hardware attached to
do anything at all — so what can be checked without a rig is that every wire is
connected at both ends.

No test framework, for the same reason as the other checks here.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "astrocontrol" / "web"

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


html = (WEB / "index.html").read_text("utf-8")
scripts = {path.name: path.read_text("utf-8") for path in WEB.glob("*.js")
           if path.name != "vendor"}
all_js = "\n".join(scripts.values())

# ---------------------------------------------------------------- the page
# Every id in the markup, and every interactive element that carries one.
page_ids = set(re.findall(r'\bid="([^"]+)"', html))
interactive = re.findall(
    r'<(button|input|select|textarea|canvas|details|dialog)\b[^>]*\bid="([^"]+)"',
    html)
control_ids = {name for _, name in interactive}

# Elements the scripts *dereference* by id. Deliberately only the direct forms:
# these are the ones that hand back null and throw on the next line.
dereferenced: set[str] = set()
for pattern in (r"\$\(\s*'([A-Za-z][\w-]*)'\s*\)",
                r'\$\(\s*"([A-Za-z][\w-]*)"\s*\)',
                r"getElementById\(\s*'([A-Za-z][\w-]*)'\s*\)",
                r'getElementById\(\s*"([A-Za-z][\w-]*)"\s*\)'):
    dereferenced.update(re.findall(pattern, all_js))

# Every id mentioned anywhere in the scripts, however it is spelled. Plenty are
# reached through a table of field names rather than a literal `$('x')` — the
# settings forms are entirely built that way — so this is the set that answers
# "is anything at all using this control".
mentioned = {name for name in control_ids
             if re.search(rf"""['"`]{re.escape(name)}['"`]""", all_js)}

# Ids the scripts create at render time: written into innerHTML, or set on an
# element after `createElement`. Those legitimately are not in the markup.
built: set[str] = set()
built.update(re.findall(r"""\bid=\\?["']([A-Za-z][\w-]*)\\?["']""", all_js))
built.update(re.findall(r"""\.id\s*=\s*['"`]([A-Za-z][\w-]*)['"`]""", all_js))

# ------------------------------------------------- referenced but not present
#
# The dangerous direction. A miss here is a null dereference at runtime, and in
# a click handler that is an unhandled rejection: the button does nothing and
# says nothing.
#
missing = sorted(dereferenced - page_ids - built)
case("every element the scripts ask for exists in the page",
     not missing, ", ".join(missing[:8]) + (" …" if len(missing) > 8 else ""))

# ------------------------------------------------- present but never mentioned
#
# Dead controls. Buttons whose id appears nowhere in any script cannot be doing
# anything, unless they are driven by a data- attribute instead.
buttons = {name for tag, name in interactive if tag == "button"}
orphans = sorted(name for name in buttons if name not in mentioned)
case("every button in the page is reached by some script",
     not orphans, ", ".join(orphans[:8]) + (" …" if len(orphans) > 8 else ""))

# Inputs and selects the code never reads are usually a rename left half-done.
inputs = {name for tag, name in interactive
          if tag in ("input", "select", "textarea")}
unread = sorted(name for name in inputs if name not in mentioned)
case("every input and select is read by some script",
     not unread, ", ".join(unread[:8]) + (" …" if len(unread) > 8 else ""))

# ---------------------------------------------------------- duplicate ids
#
# Two elements sharing an id means `$()` silently returns whichever came first,
# so half the page's wiring goes to the wrong element.
seen: dict[str, int] = {}
for name in re.findall(r'\bid="([^"]+)"', html):
    seen[name] = seen.get(name, 0) + 1
duplicates = sorted(name for name, count in seen.items() if count > 1)
case("no id is used twice", not duplicates, ", ".join(duplicates[:8]))

# ------------------------------------------------------- endpoints they call
#
# Every path the interface fetches has to exist on the server, or the button
# reaches the network and comes back 404.
main = (ROOT / "astrocontrol" / "main.py").read_text("utf-8")
routes = set()
for verb, path in re.findall(r'@app\.(get|post|delete|put|websocket)\(\s*"([^"]+)"',
                             main):
    routes.add(path)

called = set()
for pattern in (r"""api\(\s*['"`](/api/[^'"`?]+)""",
                r"""send\(\s*['"`](/api/[^'"`?]+)""",
                r"""fetch\(\s*['"`](/api/[^'"`?]+)""",
                r"""rigQuery\(\s*['"`](/api/[^'"`?]+)"""):
    called.update(re.findall(pattern, all_js))


def matches(path: str) -> bool:
    """Whether a called path lines up with a declared route, templates included."""
    for route in routes:
        if route == path:
            return True
        pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", re.escape(route)
                               .replace(r"\{", "{").replace(r"\}", "}")) + "$"
        if re.match(pattern, path):
            return True
    return False


# A path built out of a template literal cannot be checked from here — the
# interesting half of it is a variable.
unknown = sorted(path for path in called
                 if "${" not in path and not matches(path))
case("every endpoint the interface calls exists on the server",
     not unknown, ", ".join(unknown[:8]) + (" …" if len(unknown) > 8 else ""))

print()
print(f"checked {len(control_ids)} controls, {len(called)} endpoints")
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
