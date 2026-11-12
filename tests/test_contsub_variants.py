"""Continuum-subtracted images are an option on their parent's tab (Bray,
2026-11-09), not tabs of their own.

The shape processing writes from now on, on the variant's own preview entry:

    {"name": "M31_Ha_contsub_v10_preview.jpg", "role": "preview",
     "label": "Ha (continuum-subtracted)", "kind": "Ha-contsub", "filter": "Ha",
     "group": "filter", "fullres": "M31_Ha_contsub_v10_fullres.jpg",
     "starless": "M31_Ha_contsub_starless_v10_preview.jpg",   # optional twin
     "variantOf": "Ha", "variantLabel": "Continuum-subtracted"}

`variantOf` is the parent's label, `variantLabel` the option's name; a
variant published only starless says `"variantStarless": true`. Without
those fields, an entry whose kind ends in -contsub / -contsub-starless (or
whose label says continuum-subtracted) folds under the view with the same
`filter`, so M31 v9 tidies up without a republish. Its label stays a valid
deep link: #view=<old label> opens the parent with the option on.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parent.parent / "server" / "files.py").read_text("utf-8")


def _function(name: str) -> str:
    start = SOURCE.index(f"const {name} = (p) => {{")
    end = SOURCE.index("\n    };\n", start) + len("\n    };\n")
    return SOURCE[start:end]


def test_page_folds_variants_under_their_parent():
    # Variants are collected, not drawn as views, and fill the parent's list.
    assert "if (p !== hero && variantOf(p)) { later.push([p, label]); continue; }" in SOURCE
    assert "views.find((v) => v.mono && v.filter.toLowerCase() === w.filter)" in SOURCE
    assert "parent.variants.push(x)" in SOURCE
    # No parent: still its own tab, as before.
    assert "Nothing to fold under: its own tab" in SOURCE
    # The option is a toggle on the tab; the image shown is cur(view).
    assert 'data-variant="${esc(x.label)}"' in SOURCE and "function setVariant(label)" in SOURCE
    assert "swapHero(s[mode])" in SOURCE
    # Starless-only variants show starless; the Stars button says why it is off.
    assert "if (!s.stars) return 'starless';" in SOURCE and "is published starless only" in SOURCE
    # Old deep links: a variant's label (or kind) selects the parent with it on.
    assert "x.keys.includes(h.view)" in SOURCE and "wantVariant = variant" in SOURCE
    # The hash keeps the on-screen image's own label.
    assert "view: cur(v).key" in SOURCE


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_variant_rules_on_m31_v9_entries():
    entries = [
        {"label": "Ha", "kind": "Ha", "filter": "Ha"},
        {"label": "Ha (continuum-subtracted, starless)", "kind": "Ha-contsub-starless", "filter": "Ha"},
        {"label": "OIII (continuum-subtracted, starless)", "kind": "OIII-contsub-starless", "filter": "OIII"},
        {"label": "Ha (continuum-subtracted)", "kind": "Ha-contsub", "filter": "Ha"},
        {"label": "Anything", "kind": "X", "filter": "Ha", "variantOf": "Ha",
         "variantLabel": "Continuum-subtracted", "variantStarless": True},
        {"label": "HOO", "kind": "HOO"},
    ]
    script = _function("variantOf") + f"\nconsole.log(JSON.stringify({json.dumps(entries)}.map(variantOf)));"
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out[0] is None and out[5] is None
    assert out[1] == {"filter": "ha", "label": "Continuum-subtracted", "starless": True}
    assert out[2] == {"filter": "oiii", "label": "Continuum-subtracted", "starless": True}
    assert out[3] == {"filter": "ha", "label": "Continuum-subtracted", "starless": False}
    assert out[4] == {"parent": "ha", "label": "Continuum-subtracted", "starless": True}


def test_release_link_keeps_the_view():
    # files#release-<id>&view=<label>: the project id is the part before '&',
    # the hash is kept for fromHash, and the page does not scroll off the image.
    assert "url.hash.slice(9).split('&')[0] !== id" in SOURCE
    assert ".replace(/^release-/, '').split('&')[0]" in SOURCE
    assert "if (/&(view|stars)=/.test(h)) return true;" in SOURCE
