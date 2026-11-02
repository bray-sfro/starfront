# The sky on the website

A chart of where the observatory's telescopes are pointing tonight, for a
page on the Starfront website or anybody else's. It is served by the
collaboration server, so the site never needs updating when the chart does.

## What it shows

The sky over the observatory as a planetarium shows it: the dome straight
up, north at the top and east to the left as you see it looking up, the
bright stars in place, the horizon as a ring with the compass points on it
and everything below it darkened. Visitors drag to look around, scroll or
use the buttons to zoom, and press Overhead to come back to the whole sky.
On it, every telescope that shares its position: a mark where it points
now, its name and target. Underneath, a list of the telescopes with what
each is on. `data-fov` sets how much sky the view opens on; 180 is horizon
to horizon. `data-tracks="1"` also draws each telescope's track through the
night; it is off by default because a dozen slews read as a tangle.

Only what a telescope chooses to say is shown: its name, its target, its
pointing. No owners, no ids, no pictures. A telescope with position sharing
switched off in Starfront never appears.

## On a Shopify site

Theme editor → add a section → **Custom Liquid**, and paste:

```liquid
<div data-starfront-sky data-title="Tonight at Starfront"></div>
<script src="https://collab.starfront.space/sky/widget.js" async></script>
```

Or install `starfront-sky.liquid` as `sections/starfront-sky.liquid` in the
theme for a section with its own settings (heading, hours of track, refresh
rate, the observatory's latitude and longitude for the horizon shading).

## Anywhere else

The same two lines work on any page. Options go on the element:

| attribute | meaning | default |
| --- | --- | --- |
| `data-server` | the Starfront server | the script's own host |
| `data-hours` | how far back the tracks go | 14 |
| `data-refresh` | seconds between refreshes | 60 |
| `data-lat`, `data-lon` | the observatory, for the horizon | 31.9, -99.1 (the site in Starfront's settings) |
| `data-title` | the heading, blank for none | Tonight at Starfront |
| `data-height` | pixels, or `auto` for half the width | auto |

A full page version is at `https://collab.starfront.space/sky`, which also
works in an iframe.

## What the server provides

- `GET /api/v1/sky?hours=14` — the telescopes and their tracks, JSON, open
  to anyone, with permissive CORS.
- `GET /sky/widget.js`, `GET /sky/stars.json` (the Yale bright stars to
  magnitude 5), `GET /sky` (the page).

Tracks come from each telescope's check-ins, about every ten minutes, and
are kept for two nights.
