# Starfront

Equipment control for astrophotography: connect a mount, camera, filter wheel,
focuser and guider, drive them from one screen, and see every frame you capture
properly stretched and measured.

It runs as an application window on the capture PC, over a local HTTP server, so
the same screen is also reachable from a tablet on the network when you want it.

This is the foundation layer - the hardware abstraction, control surface and
image pipeline that automation (sequencing, autofocus, plate solving) gets built
on top of.

---

## Running it

**The download.** `Starfront-<version>-windows.zip` unzips to a folder with
`Starfront.exe` in it; double-click that and the window opens.  Nothing to
install and no Python needed — everything the program uses sits beside it in
`_internal`.  Windows SmartScreen may ask on the first run (*More info → Run
anyway*).  `Starfront Console.exe` is the same program with a console and the
command-line switches below, for a headless rig or for reading why a window
will not open.  `READ ME FIRST.txt` in the folder says the rest: ASCOM, ASTAP
and PHD2 are installed as they are for N.I.N.A.

To build that zip from this source: `python -m pip install pyinstaller`, then
`powershell -ExecutionPolicy Bypass -File packaging\build.ps1`.  It writes
`packaging\dist\Starfront-<version>-windows.zip`.  The version is
`astrocontrol.__version__`; bump it for a release.  The program icon is built
from `packaging\logo.png`, the Starfront Observatories mark, by
`packaging\make_icon.py`.

**The first run.** A window called *First light* opens over an empty program
and walks through the whole thing in order: bring your settings over from
N.I.N.A., check the site and optics, connect the equipment, frame a target in
the Planner, plan tonight, join the collaboration with Discord, and take part
in or start a collaboration.  It is not a second copy of any of those — each
step says what is about to happen and why, presses the real button, and then
watches: the tick appears when the camera is really connected, when a target
really exists, when the plan really has frames on it.  Steps can be skipped
and come back to; *Later* closes it and the **First light…** button in the
top bar reopens it where you left off.  Once finished (or with *Don't open
this on its own again* ticked) it stays out of the way and the button is
still there.

**From source:**

```bash
pip install -r requirements.txt
```

```bash
python run.py
```

That opens the Starfront window.  For a shortcut on the capture PC, make one
to `Starfront.pyw`, which starts the same window with no console behind it.

Useful flags:

| Flag | Effect |
| --- | --- |
| `--server --host 0.0.0.0` | serve only, so a tablet or laptop can reach the rig at `http://<pc>:8765/` |
| `--browser` | open the interface in a web browser instead of a window |
| `--port 9000` | use a fixed port (the window picks a free one by default) |
| `--debug` | developer tools in the window |
| `--reload` | auto-reload on source changes, for development |

`--browser` and `--server` do not need pywebview installed.

Frames are written to `~/Starfront/captures/<date>/` unless you point the
camera panel somewhere else.  `ASTRO_DATA_DIR` changes the default.

> **This program used to be called AstroControl.**  A rig that already has an
> `~/AstroControl` folder goes on using it, and nothing is moved or copied — so
> read `~/Starfront` below as "wherever your settings already live".  The
> alternative was a program that started up one morning having forgotten its
> site, its filters, its targets and every sequence, all of which were still on
> disk under the old name.  That is worse than either name: nothing lost, and
> nothing findable.
>
> The Python package is still `astrocontrol`, because it is an import path
> rather than a name anybody reads.

### When something goes wrong

The window has no console behind it, so everything is written to
`~/Starfront/logs/` as well:

* `astrocontrol.log` — the session log, plus any uncaught exception, including
  ones on background threads.  A capture thread dying used to take the night
  with it and print to a stderr nobody was reading.
* `crash.log` — written by `faulthandler` if the process dies outright.  An
  ASCOM driver misbehaving inside COM can bring Python down with an access
  violation, which has no Python traceback at all; this still records the stack
  of every thread, which usually names the culprit.

`GET /api/diagnostics` returns both, so the tail of a run that did not survive
can be read without going looking for the files.

### Framing against one of your own frames

**Open FITS…** in the Planner takes a frame off disk and uses it as the
background instead of a survey cutout. Two reasons, and the second is the one
that matters:

* *"Put the new mosaic where last spring's one was"* is a question about a file
  on disk, not about anything the camera has taken tonight. **Centre on it**
  drops the framing onto the middle of that frame at the angle it was shot at —
  the same field, the same way up.
* A survey picture, however pretty, is not what your telescope sees. A real
  frame shows the field at your focal length, through your filters, with your
  gradients and your star shapes, which is what a framing decision is actually
  made against.

The frame is plate solved on the way in, so the overlay knows where it points,
how wide it is and which way up it was shot — the rectangle lands on the real
sky rather than on the pixels. Three ways, in order, and the answer says which
one produced it:

1. **The header, if it has the answer.** Anything written after a plate solve —
   by this program, by N.I.N.A., by SGP, by ASTAP itself — carries it in its WCS
   keywords, and reading six numbers beats spending twenty seconds rediscovering
   them. It also means this works on a laptop with no solver installed, which is
   where planning actually gets done.
2. **ASTAP, blind.** Blind because every hint this program could offer is about
   *this* telescope: where the mount is pointing tonight, and the field of the
   camera currently plugged in. For a file from somewhere else those are not
   approximations, they are wrong — and a confident wrong hint is worse than
   none, because the search goes to the wrong patch of sky at the wrong scale
   and correctly reports nothing there.
3. **astrometry.net.** A frame off another rig can be at a scale this machine
   has no ASTAP index files for, which is exactly what that service is for. It
   needs the network and a key (**Site & Optics → Plate solver**), takes
   anywhere from twenty seconds to several minutes, and is only ever reached
   when the two instant answers have failed. Never used during a run — it is a
   planning tool, not an observing one. Point it at your own installation if you
   run one.

Whatever happens, the reason is specific: *"ASTAP: not enough stars.
astrometry.net: no API key set"* rather than "could not solve".

**The frame is rotated onto the sky, not the sky onto the frame.** The Planner
canvas is a picture of the sky — north up, east left — because everything stated
on it is stated in sky terms: the framing angle, the mosaic grid, the compass,
the `PA` readout. So a reference shot at 35° is turned by 35° when it is drawn,
and sits skewed in a north-up view with its outline showing the angle it was
taken at. Turning the *view* instead very nearly works and is wrong in a way
that is hard to see: every overlay is then correct against the picture, but the
whole thing is tilted against the sky, so `PA 0` does not look like north up and
switching between a reference and a survey cutout silently reorients everything.

Because a reference can cover far less sky than the field being planned, the
view pulls back far enough to hold both, with the frame drawn inside it at its
true angular size.

**Zooming means two different things**, and the planner does the right one.
On a survey cutout it means "fetch a wider picture", because the picture is
generated to fit. On one of your own frames there is nothing to fetch — the
frame covers the sky it covers — so it means "stand further back": the frame
stays put at its true size and more empty sky appears around it. Only changing
the survey dropdown goes back to a cutout.

The view opens at **three times what the framing covers**, worked out from your
camera rather than fixed — three degrees is far too tight at 300 mm and far too
wide at 2000. The view exists to be dragged about in, and a picture cropped to
the frame leaves nowhere to move it to.

`python tools/check_reference.py` covers the header reading, which is where the
edge cases live: FITS headers are written by a dozen programs that agree on very
little, and RA turns up as sexagesimal text, as decimal hours and as decimal
degrees.

### The night in one picture

Above the plan is the whole night on one pair of axes: every target's altitude
curve together, the Moon drawn underneath them as a filled shape rather than a
line — it is a condition sitting over the night, not another target to compare
against — and a lane along the foot saying which target the telescope is on at
each moment, with the gaps hatched and the longest one given its length.

It answers the two questions a stack of per-target graphs cannot. **Is the Moon
going to be sitting on top of the thing I am shooting**, and **how much of the
night is nobody using.** The legend says which is which, how lit the Moon is and
how long it is up, and how much of the astronomical dark the plan actually
books — in amber when more than a quarter of it is idle.

Targets that are skipped are drawn dotted and struck through in the legend, so
what tonight is leaving out is visible without opening anything.

On each target's own graph, **when it runs is now the lit part of the picture**:
the hours the telescope spends elsewhere are dimmed, the run is underlined along
the foot, and each edge carries a flag pointing into the slot with its time on
it. As a pair of hairlines these were the faintest thing on a graph whose whole
purpose is setting them.

### What the rig costs between exposures

Every plan is arithmetic on two numbers: how long the shutter is open, and
everything else. The first is exact. The second used to be three typed guesses —
fifteen seconds a frame, twenty a filter change, ninety a panel — and guesses
are always wrong in the same direction, because nobody types in the cost of the
thing they forgot about. A 61-megapixel camera on USB 2 takes eighteen seconds
to hand over a frame. A nine-point autofocus sweep at six seconds a point is
four or five minutes with the moves, and **the old arithmetic costed it at
nothing at all**. A night planned on those guesses runs out of dark an hour
early and you are left deciding at four in the morning which target to cut.

So the rig is timed while it works. Every download, every filter change, every
slew-settle-solve, every dither and every focus run is recorded, and the plan is
costed on the median of what this rig has actually done. The median rather than
the mean, so one download that hit a stalled bus does not move the estimate; a
rolling window of the last forty, so a rig that genuinely changes is followed
rather than averaged with its own history for ever.

**Equipment → Timing** shows what it has learned — the figure, how many samples
it rests on, and how much they varied — and **Calibrate overhead** does the same
measuring on purpose, in about the time one autofocus sweep takes: a few short
frames for the download, the wheel back and forth for a filter change, and one
sweep.

**One sweep is enough for every filter.** What gets measured is not "that run
took four minutes" but the cost *per sweep point* of moving the focuser and
measuring the stars — the exposures are known, so subtracting them leaves
exactly that, and it does not depend on the filter or the sub length. A
nine-point sweep at six seconds and a fifteen-point sweep at twenty are both
predicted from the same pair of numbers, so nothing is re-measured when the
sweep is retuned or the filter changes.

Slew time is deliberately *not* measured by the button: how long a slew takes
depends entirely on how far it is, and a made-up slew measures a made-up
distance. That one is learned from the real slews a sequence makes.

The typed figures stay in **Site & Optics → Planning** untouched, as the
fallback for a rig that has never run — and unticking *plan on what the rig has
measured* puts the plan straight back on them.

`python tools/check_overheads.py` checks the model: that one timed run predicts
a differently-shaped one, that a single stalled download does not move the
figure, and that the frame ceilings and the budget agree with each other.

### What Auto-arrange does

**The night is shared out before it is ordered.** Each target gets an equal
slice, capped by how long it is actually up, and time a
target cannot use — because it is only above the horizon for twenty minutes — is
handed back and shared among the ones that can. The guarantee is the point:
*every* target that is observable at all gets time before any target gets a
second helping. Without it the first target asks for its whole window, takes the
night, and the third is told the night ran out.

Then the order and the times, earliest deadline first, which fits everything in
if any order can. A target is only ever placed inside a stretch it is genuinely
up for, so one that drops below the floor and climbs back is not scheduled
straight through the hole in the middle.

**Then the leftovers are given away.** A fair share decides what each target is
*owed*; it cannot decide what the night will actually deliver, and a target
whose window closes before it can spend its slice leaves that time behind. So
every gap is offered, in order of preference, to the target already on the
mount — running on costs no slew, no settle and no re-centre — then to the one
due next, and only then, if the gap is worth a slew, to a target that has not
run at all. Nothing is ever run on past its own window. What is left after that
is dark that nothing could have used, and the legend under the night graph says
how much of it there is.

**Times you set yourself are never overwritten.** Click a target's graph to pin
when it runs and Auto-arrange works *around* it: the slot is reserved and
everything else is scheduled either side. Each target's slot says whose decision
it was — `pinned` or `auto-arranged` — and clearing the times hands it back to
the arranger. So "M31 from ten till midnight, arrange the rest around it" is a
thing you can say.

Then it **fills the slots**: which filters, how long a sub, and how many. Three
things decide that, and all three are things a person weighs up by hand every
clear night.

**The Moon.** Not whether it is up — a five per cent crescent a hundred degrees
away is nothing, and that is most of the Moon's time in the sky — but whether it
is up, lit, and near enough to matter. A bright Moon does not stop a night; it
changes what the night is for. Narrowband barely notices it, because a 3 nm
filter throws away almost all of the scattered sunlight along with almost all of
the sky. So above a lit fraction you choose, a target moves onto whatever
narrowband it carries; one that carries none gets **shorter** broadband subs
instead, because a bright sky fills the well sooner and the answer to that is
more short frames rather than fewer long ones. A target far enough across the
sky from the Moon is left alone entirely, at full sub length.

