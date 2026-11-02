# Changelog

## 1.0.0.31

- *Collect the lights on the server* in the start-a-collaboration form, and on Edit. A collaboration with it on takes everyone's lights, flats, darks and bias through the files page at collab.starfront.space/files, sorted by collaboration and person, for the coordinator to stack. A card on such a collaboration says so and has an *Open the files page* button.

## 1.0.0.30

- A telescope with its camera open is reported as shooting. The plugin told the others' charts "idle" unless its own Starfront container was the thing running, so a telescope on a collaboration's target through its own sequence, or between two runs of the container, sat at M31 marked idle all night. Shooting is now read from the camera and from the last light saved, with that light's target name.
- Telescopes online shows a Starfront program's rig as shooting too (it says "exposing" between frames), and shows cooling, centring, focusing and paused instead of idle.

## 1.0.0.29

- A mono telescope is no longer told to "set an exposure for OSC-RGB" before joining a collaboration that also takes colour data. Before joining, the plugin checks exposures only for the channels its own filters deliver; the rest is the server's to deal to others.

## 1.0.0.28

- Joining with the camera just connected works. The server judged a join by the profile from the last check-in, up to ten minutes old and from before the camera was connected, so it refused with "this telescope has not said what it can see" although the camera was on; a join now reads the camera and sends the profile first.
- A join with no camera connected is refused with what to do: connect your camera first, and set the focal length if it is not set.
- The plugin reads the sensor from the camera itself again, as before 1.0.0.27, and no longer guesses it from the Framing Assistant; the *What the server is told* line is gone.

## 1.0.0.27

- A telescope whose camera had not been connected since the plugin was installed could not join although its focal length and pixel size were set. (Taken back in 1.0.0.28.)
- On an open card the *Join* button and the can-take-part line come before the list of who is on it, which can be long.

## 1.0.0.26

- *This telescope* under Options > Plugins > Starfront is down to what matters: the name on the chart, whether to show where you point, and the Bortle class. *Hours a night to give* is gone, because the sequence already decides how long the container runs; the server deals a full night's share and the container shoots what it has time for. *Check in every* is gone, fixed at ten minutes. The *Server* box and its *Open* button are gone.

## 1.0.0.25

- Beside *Join the Starfront Discord*, on the plugin's page and in the guide, it says plainly that you do not have to be a Starfront customer: anyone in the server, anywhere, can join and start collaborations.

## 1.0.0.24

- The plugin's page says plainly that you have to be in the Starfront Discord server to take part, with the invitation first in the description and among the links at its top; its homepage is the observatory's website. The button on the Imaging-tab panel is gone; the one beside the sign-in button on the options page stays.

## 1.0.0.23

- A *Join the Starfront Discord* button beside the sign-in button on the options page opens the invitation to the Starfront Discord server, which signing in needs.

## 1.0.0.22

- *Telescopes online* folds away, with the count in its heading; it opens on a click and stays as left.

## 1.0.0.21

- The list's controls are two plain drop-downs: *Show* (all, ones I can join, ones I am on) and *Sort* (most active, least done, highest now, newest, by name). The ones you are on come first whatever the sort.
- Telescopes online is a table: telescope, owner, what it is doing, its target and the collaboration it is on, shooting ones first. Each collaboration's card lists the telescopes on it by name, the ones online marked with what they are doing.
- Every switch in the form has its label beside it, and the field-or-mosaic choice is a drop-down: N.I.N.A.'s theme draws a tick box as a bare on/off switch and shows no text on it, which left "Take OSC broadband", "Calibrated frames only" and the list's two filters as unlabeled switches.

## 1.0.0.20

- The collaboration list is built for hundreds. One line per collaboration (name, coordinator, kind and size, filters, how far along, hours collected, who is on it, how high it is in your sky now, and whether you can take part), a search box that matches a word anywhere on the card, switches for *I can take part* and *Joined*, and a sort: what needs you, most active, highest in your sky now, newest, by name. A line opens into the full card; the sky pictures are fetched only for open cards, and the list scrolls on its own, so a thousand rows cost what the visible dozen do.

## 1.0.0.19

- A colour camera's data is its own band, kept apart from the mono channels: OSC-RGB behind a clear or UV/IR cut filter, OSC-Ha+OIII behind a dual-band (OSC-SII+OIII, OSC-Ha+OIII+SII likewise). The depth map, the progress bars and the hours log it under that name, never under Ha, OIII, red, green or blue. Whoever starts a collaboration says whether it takes OSC broadband and OSC narrowband; ticking a box puts the band among the filters wanted with its own hours.
- Every telescope can rate its sky: *Bortle class* under Options > Plugins > Starfront > This telescope, 1 to 9. A collaboration can ask for a sky no worse than a given Bortle class; a rig that has not rated its sky is told what is wanted rather than refused, and a night reported from a brighter sky is rejected.

## 1.0.0.18

- One-shot colour cameras take part properly. Under Options > Plugins > Starfront each filter in your wheel has a *Delivers* choice: a mono Ha delivers Ha; a colour camera's clear or UV/IR cut delivers red, green and blue in one sub; an L-eXtreme, L-eNhance, L-Ultimate, ALP-T, NBZ or Askar D1 delivers Ha and OIII at once; a D2 delivers SII and OIII; a Triad Ultra or quad-band all three. The guess from the name is usually right. The server deals you the filters that deliver what a project wants and credits a dual-band night to both its lines.
- Browsing collaborations shows, on every card, each channel's depth against its goal as a bar with the hours, the total collected, and in one coloured line whether your telescope can take part and with which filters.
- Starting a collaboration lists the channels your filters deliver, rather than your wheel's names.

