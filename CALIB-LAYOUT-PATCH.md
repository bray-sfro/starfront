# calib-layout patch (2026-11-01) on top of releases\source-0.2.45-official

Calibration goes to the rig's Dropbox, laid out the way the Dropbox already is:

    <Dropbox>\RASA Rig\Calibration\RASA1\Bias\2026-10-30\       bias subs
    <Dropbox>\RASA Rig\Calibration\RASA1\Darks\2026-10-30\      dark subs
    <Dropbox>\RASA Rig\Calibration\RASA1\Flats\2026-10-30\      panel flats (sky flats in ...\sky)
    <Dropbox>\RASA Rig\Calibration\RASA1\DarkFlats\2026-10-30\  darks for flats
    <Dropbox>\RASA Rig\Calibration\RASA1\Masters\2026-10-30\    masters built that night

Root = calibration.libraryDirectory if set, else <Dropbox>\RASA Rig\Calibration if it
exists (Dropbox path read from %LOCALAPPDATA%\Dropbox\info.json), else the old
<data>\calibration. The old flat <data>\calibration\masters folder is still scanned,
read-only (a master in both places is listed once, from the new folder).

Files: astrocontrol\calibration.py, astrocontrol\calibrating.py, astrocontrol\capture.py,
new tools\check_calib_layout.py. Full diff: calib-layout.diff.

WARNING: the FSQ106 PC's Dropbox may sync the same "RASA Rig\Calibration" folder. Set
calibration.libraryDirectory there before running this code on it.
