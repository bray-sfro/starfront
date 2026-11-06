"""Any labelled preview in release.json is selectable in the viewer.

Processing's continuum-subtracted images (Ha, OIII) are just more previews:
each is one entry in release.json's `files` with role "preview" and its own
`label` (and `filter`, `kind`, `title`), and it shows up as its own button.
A label that duplicates another preview's is dropped, so the labels must be
distinct (e.g. "Ha" and "Ha (continuum-subtracted)").
"""
from test_files_rules_api import world  # noqa: F401


def test_viewer_takes_every_labelled_preview(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    # Every preview that is not a starless twin becomes a view, whatever its name.
    assert "x.role === 'preview' && x.starless !== true && !x.starlessOf" in html
    # The label comes from the manifest, not from a hard-coded list of filters.
    assert "p.label || (p === hero ? 'Color' : (p.kind || p.filter || nameKind(p.name)))" in html
    # So "Ha (continuum-subtracted)" needs no code: label/filter/title drive it.
    assert "filter: p.filter || ''" in html and "title: p.title || label" in html
    # Distinct labels only: a duplicate label is skipped.
    assert "seen.has(label.toLowerCase())" in html
    # The manifest can also pin the hero and name the starless twin by file name.
    assert 'previews.find((x) => x.hero)' in html
    assert "typeof p.starless === 'string'" in html
    # Deep link by label: #view=Ha%20(continuum-subtracted).
    assert '[v.key, v.kind, v.filter].some' in html
