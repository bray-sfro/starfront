# Changelog: the Starfront desktop

The desktop program (`astrocontrol/`, `Starfront-Setup-<version>.exe`). Its version is `astrocontrol.__version__`. The N.I.N.A.
plugin keeps its own, `nina/CHANGELOG.md`. Entries before 0.2.47 are taken from their release commits.

## 0.2.47 (2026-10-30)

- A volunteer worker or a hub installs even from a Starfront server that does not carry the release, a home lab's for example. After
  the server, the desktop tries the public releases: first Rapid Stacker Collab's own downloads
  (`https://collab.astro.ekeservices.com/downloads/`), then the GitHub releases on Starfront's repository. Every source is checked
  the same way (the signature against the key built in, then each archive's size and SHA-256), and one that fails is skipped and
  reported.
- collab-worker and collab-hub 0.1.1 are published in both places.

## 0.2.46 (2026-10-28)

- Volunteer installs keep the worker's scratch settings. 0.2.45 deleted the scratch folder and cap that `collab-worker enrol`
  wrote whenever the Volunteer panel's were left at their defaults, so its installs failed at `service install` after spending
  the enrol code.

## 0.2.45 (2026-10-28)

- The desktop carries the real release signing key. 0.2.44 was built with the placeholder, so it refuses to install a volunteer
  worker or a hub, whatever is published. 0.2.45 is the first build that can.

## 0.2.44 (2026-10-25)

- The desktop side of distributed live stacking: running a hub and installing a volunteer worker from the Collab tab, and the
  stacking project made and kept in step from the collaboration.
- The corrected colour-camera note.

## 0.2.43 (2026-10-09)

- The coordinator's *Re-deal tonight* button.