**What the target already has.** Two hours of luminance and ten minutes of blue
is not half a picture, it is a luminance frame with a colour problem. The night
is shared out by *deficit* against a ratio rather than by the ratio itself — so
whichever channel is furthest behind gets tonight, and over a season it
converges on L:R:G:B = 2:1:1:1 without anyone tracking it. A target with nothing
shot yet falls back to the plain ratio, which is the right answer for a first
night.

**The goal.** A target with a goal in hours is given only what is left to reach
it, so the last night on a target is the short one it should be, and a target
past its goal is given nothing.

Every choice comes back **with its reason in words**, listed under the night
graph — an allocation that arrives without one is an allocation you have to
check by hand, which is most of the work it was meant to save. And nothing is
decided at the telescope: it all lands on the plan where it can be seen and
changed before the night starts. Untick **pick exposures** next to the button to
get the times only.

**Sub lengths are per filter.** **Site & Optics → Planning** carries a default
exposure for every filter in the wheel — 90 s of luminance, 180 s of red, 900 s
of Ha, whatever your rig actually wants — and that is the number Auto-arrange
reaches for. The three fallbacks below it are only for a filter with no entry, so
something unusual in the wheel still gets a sensible length rather than a default
that suits nothing.

Slots are filled to the end. Rounding to whole frames, and dropping any filter
that could not reach a useful count, used to leave time on the table — on a
twenty-minute window it could be most of it — so the remainder is handed out a
frame at a time to whichever surviving filter is furthest behind its share.

The lit-fraction threshold and the safe distance are in the same place.
`python tools/check_autoplan.py` exercises every rule with the Moon, the
history, the wheel and the window dialled to the case each one is about.

### Which night it is

The boundary between one night and the next is **dawn** — not midnight, and not
noon.  Before this morning's dawn you are still in last night; after it, "tonight"
means the one coming.  Dawn needs the sun, so `schedule.night` decides this
itself rather than leaving it to a calendar date.

This was got wrong, and it was worth getting wrong once to see how badly it
fails: the model was anchored to *today's* noon whatever the hour, so at one in
the morning — mid-night, with a sequence running — everything switched to the
*following* night.  Dusk moved twenty-five hours into the future and every
target's rise time went with it, so a run waiting for its target to rise waited
for a rise a day away while the thing sat overhead.  Nothing looked broken; the
sequencer sat in **waiting** and said so politely.

`python tools/check_night_rollover.py` walks the clock round the day and checks
which night comes back at each hour.

Worth keeping separate: **up** and **high enough** are different questions.  A
target at 16° really is above the horizon and really is below a 30° floor, and
waiting for it to climb is the program working.  The floor is
**Site & Optics → minimum altitude**, or per target in its plan options.

### The plan is always tonight

There is no date to set. The Earth goes round and the sky with it, so a plan
pinned to a date is one that is quietly wrong from the following evening — the
transit curves, the windows, the Moon and the running order all belong to a
night that has gone. The night is worked out afresh every time the plan is
looked at, which costs a few milliseconds and is always right.

A window left open across the small hours notices the rollover on its own and
reloads once, keyed off the browser's own clock crossing noon rather than off
the server's answer — those normally agree, and when a clock is a few minutes
out either way, comparing them means the mismatch never resolves.

Slot times from a night that has gone are dropped rather than kept, because
they are not stale so much as nonsense: a target pinned to 22:40 on Tuesday has,
by Wednesday evening, a start eighteen hours in the past. The plan itself — the
targets, the allocations, each one's floor and Moon limit — is the work, and
that stays; Auto-arrange gives it tonight's times.

### Saving a sequence

A plan is a night's worth of decisions, and those are worth more than one night.
**Save as…** keeps the current plan under a name; **Load** brings it back.
"The winter narrowband run" is something you build once and want back in
October.

What comes back is the targets, their filter allocations and every per-target
setting. What does not is the times, for the same reason they are dropped at a
rollover — they belonged to the night the sequence was saved for. Run
Auto-arrange after loading and it places the whole thing in tonight.

Sequences live in `~/Starfront/sequences.json`, beside the plan and the
targets.

### The plan, and how the night walks it

Each target in the plan carries **how much has been collected on it** in its
heading — the total across every night it has ever run, not just tonight, which
is the number the plan is actually scanned for. Give it a goal in hours and the
heading becomes `16h 45m / 20h 00m`; once it is met the target drops out of the
rotation on its own, which is what makes a plan you can leave in place for a
season.

The whole tab scrolls as one page, so the night graph and the targets it
describes move together. Only the row with **Run sequence**, **Run on loop** and
**Stop** on it stays put.

**Double-click a target** to open it up. Three things are inside:

**The framing it was saved with**, over a survey picture of that piece of sky.
A target is a decision about where to point and which way up, and until now that
decision was three numbers on a plan; here it is the thing itself, with the
camera's field drawn on at the angle it will be shot at — and for a mosaic,
every panel, numbered in the order they will be captured. The rectangle comes
from what was *saved*, not from the optics as they are configured now, because
this is a record of the framing that was chosen. A target saved without a field
falls back to what the camera covers today and says so. With no network the
overlay is still drawn — it is the useful half — over an empty sky.

**Night by night.** One row per night it has run: frames, integration, the
filters it went to, the mean star size and the range it moved over, the mean
guide RMS, and what went wrong — frames taken without a guide lock, and how many
times the run had to be put back on its feet. A running total cannot answer the
question this does. Four hours made of forty good subs and four hours made of
eighty subs half of which were trailed are the same number and are not the same
night, and the bad one is obvious at a glance because its HFR column is double
everyone else's.

**How this target runs.** Everything here overrides something shared, and
everything here is read by the sequencer:

| | |
| --- | --- |
| **Skip this target** | It stays in the plan but is not shot — weather, a tree, or a target you want back tomorrow. Skipped targets are dimmed and badged on the plan, so you can see what tonight is leaving out without opening anything. |
| **Filter order** | All of one filter then the next (fewest changes), or rotate L, R, G, B, L, R… A rotation costs a filter change per frame and buys what a change cannot: a session cut short by cloud at forty per cent is forty per cent of *every* channel, not all of the luminance and none of the red. |
| **Autofocus when it starts** | A long slew across the sky is exactly when focus has moved. |
| **Turn the rotator to the collaboration's angle** | Collaboration targets on a rig with a rotator only; see *Collaborating*. |

That is the whole list, on purpose.  The altitude floor is the observatory's,
from Site & Optics; the dither interval is the rig's, from Equipment →
Guiding, every third frame unless you change it; Auto-arrange shares the night
out evenly by how long each target is up.  A collaboration target also carries
the **project's** rules — an altitude floor and a Moon distance that whoever
started it set — and the box says what they are, because they explain a night
that skipped it, but they are not yours to edit.

**The plan is walked in order**, each target finished before the next one
starts, in the order it reads down the page — which is already time order,
because that is what the arranger wrote. Nothing reshuffles it at the telescope:
a plan you can read and a night that happens in a different order would be two
different plans.

Interleaving several fields is the mosaic's job rather than the plan's. A mosaic
is already a set of panels visited in a worked-out order inside one target,
which is the same idea done where it belongs.

`python tools/check_plan_order.py` exercises the walking and every one of the
per-target options against stub devices and a fake clock.

### What the rig is doing

A line under the tabs says what the telescope is doing right now — slewing,
centring, focusing, changing filter, settling the guider, exposing, dithering,
flipping, cooling, waiting — with how long it has been at it. It is worked out
from the live device state as well as from the sequencer, so a slew started by
hand reads exactly the same as one inside a run, and a night driven by hand
still has something to read. "Why has nothing moved for two minutes" is the
question it exists to answer, and answering it should not mean opening a panel.

Anything being *recovered* gets a second line of its own, in amber, naming what
went wrong and which attempt this is. Clicking it opens the settings that govern
it, because something going wrong is exactly when those get revisited.

### When something goes wrong

The things that go wrong at three in the morning are not exotic: the guide star
goes behind a cloud, the mount drifts until the object is halfway out of the
frame, a camera misses a download. Each of them used to end the night — either
loudly, by killing the run, or worse, quietly, by filling the disk with trailed
subs of not-quite-the-right sky.

Each is now noticed between frames, named on screen while it is being put right,
and retried a bounded number of times. **The bound is the important half**: a rig
that needs rescuing every five minutes has a fault that another retry will not
mend, and going round that loop until dawn wastes the night as thoroughly as
stopping would. The budget is per target, so a night that needed three rescues on
M31 is still willing to rescue NGC 7000; when it runs out, the run moves on to
the next target, stops, or stops and parks, as you choose.

* **The guide star.** PHD2 usually finds it again by itself within an exposure
  or two, so there is a grace period first — that is not politeness, it is the
  faster of the two options, because a cloud is over before a re-acquisition
  would have finished. After that the guider is stopped, given a fresh star and
  restarted; the later attempts recalibrate as well, which is slow and is the
  only thing that fixes a mount that has been nudged by hand. If none of it
  works the night carries on unguided and says so loudly, because an unguided
  sub of a bright target is still a sub. Frames taken while the guider had no
  lock are flagged on the sub chart, and optionally moved into a `suspect`
  folder — never deleted, because whether they are usable is a judgement for the
  morning with the stack open.
* **The pointing.** The pointing check already plate solves every Nth sub; past
  a threshold of your choosing the mount is sent back to the target and solved
  onto it again, and guiding is restarted afterwards.
* **The camera.** A camera that misses one download is fine; one that has fallen
  off the USB bus is not, and asking again is the only thing that tells them
  apart — so a failed frame is retried, and a camera that keeps failing is
  disconnected and brought back up on its remembered driver. Only the telescope
  that failed is retried, so a working camera never shoots the same slot twice.

All of it is in **Equipment → Recovery**, and `python tools/check_recovery.py`
exercises every path against stub devices that fail on purpose.

### Warnings: what is quietly wrong

Recovery is for faults that announce themselves. The worse kind do not: a cooler
that cannot reach its setpoint on a warm night, a flat panel left shut over the
objective, a disk with an hour of space left, a PC clock ten minutes out, a plan
that asks for a filter this wheel does not have. Nothing crashes; the night just
produces nothing worth keeping, and you find out in the morning.

A watchdog looks at all of it every fifteen seconds and puts what it finds on a
strip under the top bar, worst first, with a count of the rest. Click the strip
for the whole list: each entry says what is wrong, for how long, and what to do
about it. **Seen it** keeps an entry on the list but takes it off the strip until
it changes. Entries clear themselves the moment the condition goes, and the log
records each one appearing and clearing, so the morning has a timeline.

Three levels. A **critical** warning means frames are being wasted or the run
cannot proceed, and it goes out as a Discord notification (once an hour per
fault at most). A **warning** means the night is worse than it should be. A
**notice** is worth knowing and nothing more. The checks, in rough order of how
often they bite:

* **The site.** No site set while there is a plan; a mount whose own site is a
  long way from the one Starfront thinks it is at.
* **The plan.** Nothing observable tonight; a filter on the plan that the wheel
  does not carry; a plan whose lights have no dark to match, so the calibration
  library will not cover them.
* **The camera.** Missing once the night is on; off its setpoint (a notice at
  first, a warning once the cooler has sat at full power for a quarter of an
  hour, which means it *cannot* get there); frames being taken with saving off;
  a download that has taken longer than any camera needs, which is a camera
  that has hung; downloads slow enough to cost a fifth of the night.
* **The mount.** Missing with targets on the plan; tracking off during a light;
  a meridian flip that did not change the side of pier.
* **The light path.** A closed cover or a lit panel during a light frame.
* **The guider.** Wanted and not running; guiding with a rough RMS; a lost star.
* **The frames.** The last few lights black, saturated or starless; focus
  drifting; plate solves failing three times running; a run that keeps needing
  rescue.
* **The night.** Weather unsafe; the disk nearly full; ASTAP missing.
* **The collaboration.** The server rejecting this telescope's token; the server
  unreachable for a long time; a rig that no longer suits its project; this PC's
  clock disagreeing with the server's.

The thresholds live under `warnings` in `settings.json` (guide RMS, disk space,
cooling patience), and `python tools/check_warnings.py` drives every check both
ways against a stub rig.

### While a sequence runs

