// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Projects;

/// <summary>A project the user has joined, with what attribution needs to know about it.</summary>
/// <param name="GridPending">The grid waits for its first light (contract 1.24.0): its centre and scale are placeholders until then.</param>
public sealed record JoinedProject(string ProjectId, string Slug, string Name, string TargetName, Grid? Grid, DateTimeOffset JoinedAt, bool GridPending = false);

/// <summary>What a saved frame says about where it was pointed and at what.</summary>
/// <param name="RaDeg">The position attribution uses: the mount's <c>RA</c>/<c>DEC</c>, else the target's <c>OBJCTRA</c>/<c>OBJCTDEC</c>.</param>
/// <param name="TargetRaDeg">The target's <c>OBJCTRA</c>/<c>OBJCTDEC</c> alone, when the header has them: a second say on where the frame was meant to point.</param>
public sealed record FramePointing(string? ObjectName, double? RaDeg, double? DecDeg, double? TargetRaDeg = null, double? TargetDecDeg = null)
{
    public bool HasPosition => RaDeg.HasValue && DecDeg.HasValue;

    /// <summary>
    /// From a header: <c>RA</c>/<c>DEC</c> (degrees, or sexagesimal where RA is hours) first, then
    /// <c>OBJCTRA</c>/<c>OBJCTDEC</c> (hours and degrees, the FITS convention).
    /// </summary>
    public static FramePointing FromHeader(InitHeader h)
    {
        double? ra = null, dec = null, tra = null, tdec = null;
        if (SkyMath.TryParseRa(h.ObjctRa, plainNumberIsHours: true, out var r) && SkyMath.TryParseDec(h.ObjctDec, out var d))
        {
            tra = r;
            tdec = d;
        }
        if (SkyMath.TryParseRa(Unbox(h.Ra), plainNumberIsHours: false, out r) && SkyMath.TryParseDec(Unbox(h.Dec), out d))
        {
            ra = r;
            dec = d;
        }
        else
        {
            ra = tra;
            dec = tdec;
        }
        return new FramePointing(h.Object, ra, dec, tra, tdec);
    }

    /// <summary>Every position the header gives: the mount's, and the target's when it is another.</summary>
    public IEnumerable<(double Ra, double Dec)> Positions()
    {
        if (HasPosition) yield return (RaDeg!.Value, DecDeg!.Value);
        if (TargetRaDeg is { } tr && TargetDecDeg is { } td && (tr != RaDeg || td != DecDeg)) yield return (tr, td);
    }

    // A header read back from the queue database carries RA/DEC as JsonElement.
    private static object? Unbox(object? v) => v is System.Text.Json.JsonElement e
        ? e.ValueKind switch
        {
            System.Text.Json.JsonValueKind.Number => e.GetDouble(),
            System.Text.Json.JsonValueKind.String => e.GetString(),
            _ => null,
        }
        : v;
}

public enum AttributionMode
{
    /// <summary>Every frame goes to the active project, unless it shows another target (<see cref="ProjectAttribution.IsOtherTarget"/>).</summary>
    ActiveOnly,
    /// <summary>Match by target name or grid footprint; frames that match nothing are not attributed.</summary>
    Auto,
    /// <summary>As <see cref="Auto"/>, but a frame that matches nothing goes to the active project, unless it shows another target.</summary>
    AutoWithActiveFallback,
}

public enum AttributionReason
{
    None,
    ActiveProject,
    ObjectName,
    GridFootprint,
    ObjectAndFootprint,
    /// <summary>The frame was pointed well away from the active project's field and is not named after it: the user moved on to another target.</summary>
    OtherTarget,
}

/// <param name="Project">The chosen project, or null when there is none or the choice is ambiguous.</param>
/// <param name="Candidates">Every project that matched, best first. More than one with <see cref="Ambiguous"/> means the user should choose.</param>
public sealed record AttributionResult(JoinedProject? Project, AttributionReason Reason, IReadOnlyList<JoinedProject> Candidates, bool Ambiguous);

/// <summary>
/// Which joined project a saved frame belongs to (plan §8.1): its <c>OBJECT</c> matches the project's target, or its
/// RA/Dec falls inside the project's grid footprint. A frame that satisfies both ranks above one that satisfies only the
/// footprint, which ranks above one that satisfies only the name (names are free text; positions are not). When several
/// projects tie, the active project wins if it is one of them; otherwise the result is ambiguous and unattributed.
/// A frame that falls back to the active project is refused instead when it shows another target (<see cref="IsOtherTarget"/>):
/// the user has moved on, and uploading it would only cost a rejection on the server.
/// Calibration frames carry no pointing; the caller sends them to the active project.
/// </summary>
public static class ProjectAttribution
{
    /// <summary>How far beyond a grid's half-diagonal a frame may point and still be of the project's target.</summary>
    public const double OtherTargetMarginDeg = 1.0;

    /// <summary>The least distance from a grid centre that counts as another target while the grid waits for its first light (its centre is the owner's typed one).</summary>
    public const double PendingGridRadiusDeg = 3.0;

