"""Blacks slider lifts as well as crushes (Bray, 2026-11-06).

Neutral-centred -50..+50: left lifts the shadow floor (out = in*(1-|v|)+|v|),
right crushes (out = (in - v)/(1 - v)), 0 is the published image. Still one
display-only SVG feComponentTransfer linear stretch combined with contrast().
"""
from test_files_rules_api import world  # noqa: F401


def test_slider_is_signed_and_neutral_centred(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert 'data-blackpoint min="-50" max="50" step="1"' in html
    assert 'let blackPoint = 0;' in html
    assert '>Lift blacks</span>' in html and '>Darken blacks</span>' in html
    assert '>Neutral</output>' in html
    assert 'aria-label="Reset blacks to neutral"' in html


def test_negative_lifts_and_positive_crushes(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    # v >= 0: crush, slope 1/(1-v), intercept -v/(1-v).
    assert 'v >= 0 ? 1 / (1 - v) : 1 - lift' in html
    assert 'v >= 0 ? -v / (1 - v) : lift' in html
    assert 'Math.abs(Math.min(0, v))' in html
    assert 'blackPoint !== 0' in html
    assert 'Math.max(-50,' in html
    # Signed readout.
    assert "`+${bpPct}% darker`" in html and "`${-bpPct}% lifted`" in html
    assert "bpPct === 0 ? 'Neutral'" in html
    assert 'let blackPoint = 0;' in html


def test_still_display_only(world):  # noqa: F811
    client, *_ = world
    html = client.get("/files").text
    assert '<filter id="sfBlackPoint"' in html and 'url(#sfBlackPoint)' in html
    assert '<canvas' not in html and 'getContext(' not in html and 'toDataURL(' not in html
    assert "else if ('blackpointReset' in d) applyBlackPoint(0);" in html
