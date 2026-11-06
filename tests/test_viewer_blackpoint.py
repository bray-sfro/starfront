"""The viewer's Black point slider is display-only (Bray, 2026-11-06).

An inline SVG feComponentTransfer linear stretch (out = (in - bp)/(1 - bp))
referenced from the previews' CSS filter alongside contrast(). 0-50%,
default 0, its own Reset, one value shared by the embedded preview and the
full-screen viewer. No canvas, no re-encode, no server write.
"""
from test_files_rules_api import world  # noqa: F401


def test_page_has_labelled_black_point_sliders_and_reset(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert '<label for="blackpoint' in html
    assert 'data-blackpoint' in html and 'min="0"' in html and 'max="50"' in html
    assert 'data-blackpoint-reset' in html and 'aria-label="Reset black point"' in html
    # Both places, through the same control builder as Contrast.
    assert "contrastControl('Viewer')" in html and "contrastControl('Hero')" in html
    assert 'id="viewerContrast"' in html


def test_black_point_is_an_svg_linear_stretch_not_a_rewrite(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert 'let blackPoint = 0;' in html
    assert '<filter id="sfBlackPoint"' in html
    for ch in ('sfBpR', 'sfBpG', 'sfBpB'):
        assert f'id="{ch}" type="linear" slope="1" intercept="0"' in html
    assert '1 / (1 - blackPoint)' in html and '-blackPoint / (1 - blackPoint)' in html
    assert 'url(#sfBlackPoint)' in html
    assert "blackPoint > 0" in html          # at 0 the filter is not referenced at all
    assert 'filter: var(--img-filter, none)' in html
    assert '<canvas' not in html and 'getContext(' not in html and 'toDataURL(' not in html


def test_each_control_has_its_own_reset(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert "if ('contrastReset' in d) applyContrast(100);" in html
    assert "else if ('blackpointReset' in d) applyBlackPoint(0);" in html
    assert 'aria-label="Reset contrast"' in html