A banner across the top of the window says what the rig is doing without you
having to go and find it: which target, which stage, a bar for the exposure in
flight with the time remaining, a bar for progress through the target's frames,
the filter, the guide RMS and how long the run has been going — plus Pause and
Stop. It appears only while a sequence is running.

The exposure timer is kept by the server from the moment the exposure was
commanded, rather than read from the camera. ASCOM's `PercentCompleted` is
optional and a good many drivers return 0 for the whole exposure, which leaves a
progress bar that never moves through a five-minute sub.

### The observatory, not the telescope

Three device slots that describe the building rather than the optics, all on the
master telescope: a **safety monitor**, a **dome or roof**, and a **switch bank**.
Each is an ordinary ASCOM or Alpaca device, so a Pegasus Powerbox, a Lunatico
relay board and a Digital Loggers strip are all the same thing here — a Switch —
and nothing in the program is specific to one make.

#### The weather

This is the only part of Starfront whose job is the equipment rather than the
data. Everything else failing costs a night; this failing costs a mirror.

It watches from start-up rather than from the first sequence, because the
dangerous state is a roof open over a telescope and that can be true with
nothing running. **Unsafe is the answer whenever the true answer is not known** —
a monitor that has been unplugged, whose driver has thrown, or which has simply
stopped answering is not evidence of good weather. The setting that relaxes that
exists because some monitors really are flaky, and turning it on is a decision to
trust the sky over the sensor.

Both edges are slow, and by different amounts. A cloud sensor flickering unsafe
for one reading is not weather, so there is a grace period. Coming back is far
slower still: starting up into a gap in the cloud is how a rig ends up opening
and closing its roof all night, and the cost of waiting another ten minutes is
ten minutes.

When it turns, either the observatory shuts down — park, close the cover, close
the roof, warm the cameras — or the run is held where it is and picks up when it
clears. Shut down for a roof this program controls; hold for a dome somebody else
closes. It also refuses to *start* a sequence while unsafe, and the Open roof
button is refused both in the browser and at the server.

#### Telling somebody

A rig at a remote site fails silently by default. **Equipment → Notifications**
takes a webhook URL — Discord, Slack, Telegram, Pushover, ntfy and anything
self-hosted all accept one, so this is a webhook rather than a list of services
to keep up with — and/or an SMTP account.

**Discord** is the one most people want, so it is first-class: paste the
channel's webhook URL (channel settings → Integrations → Webhooks → New Webhook
→ Copy Webhook URL) and every message arrives as a card with a coloured bar —
red for what needs you, amber for what is being handled, green for a night that
ended well, blue for the telescope going about its business — and the
telescope's name in the foot, so a club channel with several rigs posting into
it still reads.  **Send a test message** proves it before a night depends on
it.

What gets sent: failures, the weather turning, recovery giving up, the sequence
finishing or the night being put to bed, and — new — the telescope's *activity*:
a night opening (each night, on loop), each target starting with what is
planned for it, a meridian flip, when the next night opens, and a calibration
run finishing with what it built.  Every individual rescue is off by default,
because that is a lot of messages.  Failure-class messages are rate limited so a
rig failing in a loop at 3am does not empty a phone battery; activity is spaced
by the sky and is never held back.  The mail password is stored but never sent
back to the browser.

#### Power

Every channel the driver reports, under the name the driver gives it — `Pegasus
UPB: Dew heater A` rather than `channel 3`. Relays get a button, ranges get a
number. Worth having beyond convenience: recovery can re-open a driver, but only
a power cycle fixes a camera that has wedged its USB controller.

### Centre and rotate

A rotator is commanded in its own mechanical coordinates, and those agree with
sky position angle only as far as its calibration does — which on most is
"roughly". The plate solve that centres a target also measures the real angle, so
the error is known exactly: the rotator is turned by it and the frame re-solved,
until both the position and the angle are inside tolerance. Only when a framing
angle was actually asked for, so a plain re-centre never starts turning a rotator
nobody mentioned.

### When a target stops

Three things end a target, and all three are checked **before** the expensive
work rather than after it: its own end time, the end of astronomical dark, and
the altitude floor.

That ordering is the whole point. A slew and a centre is minutes and a focus
sweep can be ten, and they used to happen first — so the end time was noticed
only once the telescope had already been driven to a field that had set and the
focuser swept through it. Which is how a scope ends up autofocusing at the
ground at dawn. The checks now run before the slew (allowing for what the whole
setup is about to cost), again after it, again before the focus sweep, and again
before every frame.

**Dawn is a separate backstop, because the plan does not provide one.** A target
with no end time set has no end time at all, so without this a plan of several
targets goes on slewing, focusing and exposing into full daylight, one target at
a time, until it runs out of targets. `stopAtDawn` ends the run at the end of
astronomical dark whatever the plan says, with a margin so the last frame
finishes in the dark rather than starting in it.

Autofocus refuses outright when the field is below the limit or there is not
enough night left for a sweep.

### A focus sweep always stops

The sweep extends itself towards whichever arm of the V is short, and the next
position is worked out from the points that **measured** — `min(positions) -
step`, where `positions` holds only points with a usable HFD.

At dawn the new points have no stars in them, so they never join `positions`.
The edge therefore never moves, the same focuser position is chosen again, and
the ceiling on the run — which counted *distinct positions measured* — never
grew, because `measured` is keyed by position. The result was a sweep that could
not converge and could not stop: one real run took **915 frames over four and a
half hours** and ended only because somebody pressed Abort. A normal run on that
rig is eleven frames.

Three bounds now, and a sweep that cannot converge hits one of them within a
handful of frames:

* **No progress.** If the next position has already been measured, the sweep
  cannot move and says so, rather than asking for the same frame until morning.
* **No stars.** Four points in a row with nothing measurable means the sky has
  gone — dawn, cloud, or a cover still on — and no further point will help,
  because every one of them is discarded before the curve is fitted.
* **Too long.** Counted in *samples taken*, not in distinct positions measured.
  That distinction was the bug.

`python tools/check_focus_bounds.py` reproduces the original failure and checks
every route out. It hangs against the build that produced it. There is a check before it is called as well;
that one is there because this is the longest operation the sequencer has, it
fails *slowly* on a starless frame, and it is the worst possible thing to start
on a telescope pointing at the ground.

### Stop, and Abort & park

Two different things, and the difference matters at four in the morning.

**Stop** ends the run and leaves the rig exactly where it is, still tracking.
That is right for "enough for tonight" — you may well want to look at something
by hand afterwards.

**Abort & park** puts the observatory to bed: the sequence stops, guiding stops,
the cameras stop, the flat panel's light goes out and its cover closes, the
mount parks, and the cameras warm in the background. It is available whether or
not a sequence is running, because "something is wrong, shut it down" is not a
thing that only happens mid-plan.

Each step is attempted whatever the one before it did — a mount that will not
park is not a reason to leave the cover open and the cameras at -10 C — and the
result says which steps it managed. **The park is verified rather than assumed**:
a driver can accept `Park` and do nothing, so `AtPark` is checked afterwards. An
abort that reported success without parking would be worse than one that says it
could not, because you read "shut down" and go to bed.

**The abort is latched before anything else happens** — before the reporting,
before the wait, before any check of whether a run is even going. From that
instant nothing can start another exposure, whatever races below. It used to be
set inside a `if self.running:` branch, so a shutdown arriving in the wrong
moment left the flag clear and the run carried on taking frames around a mount
that had just been parked.

If the run still has not stopped when the wait runs out, the observatory is
parked anyway — unparked is worse — but that is recorded as a **failure** and
sent as a notification, rather than reported as a clean shutdown.

### A warm-down yields to real work

Starting a sequence, a calibration run, a focus run or a plain exposure cancels
a warm-down that is still going, and puts the cameras back on their setpoint.

This is not only about the wait. A warm-down raises the setpoint a few degrees a
minute for ten minutes, and a dark library is indexed by temperature — so darks
shot during one are taken up a ramp, match nothing, and are quietly worthless.

### How a night starts and ends

The parts nobody watches. A sequence starts while you are still in the room, so
a slow start is merely annoying; it *ends* at four in the morning, and whatever
goes wrong there is found at breakfast.

**Starting.** The mount is sent to its home switches before the first slew. A
mount that has been power-cycled, hand-slewed, or left somewhere by another
program does not know where it is pointing, and every slew after that is wrong by
the same amount — which shows up as a plate solve that cannot find the field and
a night that never starts. A mount that cannot home is skipped, and one that
refuses is reported without ending the night. **Equipment → Sequencer**.

**Cooling does not hold the night up.** The cooler comes on the moment the
sequence starts, and then homing, slewing, centring and focusing all happen while
the sensor comes down. The wait for temperature is taken immediately before the
first light frame instead, by which time there is usually nothing left to wait
for. Turn on *wait for temperature before the first slew* to get the old
behaviour back.

**Ending**, in this order and for these reasons:

* **Guiding stops, always** — not only when the mount is being parked, and not
  only when PHD2 says it is guiding. PHD2 left looping, settling, calibrating or
  sitting in LostLock is still driving the guide camera and still pushing the
  mount, and it will happily do so until morning.
* **Then the mount**, while there is still a thread paying attention to it.
  Parking stays opt-in: a surprise slew in the dark is worse than a mount left
  tracking, unless there is a roof that has to close over it.
* **A copy of the log is written into the night's capture folder.** The real log
  lives under the data directory of whichever machine ran the night, which at a
  remote site is exactly where you cannot get at it. The capture folder is
  already synced — it is where the data goes — so the log arrives with the
  frames. **Equipment → Capture**.
* **Warming last, on a thread of its own.** Ramping a sensor from -10 C to
  ambient is about ten minutes of sleeping, and doing it on the sequencer thread
  made Stop appear to hang: the run was over, but everything gated on the
  sequencer stayed gated. It is safety work rather than sequence work, so it
  outlives the run and is reported separately. Starting a new sequence cuts it
  short, since that one is about to cool the sensor again anyway.

`python tools/check_night.py` covers all of it against stub devices.

### Run on loop

**Run on loop**, beside Run sequence, runs the plan tonight and every night
after with nobody in the room.  What it adds to a night is the two ends of it:

* **Opening**, this long before astronomical dusk (*Equipment → Sequencer →
  Run on loop*, twenty minutes by default): the coolers come on, the flat
  panel's light goes out and its cover opens, the mount is released from park
  and sent to its home switches, and then the plan is walked exactly as Run
  sequence walks it.  A target with a start time still waits for it.  Pressed
  in the afternoon, the loop waits; pressed in the dark, it starts at once.
  The safety monitor is asked before each night opens, and an unsafe sky is
  waited out.
* **Closing**, at dawn or when the plan runs out, whichever comes first and
  whatever *park at end* says: guiding stops, the cameras stop, the cover
  closes, the mount is sent **home and then parked**, the park is verified
  rather than assumed, the roof closes, a copy of the log goes with the data,
  and the cameras warm.  Every step is attempted whatever the one before it
  did.  A park that cannot be verified is sent as a **failure** notification,
  because an observatory left pointing at the sky until the next evening is
  the thing this exists to prevent.  Then it waits for the next dusk.

A plan that runs out at one in the morning is put to bed at one and not run
again until the following dusk.  The plan's slot times roll forward each night
on their own, and each night reads the plan afresh, so changes made during the
day are picked up.  **Stop** ends the loop as well as the night; so does
**Abort & park**.  A mount that cannot park is refused the loop rather than
left up.

Two things outside the program still have to be true for a week unattended: the
mount's own limits (a home position and a park position set in the driver, and
a meridian limit a few minutes past transit so a flip that fails cannot drive
the camera into the pier), and PHD2 set to handle the flip — with the mount
connected to PHD2 over ASCOM it knows which side it is on; on an ST-4 cable it
needs *reverse Dec output after meridian flip*.

### The meridian flip, checked

Before every frame the sequencer asks whether that frame would still be running
when the mount has to stop, *pause before meridian* minutes ahead of transit.
If so it waits until *flip minutes after meridian* past transit, stops guiding,
slews to the same coordinates — which is what makes a German equatorial change
sides — waits for the mount, checks the side of pier changed and says so if it
did not, puts the rotator back to the framing angle, re-centres by plate solve,
restarts guiding and refocuses if the triggers say so.  It no longer needs the
driver to report a side of pier at all: a mount that leaves that unknown still
runs into its limit, and the flip slew is harmless on a fork, which can turn
the setting off.  A panel slewed to *after* transit is already on the side the
driver chose for it and is not "flipped" again — that used to cost a second slew,
a re-centre and a warning on every panel of a mosaic shot late.

### The camera watching the telescope

