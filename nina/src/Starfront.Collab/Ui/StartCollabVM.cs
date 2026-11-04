using System.Collections.ObjectModel;
using System.Globalization;
using System.Text.RegularExpressions;
using CommunityToolkit.Mvvm.Input;
using RelayCommand = CommunityToolkit.Mvvm.Input.RelayCommand;
using Newtonsoft.Json.Linq;
using NINA.Astrometry;
using NINA.Core.Utility;
using NINA.Core.Utility.Notification;
using NINA.WPF.Base.Interfaces.ViewModel;
using Starfront.Collab.Server;
using Starfront.Collab.Services;

using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Ui {

    /// <summary>One filter a collaboration wants: how deep, and how narrow the filter may be.</summary>
    public sealed class FilterGoalRow : BaseINPC {
        private string name;
        private string hours;
        private string bandpass;

        public FilterGoalRow(string name, string hours = "", string bandpass = "") {
            this.name = name ?? "";
            this.hours = hours ?? "";
            this.bandpass = bandpass ?? "";
        }

        public string Name { get => name; set { name = (value ?? "").Trim(); RaisePropertyChanged(); } }
        /// <summary>Hours of depth wanted at every point, as text so a half-typed number is left alone; blank is no goal.</summary>
        public string Hours { get => hours; set { hours = value ?? ""; RaisePropertyChanged(); } }
        /// <summary>The widest bandpass accepted, in nm; blank takes any filter of that name.</summary>
        public string Bandpass { get => bandpass; set { bandpass = value ?? ""; RaisePropertyChanged(); } }
    }

    /// <summary>
    /// The form that starts a collaboration from N.I.N.A., and edits one you
    /// started. It fills the same request the Starfront program sends, so a
    /// project looks the same to the server whichever program started it.
    /// The target comes from a name the server looks up, from the Framing
    /// Assistant, or typed as coordinates; a single field is shot at this
    /// camera's size and a mosaic at the size you give.
    /// </summary>
    public sealed class StartCollabVM : BaseINPC {
        private readonly IFramingAssistantVM framing;
        private readonly Func<Task> refresh;

        private bool open;
        private bool busy;
        private string message = "";
        private string editingId = "";
        private string name = "";
        private string target = "";
        private string raText = "";
        private string decText = "";
        private double rotation;
        private bool single = true;
        private string widthText = "";
        private string overlapText = "10";
        private string heightText = "";
        private string notes = "";
        private string minFocal = "", maxFocal = "", maxHfr = "", maxGuideRms = "", minExposure = "", maxExposure = "";
        private string maxMoon = "", minMoonSeparation = "", minAltitude = "", minFrames = "10", maxBortle = "";
        private bool requireCalibrated;
        private bool acceptOscRgb = true;
        private bool acceptOscNarrowband = true;
        private string foundText = "";

        public StartCollabVM(IFramingAssistantVM framing, Func<Task> refresh) {
            this.framing = framing;
            this.refresh = refresh;
            Filters = new ObservableCollection<FilterGoalRow>();
            LookupCommand = new AsyncRelayCommand(LookupAsync, () => !Busy);
            FromFramingCommand = new RelayCommand(FromFraming, () => !Busy);
            AddFilterCommand = new RelayCommand(() => Filters.Add(new FilterGoalRow("")));
            RemoveFilterCommand = new RelayCommand<FilterGoalRow>(row => { if (row != null) Filters.Remove(row); });
            StartCommand = new AsyncRelayCommand(SubmitAsync, () => !Busy);
            CancelCommand = new RelayCommand(Reset);
            Reset();
        }

        public ObservableCollection<FilterGoalRow> Filters { get; }
        public AsyncRelayCommand LookupCommand { get; }
        public RelayCommand FromFramingCommand { get; }
        public RelayCommand AddFilterCommand { get; }
        public RelayCommand<FilterGoalRow> RemoveFilterCommand { get; }
        public AsyncRelayCommand StartCommand { get; }
        public RelayCommand CancelCommand { get; }

        public bool Open { get => open; set { open = value; RaisePropertyChanged(); } }
        public bool Busy { get => busy; private set { busy = value; RaisePropertyChanged(); LookupCommand.NotifyCanExecuteChanged(); StartCommand.NotifyCanExecuteChanged(); FromFramingCommand.NotifyCanExecuteChanged(); } }
        public string Message { get => message; private set { message = value ?? ""; RaisePropertyChanged(); } }
        public bool Editing => editingId.Length > 0;
        public string Title => Editing ? $"Editing \"{name}\"" : "Start a collaboration";
        public string SubmitLabel => Editing ? "Save changes" : "Start it";

        public string Name { get => name; set { name = value ?? ""; RaisePropertyChanged(); RaisePropertyChanged(nameof(Title)); } }
        /// <summary>A name to look up: M31, NGC 7000, Sh2-129, or anything Sesame knows.</summary>
        public string Target { get => target; set { target = value ?? ""; RaisePropertyChanged(); } }
        public string RaText { get => raText; set { raText = value ?? ""; RaisePropertyChanged(); } }
        public string DecText { get => decText; set { decText = value ?? ""; RaisePropertyChanged(); } }
        public string FoundText { get => foundText; private set { foundText = value ?? ""; RaisePropertyChanged(); } }
        public bool Single { get => single; set { single = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(Mosaic)); RaisePropertyChanged(nameof(Shape)); RaisePropertyChanged(nameof(SizeHint)); } }
        public bool Mosaic { get => !single; set { Single = !value; } }

        /// <summary>The shape as a drop-down code, "single" or "mosaic": N.I.N.A.'s theme shows no text on a radio button.</summary>
        public string Shape { get => single ? "single" : "mosaic"; set { Single = value != "mosaic"; } }
        public IReadOnlyList<DeliveryChoice> ShapeChoices { get; } = new List<DeliveryChoice> {
            new("single", "One field: everybody points at the same spot"),
            new("mosaic", "A region to cover: each telescope tiles it"),
        };
        public string WidthText { get => widthText; set { widthText = value ?? ""; RaisePropertyChanged(); } }
        public string HeightText { get => heightText; set { heightText = value ?? ""; RaisePropertyChanged(); } }
        /// <summary>
        /// How much neighbouring panels share, in percent: the server cuts every
        /// telescope's panels with it. Taken from the Framing Assistant with the
        /// rest of the framing (N.I.N.A. usually asks for 20); more overlap costs
        /// panels but gives the stack more shared sky to even the panels out by.
        /// </summary>
        public string OverlapText { get => overlapText; set { overlapText = value ?? ""; RaisePropertyChanged(); } }
        public string Notes { get => notes; set { notes = value ?? ""; RaisePropertyChanged(); } }
        public string MinFocal { get => minFocal; set { minFocal = value ?? ""; RaisePropertyChanged(); } }
        public string MaxFocal { get => maxFocal; set { maxFocal = value ?? ""; RaisePropertyChanged(); } }
        public string MaxHfr { get => maxHfr; set { maxHfr = value ?? ""; RaisePropertyChanged(); } }
        public string MaxGuideRms { get => maxGuideRms; set { maxGuideRms = value ?? ""; RaisePropertyChanged(); } }
        public string MinExposure { get => minExposure; set { minExposure = value ?? ""; RaisePropertyChanged(); } }
        public string MaxExposure { get => maxExposure; set { maxExposure = value ?? ""; RaisePropertyChanged(); } }
        public string MaxMoon { get => maxMoon; set { maxMoon = value ?? ""; RaisePropertyChanged(); } }
        public string MinMoonSeparation { get => minMoonSeparation; set { minMoonSeparation = value ?? ""; RaisePropertyChanged(); } }
        public string MinAltitude { get => minAltitude; set { minAltitude = value ?? ""; RaisePropertyChanged(); } }
        public string MinFrames { get => minFrames; set { minFrames = value ?? ""; RaisePropertyChanged(); } }
        public string MaxBortle { get => maxBortle; set { maxBortle = value ?? ""; RaisePropertyChanged(); } }
        public bool RequireCalibrated { get => requireCalibrated; set { requireCalibrated = value; RaisePropertyChanged(); } }
        /// <summary>Collect everyone's lights (and calibration frames) on the server, for the coordinator to stack.</summary>
        public bool CollectFiles { get => collectFiles; set { collectFiles = value; RaisePropertyChanged(); } }
        private bool collectFiles;
        /// <summary>
        /// An old collaboration's pasted Rapid Stacker Collab join code, kept as it is. Since T706
        /// stacking follows the collaboration: the server binds a new one to its owner's hub, or makes
        /// and keeps the Rapid Stacker Collab project itself, and refuses a newly pasted code. The box
        /// shows only when the collaboration being edited has a pasted code (it may be cleared).
        /// </summary>
        public string StackingJoinCode { get => stackingJoinCode; set { stackingJoinCode = value ?? ""; RaisePropertyChanged(); } }
        private string stackingJoinCode = "";
        /// <summary>The code as the form was loaded with it, so an edit sends it only when it was changed.</summary>
        private string loadedStackingJoinCode = "";
        /// <summary>Whether the form shows the join code box: only for an old collaboration's pasted code.</summary>
        public bool ShowJoinCode => loadedStackingJoinCode.Length > 0;

        /// <summary>
        /// A colour camera's data is its own band, kept apart from the mono
        /// channels: OSC-RGB behind a clear filter, OSC-Ha+OIII behind a
        /// dual-band. Taking one puts the band among the filters wanted, with
        /// its own hours; not taking it takes the row away.
        /// </summary>
        public bool AcceptOscRgb {
            get => acceptOscRgb;
            set { acceptOscRgb = value; RaisePropertyChanged(); OfferBand("OSC-RGB", value); }
        }
        public bool AcceptOscNarrowband {
            get => acceptOscNarrowband;
            set { acceptOscNarrowband = value; RaisePropertyChanged(); OfferBand("OSC-Ha+OIII", value); }
        }

        private void OfferBand(string band, bool wanted) {
            var row = Filters.FirstOrDefault(f => string.Equals(f.Name, band, StringComparison.OrdinalIgnoreCase));
            if (wanted && row == null) Filters.Add(new FilterGoalRow(band));
            else if (!wanted && row != null) Filters.Remove(row);
        }

        public string SizeHint {
            get {
                var field = Runtime.Ready ? RigProfile.Field(Runtime.Host, Runtime.Settings) : null;
                var mine = field is (double w, double h) ? $"{w:0.00}° × {h:0.00}°" : "not known until the camera has connected";
                return Single
                    ? $"One field: everybody points at the same spot. Your camera's field is {mine}."
                    : $"A region to cover: each telescope tiles it with its own camera ({mine}).";
            }
        }

        /// <summary>Back to a blank form for a new collaboration, with this rig's filters offered.</summary>
        public void Reset() {
            editingId = "";
            Name = ""; Target = ""; RaText = ""; DecText = ""; rotation = 0; Single = true;
            WidthText = ""; HeightText = ""; OverlapText = "10"; Notes = ""; FoundText = "";
            MinFocal = ""; MaxFocal = ""; MaxHfr = ""; MaxGuideRms = ""; MinExposure = ""; MaxExposure = "";
            MaxMoon = ""; MinMoonSeparation = ""; MinAltitude = ""; MinFrames = "10"; MaxBortle = "";
            RequireCalibrated = false;
            CollectFiles = false;
            StackingJoinCode = "";
            loadedStackingJoinCode = "";
            RaisePropertyChanged(nameof(ShowJoinCode));
            Filters.Clear();
            acceptOscRgb = true; acceptOscNarrowband = true;
            RaisePropertyChanged(nameof(AcceptOscRgb));
            RaisePropertyChanged(nameof(AcceptOscNarrowband));
            if (Runtime.Ready) {
                // The project asks for channels and bands, not the names in
                // this wheel: a mono wheel offers its letters, a colour camera
                // its OSC bands, and the two OSC bands are offered either way
                // because the boxes above take them.
                var delivered = new List<string>();
                foreach (var filter in RigProfile.FilterNames(Runtime.Settings)) {
                    foreach (var band in Runtime.Settings.ChannelsFor(filter)) {
                        if (!delivered.Contains(band)) delivered.Add(band);
                    }
                }
                if (delivered.Count == 0) delivered.AddRange(Sky.Filters.Channels);
                foreach (var letter in Sky.Filters.Channels) {
                    if (delivered.Contains(letter)) Filters.Add(new FilterGoalRow(letter));
                }
                foreach (var band in delivered.Where(b => !Sky.Filters.Channels.Contains(b))) {
                    Filters.Add(new FilterGoalRow(band));
                }
                OfferBand("OSC-RGB", true);
                OfferBand("OSC-Ha+OIII", true);
                var field = RigProfile.Field(Runtime.Host, Runtime.Settings);
                if (field is (double w, double h)) {
                    WidthText = (Math.Round(w * 2, 1)).ToString("0.0", CultureInfo.InvariantCulture);
                    HeightText = (Math.Round(h * 2, 1)).ToString("0.0", CultureInfo.InvariantCulture);
                }
            }
            Message = "";
            RaisePropertyChanged(nameof(Editing));
            RaisePropertyChanged(nameof(Title));
            RaisePropertyChanged(nameof(SubmitLabel));
        }

        /// <summary>Load a collaboration you started into the form.</summary>
        public void BeginEdit(ProjectListing listing) {
            Reset();
            editingId = listing.Id;
            Name = listing.Name;
            Notes = listing.Notes ?? "";
            CollectFiles = listing.CollectFiles;
            // A server-made cloud project's code is the server's to keep: not shown, never sent.
            StackingJoinCode = listing.Stacking?.Managed == true ? "" : listing.StackingJoinCode ?? "";
            loadedStackingJoinCode = StackingJoinCode;
            RaisePropertyChanged(nameof(ShowJoinCode));
            Single = listing.Kind == "single";
            var region = listing.Region;
            if (region != null) {
                RaText = Hms(region.Ra);
                DecText = Dms(region.Dec);
                rotation = region.Rotation;
                WidthText = region.Width.ToString("0.00", CultureInfo.InvariantCulture);
                HeightText = region.Height.ToString("0.00", CultureInfo.InvariantCulture);
            }
            OverlapText = listing.Overlap.ToString("0.#", CultureInfo.InvariantCulture);
            var wants = listing.Requirements ?? new Requirements();
            MinFocal = Text(wants.MinFocalLength); MaxFocal = Text(wants.MaxFocalLength);
            MaxHfr = Text(wants.MaxHfr); MaxGuideRms = Text(wants.MaxGuideRms);
            MinExposure = Text(wants.MinExposure); MaxExposure = Text(wants.MaxExposure);
            MaxMoon = wants.MaxMoonIllumination.HasValue ? Text(Math.Round(wants.MaxMoonIllumination.Value * 100)) : "";
            MinMoonSeparation = Text(wants.MinMoonSeparation); MinAltitude = Text(wants.MinAltitude);
            MinFrames = wants.MinFramesPerVisit.ToString(CultureInfo.InvariantCulture);
            MaxBortle = wants.MaxBortle.HasValue ? wants.MaxBortle.Value.ToString(CultureInfo.InvariantCulture) : "";
            RequireCalibrated = wants.RequireCalibrated;
            acceptOscRgb = wants.AcceptColour && wants.AcceptOscRgb;
            acceptOscNarrowband = wants.AcceptColour && wants.AcceptOscNarrowband;
            RaisePropertyChanged(nameof(AcceptOscRgb));
            RaisePropertyChanged(nameof(AcceptOscNarrowband));
            Filters.Clear();
            var goals = listing.Goals ?? new Dictionary<string, double>();
            foreach (var kv in wants.Filters ?? new Dictionary<string, double?>()) {
                goals.TryGetValue(kv.Key, out var hours);
                Filters.Add(new FilterGoalRow(kv.Key, hours > 0 ? Text(hours) : "", Text(kv.Value)));
            }
            foreach (var kv in goals.Where(g => !Filters.Any(f => string.Equals(f.Name, g.Key, StringComparison.OrdinalIgnoreCase)))) {
                Filters.Add(new FilterGoalRow(kv.Key, Text(kv.Value)));
            }
            Open = true;
            RaisePropertyChanged(nameof(Editing));
            RaisePropertyChanged(nameof(Title));
            RaisePropertyChanged(nameof(SubmitLabel));
        }

        // -- the target ----------------------------------------------------------------

        private async Task LookupAsync() {
            var query = Target.Trim();
            if (query.Length == 0) { Message = "Type a name to look up: M31, NGC 7000, Sh2-129."; return; }
            Busy = true;
            try {
                Message = $"Looking up {query}...";
                var answer = await Runtime.Session.LookupAsync(query, CancellationToken.None);
                var hit = answer.Results.FirstOrDefault();
                if (hit == null) {
                    Message = $"Nothing is known by the name \"{query}\". Try another designation, or type the coordinates.";
                    return;
                }
                RaText = Hms(hit.Ra);
                DecText = Dms(hit.Dec);
                rotation = 0;
                if (Name.Trim().Length == 0) Name = hit.Name.Length > 0 ? hit.Name : hit.Id;
                var size = hit.SizeArcmin is double arcmin && arcmin > 0 ? $", about {arcmin:0}′ across" : "";
                FoundText = $"{hit.Name}{(hit.Id.Length > 0 && hit.Id != hit.Name ? $" ({hit.Id})" : "")}: {hit.Type}{size}";
                if (hit.SizeArcmin is double across && across > 0 && Mosaic) {
                    var span = Math.Max(0.5, Math.Round(across / 60.0 * 1.4, 1));
                    WidthText = span.ToString("0.0", CultureInfo.InvariantCulture);
                    HeightText = Math.Max(0.4, Math.Round(span * 0.75, 1)).ToString("0.0", CultureInfo.InvariantCulture);
                }
                Message = "";
            } catch (Exception ex) {
                Message = $"Could not look that up: {ex.Message}";
            } finally {
                Busy = false;
            }
        }

        /// <summary>
        /// The rectangle drawn in the Framing Assistant, as drawn: its centre
        /// after any dragging, its panels and overlap, its rotation. The sky
        /// size is worked out the way N.I.N.A. does for its own rectangle -
        /// the camera's field from the pixel size and focal length the
        /// Framing Assistant is using, times the panels, less the overlap -
        /// so what the collaboration asks for is what the person saw. The
        /// first version took the view's zoom for the width, and a test
        /// mosaic came out thirty-nine degrees across.
        /// </summary>
        private void FromFraming() {
            try {
                var rect = framing?.Rectangle;
                var dso = framing?.DSO;
                var centre = rect?.Coordinates ?? dso?.Coordinates;
                if (centre == null) { Message = "The Framing Assistant has no target yet: load an image there first."; return; }

                var scale = AstroUtil.ArcsecPerPixel(framing.CameraPixelSize, framing.FocalLength);
                double fieldW, fieldH;
                if (framing.CameraWidth > 0 && framing.CameraHeight > 0 && scale > 0 && !double.IsNaN(scale) && !double.IsInfinity(scale)) {
                    fieldW = framing.CameraWidth * scale / 3600.0;
                    fieldH = framing.CameraHeight * scale / 3600.0;
                } else {
                    var field = Runtime.Ready ? RigProfile.Field(Runtime.Host, Runtime.Settings) : null;
                    (fieldW, fieldH) = field ?? (1.0, 0.75);
                }
                var across = Math.Max(1, framing.HorizontalPanels);
                var down = Math.Max(1, framing.VerticalPanels);
                double overlapW, overlapH;
                if (framing.SelectedOverlapUnit == "%") {
                    var fraction = framing.OverlapPercentage;
                    if (fraction > 1.0) fraction /= 100.0;
                    overlapW = fieldW * fraction;
                    overlapH = fieldH * fraction;
                } else {
                    overlapW = overlapH = Math.Max(0, framing.OverlapPixels) * (scale > 0 ? scale : 0) / 3600.0;
                }
                // The panels are cut on the server with one fraction for both axes:
                // the larger of the two keeps at least what was drawn everywhere.
                var share = Math.Max(fieldW > 0 ? overlapW / fieldW : 0, fieldH > 0 ? overlapH / fieldH : 0);
                if (across > 1 || down > 1)
                    OverlapText = Math.Round(Math.Min(50, Math.Max(0, share * 100)), 1).ToString("0.#", CultureInfo.InvariantCulture);
                var width = across * fieldW - (across - 1) * overlapW;
                var height = down * fieldH - (down - 1) * overlapH;

                // N.I.N.A. draws the rectangle turned by TotalRotation and calls
                // the sky position angle 360 minus that; a region's rotation is
                // the position angle.
                var total = rect != null ? rect.TotalRotation : (dso?.RotationPositionAngle ?? 0.0);
                rotation = ((360.0 - total) % 360.0 + 360.0) % 360.0;

                RaText = Hms(centre.RADegrees);
                DecText = Dms(centre.Dec);
                WidthText = width.ToString("0.00", CultureInfo.InvariantCulture);
                HeightText = height.ToString("0.00", CultureInfo.InvariantCulture);
                Single = across == 1 && down == 1;
                var label = string.IsNullOrWhiteSpace(dso?.Name) ? "the framing" : dso.Name;
                if (Name.Trim().Length == 0 && !string.IsNullOrWhiteSpace(dso?.Name)) Name = dso.Name;
                FoundText = Single
                    ? $"From the Framing Assistant: {label} at {RaText} {DecText}, one panel of {width:0.00}° × {height:0.00}°, angle {rotation:0}°"
                    : $"From the Framing Assistant: {label} at {RaText} {DecText}, {across} × {down} panels, {width:0.00}° × {height:0.00}° at {rotation:0}°";
                Message = "";
            } catch (Exception ex) {
                Message = $"Could not read the Framing Assistant: {ex.Message}";
            }
        }

        // -- the request ---------------------------------------------------------------

        private async Task SubmitAsync() {
            JObject body;
            try {
                body = Body();
            } catch (ArgumentException ex) {
                Message = ex.Message;
                return;
            }
            Busy = true;
            try {
                if (Editing) {
                    Message = "Saving...";
                    await Runtime.Session.UpdateProjectAsync(editingId, body, CancellationToken.None);
                    Message = $"\"{Name}\" is updated; every telescope on it finds out at its next check-in.";
                    Notification.ShowSuccess($"Starfront: {Name} updated");
                } else {
                    Message = "Starting...";
                    await Runtime.Session.CreateProjectAsync(body, CancellationToken.None);
                    Message = $"\"{Name}\" is live. It is in the list below for everybody; join it yourself to shoot your share.";
                    Notification.ShowSuccess($"Starfront: {Name} is live");
                }
                var said = Message;
                Reset();
                Message = said;
                Open = false;
                if (refresh != null) await refresh();
            } catch (Exception ex) {
                Message = (Editing ? "Could not save: " : "Could not start it: ") + ex.Message;
                Notification.ShowWarning($"Starfront: {ex.Message}");
            } finally {
                Busy = false;
            }
        }

        /// <summary>The server's request, or an ArgumentException that says what is missing.</summary>
        public JObject Body() {
            var title = Name.Trim();
            if (title.Length == 0) throw new ArgumentException("Give it a name.");
            var ra = ParseAngle(RaText, hours: true);
            var dec = ParseAngle(DecText, hours: false);
            if (ra == null || dec == null) throw new ArgumentException("Where is it? Look the name up, take it from the Framing Assistant, or type RA and Dec.");
            if (dec < -90 || dec > 90) throw new ArgumentException("Dec is between -90 and +90.");

            double width, height;
            if (Single) {
                var field = Runtime.Ready ? RigProfile.Field(Runtime.Host, Runtime.Settings) : null;
                (width, height) = field ?? (1.0, 0.75);
            } else {
                width = Num(WidthText) ?? 0;
                height = Num(HeightText) ?? 0;
                if (width <= 0 || height <= 0) throw new ArgumentException("A mosaic needs its width and height in degrees.");
                if (width > 60 || height > 60) throw new ArgumentException("A region is at most 60° across.");
            }
            double? overlap = null;
            if (!Single) {
                overlap = Num(OverlapText);
                if (overlap is not double o || o < 0 || o > 50) throw new ArgumentException("The panel overlap is 0 to 50 %.");
            }

            var filters = new JObject();
            var goals = new JObject();
            foreach (var row in Filters) {
                var filter = Sky.Filters.Canonical(row.Name);
                if (string.IsNullOrEmpty(filter)) continue;
                // An OSC band is taken only while its box is ticked.
                if (Sky.Filters.IsOsc(filter)) {
                    var narrow = filter.Contains('+');
                    if (narrow && !AcceptOscNarrowband) continue;
                    if (!narrow && !AcceptOscRgb) continue;
                }
                var band = Num(row.Bandpass);
                filters[filter] = band.HasValue ? new JValue(band.Value) : JValue.CreateNull();
                var hours = Num(row.Hours);
                if (hours is double h && h > 0) goals[filter] = h;
            }
            if (filters.Count == 0) throw new ArgumentException("Name at least one filter the collaboration wants.");

            var moon = Num(MaxMoon);
            var bortle = Num(MaxBortle);
            if (bortle is double b && (b < 1 || b > 9)) throw new ArgumentException("Bortle is 1 to 9.");
            if (!JoinCodes.TryNormalize(StackingJoinCode, out var stackingCode)) throw new ArgumentException(JoinCodes.Hint + ", or leave it blank.");
            var requirements = new JObject {
                ["minFocalLength"] = Json(Num(MinFocal)),
                ["maxFocalLength"] = Json(Num(MaxFocal)),
                ["maxHfr"] = Json(Num(MaxHfr)),
                ["maxGuideRms"] = Json(Num(MaxGuideRms)),
                ["minExposure"] = Json(Num(MinExposure)),
                ["maxExposure"] = Json(Num(MaxExposure)),
                ["maxMoonIllumination"] = moon.HasValue ? new JValue(Math.Max(0, Math.Min(1, moon.Value / 100.0))) : JValue.CreateNull(),
                ["minMoonSeparation"] = Json(Num(MinMoonSeparation)),
                ["minAltitude"] = Json(Num(MinAltitude)),
                ["minFramesPerVisit"] = Math.Max(1, (int)Math.Round(Num(MinFrames) ?? 10)),
                ["requireCalibrated"] = RequireCalibrated,
                ["acceptColour"] = AcceptOscRgb || AcceptOscNarrowband,
                ["acceptOscRgb"] = AcceptOscRgb,
                ["acceptOscNarrowband"] = AcceptOscNarrowband,
                ["maxBortle"] = bortle.HasValue ? new JValue((int)Math.Round(bortle.Value)) : JValue.CreateNull(),
                ["filters"] = filters,
            };
            var body = new JObject {
                ["name"] = title,
                ["kind"] = Single ? "single" : "mosaic",
                ["notes"] = Notes.Trim(),
                ["collectFiles"] = CollectFiles,
                ["overlap"] = overlap.HasValue ? new JValue(overlap.Value) : JValue.CreateNull(),
                ["region"] = new JObject {
                    ["ra"] = Math.Round(ra.Value, 5), ["dec"] = Math.Round(dec.Value, 5),
                    ["width"] = Math.Round(width, 4), ["height"] = Math.Round(height, 4),
                    ["rotation"] = Math.Round(rotation, 2),
                },
                ["requirements"] = requirements,
                ["goals"] = goals,
            };
            // An old collaboration's pasted code goes on an edit only when it was
            // changed (blank clears it; a different one the server refuses).
            // A new collaboration sends none: the server stacks it where its
            // owner can (their hub, or the Rapid Stacker Collab project it
            // makes), and the Collab tab in Starfront changes that.
            JoinCodes.TryNormalize(loadedStackingJoinCode, out var loaded);
            if (Editing && stackingCode != loaded) body["stackingJoinCode"] = stackingCode;
            return body;
        }

        private static JToken Json(double? value) => value.HasValue ? new JValue(value.Value) : JValue.CreateNull();

        private static string Text(double? value) => value.HasValue ? value.Value.ToString("0.###", CultureInfo.InvariantCulture) : "";

        /// <summary>A number typed either way round the decimal point; blank or nonsense is null.</summary>
        public static double? Num(string text) {
            var clean = (text ?? "").Trim();
            if (clean.Length == 0) return null;
            if (double.TryParse(clean, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)) return value;
            if (double.TryParse(clean, NumberStyles.Float, CultureInfo.CurrentCulture, out value)) return value;
            return null;
        }

        private static readonly Regex Parts = new(@"[-+]?\d+(?:[.,]\d+)?", RegexOptions.Compiled);

        /// <summary>
        /// "00:42:44", "00 42 44.3", "0h42m44s", "10.68" or "+41:16:09", "41° 16' 09"", "-5.4":
        /// degrees out. A lone number is hours when it is RA and no bigger than 24.
        /// </summary>
        public static double? ParseAngle(string text, bool hours) {
            var clean = (text ?? "").Trim();
            if (clean.Length == 0) return null;
            var matches = Parts.Matches(clean);
            if (matches.Count == 0) return null;
            var numbers = matches.Select(m => double.Parse(m.Value.Replace(',', '.'), CultureInfo.InvariantCulture)).ToList();
            var negative = clean.TrimStart().StartsWith("-");
            if (numbers.Count == 1) {
                var lone = numbers[0];
                if (hours) return lone <= 24 && !clean.Contains('°') && !clean.EndsWith("d") ? lone * 15.0 : lone;
                return lone;
            }
            var value = Math.Abs(numbers[0]) + numbers[1] / 60.0 + (numbers.Count > 2 ? numbers[2] / 3600.0 : 0.0);
            if (negative) value = -value;
            return hours ? value * 15.0 : value;
        }

        public static string Hms(double raDegrees) {
            var h = ((raDegrees % 360 + 360) % 360) / 15.0;
            var hh = (int)h; var m = (h - hh) * 60; var mm = (int)m; var ss = (m - mm) * 60;
            return $"{hh:00}:{mm:00}:{ss:00}";
        }

        public static string Dms(double dec) {
            var sign = dec < 0 ? "-" : "+";
            var d = Math.Abs(dec);
            var dd = (int)d; var m = (d - dd) * 60; var mm = (int)m; var ss = (m - mm) * 60;
            return $"{sign}{dd:00}:{mm:00}:{ss:00}";
        }
    }
}
