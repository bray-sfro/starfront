// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Panels;

/// <summary>One filter's line of a panel: hours done against the target (if any).</summary>
/// <param name="Fraction">Done / target, 0 and up (more than 1 when over target); null without a target.</param>
public sealed record PanelFilterProgress(string Filter, double HoursDone, double? TargetHours, double? Fraction, string Text);

/// <param name="Role"><c>assigned</c>, <c>claimed</c> or <c>both</c> (assigned to you and also claimed by you).</param>
public sealed record YourPanelRow(
    string PanelId,
    string Role,
    double RaDeg,
    double DecDeg,
    double PaDeg,
    string RaText,
    string DecText,
    string PositionText,
    string PaText,
    string OverlapText,
    string RoleText,
    string SharedWithText,
    IReadOnlyList<PanelFilterProgress> Progress,
    string ProgressText);

public sealed record YourPanelsView(string Header, IReadOnlyList<YourPanelRow> Rows, string EmptyText)
{
    public bool HasRows => Rows.Count > 0;
}

/// <summary>
/// "Your panels" (MOSAIC_PLAN.md section 8): the panels the owner assigned to the member and the ones the member claimed, with
/// where to point, the project overlap and the progress against the targets. A pure function of the mosaic state and the optional
/// <c>panels.json</c>, so every sentence is tested without WPF. Display names only: nothing here receives a user id or an e-mail.
/// </summary>
public static class YourPanels
{
    public static YourPanelsView Build(MosaicState mosaic, PanelsDocument? progress, string? myDisplayName = null)
    {
        var byId = mosaic.Panels.ToDictionary(p => p.PanelId, StringComparer.Ordinal);
        var done = progress is null
            ? new Dictionary<string, PanelProgress>(StringComparer.Ordinal)
            : progress.Panels.GroupBy(p => p.PanelId, StringComparer.Ordinal).ToDictionary(g => g.Key, g => g.First(), StringComparer.Ordinal);
        var assigned = new HashSet<string>(mosaic.MyPanels, StringComparer.Ordinal);
        var claimed = new HashSet<string>(mosaic.MyClaims, StringComparer.Ordinal);

        var rows = new List<YourPanelRow>();
        foreach (var id in mosaic.MyPanels.Concat(mosaic.MyClaims.Where(c => !assigned.Contains(c))).Distinct(StringComparer.Ordinal))
        {
            if (!byId.TryGetValue(id, out var panel)) continue;
            var role = assigned.Contains(id) ? (claimed.Contains(id) ? "both" : "assigned") : "claimed";
            rows.Add(Row(mosaic, panel, role, progress is not null, done.GetValueOrDefault(id), myDisplayName));
        }
        rows.Sort((a, b) => ComparePanelIds(a.PanelId, b.PanelId));

        var l = mosaic.Layout;
        var header = string.Create(CultureInfo.InvariantCulture,
            $"Mosaic {l.Rows} x {l.Cols} panels, {Num(l.OverlapPct)} % overlap, panel {Num(l.PanelWDeg, 2)} x {Num(l.PanelHDeg, 2)} deg");
        return new YourPanelsView(header, rows, "You have no panels in this mosaic yet. Claim some on the project page, or ask the owner to assign them.");
    }

    private static YourPanelRow Row(MosaicState mosaic, MosaicPanel p, string role, bool progressKnown, PanelProgress? done, string? me)
    {
        var ra = Sexagesimal.Ra(p.RaDeg);
        var dec = Sexagesimal.Dec(p.DecDeg);
        var lines = ProgressLines(mosaic, p.PanelId, done);
        var progressText = lines.Count == 0
            ? (progressKnown ? "No target set" : "No target set")
            : string.Join("; ", lines.Select(x => x.Text));
        if (lines.Count > 0 && !progressKnown && lines.All(x => x.HoursDone == 0)) progressText += " (progress not available yet)";

        var others = p.ClaimedBy.Concat(p.AssignedTo is { } a ? new[] { a } : Array.Empty<string>())
            .Where(n => !string.Equals(n, me, StringComparison.Ordinal)).Distinct(StringComparer.Ordinal).OrderBy(n => n, StringComparer.Ordinal).ToList();

        return new YourPanelRow(
            p.PanelId, role, p.RaDeg, p.DecDeg, p.PaDeg, ra, dec,
            string.Create(CultureInfo.InvariantCulture, $"RA {ra} ({p.RaDeg:0.0000}°), Dec {dec} ({p.DecDeg:0.0000}°)"),
            string.Create(CultureInfo.InvariantCulture, $"PA {p.PaDeg:0.0}°"),
            string.Create(CultureInfo.InvariantCulture, $"Overlap with neighbours {Num(mosaic.Layout.OverlapPct)} %"),
            role switch { "assigned" => "Assigned to you", "both" => "Assigned to you (also claimed)", _ => "Claimed by you" },
            others.Count == 0 ? "" : "Also: " + string.Join(", ", others),
            lines, progressText);
    }