A **pier cam** is a camera pointed *at* the rig rather than at the sky, and a
remote observatory without one is a rig you are flying blind. Is the scope where
the software thinks it is? Is the cover off? Are the cables about to wrap? Is
there frost on everything?

It connects on the Equipment rows like anything else — it is an ordinary ASCOM,
ZWO or Alpaca camera in a different job, so every backend already drives it —
and the live view is the **Pier cam** button in the top bar, beside the frames
rather than in a window of its own. "Is the scope where it should be" is a
question you ask while watching a sub come in, not one you go elsewhere for.

**Auto-exposure and auto-gain, on by default**, because this is not an
astronomical exposure. An imaging camera is told what to do; a pier cam has to
cope with afternoon sun, dusk, moonlight and a pitch-dark dome on the same
night, across something like fourteen stops, with nobody awake to adjust it. So
it meters each frame and corrects towards a target brightness:

* **Exposure moves first**, because it costs nothing in noise, and
  multiplicatively — the error is a ratio, not a difference.
* **Gain rises only when the exposure has run out of room**, and *falls again
  the moment the exposure can carry the load*. That second half is not
  decoration: without it a gain raised during one dark night is never lowered,
  because come dawn the loop drops the exposure, lands on target, finds itself
  inside the deadband and stops — leaving the feed noisy all day.
* **Both are damped and have a deadband**, so a cloud crossing the Moon does not
  set off an oscillation that takes ten minutes to settle.
* **Metering is a high percentile, not a mean.** The picture is mostly dark dome
  with a telescope in it, and a mean would expose for the wall and leave the
  telescope a white smear.

The frame is sent as a plain PNG that the page re-fetches when the server says
there is a new one — no stream, because at a frame a second a poll is simpler,
survives a dropped link with no reconnect logic, and costs nothing at all while
the panel is closed. Settings are under **Equipment → Pier cam**;
`python tools/check_piercam.py` drives the control loop across fourteen stops of
scene brightness and checks it converges, stays put, and gives the gain back.

### What the filters are called

The names are not cosmetic. Almost everything downstream matches on them rather
than on slot numbers: the sequencer looks up `Ha` to decide where to move the
wheel, the FITS header records the name, the planner allocates against it and
the calibration library files flats by it. A wheel whose driver calls its slots
`1`..`7` therefore does not merely look wrong — the plan asks for Ha, nothing
matches, and the night goes through whichever slot happened to be loaded.

ASCOM's `Names` is read-only, so a driver that is only counting cannot be told
any better. **The names typed into Equipment are laid over the wheel's slots**,
slot by slot, and that merged list is what every part of the program sees. Name
three of seven and the other four keep the driver's own names; leave an entry
blank and that slot does too. Clear them all and you get the driver's list back.

They are applied when the wheel connects and again the moment they are saved, so
the buttons, the sequencer's filter matching and the FITS headers all change
together without reconnecting anything.

**The order is the slot order.** There is one control: numbered boxes, one per
slot, and you type the name into the box. Slot 1 there has to be slot 1 in the
wheel. This matters more than it looks — LRGBSHO on a wheel loaded that way has
SII in slot 5, and a list that says Ha there instead will have the sequencer
asking for Ha and moving to the slot holding SII, all night, with every frame
labelled wrongly in its header.

`python tools/check_filters.py` covers the merge, the order and what the wheel
reports.

### Measuring the filter offsets

Filters are not parfocal, and the usual answer — focus on every filter change —
costs a sweep each time. **Equipment → Autofocus → Measure them** does it the
other way: focus each filter once, keep the differences, and then *move* by the
difference on every change. A filter change becomes a focuser move of known size
instead of five minutes of sweeping.

The arithmetic is the part worth explaining. Focus drifts with temperature and a
sweep takes minutes, so measuring L at 22:00 and Ha at 22:12 puts twelve minutes
of cooling into the difference between them. Two things are done about it: each
pass is reduced against **its own** reading of the reference filter, and every
pass after the first is swept in the **opposite direction** — so a filter
measured late in one pass is measured early in the next and the drift enters with
one sign and then the other. Sweeping every pass the same way would not work:
the error would be identical each time and averaging would preserve it exactly.

That cancellation needs two passes, which is why the default is two. The raw
per-pass numbers and their spread are shown as it goes, because two passes ninety
steps apart are two guesses and an average of them is not a measurement. A filter
that will not focus is skipped and keeps whatever was already known about it,
rather than silently becoming zero.

### Re-shooting part of a mosaic

A panel came out under cloud, or with a satellite through it, or simply wants
another hour. Open the target and **click the panels on the picture** — they are
already drawn there, numbered, over a survey image of that piece of sky. Picked
panels are filled red; the heading says which ones even while the box is shut.

Set the exposure and counts in the filter grid the way you would for any target,
and the run shoots only those panels. Nothing else about the target changes, so
nothing has to be put back afterwards except the selection, and **All panels**
does that. The order is still the one the sky wants — the selection filters the
tile order rather than replacing it.

### Every control, checked

`python tools/check_controls.py` cross-checks the interface against the code
behind it: every element the scripts dereference exists in the page, every
button and input is reached by something, no id is used twice, and every
endpoint the interface calls exists on the server.

The first of those is the one that matters. `$('btnFoo')` on an element that is
not there returns null, the next line throws inside a click handler nobody
catches, and the button silently does nothing — which is exactly how the
guider's Connect button came to do nothing at all.

### What a target does unless told otherwise

Every target **autofocuses when it starts** and **dithers every third frame**.

Focus on start, because a target begins with a long slew across the sky, usually
into different air and often onto the other side of the pier — and the first
frames of a target are the ones most likely to be thrown away for being soft.
Dither every third rather than every frame, because dithering after every sub
spends a settle on every frame of the night and a few subs at one position
still stack out walking noise perfectly well. Focus on start is per-target, in
the box that opens when you double-click one; the dither interval is the rig's,
under Equipment → Guiding, and the same for every target.

A target stores **only the options that differ from those defaults**. Storing the
whole set the first time any one of them was touched would quietly pin the rest,
so a later change to what the program does by default would reach the targets
nobody had opened and silently skip the ones they had.

The same problem exists for the settings file, and `MIGRATIONS` in `config.py`
is the answer: a stored value beats a default, which is right, but it means a
changed default reaches new installs only and rigs that have been running for
months go on doing the old thing for ever. Each migration moves a value **only
where it is still sitting on the old default**, so it can never overwrite a
choice somebody made — only one they never made — and records itself so a value
moved and then deliberately put back stays put.

### From one night to the next

Slot times are stored as absolute moments, so by the next evening last night's
are eighteen hours in the past and describe a window that closed before sunset.
They used to be deleted for that reason, which was the wrong conclusion from the
right observation: what you chose was never an instant, it was *a time of night*
— "start Cygnus at half nine, stop at one" — and that is as true tonight as it
was yesterday.

So they **move forward a whole number of days instead of being thrown away**.
Whole days because that is what keeps the clock time: 21:30 stays 21:30. A time
that no longer fits tonight's darkness — the nights draw in, and a plan made in
December does not fit June — is pulled to the nearest edge of the window rather
than discarded, because a target you wanted first is still the one you want
first. Pinned slots stay pinned. Auto-arrange is still what re-places a plan
against the sky when you want that.

The camera angle follows the same principle from the other direction: the
framing rectangle in the planner opens at the angle the camera is **actually**
at — measured by the last plate solve when there has been one, and the number
from Site & Optics otherwise — rather than at north-up, which it never is. It
keeps following as the night goes on, so solving and syncing the rig updates the
rectangle you are looking at. Turn the angle dial yourself and your choice holds
from then on; **Match camera** hands control back.

### The window

The menu bar carries the things that are not on screen all the time:

* **File** - open the same interface in a browser, quit.
* **Equipment** - open the connection dialog, disconnect everything.
* **View** - fit the image, 1:1, reload the interface.

`E` opens and closes the Equipment dialog from the keyboard.

---

## Connecting equipment

Everything connects from **Equipment...** in the menu (or the button next to the
title).  It is a dialog rather than a permanent panel: you connect the rig at the
start of the night, close it, and get the space back.  Each row picks its driver
and connects independently, so you can mix backends - an ASCOM camera with an
Alpaca focuser works fine.

