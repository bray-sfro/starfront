// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;

namespace RapidStackerCollab.Core.Library;

/// <summary>What the plugin knows of the rig the library belongs to: the confirmed rig of the profile (<c>GET /rigs</c>).</summary>
/// <param name="WidthPx">The sensor size when the rig records it; null skips the size check.</param>
public sealed record LibraryRigProfile(string Camera, int Binning, int? WidthPx, int? HeightPx);

/// <summary>A file of the library folder with its header (<see cref="LibraryHeaderReader"/>).</summary>
public sealed record LibraryCandidate(string Path, long Size, long ModifiedMs, InitHeader Header)
{
    public FrameKind Kind => FrameKinds.Classify(Header.ImageTyp);
}

public sealed record LibraryPlannerOptions(bool PreferRaw = false, int MinRawFrames = 3);

public sealed record LibraryPlanItem(LibraryCandidate File, bool IsMaster, string Group);

public sealed record LibrarySkip(string Path, string Reason);

/// <param name="Description">"dark 120 s, gain 100, offset 50, -10 C, bin 1".</param>
/// <param name="Chosen"><c>master</c>, <c>raw</c> or <c>none</c>: what goes up for this group.</param>
public sealed record LibraryPlanGroup(string Key, string Family, string Description, string Chosen, int RawFiles, int MasterFiles);

/// <param name="Upload">Masters first, then raw frames, each in group and path order.</param>
/// <param name="Ignored">Lights, flats and files of any other type: not calibration for a library (flats never come from the library).</param>
public sealed record LibraryPlan(IReadOnlyList<LibraryPlanItem> Upload, IReadOnlyList<LibrarySkip> Skipped, IReadOnlyList<LibraryPlanGroup> Groups, int Ignored);

/// <summary>
/// Decides what of a folder goes into a rig's calibration library (calibration library plan B6): bias, dark and dark-flat frames and their
/// masters, grouped by family, camera, gain, offset, binning, size, exposure (to 0.01 s) and set-point (to 1 degree C).
/// <list type="bullet">
/// <item>A group with a <i>usable</i> master sends its newest master only. A master is usable when it names its camera (<c>INSTRUME</c>) and,
/// when the raw frames of the same settings carry <c>GAIN</c>, it carries it too (a master that lost its gain would be matched to any gain).</item>
/// <item>Otherwise the group's raw frames go, when there are at least <see cref="LibraryPlannerOptions.MinRawFrames"/> of them (fewer are a poor master).</item>
/// <item><see cref="LibraryPlannerOptions.PreferRaw"/> turns that around for masters made by tools that subtract the bias: raw frames first.</item>
/// <item>A file whose camera, binning or size is not the rig's is skipped with the reason, never sent.</item>
/// </list>
/// Pure: the same files always give the same plan, whatever order they are listed in.
/// </summary>
public static class LibraryPlanner
{
    public static LibraryPlan Plan(IEnumerable<LibraryCandidate> files, LibraryRigProfile rig, LibraryPlannerOptions? options = null)
    {
        options ??= new LibraryPlannerOptions();
        var skipped = new List<LibrarySkip>();
        var ignored = 0;
        var entries = new List<Entry>();

        foreach (var f in files.OrderBy(f => f.Path, StringComparer.Ordinal))
        {
            var kind = f.Kind;
            var family = FamilyOf(kind);
            if (family is null) { ignored++; continue; }
            var h = f.Header;
            var name = System.IO.Path.GetFileName(f.Path);
            var master = IsMaster(kind);

            if (string.IsNullOrWhiteSpace(h.Instrume))
            {
                skipped.Add(new LibrarySkip(f.Path, master ? $"{name}: the master does not name its camera (no INSTRUME), so it cannot be matched to your rig." : $"{name}: no INSTRUME, so the camera is unknown."));
                continue;
            }
            if (!string.Equals(h.Instrume.Trim(), rig.Camera.Trim(), StringComparison.OrdinalIgnoreCase))
            {
                skipped.Add(new LibrarySkip(f.Path, $"{name}: taken with '{h.Instrume.Trim()}', not your rig's camera '{rig.Camera.Trim()}'."));
                continue;
            }
            var binning = h.XBinning ?? 1;
            if (binning != rig.Binning || (h.YBinning ?? binning) != binning)
            {
                skipped.Add(new LibrarySkip(f.Path, $"{name}: binning {binning} is not your rig's {rig.Binning}."));
                continue;
            }
            if (h.Naxis1 is not { } w || h.Naxis2 is not { } ht)
            {
                skipped.Add(new LibrarySkip(f.Path, $"{name}: the image size is not in the header."));
                continue;
            }
            if (rig.WidthPx is { } rw && rig.HeightPx is { } rh && !SizeMatches(w, ht, rw, rh, rig.Binning))
            {
                skipped.Add(new LibrarySkip(f.Path, $"{name}: {w} x {ht} px is not your rig's sensor size ({rw} x {rh} px)."));
                continue;
            }
            entries.Add(new Entry(f, family.Value, master, Loose(family.Value, h, binning, w, ht), h.Gain));
        }

        // Which loose keys have raw frames that carry a gain: a master of those settings must carry one too.
        var rawGainByLoose = entries.Where(e => !e.IsMaster && e.Gain is not null).Select(e => e.Loose).ToHashSet(StringComparer.Ordinal);
        var groups = new SortedDictionary<string, List<Entry>>(StringComparer.Ordinal);
        foreach (var e in entries)
        {
            var key = e.Loose + "|gain=" + Num(e.Gain);
            if (!groups.TryGetValue(key, out var list)) groups[key] = list = new List<Entry>();
            list.Add(e);
        }

        var masters = new List<LibraryPlanItem>();
        var raws = new List<LibraryPlanItem>();
        var summary = new List<LibraryPlanGroup>();
        foreach (var (key, list) in groups)
        {
            var rawList = list.Where(e => !e.IsMaster).ToList();
            var masterList = list.Where(e => e.IsMaster).ToList();
            var usable = new List<Entry>();
            foreach (var m in masterList)
            {
                if (m.Gain is null && rawGainByLoose.Contains(m.Loose))
                    skipped.Add(new LibrarySkip(m.File.Path, $"{System.IO.Path.GetFileName(m.File.Path)}: the master has no GAIN, but frames of the same settings do; not used."));
                else
                    usable.Add(m);
            }

            var description = Describe(list[0]);
            var family = list[0].Family.ToString();
            string chosen;
            var newest = usable.OrderByDescending(m => TakenAt(m.File)).ThenBy(m => m.File.Path, StringComparer.Ordinal).FirstOrDefault();
            var enoughRaw = rawList.Count >= options.MinRawFrames;
            if (options.PreferRaw ? enoughRaw : newest is null && enoughRaw)
            {
                chosen = "raw";
                raws.AddRange(rawList.Select(e => new LibraryPlanItem(e.File, false, key)));
                foreach (var m in usable) skipped.Add(new LibrarySkip(m.File.Path, $"{System.IO.Path.GetFileName(m.File.Path)}: your raw frames of the same settings are sent instead."));
            }
            else if (newest is not null)
            {
                chosen = "master";
                masters.Add(new LibraryPlanItem(newest.File, true, key));
                foreach (var m in usable.Where(m => m != newest))
                    skipped.Add(new LibrarySkip(m.File.Path, $"{System.IO.Path.GetFileName(m.File.Path)}: an older master of the same settings; only the newest is sent."));
                foreach (var r in rawList)
                    skipped.Add(new LibrarySkip(r.File.Path, $"{System.IO.Path.GetFileName(r.File.Path)}: the group's master is sent instead of its raw frames."));
            }
            else
            {
                chosen = "none";
                foreach (var r in rawList)
                    skipped.Add(new LibrarySkip(r.File.Path, $"{System.IO.Path.GetFileName(r.File.Path)}: only {rawList.Count} raw frame(s) of {description}, and a master needs at least {options.MinRawFrames}."));
            }
            summary.Add(new LibraryPlanGroup(key, family, description, chosen, rawList.Count, masterList.Count));
        }

        return new LibraryPlan(masters.Concat(raws).ToList(), skipped, summary, ignored);
    }