    /// <summary>The hours of one panel per filter, against the target when there is one. Filters with a target come first.</summary>
    public static IReadOnlyList<PanelFilterProgress> ProgressLines(MosaicState mosaic, string panelId, PanelProgress? done)
    {
        var hours = new Dictionary<string, double>(StringComparer.Ordinal);
        if (done is not null)
            foreach (var (key, s) in done.Streams)
                hours[FilterOf(key)] = hours.GetValueOrDefault(FilterOf(key)) + s.IntegrationS / 3600.0;

        var filters = mosaic.Targets.Default.Keys.Concat(mosaic.Targets.Panels.GetValueOrDefault(panelId)?.Keys ?? Enumerable.Empty<string>())
            .Concat(hours.Keys).Distinct(StringComparer.Ordinal).OrderBy(f => f, StringComparer.Ordinal);
        var lines = new List<PanelFilterProgress>();
        foreach (var f in filters)
        {
            var target = TargetHours(mosaic.Targets, panelId, f);
            var h = hours.GetValueOrDefault(f);
            if (target is null && h <= 0) continue;
            lines.Add(target is { } t
                ? new PanelFilterProgress(f, h, t, h / t, string.Create(CultureInfo.InvariantCulture, $"{f} {Hours(h)} of {Hours(t)} ({Math.Round(h / t * 100):0} %)"))
                : new PanelFilterProgress(f, h, null, null, $"{f} {Hours(h)}"));
        }
        return lines.OrderBy(l => l.TargetHours is null ? 1 : 0).ToList();
    }

    /// <summary>The target in hours for a panel and filter: the panel's override if any (0 means none), else the default (0 or absent means none).</summary>
    public static double? TargetHours(MosaicTargets targets, string panelId, string filter)
    {
        if (targets.Panels.TryGetValue(panelId, out var over) && over.TryGetValue(filter, out var o))
            return o > 0 ? o : null;
        return targets.Default.TryGetValue(filter, out var d) && d > 0 ? d : null;
    }

    /// <summary>The canonical filter of a stream key: <c>HA@3nm</c> and <c>HA@osc</c> are the filter <c>HA</c>.</summary>
    public static string FilterOf(string streamKey)
    {
        var at = streamKey.IndexOf('@');
        return at < 0 ? streamKey : streamKey[..at];
    }

    /// <summary>"3.2 h", "6 h", "0 h".</summary>
    public static string Hours(double h) => string.Create(CultureInfo.InvariantCulture, $"{Math.Round(h, 1):0.#} h");

    private static string Num(double v, int max = 1) => v.ToString("0." + new string('#', max), CultureInfo.InvariantCulture);

    /// <summary>Row-major order: r2c10 after r2c9 and before r3c1.</summary>
    public static int ComparePanelIds(string a, string b)
    {
        var (ra, ca) = Parse(a);
        var (rb, cb) = Parse(b);
        return ra != rb ? ra.CompareTo(rb) : ca != cb ? ca.CompareTo(cb) : string.CompareOrdinal(a, b);
    }

    private static (int, int) Parse(string id)
    {
        var c = id.IndexOf('c');
        if (id.Length > 3 && id[0] == 'r' && c > 1 && int.TryParse(id.AsSpan(1, c - 1), out var r) && int.TryParse(id.AsSpan(c + 1), out var col)) return (r, col);
        return (int.MaxValue, int.MaxValue);
    }
}