**ASCOM** - the Windows ASCOM Platform.  Any installed driver shows up
automatically in the dropdown.  Requires the
[ASCOM Platform](https://ascom-standards.org/) plus `pywin32`.

Each telescope connects independently, and each gets its own COM apartment
thread, so a twenty-second download on one camera cannot stall the other one.

**Alpaca** - network devices speaking the ASCOM Alpaca REST protocol: ASCOM
Remote, native Alpaca cameras, INDIGO's Alpaca bridge.  Press **Scan Alpaca** to
broadcast for devices on the local network.  Nothing has to be installed and the
device can live on another machine.

**PHD2** - the guider row dials PHD2's event server rather than picking a driver,
so it takes an address (`127.0.0.1:4400` by default).

Connecting does the same four things NINA and SGP do, because "connected" to
PHD2 means more than an open socket:

1. **Starts PHD2** if nothing answers on the port.  Where it is installed comes
   from the Windows uninstall registry — the installer's folder is
   `PHDGuiding2`, not `PHD2`, so guessing the name is not good enough — falling
   back to the usual locations and then `PATH`.  Set the path by hand in
   Equipment if it lives somewhere unusual, or untick *Start PHD2* to go back to
   requiring it up front.  Only a PHD2 on this machine can be started this way.
2. **Loads the equipment profile**, if you named one.  PHD2 only allows this
   while its equipment is disconnected, so that is done first.
3. **Connects PHD2's own camera and mount.**  This is the step whose absence
   looks exactly like "guiding will not start": the socket is fine and
   `get_app_state` answers cheerfully, but `guide` fails because PHD2 has no
   camera.  It can take a couple of minutes with an ASCOM camera and a mount
   behind TheSky, during which PHD2's server answers nothing at all — so the
   connection is started and then *polled* for, rather than waiting on a reply
   that will not come.
4. **Finds a guide star** when guiding starts, by looping exposures and
   auto-selecting, rather than hoping `guide` manages it from a standing stop.

PHD2 must still have **Tools -> Enable Server** ticked; that is the one thing
that cannot be set from outside.  Starfront starts, stops and dithers the
guiding session and reads back how well it is going; PHD2 owns everything else.

Guide, stop, dither and pause all write to the session log, successes and
failures alike — a guider that will not start is the single thing most worth
having a reason for in the morning.

**A sequence starts guiding itself** once it has slewed, centred and focused on
a target, and waits for it to settle before the first frame. Nothing else does:
dithering between subs checks that guiding is running and quietly does nothing
when it is not, and the meridian flip only restarts guiding it stopped itself —
so without this a whole night could run unguided with every frame trailed and
nothing in the log to say why. A guider that will not start is logged loudly and
the night carries on unguided rather than stopping, because an unguided sub is
still a sub.

---

## More than one telescope

A rig is one optical train: a camera, and whatever filter wheel, focuser and
rotator sit in front of it.  **Add telescope** in the Equipment dialog gives you
another, up to four.

One telescope is the **master**.  It carries the mount and the guider, so it
decides where everything points and when everything dithers.  The others are
bolted to the same mount, cannot point themselves, and are driven in step:

* **Frames go together.**  The night runs in *slots*.  A slot starts an exposure
  on every telescope at once, waits for all of them, and only then dithers — so
  a slave is never mid-frame while the mount is being nudged.  Exposures start
  within a millisecond of each other.
* **Focus goes together.**  Each telescope has its own focuser and its own
  sweep, and when any of them is due a run they all sweep at the same time.  A
  telescope in the middle of a sweep is deliberately out of focus, so nothing
  else on the mount could be shooting anyway; running them in series would just
  double the time the mount spends not imaging.
* **Each telescope shoots its own thing.**  A plan entry carries a filter
  allocation per telescope, so the master can take five-minute luminance while
  the faster scope beside it takes ten-minute Ha of the same field.  They still
  start together and the slot waits for the slowest.  A telescope with no
  allocation of its own follows the master's, which is what a second scope
  carrying the same filters usually wants.
* **The night costs the slowest telescope**, not the sum of them.  Two scopes
  shooting an hour of the same target is an hour of the night, and the planner's
  budget says so.
* **Settings that belong to the optics are per telescope** — focal length,
  sensor, camera angle, gain, cooling setpoint, focus step size and the refocus
  triggers.  The site, the solver, the dithering and the root folder are shared,
  because they describe the observatory.  A telescope only stores what it
  actually differs on; everything else falls through to the shared value.
* **Frames land in their own folder** — `<root>/<target>/<night>/<telescope>/` —
  as soon as there is more than one, so two cameras cannot write over each
  other.  A single telescope keeps the original layout.
* **`TELESCOP`** in the FITS header names the telescope the frame came off.

The switcher next to **Equipment...** in the topbar says which telescope the
Camera, Filter, Focuser and Rotator panels are driving.  It only appears once
there are two.  The Mount and Guiding panels always show the master, because
there is only one of each.

### Equipment profiles

A profile is a named snapshot of the whole setup: every telescope, which driver
each of its slots connects to, and the settings around them.  Rigs come apart —
the refractor comes off for a season of narrowband on the RASA, and in March it
goes back on.  **Save as...** in the Equipment dialog remembers a setup;
**Load** puts it back, and **Connect this telescope** brings the whole thing up
from what it last connected to rather than eight dropdowns.

Profiles live in `~/Starfront/equipment.json`, beside the settings.

### Filter names: one letter each

Every filter is called by one letter — **L R G B H O S** — everywhere in the
program: the wheel, the plan, the FITS header, the calibration library, the
night log, and the collaboration server judging whether a rig carries what a
project wants.  Whatever is typed or read is folded to it at the door: `Ha`,
`H-alpha`, `ha 3nm` and `HAlpha` are all `H`; `OIII` and `O3` are `O`; `SII`
is `S`; `Lum`, `luminance`, `Clear` and `UV/IR` are `L`.  A name that is none
of the seven — a dual-band filter, say — is kept as typed.  Settings, plans and
targets from before the fold are folded when they are read, and a flat filed as
`Ha` still matches a frame shot through `H`.  Two spellings of one filter were
two filters to everything that matched on the name, which is how a plan asks
for a filter the wheel no longer has.

### Coming over from N.I.N.A.

**Import from N.I.N.A.…** in the Equipment dialog reads the profile N.I.N.A.
last used on this PC (`%LOCALAPPDATA%\NINA\Profiles`) and brings across
everything that has a home here: the site, focal length, pixel size and sensor,
gain, offset and setpoint, the filters in slot order with their focus offsets
and the autofocus filter, the focus sweep (exposure, step, points either side,
curve fit, backlash), the meridian flip and its timing, park and warm at the
end, PHD2's path and the dither and settle figures, ASTAP and the solve
settings, the image folder, the flat target — and which ASCOM driver is in
which slot, remembered the way choosing it in the dialog remembers it.

Everything is shown before it is written: each value beside what this telescope
has now, with the N.I.N.A. setting it came from, grouped so a group can be left
out; rows already matching are greyed.  What is deliberately *not* carried
across is listed with the reason — the framing assistant's rotation angle is
the angle of a framing, not of the camera; the file pattern, because frames
are filed as `<root>/<target>/<night>` here and the night log relies on it.
Drivers are remembered, not connected; **Connect this telescope** brings them
up when you are ready.  `python tools/check_nina.py` runs the mapping against a
profile in N.I.N.A.'s own format.

---

## The FITS header

Every frame carries enough in its header for a pipeline to sort, group, grade
and register a season of subs without asking anybody.  The cards follow the
names N.I.N.A., SGP and PixInsight already read, so WBPP groups by them as it
is; on top of those are the things only the program that planned the night
knows.  A card whose value is not known is left out rather than written as
`None`.  `python tools/check_header.py` takes a frame through the real capture
service and reads all of this back.

**Grouping** — `IMAGETYP`, `OBJECT` (the panel, for a mosaic: `Orion mosaic -
Panel 5`), `FILTER`, `EXPTIME`/`EXPOSURE`, `GAIN`, `OFFSET`, `XBINNING`/
`YBINNING`, `CCD-TEMP`, `SET-TEMP`, `INSTRUME`, `TELESCOP` (which scope on a
tandem rig), `BAYERPAT` on a colour camera.

**Time** — `DATE-OBS` is the shutter opening, in UTC to the millisecond (it
used to be stamped when the file was written, a minute late on a long sub);
`DATE-END`; `DATE-LOC` local; `JD` at mid-exposure and `MJD-OBS` at the start;
`NIGHT`, the evening the frame belongs to; `FRAMENO`, its number in the set.

**Pointing** — `OBJCTRA`/`OBJCTDEC` in sexagesimal, the *target's* coordinates
(the panel's, on a mosaic); `RA`/`DEC` in degrees, where the mount said it was;
`PIERSIDE`; `CENTALT`, `CENTAZ`, `AIRMASS`, `HA`; `SUNALT`, `MOONALT`,
`MOONILLU` (fraction lit) and `MOONSEP` (degrees from the frame) — the numbers
a grader sorts by before it has measured a star.

**Optics and site** — `FOCALLEN`, `PIXSCALE` (arcseconds per binned pixel),
`XPIXSZ`/`YPIXSZ`, `POSANGLE` (the camera's angle on the sky: the rotator's, or
the measured one), `ROTATANG` (rotator mechanical), `FOCUSPOS`, `FOCTEMP`,
`SITELAT`, `SITELONG`, `SITEELEV`, `OBSERVER` (your Discord name, once signed
in), `SWCREATE`, `SWVER`.

**The night as it happened** — `GUIDESTA` and `GUIDING` (did PHD2 have a lock
when the shutter opened), `GUIDERMS`, `GUIDRMSR`, `GUIDRMSD` in arcseconds,
`DITHERED` (the first frame after a dither, where the offsets are).

**What the planner knows** — `TARGET` (the whole mosaic's name), `TARGETID`
(stable across a rename), `MOSAIC`, `PANEL`, `NPANELS`, `PANELPA` (the panel's
framing angle), `ENTRYID`; and for a collaboration `PROJECT`, `PROJID` and
`COLTASK`, so frames from every rig on a project can be pooled by id.  Darks,
flats and bias frames carry none of these, and a frame taken by hand on the
Image tab forgets them.

**Calibrated copies** add `CALSTAT` (the steps applied, e.g. `DF`), `MDARK`,
`MFLAT`, `MBIAS` naming the masters used, and `CALSWARE`.

## What the UI does

The window a night is actually spent in front of is an image, so that is what it
shows. The left column holds one panel — the camera — carrying only what a frame
is taken with: exposure, type, **Capture**, **Loop**, **Abort**, **Autofocus**, a
progress bar, the filter buttons, and a three-value readout of sensor
temperature, focuser position and guide RMS so the panels those belong to can
stay shut. Binning, gain and offset, cooling and the saving paths are each one
disclosure below, closed until wanted.

The focuser, rotator, mount and guiding panels are not on screen at all until
they are asked for: four buttons above the camera panel bring each one up, and
the choice is remembered. A d-pad that is not being used is in the way.

Everything that is set once and then left — how to focus, how to dither, how to
flip, what to do when something goes wrong — lives in the two settings dialogs
rather than on the main screen. Both are paged, with a rail down the left saying
what is in them and a Save button that never scrolls away.

**Equipment** - Connection, Camera, Filters, Files, Autofocus, Mount, Guiding,
Recovery, Monitoring. **Site & Optics** - Site, Optics, Plate solver, Planning.

**Camera** - exposure, frame type (light/dark/bias/flat/dark-flat), binning,
gain, offset, frame count, and a Loop mode for framing and focusing.  The things
that are part of taking a frame live here rather than in panels of their own:

* *Filters* - one button per slot, showing the current and in-transit position.
* *Cooling* - cooler on/off with a setpoint, live sensor temperature and cooler
  power; the temperature turns amber when it has drifted more than a degree from
  the setpoint.
* *Saving* - whether frames are written at all, which folder they go to, and the
  target name.  Turn saving off while framing and focusing and the frames still
  appear in the viewer without filling the night's folder.  With a target name
  set, files are named `M31_L_120s_-10C_0001.fits` - target, filter, exposure,
  sensor temperature and a sequence number that continues where an earlier
  session stopped.  Without one they keep the older
  `LIGHT_L_120s_g120_bin1_<timestamp>.fits` form.  A mosaic panel adds its
  number: `M31_P3_L_120s_-10C_0001.fits`.

### The OBJECT header

`OBJECT` is what a stacker sorts by, so it names the thing the frame is actually
of.  For a mosaic that is the *panel*, not the mosaic — `M31 - Panel 3` — because
each panel is a different piece of sky and stacking them together would be
wrong.  Every telescope shooting that panel writes the same string, so frames off
two scopes sort into the same set of stacks whichever camera took them.  The
folder stays keyed on the target, so one mosaic remains one folder.

**Focuser** - absolute position, relative steps from +-10 to +-1000, halt, and
temperature.

### Filters are not parfocal

A 3 nm Ha and a luminance filter sit a few hundred steps apart on most optical
trains, and sweeping on every filter change costs minutes of every hour.
**Equipment → Filters** takes an offset per filter, stored *relative to each
other* rather than as absolute positions: leave whichever filter you focus on at
0 and say how far the focuser has to move for the rest. A filter change then
costs a second, and it does not queue a sweep, because focus has just been
corrected for it.

The same page names which filter autofocus sweeps *through*. Sweeping through
one filter every time keeps runs comparable, keeps the offsets meaningful, and
finds stars far faster than narrowband does; the imaging filter goes back as soon
as the sweep lands. The offset standing in the focuser is re-derived from every
successful focus run, so the two never drift apart.

**Mount** - RA/Dec in sexagesimal, altitude, azimuth and local sidereal time;
altitude turns amber below 20 degrees.  Slew and sync to coordinates, a direction
pad with selectable rate, tracking, park/unpark, and abort.

**Guiding** - PHD2's state, guide RMS in arcseconds (total, RA and Dec) and star
SNR, with start, stop and a manual dither.  The RMS window resets on a dither, so
what you read is the guiding since the last one rather than an average smeared
across it.  Settle parameters are shared by guiding and dithering.

**Viewer** - every frame is displayed with a screen transfer function.
Auto-stretch derives the black point and midtone from the frame's own noise
statistics (median and MAD), the way PixInsight's STF does, so a raw sub that
looks black linearly comes up correctly exposed.  Manual black, midtone, white
and invert are there when you want them.

Zoom and pan with the wheel and drag.  Below about 1:1 you are looking at a
downsampled preview; at 1:1 and above the client requests a full-resolution crop
of just the visible region, so focusing on a 60-megapixel sensor does not mean
shipping a 60-megapixel PNG.  Hovering reads the raw ADU under the cursor plus a
5x5 mean.

The histogram uses a fourth-root x-axis and log counts - a linear histogram of an
astronomical frame is a single spike at zero and tells you nothing.

Keyboard: `e` equipment, `f` fit, `1` for 1:1, space to capture.

---

## Solar system survey

> **Not in the first release.** The Survey and All-Sky tabs are finished and
> stay in the build, but they are hidden: a sweep of the twilight sky is a
> different program from an evening on one target, and a window offering both
> looks like it needs a manual. Everything below still works — set
> `astro.tabs.surveys` to `"on"` in the browser's local storage to bring the two
> tabs back.

A **Survey** tab, next to the Planner, for sweeping the twilight sky near the
Sun — where comets and near-Earth asteroids emerge from conjunction, and where
the big professional surveys largely do not look. It plans and generates
targets; detection is a separate job for Tycho Tracker downstream.

**The geometry is Sun-relative, and that is the whole design.** A survey region
is not a patch of RA and Dec; it is a shape in solar elongation and ecliptic
latitude, which is the same shape every night. What changes is where that shape
lands, because the Sun moves about a degree a day along the ecliptic. Store
panels as RA and Dec and the sweep is stale within the week — so the region is
stored in the Sun's frame and transformed afresh for whatever date it is planned
for. Saved sweeps keep their region definition, so re-planning one for another
night puts it where the Sun is *then*.

**There are two surveys, not one**, and they want opposite geometry at the same
clock time — so the mode belongs to a sweep rather than to the program:

* **Deep NEA survey** — magnitude ~20 in the dark hours, 30 s × 36 for synthetic
  tracking. No extinction margin at all at that depth, so the altitude floor is
  strict (15° hard, 20–30° working) and sky near the Sun is no use.
* **Twilight comet sweep** — magnitude 11–13, **20–45° from the Sun and as low
  as 5°**. Nine magnitudes of margin makes 2.5 magnitudes of extinction at 5°
  affordable, and that sliver is the sky the professional surveys cannot reach.
  Nishimura was found at 23° elongation, 5–8° up, through a 66 mm lens.

Whether the comet sweep is possible at all is a seasonal question, and the tab
answers it before you plan anything. The zone sits ~23° from the Sun *along the
ecliptic*; whether that becomes altitude depends on the angle the ecliptic makes
with the horizon, which swings enormously over the year and is inverted between
morning and evening. The **viability readout** gives the one number that
matters — the smallest elongation reachable above the floor with the Sun at
−12° — and the **year view** shows it for every date, so you can see which
twenty-odd mornings not to miss. Measured at Rockwood, the ecliptic stands 82°
from horizontal on an October morning and 34° on an April one, and the reachable
elongation goes from 17.5° to 30° with it. That is the same geometry that makes
Mercury a spring-evening and autumn-morning object.

Which side of the Sun matters, and they are not worth the same:

* **Morning** — west of the Sun, rising ahead of it: sky coming *out* of
  conjunction, hidden for months, so anything new is here first. Both
  2I/Borisov and Nishimura were morning-twilight discoveries, and a find here
  stays observable long enough to build the follow-up arc a confirmation needs.
  Scored higher for that reason.
* **Evening** — east of the Sun and heading *into* conjunction. Already picked
  over and anything found is about to be lost, but the rig is otherwise idle and
  it catches a different population.

What the planner works out, and what it will tell you plainly:

* **The twilight window**, from the Sun altitudes you choose (default −8° to
  −18°). It is about 45 minutes and it is the binding constraint on everything.
* **Which fields are observable** — altitude floor (20–30° is the working band,
  15° a hard floor), airmass, Moon avoidance, distance from the galactic plane,
  and whether a field was shot in the last few nights.
* **How many actually fit.** With 36 × 30 s per field a sweep is about 21
  minutes of shutter, so a 47-minute window holds **two fields**. The tab says
  so, and says how many were observable but did not fit — the arithmetic that
  matters is exposures against sky coverage, and it is better seen than
  discovered at dawn.

The Moon rule is worth a note. The spec's flat 40° kills the best mornings: a
*waning crescent* sits in the morning twilight zone on its way to conjunction,
so a 5%-lit sliver would block an entire sweep, while a full Moon — 180° from
the Sun and nowhere near a 30–60° elongation region — never interferes at all.
The radius therefore scales with the lit fraction by default, keeping about a
third of it at new Moon. Untick it for the flat rule.

**Best for tonight** settles the parameters from the geometry, and says why in
plain words. Most of the knobs are not really free on a given night: the season
decides whether the comet zone is reachable at all, which side of the Sun is
worth having, and how near the Sun the sweep can start. It picks the comet sweep
whenever the zone reaches inside about 35° — that window is twenty minutes,
seasonal, and unlike the deep survey it cannot be made up on another night —
otherwise it spends the time on deep panels. Morning is preferred, but as a
five-degree thumb on the scale rather than a rule: through February to May the
morning ecliptic lies flat and the evening stands up, and insisting on morning
then would send the sweep to the worse half of the sky. Across the year it
chooses morning from June to January and evening from February to May, which is
the Mercury pattern.

**Each panel carries its own camera angle.** The grid runs along ecliptic
longitude and latitude, not along the meridian, so a camera left at north-up
sits skewed across it — by about 24° in the twilight zone at this latitude —
and panels that were supposed to overlap by 10% no longer meet. Every panel is
therefore given the sky position angle that makes the grid abut, the same way
the mosaic planner corrects for convergence. Sampling the seams of a real
sweep: **0 gaps in 1600 with the rotation applied, 35 without it.** Where the
rig has no rotator the planner says how far the fixed camera is from what the
grid wants, how much of the overlap survives, and whether that leaves holes.

The chart is drawn in altitude and azimuth rather than as a star chart, because
everything the tab weighs up is horizon-relative. Wheel to zoom and drag to pan
— a 2.5° footprint on a 180° chart is a speck, and getting in close is the only
way to see whether the panels really overlap. The time scrubber re-projects
everything for the moment it points at, so you can watch the fields and the
ecliptic move against the horizon through the window. **The ecliptic's angle
differs enormously between dusk and dawn on the same night** — 37° and 82° on an
October evening and morning at Rockwood — and that is the seasonal geometry
rather than a fault, so the chart states it outright.

Both twilights are judged at the *middle* of their window. Judging at the open
is not the same instant for the two of them — an evening window opens with the
Sun at its shallowest and a morning window at its deepest — and since evening
fields are setting while morning fields are rising, that quietly graded the
evening at its best and the morning at its worst.

**Coverage history is kept in the Sun's frame too**, for the same reason: "have
I already shot this?" is a question about the Sun's neighbourhood, and that is a
different piece of celestial sphere every night. Record RA and Dec and every
night looks like fresh sky, so the sweep would re-cover the same relative region
for ever and never widen. Fields are ticked off as the sequence shoots them.

With several telescopes on the mount they all shoot each field together, so the
grid is laid out for the **smallest** of them — tile on the widest and the
narrow instruments miss the seams, which for a survey is a missed discovery
rather than a cosmetic gap. Set **usable field** in Site & Optics where the
optics correct less than the sensor covers: a RASA 8's 22 mm image circle does
not reach an APS-C chip's corners.

## Collaborating

Several telescopes, in different places, filling in the same patch of sky.  The
**Collab** tab is both halves of that: taking part in somebody's project, and
running one.

**The region is drawn, not typed.**  Find the object in the Planner, press
*Draw a collab region*, and drag a rectangle across the picture; it comes over
to the Collab tab with the shape already filled in.  Four numbers describing a
rectangle tell you nothing about whether the nebula is inside it, which is the
only question that matters when you are choosing one.

The box is **north-up**, because `collab.chunk` lays its tiles out along right
ascension and declination without turning the grid — a box drawn at an angle
would be filled in by a tiling that did not match it, and a rectangle that looks
right while being wrong is worse than a plainer one.  The framing angle travels
with the region as `rotation`, meaning the camera angle contributing rigs should
shoot at so their panels stack the same way up.  It is the angle of the frames,
not of the box.

A *project* is a rectangle of sky with rules attached — focal length, star size,
guiding, sub length, the Moon, which filters and how narrow, whether one-shot
colour cameras may take part.  Focal length rather than image scale, because it
is the number everybody knows about their own telescope; the scale limit is
still honoured on a project that has one, but is no longer offered.  A *task* is
a smaller rectangle inside it, handed to one telescope.

**Whoever starts a project sets its rules**, under *What you will accept*:
focal length, star size, guiding, sub length, the filters and their bandpasses,
and the conditions — the Moon at most so much lit, the Moon at least so far
away, the target at least so high.  Those last three are the project's, not
each participant's: they are written onto the target on every rig that joins
and re-applied on every sync, so the coordinator's decision is what the
telescopes do, and nobody has a private setting to get wrong.  A project has no
"stop at" figure either; reaching its goal is a reason to keep going or to
close it, and that is the coordinator's call — **Close** on the project's card,
which takes it off everybody's list and keeps what was collected.

A project is **one target** or **a mosaic**, and the difference is not
cosmetic.  A single-target collaboration is one object and everybody points at
it: a telescope with a narrower camera than the framer's is *not* sent to tile
the framer's field — the object is the point, at whatever field each rig has —
so it gets one frame with the object in the middle, and the depth map is built
from the frames that really come home.  Settings that only mean something with
more than one panel, such as the fewest frames per panel a night, are not
offered for one.  A mosaic is a region to be covered, and each rig tiles it
with its own camera.  Sizes everywhere are
**degrees of sky**, never degrees of the RA coordinate: at +60 those differ by a
factor of two, and a rectangle dragged onto a picture only ever gives you the
first.  **The unit of work is an
area of sky, not a panel**: panels are an artifact of one particular camera on
one particular telescope, and a project several rigs contribute to cannot be
defined in them.  Depth is integration time at a point on the sky, which is the
only definition that means the same thing to a 300 mm refractor and a 2000 mm
reflector.

Nothing starts on its own.  A task arrives **offered** and stays that way until
somebody accepts it, and the server can go down without taking a night with it:
it is a source of suggestions, never a dependency.

### Who is about

Every check-in tells the server where the telescope is pointing and what it
is shooting — the sky coordinates and the target's name, never where the
observatory is — and the server hands back the same for everybody else.  The
Collab tab's toolbar says how many telescopes are online (checked in within
the last twenty-five minutes) and how many people are behind them; each
collaboration's card says how many telescopes are on it and how many of those
are about right now, with the names on hover.  The Planetarium draws the other
telescopes as small ringed dots with the name and target beside them
(*Other telescopes* in the options row), dimmed once a check-in is old.
**Share my position** on the Collab tab turns your own off.

### The Collab tab does two things

**Start one**, and **take part in one**.  That is the whole tab.  Enrolling
telescopes by hand, dealing chunks out to them, a ledger with an override button
beside every night anybody had contributed — all of that was machinery showing
through the front of the program, and none of it had to be on screen: the server
hands the sky out by itself and the progress bars say how it is going.

**Starting one** is a name, a shape and what you will accept.  The shape is
either **one target** — anything in your target list, framed already, which is
the commonest case by far — or **a mosaic**, drawn in the Planner.  A
collaboration you start appears in the list below like anybody else's, so you
join your own the same way you join theirs.

**Changing one you started** is *Edit* on its row: the same form, filled in,
with the region kept as it is unless you pick a target or draw a new rectangle.
A coordinator learns as the season goes — the Ha limit was too strict, the goal
was reached, the notes were wrong — and starting a fresh collaboration for each
of those would lose everything collected.  Nights already accepted stay
accepted; every rig on it finds out on its next poll.  *Close* takes it off
everybody's list and keeps the ledger.  A collaboration is yours if you started
it, by Discord identity; the server's owner may change anything.  The buttons
only appear where they would work, and the server enforces it regardless.

**Taking part** is one button: *Take part tonight*.  Behind it the program joins,
turns the chunk it is given into a target, and puts that target in tonight's
plan.  Three steps only in the sense that the program does three things; to you
it is one decision.  A collaboration you cannot satisfy says *why* — "530 mm is
shorter than the 800 mm the project wants" is actionable, and a greyed-out
button is not.

With a rotator, the button has a choice beside it: **turn to N°**, the
project's angle, so a single target is framed the way it was framed and a
mosaic's panels lie along the project's grid — or untick it and the camera
stays where it is.  Without a rotator there is nothing to choose and nothing is
shown.

**One-shot colour cameras** are broadband whatever is in front of them: no
narrowband, and nothing usable on a bright night.  The camera says what it is —
learned from its Bayer matrix the first time it connects, or ticked under
Equipment → Filters before then — and a project says whether it will take one,
and under how much Moon.  A colour camera with no filters typed in shoots *RGB*;
a project that wants that data lists a filter named RGB, and one that asks only
for Ha and OIII refuses the camera, saying what it shoots.  A colour camera's
night above the project's Moon limit is refused when reported; a mono camera's
is judged by the ordinary rules.

Nobody has to deal you in.  You ask, and the server hands you **the whole
project's sky, tiled with your own camera, and tonight's panels** — the cells
of that tiling that are yours to shoot tonight, and how many frames to put on
each.  Cells from different cameras do not line up and have no need to: depth
is integration time at a point on the sky, not a tick against a grid cell.

### How a night is dealt

The list is made **per night, from what everybody has already collected**.
Every frame a rig takes is tallied against the panel it landed on and reported
to the server with that panel's own footprint, as it is shot; the server builds
a depth map from those footprints at the resolution of each rig's own cells,
and from it knows two things about every cell — how deep the collaboration is
there, in each filter, and how much *this* rig has put there.  Tonight's list
for a rig is chosen from that, in this order of pull:

- **Not where somebody else is tonight.**  A panel two rigs shoot the same
  night is a panel one of them wasted.
- **Where this rig has been least.**  Every rig should touch every part of the
  field over the season, so no patch of the mosaic is one camera's alone — one
  telescope's optics, one night's seeing, one set of gradients printed on a
  corner is exactly the artifact a collaboration exists to average out.
- **Where the field is thinnest.**  With everything else equal, the cell with
  the most still wanted, so the mosaic advances everywhere rather than being
  polished in one corner.

Panels already at the project's depth are not visited.  The night is cut into
as many visits as it holds — greedy on the whole field: six hours that could
give ten frames each to seven panels does that rather than seventy to one —
but never so many that a visit falls under the project's **fewest frames per
panel a night** in any filter.  A telescope stacks its own frames before
anything is blended, and three subs on a panel do not stack; ten is the
default.  Visits are split across the project's filters in proportion to how
deep it wants each.

The list **holds for the night**.  The program tells the server which night it
is in on every poll, and the list is remade the first time it asks in a new
one — so everything moves on by default each night, and nothing moves under a
rig at 2 a.m. because somebody else's frames arrived.  Somebody joining at nine
is dealt around the panels you have been shooting since eight.  A rig that
gives six hours a night is sent to twice the panels of one giving three.

`python tools/check_server.py` runs three telescopes across four nights of
this and checks that nobody is sent to the same sky the same night, that each
is sent across the field rather than back to one panel, that the second night
is the sky the first was not, and that a finished panel is handed to nobody.

**A rig with no rotator is laid out honestly.**  Its camera sits at whatever
angle it is in the focuser and every panel is shot there, so the mosaic is built
with the rotator *locked* — the frames are not corrected between panels, because
there is nothing to correct them with, and the target says so.  The grid is laid
at the camera's own angle rather than the project's, and sized to cover the
region as measured along the camera's axes: at 268° a 5.3° × 3.5° field spans
3.5° of RA and 5.3° of declination, so the rows and columns swap.  The server
tiles that rig's cells the same way, from the angle its profile reports.  A rig
*with* a rotator gets the project's angle and per-panel correction, as before.
There is no per-panel rotation to offer without a rotator; what there is, is the
truth about the overlap, drawn on the framing.

Your target on the plan is the **whole mosaic**, so the framing shows all of it,
with tonight's panels filled in green and named in the heading — *panels 3, 7,
12 only* — and the **Tonight** line says the same.  A panel is either reachable
tonight or it is not, and there is no third colour: the hours the server deals
for come off this plan — the target's window clipped by the pinned times and
dawn, the same figure the Tonight line is worked out from — so the list it
deals is the list the telescope can shoot.  When the window shrinks after a
deal, the next check-in reports the smaller figure and the server deals again,
handing the rest back so nobody waits on them.  The list moves as others join
and as frames come in; a rig finds out when it polls, and its plan follows.
That used to be got badly wrong: a task was a single camera-sized cell, so a rig
alone on a big mosaic owned exactly one panel and its target was a single frame
with no sign of the other thirty-nine.

**Joining is one button.**  *Join with Discord*: the browser opens, Discord
asks whether Starfront may know who you are, and when you are back the tab has
already noticed, signed you in *and enrolled this telescope* — nothing is typed,
nothing is pasted, and the program never sees your Discord password.  The
server's address is built in, so nobody is told a URL, and there are no
settings on the tab at all: the check-in is every ten minutes, and whoever
runs the server is an owner by their Discord id rather than by a token.
Anybody running a server of their own points `collab.serverUrl` at it in
`settings.json`.  Only members of the coordinator's Discord server can join; a
role can be required to *start* collaborations, never to join one.

Each collaboration in the list is shown **on the sky**: its rectangle over the
survey, so the Veil is the Veil before a word is read.

There is no scheduling on this tab.  How long a rig gives a collaboration is
decided on that collaboration's box on the Plan tab — *give it N hours
tonight* — and the server is told that figure from the plan, rather than from
a second box here that had to agree with it.

Signing in is done the way a television does it, because Starfront is a
desktop window on a port that changes every launch and a browser cannot be
sent back into it: the program asks the server for a short login code and
opens the browser on it; you sign in there; the server ties you to the code;
the program, polling with the code, is handed a user token that it keeps.  The
Discord client secret lives on the server and nowhere else.

The requirements are checked on the server against the profile your rig last
reported, as well as in your own program.  Yours so you are told before you waste
a night; the server's because a check that only runs on the client is not a
check.

### Joining makes a target; the Planner plans it

A chunk you join or accept becomes a **target**, marked `collab`, and nothing
more.  Drag it into **Tonight** and give it the start and end times you want.
Those are the plan's own pinned slot times, so auto-arrange works around them
rather than over them, they roll forward each night keeping the clock time, and
the sequencer already honours them.  A chunk carries its own filters and counts
in, so dragging it into the night does not mean typing its allocation in by hand
off another tab.

The Plan tab is the same plan seen from the other side, and its **Tonight** line
says which panels and how many frames actually fit.

On a collaboration's box, **Give it N hours tonight** is a *duration*: it pins
the entry's end that many hours after its turn can begin.  The frame table
shows **Tonight / panel** — the visit the server sized for each of tonight's
panels — beside the project's **full depth / panel**, and the run walks
tonight's panels in order, each at tonight's depth, and gets through them.
The first version divided the hours across the panels instead, which on a
forty-panel share left each panel a few minutes of frames, the project's depth
destroyed for nothing; the second put the full depth on every panel, so the
first panel ate the night and the rest were never started.

The framing on a collaboration's box draws two shapes: your panels, and the
**collaboration's own rectangle**, dashed, underneath them.  They differ, and on
a camera that cannot turn they differ a lot — the mosaic is laid along the
camera's axes and circumscribes the region.  *All the time it has* pins the
entry to the target's whole window, rise to set, so the box shows those two
times; the first version read the window already clipped by its own previous
pin, and every press crept the end away from dawn by a few minutes, and the
second cleared the end and left the box reading as if nothing were set.

A collaboration entry that has a task but no frames — which the first hours
control could leave behind — is put right when the plan is read: the project's
depth goes back on every panel, once, with a line in the log.

The chunk was sized to the *smallest* field in the collaboration, so on a wider
rig it is a single frame and on a narrower one a small mosaic.  Each panel gets
the hours the task asked for rather than a share of them, because depth is
integration time at a point on the sky.  Accepting twice adopts once.

### What is actually happening tonight

Every target box carries a **Tonight** line: the frames that will really be
taken, the time they take, and — for a mosaic — which panels get shot.

> **Tonight** 12× Ha, 10× R · 1h 59m · panel 1 is cut short at 22 frames

An allocation is what somebody *asked* for.  Forty panels of narrowband is
forty-five hours of work, and in a five-hour window thirty-six of them are never
started; a plan that reports only the ask is how you find that out in the
morning.  The forecast is modelled on what the run really does — panels walked
in the capture order the sky dictates, each taking its whole allocation, the
clock checked before every frame, so the last panel started is *cut short* rather
than skipped.  Whole panels are costed through `schedule.plan_seconds`, the same
function the budget and the filter ceilings use, so the forecast cannot drift
away from them.  It honours the filter order too: `grouped` and `rotate` bring
home different data from a session cut short, which is the whole reason `rotate`
exists.

`python tools/check_tonight.py` covers it.

## Flats: the panel and the exposure together

The cover is **shut** to shoot a flat.  On every common device the light *is*
the cover — an Alnitak Flip-Flat, a FlatMan on a flip mount, a Deep Sky Dad —
and the illuminated face only points down the tube when the lid is closed.  It
opens again for light frames, in `Capture.open_light_path`, which is where that
belongs.

**Both the brightness and the exposure are found by measuring.**  Searching the
exposure alone cannot work for luminance: L passes several times the light of
any narrowband filter, so a panel set for Ha saturates L before the shutter can
close, and the search walks to its floor with nothing to show for it.  Dimming
the panel is the move a person would make.

Both knobs are near enough linear in signal, so one frame gives the throughput
of the whole path — panel, filter, optics, sensor — and the pair that lands on
target follows from it.  The exposure is aimed at a comfortable few seconds and
the brightness set to suit, rather than the other way round: a flat of a few
milliseconds is at the mercy of panel flicker and shutter travel, and neither
averages out in a frame that short.  The panel is not driven below **never dim
below**, because most dim by pulse width and the pulses start to beat against
short exposures at the bottom of the range.

A set that names its own brightness keeps it, and **set the panel brightness
automatically** turns the whole thing off.

The panel is believed rather than the command: a driver that quietly declined
to light it is reported, because those frames still arrive and are simply dark,
and dark flats that call themselves flats poison a library for a season.

`python tools/check_flats.py` drives it against a panel that behaves like the
hardware — including one that refuses to light with the cover open.

## The calibration library

**One exposure table.**  The sub length each filter is shot at lives in one
place, *Equipment → Default exposure per filter*, and everything reads it:
Auto-arrange writes it onto the plan, the collaboration server is told it when
you join a project, and the library is judged against it.  That is what makes
the library checkable at all — the darks a night needs are the darks at those
exposures, and no others.

**Calibration masters**, at the top of the Calibrate tab's right column, is the
whole question in one heading: *Ready*, *Out of date* or *Incomplete*, with one
row per thing the night needs — a bias, a dark at each default exposure at the
camera's setpoint, a flat through each filter — and each row one of three words.
*ok* needs nothing.  *out of date* means the master is there but older than the
limit under *What counts as a match*; shoot it again.  *missing* means shoot it,
or bring one in.  Change a default exposure and its dark is missing the moment
you look.

**Suggest** builds exactly that set: bias frames, one set of darks per distinct
default exposure, one line of flats for every filter.  No dark flats: a bias is
what a flat of a few seconds is corrected by here, and a set the library does
not need is a set nobody wants to sit through.  Flats always measure their own
exposure — there is no box for it, on the panel or the sky — because what
reaches the sensor depends on the panel, the filter and the optics, and a sky
flat re-measures every frame anyway.

**Add a master file…** brings in a master built somewhere else, as FITS or
XISF (PixInsight's own format, including its zlib-compressed, byte-shuffled
default; LZ4 is refused with a message).  The file's header fills the form in
and you correct what it got wrong — other programs' headers are often missing
the one card that matters, usually the filter or the temperature.  A float
master normalised to 0–1 is scaled back to 16-bit ADU on the way in so a dark
subtracts in the same units as the lights.  The original is not touched; the
copy in the library says where it came from.  `python tools/check_masters.py`
covers the suggestion, the three-word coverage and both formats.

**Collaboration shares are dealt at your exposures.**  Joining a project sends
your default exposure per filter, and the server writes the share at those
lengths rather than at some middle of the project's range, so every light you
shoot for it has a dark of its own length.  A default outside what the project
takes is refused at the door, naming the filter and the fix, and shows on the
project's card before you press anything.  The program assumes your defaults
are good ones; that is the deal.

### Keeping up with the coordinator

The server deals each night's panels afresh, and a coordinator can change a
project after it started — more hours, a different filter.  Every poll brings
each collaboration target you have joined into step with what the server now
says, and so does reading the plan; there is no switch for it, because a plan
one night behind the deal was the failure this replaces.  Joining is still a
decision you make; what happens to a target once joined is the server's.

A sync re-applies the allocation, the region, the window and the project's rules
(its altitude floor and Moon distance), and touches nothing the *operator*
chose: where the target sits in the plan, whether it is skipped, which panels it
is limited to.  A sync that reset somebody's own settings would be a worse
problem than the one it solves.

### What you can give it

How many hours of a night you will give a collaboration is read off the plan:
the span pinned on the collaboration's entry (*give it N hours tonight* on its
box).  A clock window can still be set in the settings file for anybody who
had one.  Both are honoured twice over.

*When work is delegated* — the coordinator's deal is weighted by what each rig
said it can give, so a rig offering six hours a night is dealt twice the chunks
of one offering three.  An equal share regardless hands the same work to
somebody who cannot start it until Friday, and the project then waits on them.
The proposal shows the consequence per telescope — *8 chunks, 8.0h asked for,
6h a night → 2 nights* — and **Size it to one night** scales the hours so the
most-loaded rig finishes in one.  A rig that has not said is dealt the average
of those who have, never nothing: zero here means "as much of the night as the
target is up for", and a coordinator who picked a telescope and saw it handed no
work would reasonably think the program was broken.

*When work is accepted* — the allocation is trimmed to the hours you said you
would give, and the clock times become the target's **pinned start and end** in
the plan.  That is the same mechanism as "start Cygnus at half nine, stop at
one": the run already honours it, auto-arrange already works around it rather
than over it, and it already rolls forward each night keeping the clock time.
There was no second window to build.

The clock times are local to your observatory and deliberately never converted.
The server cannot know your timezone, your horizon or your trees; the only
machine that can turn "after nine" into a moment is the one standing under that
sky.  They travel only so a coordinator can read them.

**Star size is in arcseconds, never pixels.**  2.5 px is superb at 0.5"/px and
unusable at 3"/px, and a ledger that compared them directly would be worse than
no ledger.  Before a night is spent, the tab says whether this rig can satisfy
the project at all — "your scale is fine, your Ha is too wide" is actionable and
a rejection the following morning is not.

Running the server is `Start the collab server.cmd`, or:

```bash
python server/run.py
```

It makes up an owner token on the first run, keeps it beside its database and
prints it; paste that into **Collab → Server and sign-in → Tokens**.  It listens
on this machine only unless given `--host 0.0.0.0`.  For a server other people
use, `server/deploy/install.sh` puts it on a Linux VPS behind Caddy with HTTPS
in one command — see `server/README.md`.

Three credentials, deliberately not interchangeable.  An **agent** token belongs
to a machine, lives in that observatory's settings file and can do exactly two
things: fetch work and report frames.  A **user** token comes from signing in
with Discord, belongs to a person, and lets them enrol their own telescopes,
start collaborations (if the server requires a role, only with it) and change
the ones they started.  The **owner** token belongs to whoever runs the server
and can do anything.  A token sitting in a settings file next to a telescope
must not be able to administer anything, and the day one leaks is the day that
distinction is the only thing that matters.  None of them ever reaches the
browser: the page calls this program, and this program calls the server.

One person with three telescopes on three PCs signs in on each and enrols three
agents, each with its own token.  Tasks are routed per telescope rather than
per person, because whether a chunk of sky is achievable depends on plate scale
and field — which belong to a rig, not to you.

`python tools/check_server.py` exercises the protocol over real HTTP against a
real server on a real port, with a fake Discord standing in for the real one so
the whole sign-in — start page, redirect, callback, poll — is driven end to end,
and then tries a member, a member without the role, a stranger, a telescope and
the owner against each other's things.  `python tools/check_coordinator.py`
covers the tiling arithmetic and the credential boundary in both directions.

Still to come, and written down rather than implied: the depth map rasterised
from the solved footprints already being stored.

## Layout

```
run.py                     entry point (window, browser or server)
Starfront.pyw           double-clickable launcher, no console
server/                    the collaboration server (its own README)
astrocontrol/
  collab.py                what a project, task and contribution are - imported
                           by both sides, so the rules cannot drift apart
  collabclient.py          this telescope's half: ask, hold, report
  collabadmin.py           the coordinator's half: enrol, delegate, overrule
  desktop.py               the application window, menu bar and native dialogs
  main.py                  HTTP + WebSocket API, serves the UI
  equipment.py             telescopes and saved equipment profiles, on disk
  rigs.py                  the live telescopes: devices, capture and focus each
  capture.py               exposure loop, FITS writing, session image store
  devices/
    base.py                abstract Camera/Mount/FilterWheel/Focuser/FlatPanel/Guider
    ascom.py               ASCOM Platform (COM) backend
    alpaca.py              ASCOM Alpaca (REST) backend
    phd2.py                PHD2 autoguider over its JSON event socket
    manager.py             driver discovery, connection state, event log
  focusing.py              the autofocus routine
  focusfit.py              trend line, parabolic and hyperbolic curve fits
  solarsystem.py           twilight sweep planning in the Sun's frame
  coverage.py              which survey fields have been shot, and when
  schedule.py              the night, the windows, the running order, the Moon
  autoplan.py              what to shoot in a slot: filters, subs and counts
  overheads.py             what the rig costs between exposures, measured
  astrometry.py            plate solving at astrometry.net, when ASTAP cannot
  sequencer.py             the plan being run, and put right when it goes wrong
  imaging/
    fits.py                minimal 16-bit FITS reader/writer
    png.py                 minimal PNG encoder
    render.py              statistics, auto-stretch, preview rendering
    stars.py               star detection and the half-flux diameter
  web/                     the UI (no build step, no framework)
```

Three design decisions worth knowing about:

**Stars are found as blobs, not as peaks** (`imaging/stars.py`). This is the
whole game for a focus metric, and getting it wrong is what makes an autofocus
run wander. A defocused star is a disc twenty or thirty pixels across, so its
light is spread over hundreds of pixels and its *peak* value is low — lower than
a single hot pixel or a cosmic ray. Anything that ranks candidates by peak
brightness therefore finds the sensor's defects first and the real stars last,
and measures a radius of nearly zero for them. On a real out-of-focus sub the
brightest "star" found that way was a **single hot pixel**.

So the frame is thresholded against a *local* background — a global one is
defeated by light pollution and vignetting, which lit up 0.9% of that same sub —
and the connected regions above it are measured, discarding blobs that are too
small, too big, too elongated or too hollow. Each star's background comes from a
ring around it, and HFR is `sum(value x distance from centroid) / sum(value)`.
That is the pipeline NINA uses, and it is worth following because it is known to
work on real frames.

Checked against stars whose true HFR is known analytically — Gaussians, filled
discs and annuli, from tight focus to badly defocused — the answer is within
**2%** and rises monotonically with defocus. The previous peak-based version was
66% low at large defocus and saturated, which flattens exactly the part of the
V-curve that tells autofocus which way to move.

**The sweep extends itself until the minimum is bracketed** (`focusing.py`),
following NINA. A sweep centred on the current position only finds focus if
focus happened to be near the middle of it. When it is not — and after a filter
change or a big temperature swing it often is not — every point lands on one
rising arm of the V and there is no minimum to fit. Rather than give up,
whichever side of the lowest point has too few measurements gets another one,
one step at a time, until both arms can carry a line. A run that starts 700
steps the wrong side of focus simply walks until it finds it. The answer is then
*checked*: move there, measure again, and only accept it if the stars really are
no worse than they were at the start, otherwise put the focuser back and retry.

**Every position is arrived at travelling the same way.** A focuser has
backlash: the gears take up slack when the direction reverses, so the same
commanded position reached from above and from below is not the same place
optically. A sweep walks steadily in one direction and is consistent with
itself, but the move onto the fitted position at the end reverses — and the
confirming frame is then taken somewhere the sweep never measured. On a real run
this showed up as the sweep reading HFD 3.86 at 32875 and the confirmation
reading 7.34 at 32876: the value the sweep saw fifty steps lower. So every
arrival is made from above, stepping past the target and coming back onto it.
The overshoot has to be *larger* than the real backlash — if it is not, the
return move is entirely swallowed taking up slack — so it defaults to five sweep
steps, a run that detects the shortfall triples it and tries again, and the
Backlash setting is there for a focuser stiffer than that.

**COM calls run on a dedicated apartment thread, one per telescope**
(`devices/ascom.py`). ASCOM drivers are overwhelmingly single-threaded apartment
objects; calling them from a web server's thread pool is the classic cause of
intermittent `RPC_E_*` failures. The executor is what makes the ASCOM backend
reliable rather than mostly-working. One per telescope rather than one overall,
because otherwise a long `ImageArray` download on one camera sits in front of the
other camera's `ImageReady` poll — and two scopes exposing together is precisely
what must not be serialised.

**The master telescope is looked up on every use, not bound at startup**
(`main.py`). Which telescope carries the mount can change while the program is
running, and a stale reference to the old one would quietly point the wrong
mount.

**Captures run on their own thread.** HTTP handlers queue work and read state,
so a camera taking 20 seconds to download cannot stall the interface.

**PHD2 gets a reader thread of its own** (`devices/phd2.py`).  Its socket carries
unsolicited events and our call responses on the same stream; one thread
demultiplexes them, so a silent PHD2 can never wedge an HTTP handler.

There are no image-library or FITS-library dependencies — the PNG encoder and
FITS reader/writer are about 100 lines each. Files written here open in
PixInsight, Siril, ASTAP, DeepSkyStacker and astropy.

---

## API

The UI is a client of a plain HTTP API; anything below can be scripted.

Anything that is about one telescope takes an optional `?rig=<id>`.  Leave it off
and you get the master, which is why a single-telescope rig can ignore all of
this.  The mount and guider routes have no `rig` — there is only one of each.

```
GET  /api/backends                     available backends
GET  /api/drivers/{kind}               camera|mount|filterwheel|focuser|flatpanel|guider
POST /api/alpaca/scan                  {host?, port?} - broadcast or direct
POST /api/devices/{kind}/connect       {backend, driverId, name?}          ?rig
POST /api/devices/{kind}/disconnect                                        ?rig
GET  /api/status                       everything: every telescope, capture state, log
WS   /ws                               the same, pushed ~2.5x/second

GET  /api/equipment                    telescopes, their remembered drivers, profiles
POST /api/equipment/telescopes         {name?} - add one
POST /api/equipment/telescopes/{id}    {name?, master?} - rename, or hand it the mount
DELETE /api/equipment/telescopes/{id}
POST /api/equipment/telescopes/{id}/connect    connect everything it remembers
POST /api/equipment/connect  |  /disconnect    all of them
POST /api/equipment/profiles           {name, id?} - snapshot the whole setup
POST /api/equipment/profiles/{id}/load ?connect
DELETE /api/equipment/profiles/{id}

POST /api/camera/expose                {exposure, frameType, binning?, gain?, offset?, loop?, count?}
POST /api/camera/abort
POST /api/camera/cooler                {on?, setpoint?}
POST /api/camera/settings              {binning?, gain?, offset?}

POST /api/filterwheel/position         {position}
POST /api/focuser/move                 {position} or {delta}
POST /api/focuser/halt

POST /api/mount/slew                   {ra, dec}      RA in hours, Dec in degrees
POST /api/mount/sync                   {ra, dec}
POST /api/mount/tracking               {on}
POST /api/mount/jog                    {direction, rate}
POST /api/mount/jog/stop
POST /api/mount/park  |  /unpark  |  /abort

POST /api/flatpanel/light              {on, brightness?}
POST /api/flatpanel/cover              {on}

POST /api/capture/output               {save?, directory?, target?}         ?rig

GET  /api/settings                     as one telescope sees them             ?rig
POST /api/settings/optics              per telescope                          ?rig
POST /api/settings/camera              per telescope                          ?rig
POST /api/settings/sequencer           per telescope                          ?rig
POST /api/settings/site | /solver | /guiding | /capture      shared
POST /api/settings/recovery            what a run does when something goes wrong
POST /api/framing/reference            {path} - open a FITS to frame against
GET  /api/framing/reference/image.png  ?token - the rendered frame
GET  /api/overheads                    what the rig has timed itself at
POST /api/overheads/calibrate          {frames?, focus?} - measure it on purpose
DELETE /api/overheads                  forget it and use the typed figures
POST /api/settings/autoplan            what Auto-arrange puts in the slots
GET  /api/sequences                    saved plans, newest first
POST /api/sequences                    {name, id?} - keep this plan under a name
POST /api/sequences/{id}/load          replace the plan with a saved one
DELETE /api/sequences/{id}
POST /api/plan/arrange                 {chooseExposures?} - order, times, filters
POST /api/plan/entries/{id}/options     one target's own sequence settings

GET  /api/survey                       settings, twilight windows, tiling field
POST /api/survey/optimise              settle tonight's parameters from the geometry
GET  /api/survey/modes                 the deep and comet parameter sets
GET  /api/survey/viability             ?date&floor - how near the Sun tonight reaches
GET  /api/survey/season                ?year&floor&step - that, for a whole year
POST /api/survey/settings              the region and how to acquire it
POST /api/survey/plan                  work out a sweep without saving it
POST /api/survey/save                  {name, date?, position} -> target + plan entry
GET  /api/survey/coverage  |  POST  |  DELETE

GET  /api/guider/phd2                  where PHD2 is, whether it is running
POST /api/focus/run                    ?rig, or ?all=true for every telescope
POST /api/focus/abort                  stop the sweep; the focuser goes back
POST /api/plan/entries/{id}/filters    one telescope's allocation             ?rig
DELETE /api/plan/entries/{id}/filters  back to following the master           ?rig

POST /api/guider/guide                 {settlePixels?, settleTime?, settleTimeout?, recalibrate?}
POST /api/guider/stop
POST /api/guider/dither                {pixels?, raOnly?, settlePixels?, settleTime?, ...}
POST /api/guider/pause                 {on}

GET  /api/images
GET  /api/images/{id}/stats
GET  /api/images/{id}/render.png       ?auto|black|white|midtone|invert|maxDim|region
GET  /api/images/{id}/pixel            ?x=&y=
GET  /api/images/{id}/download
```

Driver failures come back as HTTP 400 with the driver's own message, which the UI
shows as a toast.

---

## Next steps

The pieces this was shaped to support:

- **Flat panel** — the backend, API and connection row are still there; it has no
  panel in the main window yet.
- **Session persistence** — the image list is currently in memory and resets when
  the server restarts, though the FITS files themselves are safe on disk.
- **Calibration frames** — the frames autofocus and plate-solve centring take are
  written to the target's folder alongside the real subs. They are honest frames
  of that panel and carry the right `OBJECT`, but they are not part of the
  allocation and a stacker has to be told to ignore them by exposure length.