    public static AttributionResult Attribute(FramePointing frame, IReadOnlyList<JoinedProject> projects, string? activeProjectId = null,
        AttributionMode mode = AttributionMode.Auto, double footprintMarginPx = 0)
    {
        var active = activeProjectId is null ? null : projects.FirstOrDefault(p => p.ProjectId == activeProjectId);
        if (mode == AttributionMode.ActiveOnly)
            return active is null
                ? new AttributionResult(null, AttributionReason.None, Array.Empty<JoinedProject>(), false)
                : IsOtherTarget(frame, active)
                    ? new AttributionResult(null, AttributionReason.OtherTarget, Array.Empty<JoinedProject>(), false)
                    : new AttributionResult(active, AttributionReason.ActiveProject, new[] { active }, false);

        var scored = new List<(JoinedProject P, int Score, AttributionReason Reason)>();
        foreach (var p in projects)
        {
            var byName = NameMatches(frame.ObjectName, p);
            var byFootprint = frame.HasPosition && p.Grid is { } g && SkyMath.InsideGrid(g, frame.RaDeg!.Value, frame.DecDeg!.Value, footprintMarginPx);
            if (byName && byFootprint) scored.Add((p, 3, AttributionReason.ObjectAndFootprint));
            else if (byFootprint) scored.Add((p, 2, AttributionReason.GridFootprint));
            else if (byName) scored.Add((p, 1, AttributionReason.ObjectName));
        }

        if (scored.Count == 0)
        {
            if (mode == AttributionMode.AutoWithActiveFallback && active is not null)
                return IsOtherTarget(frame, active)
                    ? new AttributionResult(null, AttributionReason.OtherTarget, Array.Empty<JoinedProject>(), false)
                    : new AttributionResult(active, AttributionReason.ActiveProject, new[] { active }, false);
            return new AttributionResult(null, AttributionReason.None, Array.Empty<JoinedProject>(), false);
        }

        var ordered = scored.OrderByDescending(s => s.Score).ThenByDescending(s => s.P.JoinedAt).ToList();
        var top = ordered[0].Score;
        var best = ordered.Where(s => s.Score == top).ToList();
        var candidates = ordered.Select(s => s.P).ToList();
        if (best.Count == 1)
            return new AttributionResult(best[0].P, best[0].Reason, candidates, false);
        var activeBest = best.FirstOrDefault(s => s.P.ProjectId == activeProjectId);
        if (activeBest.P is not null)
            return new AttributionResult(activeBest.P, activeBest.Reason, candidates, false);
        return new AttributionResult(null, best[0].Reason, candidates, true);
    }

    /// <summary>
    /// Whether a frame shows another target than <paramref name="project"/>'s: its <c>OBJECT</c> does not name the project, and every
    /// position its header gives (the mount's and the target's) is farther from the grid centre than <see cref="OnTargetRadiusDeg"/>.
    /// False when the frame gives no position or the project no grid: nothing then says it is elsewhere.
    /// </summary>
    public static bool IsOtherTarget(FramePointing frame, JoinedProject project) =>
        !NameMatches(frame.ObjectName, project)
        && SeparationDeg(frame, project) is { } sep
        && sep > OnTargetRadiusDeg(project);

    /// <summary>
    /// The farthest a frame may point from <paramref name="project"/>'s grid centre and still be of its target: the grid's half-diagonal
    /// plus <see cref="OtherTargetMarginDeg"/>, and at least <see cref="PendingGridRadiusDeg"/> while the grid waits for its first light.
    /// </summary>
    public static double OnTargetRadiusDeg(JoinedProject project)
    {
        var g = project.Grid;
        var halfDiagonal = g is null ? 0 : Math.Sqrt((double)g.WidthPx * g.WidthPx + (double)g.HeightPx * g.HeightPx) / 2 * g.ScaleArcsecPx / 3600;
        var radius = halfDiagonal + OtherTargetMarginDeg;
        return project.GridPending ? Math.Max(radius, PendingGridRadiusDeg) : radius;
    }

    /// <summary>The smallest angular distance, degrees, from any position the frame gives to the grid centre; null without either.</summary>
    public static double? SeparationDeg(FramePointing frame, JoinedProject project)
    {
        if (project.Grid is not { } g) return null;
        double? best = null;
        foreach (var (ra, dec) in frame.Positions())
        {
            var s = SkyMath.SeparationDeg(ra, dec, g.CenterRaDeg, g.CenterDecDeg);
            if (best is null || s < best) best = s;
        }
        return best;
    }

    /// <summary>
    /// <c>OBJECT</c> against the project's target and name: equal after lower-casing and dropping everything that is not a
    /// letter or digit ("NGC 7000" = "ngc-7000"), also against each part of "M42 - Orion Nebula" split at dashes, slashes,
    /// commas, semicolons and brackets.
    /// </summary>
    public static bool NameMatches(string? objectName, JoinedProject project)
    {
        var o = Key(objectName);
        if (o.Length == 0) return false;
        foreach (var source in new[] { project.TargetName, project.Name })
        {
            if (Key(source) == o) return true;
            foreach (var part in source.Split(new[] { " - ", " – ", " — ", "/", ",", ";", "(", ")", "[", "]", "|" }, StringSplitOptions.RemoveEmptyEntries))
                if (Key(part) == o) return true;
        }
        return false;
    }

    private static string Key(string? s)
    {
        if (string.IsNullOrWhiteSpace(s)) return "";
        var sb = new StringBuilder(s.Length);
        foreach (var c in s.Normalize(NormalizationForm.FormKC))
            if (char.IsLetterOrDigit(c)) sb.Append(char.ToLowerInvariant(c));
        return sb.ToString();
    }
}
