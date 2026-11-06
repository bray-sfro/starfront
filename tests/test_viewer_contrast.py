"""The viewer's Contrast slider is display-only (Bray, 2026-11-06).

A CSS contrast() filter on the preview <img> elements, never a new
file, a canvas re-encode or a server write. 50-300%, 100% as published,
Reset back to 100%, one value shared by the embedded preview and the
full-screen viewer for the page's life.
"""
from test_files_rules_api import world  # noqa: F401


def test_page_has_labelled_contrast_sliders_and_reset(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert '<label for="contrast' in html
    assert 'type="range"' in html and 'min="50"' in html and 'max="300"' in html
    assert 'data-contrast-reset' in html and '>Reset</button>' in html
    # One on the embedded preview, one in the full-screen viewer.
    assert 'id="viewerContrast"' in html and "contrastControl('Viewer')" in html
    assert "contrastControl('Hero')" in html


def test_contrast_is_css_only_and_defaults_to_100(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert 'let contrastPct = 100;' in html
    assert "contrastPct === 100 ? ''" in html
    assert 'filter: var(--img-contrast, contrast(1))' in html
    assert "'--img-contrast'" in html
    # Only the two preview elements are filtered; no image is re-encoded.
    assert '#hero .shot img' in html and '#viewerImg { filter:' in html
    assert '<canvas' not in html and 'getContext(' not in html and 'toDataURL(' not in html
    # The contrast value itself is never persisted (no storage of it).
    assert 'contrastPct' in html and 'localStorage.setItem' not in html.split('applyContrast')[1].split('contrastControl')[0]
    assert 'applyContrast(e.target.value)' in html and 'applyContrast(100)' in html