## 1.0.0.17

- *From Framing Assistant* takes the rectangle as drawn: its centre after dragging, its panels and overlap, its rotation. It took the view's zoom for the width, so a test mosaic came out thirty-nine degrees across.
- The *Check in* buttons are gone. Signing in checks in at once, and the plugin keeps checking in on its own at the interval in the options.

## 1.0.0.16

- The plugin is called *Starfront* now, in the Plugins list and under Options, instead of *Starfront Collaboration*. Everything else is as it was; your sign-in and settings carry over.

## 1.0.0.15

- Start a collaboration from N.I.N.A. The Starfront panel has a *Start a collaboration* form: name it, look the target up by name (or take it from the Framing Assistant, or type RA and Dec), choose one field or a region to tile, say which filters and how many hours you want, set any rules, and it is live for every telescope on the server. Anybody signed in with Discord may start one; being in the Starfront Discord server is the whole gate.
- Collaborations you started show *Edit*, *Deal again* and *Close* on their card.

## 1.0.0.14

- Each field starts guiding after it is centred, as a target container does, when a guider is connected. N.I.N.A.'s *Center* only resumes guiding it stopped itself, so a sequence that starts the guider inside each of its own targets rather than in its Start area reached the Starfront container with the guider idle: one tester's 23 Blue subs were shot unguided at 5–7″ with every dither a no-op.
- A sub whose stars are larger than the project's star-size limit is not credited, and the log says so. The server judges a night by its mean star size, so a night of good subs with a trailed hour in it passed or failed as a whole; now only the subs that meet the rule are reported.

## 1.0.0.13

- The drift trigger the container builds inside each field is named *Center After Drift* again instead of "MISSING LABEL Lbl_SequenceTrigger_Platesolving_CenterAfterDriftTrigger_Name". The label key was wrong, and the fallback did not recognise N.I.N.A.'s "MISSING LABEL" answer as missing.

## 1.0.0.12

- A meridian-flip trigger on a container above the Starfront one no longer runs the flip routine (autofocus, recentre, settle) the moment the Starfront container is next. The container told the trigger its next instruction would take the whole night, so the trigger decided it had no time left; it now answers with one exposure, as *Take Many Exposures* does, and the trigger gets its look again before every item inside.

## 1.0.0.11

- Runs on N.I.N.A. 3.3. The container failed the moment it built a panel there ("Method not found: DitherAfterExposures..ctor"): 3.3 gave the dither and centre-after-drift triggers a safety-monitor parameter, and the plugin was bound to the 3.2 constructors. The host's triggers are now built from whichever constructor the running N.I.N.A. has.

## 1.0.0.10

- Every collaboration on the Starfront panel shows the target's altitude through tonight: dusk to dawn, the horizon and the project's floor shaded, the usable hours filled, transit marked and "now" drawn, with one line saying when it is up.

## 1.0.0.9

- Every field recentres on its own coordinates when it drifts (the distance is an option; 0 switches it off). A *Center after drift* trigger placed on a container above the Starfront one takes its target from that container, and one run was slewed to M33 in the middle of M31 that way.
- A single-field collaboration no longer spends a slew and a plate solve measuring the camera's angle before starting, nor warns that the panels were laid for another angle; only a mosaic's grid depends on it.
- A run that is stopped still reports the frames shot since its last check-in.

## 1.0.0.8

- The container no longer fails the moment it runs. Its panels were added to the sequence from the sequencer's worker thread, which WPF refuses once the sequence editor is bound to them; N.I.N.A. then carried on with whatever instructions followed, and nothing was reported. Panels are now added and cleared on the window's thread.

## 1.0.0.7

- A sequence started again during the night carries on where it left off: frames already shot on a panel tonight are taken off its count, and a finished panel is not shot again.
- Frame times are no longer shifted when N.I.N.A. stamps them in local time.

## 1.0.0.6

- A camera without a rotator is plate-solved once at the first panel when its angle is unknown or differs from the angle the panels were laid for; the server then lays the panels again before the night starts, instead of a night being shot on a grid the camera cannot cover.
- Reported footprints carry the camera's own field size at the solved angle.

## 1.0.0.5

- Every collaboration on the Starfront panel shows its sky (DSS2 colour) with the region outlined and, once joined, your own panels drawn on it with tonight's share filled; cutouts are kept on disk.
- *Show in Framing Assistant* opens N.I.N.A.'s Framing Assistant on the region at its angle, with your camera's footprint.

## 1.0.0.4

- The Starfront panel on the Imaging tab lists every field of the night's run with where it has got to - done, shooting (frame n of N, or slewing, centring, changing filter), waiting, skipped and why - live, and keeps the last run until the next begins.

## 1.0.0.3

- Filters are shown everywhere as your own wheel names them; the server does the matching to its standard names.
- Wording covers single-field collaborations as well as mosaics.

## 1.0.0.2

- The server lives at collab.starfront.space; the old address, if typed in, is pointed at the new one.

## 1.0.0.1

- The container follows the server only while it sits in a sequence.

## 1.0.0.0

First release for the Starfront Observatories community.

- Sign in with Discord; only members of the Starfront server can take part.
- Per-filter exposure table; the server deals your share at your own sub lengths.
- *Starfront collaboration* sequence container: reads tonight's deal once, then slews, centres, switches filter and shoots each dealt panel, skipping panels below the project's altitude floor.
- Frames credited to their panel with star size and guiding error, reported on each check-in.
- Imaging-tab panel: who is online, open collaborations with compatibility and progress, tonight's deal.