    // ---- grouping -----------------------------------------------------------------------------------------------------

    private sealed record Entry(LibraryCandidate File, Family Family, bool IsMaster, string Loose, double? Gain);

    private enum Family { Bias, Dark, DarkFlat }

    private static Family? FamilyOf(FrameKind k) => k switch
    {
        FrameKind.Bias or FrameKind.MasterBias => Family.Bias,
        FrameKind.Dark or FrameKind.MasterDark => Family.Dark,
        FrameKind.DarkFlat or FrameKind.MasterDarkFlat => Family.DarkFlat,
        _ => null,
    };

    private static bool IsMaster(FrameKind k) => k is FrameKind.MasterBias or FrameKind.MasterDark or FrameKind.MasterDarkFlat;

    // Everything but the gain: a master with no gain is matched against the raw frames of the same loose key.
    private static string Loose(Family family, InitHeader h, int binning, int w, int ht) =>
        string.Join('|', family.ToString().ToLowerInvariant(), h.Instrume!.Trim().ToLowerInvariant(), "offset=" + Num(h.Offset), "bin=" + binning,
            $"size={w}x{ht}", "exp=" + Exposure(h.ExpTime), "temp=" + SetPoint(h.SetTemp));

    private static string Num(double? v) => v is { } d ? d.ToString("0.###", CultureInfo.InvariantCulture) : "none";

    private static string Exposure(double? v) => v is { } d ? Math.Round(d, 2, MidpointRounding.AwayFromZero).ToString("0.00", CultureInfo.InvariantCulture) : "none";

    private static string SetPoint(double? v) => v is { } d ? Math.Round(d, 0, MidpointRounding.AwayFromZero).ToString("0", CultureInfo.InvariantCulture) : "none";

    private static string Describe(Entry e)
    {
        var h = e.File.Header;
        var parts = new List<string>
        {
            e.Family switch { Family.Bias => "bias", Family.Dark => "dark", _ => "dark flat" } + (e.Family == Family.Bias || h.ExpTime is null ? "" : " " + Num(h.ExpTime) + " s"),
        };
        if (h.Gain is not null) parts.Add("gain " + Num(h.Gain));
        if (h.Offset is not null) parts.Add("offset " + Num(h.Offset));
        if (h.SetTemp is not null) parts.Add(SetPoint(h.SetTemp) + " C");
        parts.Add("bin " + (h.XBinning ?? 1));
        return string.Join(", ", parts);
    }

    // The sensor size in the rig is the full frame; a binned header carries the binned size. Either spelling matches.
    private static bool SizeMatches(int w, int h, int rigW, int rigH, int binning) =>
        (w == rigW && h == rigH) || (binning > 1 && w == rigW / binning && h == rigH / binning);

    private static DateTimeOffset TakenAt(LibraryCandidate c) =>
        DateTimeOffset.TryParse(c.Header.DateObs, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var t)
            ? t
            : DateTimeOffset.FromUnixTimeMilliseconds(c.ModifiedMs);
}
