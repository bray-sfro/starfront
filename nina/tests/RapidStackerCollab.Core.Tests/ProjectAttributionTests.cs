// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Projects;

namespace RapidStackerCollab.Core.Tests;

public class ProjectAttributionTests
{
    private static Grid MakeGrid(double ra = 85.25, double dec = -2.458, double rot = 0, double scale = 1.5, int w = 3000, int h = 2000) => new()
    {
        CenterRaDeg = ra, CenterDecDeg = dec, RotationDeg = rot, ScaleArcsecPx = scale, WidthPx = w, HeightPx = h,
    };

    /// <summary>
    /// Independent oracle: the sky position of grid pixel (x, y) built exactly as worker/crates/engine/src/project.rs builds
    /// its WCS (CD from scale and rotation, CRPIX at the centre, y down) and deprojected with the standard TAN inverse.
    /// </summary>
    private static (double Ra, double Dec) PixelToSky(Grid g, double x, double y)
    {
        var s = g.ScaleArcsecPx / 3600.0;
        var t = g.RotationDeg * Math.PI / 180;
        var (sin, cos) = (Math.Sin(t), Math.Cos(t));
        var dx = x - g.WidthPx / 2.0;
        var dy = y - g.HeightPx / 2.0;
        var xi = (-s * cos * dx + -s * sin * dy) * Math.PI / 180;
        var eta = (s * sin * dx + -s * cos * dy) * Math.PI / 180;
        var a0 = g.CenterRaDeg * Math.PI / 180;
        var d0 = g.CenterDecDeg * Math.PI / 180;
        var rho = Math.Sqrt(xi * xi + eta * eta);
        if (rho < 1e-15) return (g.CenterRaDeg, g.CenterDecDeg);
        var c = Math.Atan(rho);
        var dec = Math.Asin(Math.Cos(c) * Math.Sin(d0) + eta * Math.Sin(c) * Math.Cos(d0) / rho);
        var ra = a0 + Math.Atan2(xi * Math.Sin(c), rho * Math.Cos(d0) * Math.Cos(c) - eta * Math.Sin(d0) * Math.Sin(c));
        return (SkyMath.Wrap360(ra * 180 / Math.PI), dec * 180 / Math.PI);
    }

    // ---- footprint geometry ------------------------------------------------------------------------------------

    [TestCase(0.0, 0.0)]
    [TestCase(0.0, 60.0)]
    [TestCase(0.0, -80.0)]
    [TestCase(359.95, 10.0)]
    [TestCase(0.05, -45.0)]
    [TestCase(180.0, 85.0)]
    public void The_footprint_agrees_with_the_engines_wcs_for_every_rotation(double ra, double dec)
    {
        for (var rot = -180.0; rot <= 180.0; rot += 22.5)
        {
            var g = MakeGrid(ra, dec, rot);
            var inset = 3.0; // px
            // Points just inside every edge and corner, and just outside each.
            foreach (var (x, y) in new[] { (0.0, 0.0), (3000.0, 0.0), (0.0, 2000.0), (3000.0, 2000.0), (1500.0, 0.0), (1500.0, 2000.0), (0.0, 1000.0), (3000.0, 1000.0), (1500.0, 1000.0) })
            {
                var inx = x == 0 ? inset : x == 3000 ? 3000 - inset : x;
                var iny = y == 0 ? inset : y == 2000 ? 2000 - inset : y;
                var outx = x == 0 ? -inset : x == 3000 ? 3000 + inset : x;
                var outy = y == 0 ? -inset : y == 2000 ? 2000 + inset : y;
                var (ri, di) = PixelToSky(g, inx, iny);
                Assert.That(SkyMath.InsideGrid(g, ri, di), Is.True, $"inside ({inx},{iny}) rot {rot} at {ra},{dec}");
                if (outx != x || outy != y)
                {
                    var (ro, dd) = PixelToSky(g, outx, outy);
                    Assert.That(SkyMath.InsideGrid(g, ro, dd), Is.False, $"outside ({outx},{outy}) rot {rot} at {ra},{dec}");
                }
            }
        }
    }

    [Test]
    public void The_pixel_offset_matches_the_oracle_including_which_way_is_up_and_right()
    {
        var g = MakeGrid(rot: 37);
        foreach (var (x, y) in new[] { (100.0, 200.0), (2900.0, 1900.0), (1500.0, 100.0), (700.0, 1700.0) })
        {
            var (ra, dec) = PixelToSky(g, x, y);
            Assert.That(SkyMath.TryGridOffset(g, ra, dec, out var right, out var up), Is.True);
            Assert.That(right, Is.EqualTo(x - 1500).Within(0.01));
            Assert.That(up, Is.EqualTo(1000 - y).Within(0.01), "image up is a smaller y");
        }
    }

    [Test]
    public void At_rotation_zero_north_is_up_and_east_is_left_and_the_edges_are_where_the_size_says()
    {
        var g = MakeGrid(dec: 0, ra: 100); // half-height 1000 px * 1.5" = 0.41667 deg, half-width 0.625 deg
        Assert.That(SkyMath.InsideGrid(g, 100, 0.41), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 100, 0.42), Is.False);
        Assert.That(SkyMath.InsideGrid(g, 100, -0.41), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 100.62, 0), Is.True, "east of centre, within the half-width");
        Assert.That(SkyMath.InsideGrid(g, 100.63, 0), Is.False);
        Assert.That(SkyMath.InsideGrid(g, 99.38, 0), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 99.37, 0), Is.False);
        SkyMath.TryGridOffset(g, 100, 0.1, out var right, out var up);
        Assert.That(up, Is.GreaterThan(0), "north is up");
        SkyMath.TryGridOffset(g, 100.1, 0, out right, out up);
        Assert.That(right, Is.LessThan(0), "east is left");
    }

    [Test]
    public void At_rotation_ninety_east_is_up_so_the_long_axis_runs_north_south()
    {
        var g = MakeGrid(dec: 0, ra: 100, rot: 90);
        Assert.That(SkyMath.InsideGrid(g, 100.41, 0), Is.True, "east now spans the 2000 px height");
        Assert.That(SkyMath.InsideGrid(g, 100.42, 0), Is.False);
        Assert.That(SkyMath.InsideGrid(g, 100, 0.62), Is.True, "north now spans the 3000 px width");
        Assert.That(SkyMath.InsideGrid(g, 100, 0.63), Is.False);
        SkyMath.TryGridOffset(g, 100.1, 0, out _, out var up);
        Assert.That(up, Is.GreaterThan(0));
        SkyMath.TryGridOffset(g, 100, 0.1, out var right, out _);
        Assert.That(right, Is.GreaterThan(0), "north is right");
    }

    [Test]
    public void The_margin_shrinks_the_footprint()
    {
        var g = MakeGrid(dec: 0, ra: 100);
        // 0.41 deg north = 984 px of 1000 half-height.
        Assert.That(SkyMath.InsideGrid(g, 100, 0.41, marginPx: 10), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 100, 0.41, marginPx: 30), Is.False);
    }

    [Test]
    public void The_far_side_of_the_sky_and_degenerate_grids_are_never_inside()
    {
        var g = MakeGrid(ra: 10, dec: 20);
        Assert.That(SkyMath.InsideGrid(g, 190, -20), Is.False, "the antipode");
        Assert.That(SkyMath.InsideGrid(g, 100, 0), Is.False, "90 degrees away");
        Assert.That(SkyMath.InsideGrid(MakeGrid(scale: 0), 85.25, -2.458), Is.False);
        Assert.That(SkyMath.InsideGrid(MakeGrid(w: 0), 85.25, -2.458), Is.False);
    }

    [Test]
    public void The_antimeridian_is_no_obstacle()
    {
        var g = MakeGrid(ra: 359.99, dec: 0);
        Assert.That(SkyMath.InsideGrid(g, 0.2, 0), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 359.8, 0), Is.True);
        Assert.That(SkyMath.InsideGrid(g, 1.0, 0), Is.False);
    }

    // ---- coordinates ---------------------------------------------------------------------------------------------

    [TestCase("05 41 00", true, 85.25)]
    [TestCase("05:41:00", true, 85.25)]
    [TestCase("05h41m00s", true, 85.25)]
    [TestCase("05 35 17.3", false, 83.8220833)]
    [TestCase("12", true, 180.0)]
    [TestCase("85.25", false, 85.25)]
    [TestCase("0 0 0", true, 0.0)]
    public void Right_ascension_parses(string text, bool plainIsHours, double expectedDeg)
    {
        Assert.That(SkyMath.TryParseRa(text, plainIsHours, out var deg), Is.True);
        Assert.That(deg, Is.EqualTo(expectedDeg).Within(1e-6));
    }

    [TestCase("-02 27 30", -2.4583333)]
    [TestCase("+41 16 04", 41.2677778)]
    [TestCase("-00 30 00", -0.5)]
    [TestCase("-05:23:28", -5.3911111)]
    [TestCase("12.5", 12.5)]
    public void Declination_parses_and_keeps_its_sign_even_at_zero_degrees(string text, double expected)
    {
        Assert.That(SkyMath.TryParseDec(text, out var d), Is.True);
        Assert.That(d, Is.EqualTo(expected).Within(1e-6));
    }

    [TestCase("")]
    [TestCase("abc")]
    [TestCase("1 2 3 4")]
    [TestCase("--5")]
    [TestCase("95 00 00")]
    public void Garbage_does_not_parse(string text)
    {
        Assert.That(SkyMath.TryParseDec(text, out _), Is.False);
    }

    [Test]
    public void Pointing_comes_from_RA_DEC_first_then_from_OBJCTRA_OBJCTDEC()
    {
        var a = FramePointing.FromHeader(new InitHeader { Object = "x", Ra = 85.25, Dec = -2.4583, ObjctRa = "00 00 00", ObjctDec = "+00 00 00" });
        Assert.That((a.RaDeg, a.DecDeg), Is.EqualTo((85.25, -2.4583)));
        var b = FramePointing.FromHeader(new InitHeader { ObjctRa = "05 41 00", ObjctDec = "-02 27 30" });
        Assert.That(b.RaDeg, Is.EqualTo(85.25).Within(1e-9));
        Assert.That(b.DecDeg, Is.EqualTo(-2.4583333).Within(1e-6));
        var c = FramePointing.FromHeader(new InitHeader { Ra = "05 35 17.3", Dec = "-05 23 28" });
        Assert.That(c.RaDeg, Is.EqualTo(83.8220833).Within(1e-6));
        Assert.That(FramePointing.FromHeader(new InitHeader { Object = "M42" }).HasPosition, Is.False);
        // A header that went through the queue database carries RA/DEC as JsonElement.
        var back = ApiJson.Deserialize<InitHeader>(ApiJson.Serialize(new InitHeader { Ra = 85.25, Dec = -2.4 }))!;
        Assert.That(FramePointing.FromHeader(back).RaDeg, Is.EqualTo(85.25));
    }

    // ---- attribution decisions ----------------------------------------------------------------------------------

    private static readonly DateTimeOffset T0 = new(2026, 10, 1, 0, 0, 0, TimeSpan.Zero);

    private static JoinedProject Project(string id, string target, Grid? grid, int joinedDay = 1, string? name = null) =>
        new(id, id.Replace("prj_", ""), name ?? target + " SHO", target, grid, T0.AddDays(joinedDay));

    private static readonly Grid Horsehead = MakeGrid(85.25, -2.458, 0, 1.5, 3000, 2000);
    private static readonly Grid Orion = MakeGrid(83.82, -5.39, 0, 2.0, 3000, 2000);

    [Test]
    public void A_frame_inside_one_footprint_goes_there_even_when_OBJECT_is_something_else()
    {
        var projects = new[] { Project("prj_hh", "Horsehead Nebula", Horsehead), Project("prj_m42", "M42", Orion) };
        var r = ProjectAttribution.Attribute(new FramePointing("my target 3", 85.3, -2.4), projects);
        Assert.That(r.Project!.ProjectId, Is.EqualTo("prj_hh"));
        Assert.That(r.Reason, Is.EqualTo(AttributionReason.GridFootprint));
        Assert.That(r.Ambiguous, Is.False);
    }

    [Test]
    public void Name_only_matching_works_without_a_position_or_a_grid()
    {
        var projects = new[] { Project("prj_hh", "Horsehead Nebula", null), Project("prj_m42", "M42 - Orion Nebula", null) };
        Assert.That(ProjectAttribution.Attribute(new FramePointing("horsehead nebula", null, null), projects).Project!.ProjectId, Is.EqualTo("prj_hh"));
        var r = ProjectAttribution.Attribute(new FramePointing("Orion Nebula", null, null), projects);
        Assert.That((r.Project!.ProjectId, r.Reason), Is.EqualTo(("prj_m42", AttributionReason.ObjectName)));
        Assert.That(ProjectAttribution.Attribute(new FramePointing("m42", null, null), projects).Project!.ProjectId, Is.EqualTo("prj_m42"));
        Assert.That(ProjectAttribution.Attribute(new FramePointing("M 42", null, null), projects).Project!.ProjectId, Is.EqualTo("prj_m42"));
    }

    [TestCase("NGC 7000", "ngc7000", true)]
    [TestCase("NGC-7000", "NGC 7000", true)]
    [TestCase("IC 434", "IC 4340", false)]
    [TestCase("", "anything", false)]
    [TestCase(null, "anything", false)]
    [TestCase("Barnard 33", "Horsehead Nebula", false)]
    public void Name_matching_ignores_case_spaces_and_punctuation_but_not_digits(string? obj, string target, bool expected) =>
        Assert.That(ProjectAttribution.NameMatches(obj, Project("prj_a", target, null)), Is.EqualTo(expected));

    [Test]
    public void Name_and_footprint_together_beat_a_footprint_alone()
    {
        // Two overlapping projects; the frame is in both footprints but is named for one.
        var a = Project("prj_a", "Horsehead Nebula", Horsehead, joinedDay: 1);
        var b = Project("prj_b", "Flame Nebula", MakeGrid(85.4, -2.5, 0, 1.5, 3000, 2000), joinedDay: 2);
        var r = ProjectAttribution.Attribute(new FramePointing("Flame Nebula", 85.3, -2.45), new[] { a, b });
        Assert.That(r.Project!.ProjectId, Is.EqualTo("prj_b"));
        Assert.That(r.Reason, Is.EqualTo(AttributionReason.ObjectAndFootprint));
        Assert.That(r.Candidates.Select(c => c.ProjectId), Is.EqualTo(new[] { "prj_b", "prj_a" }));
    }

    [Test]
    public void A_tie_goes_to_the_active_project_and_is_otherwise_ambiguous_with_both_candidates_offered()
    {
        var a = Project("prj_a", "Horsehead Nebula", Horsehead, joinedDay: 1);
        var b = Project("prj_b", "Flame Nebula", MakeGrid(85.4, -2.5, 0, 1.5, 3000, 2000), joinedDay: 2);
        var frame = new FramePointing("unknown", 85.3, -2.45);

        var amb = ProjectAttribution.Attribute(frame, new[] { a, b });
        Assert.That(amb.Project, Is.Null);
        Assert.That(amb.Ambiguous, Is.True);
        Assert.That(amb.Candidates.Select(c => c.ProjectId), Is.EquivalentTo(new[] { "prj_a", "prj_b" }));

        var resolved = ProjectAttribution.Attribute(frame, new[] { a, b }, activeProjectId: "prj_a");
        Assert.That(resolved.Project!.ProjectId, Is.EqualTo("prj_a"));
        Assert.That(resolved.Ambiguous, Is.False);
    }

    [Test]
    public void Modes_active_only_auto_and_auto_with_fallback()
    {
        var projects = new[] { Project("prj_hh", "Horsehead Nebula", Horsehead), Project("prj_m42", "M42", Orion) };
        var elsewhere = new FramePointing("Andromeda", 10.7, 41.3);
        var inHorsehead = new FramePointing("whatever", 85.25, -2.45);

        var nearOrion = new FramePointing("whatever", 83.0, -6.5); // outside M42's footprint, well inside its on-target radius

        Assert.That(ProjectAttribution.Attribute(nearOrion, projects, "prj_m42", AttributionMode.ActiveOnly).Project!.ProjectId, Is.EqualTo("prj_m42"));
        Assert.That(ProjectAttribution.Attribute(inHorsehead, projects, null, AttributionMode.ActiveOnly).Project, Is.Null);
        Assert.That(ProjectAttribution.Attribute(inHorsehead, projects, "ghost", AttributionMode.ActiveOnly).Project, Is.Null, "an active id we have not joined");
        var other = ProjectAttribution.Attribute(elsewhere, projects, "prj_m42", AttributionMode.ActiveOnly);
        Assert.That((other.Project, other.Reason), Is.EqualTo(((JoinedProject?)null, AttributionReason.OtherTarget)), "active only, but another target");

        Assert.That(ProjectAttribution.Attribute(elsewhere, projects, "prj_m42", AttributionMode.Auto).Project, Is.Null);
        var fb = ProjectAttribution.Attribute(nearOrion, projects, "prj_m42", AttributionMode.AutoWithActiveFallback);
        Assert.That((fb.Project!.ProjectId, fb.Reason), Is.EqualTo(("prj_m42", AttributionReason.ActiveProject)));
        var away = ProjectAttribution.Attribute(elsewhere, projects, "prj_m42", AttributionMode.AutoWithActiveFallback);
        Assert.That((away.Project, away.Reason), Is.EqualTo(((JoinedProject?)null, AttributionReason.OtherTarget)), "no fallback for another target");
        Assert.That(ProjectAttribution.Attribute(inHorsehead, projects, "prj_m42", AttributionMode.AutoWithActiveFallback).Project!.ProjectId, Is.EqualTo("prj_hh"),
            "a real match beats the fallback");
    }

    // ---- frames of another target (the user moved on) -----------------------------------------------------------

    // M33 as the live project has it: 3840 x 2160 px at 1.1876"/px (1.27 x 0.71 deg), half-diagonal 0.73 deg.
    private static readonly Grid M33 = MakeGrid(23.47155, 30.66404, 31.42, 1.1876, 3840, 2160);

    [Test]
    public void A_frame_of_another_target_is_refused_instead_of_falling_back_to_the_active_project()
    {
        var projects = new[] { Project("prj_m33", "M33", M33) };
        // The Bubble Nebula frames that reached M33 on 2026-10-21: RA/DEC and OBJCTRA/OBJCTDEC both on the Bubble.
        var bubble = FramePointing.FromHeader(new InitHeader { Object = "Bubble Nebula", Ra = 350.204, Dec = 61.201, ObjctRa = "23 20 48", ObjctDec = "+61 12 06" });
        foreach (var mode in new[] { AttributionMode.ActiveOnly, AttributionMode.AutoWithActiveFallback })
        {
            var r = ProjectAttribution.Attribute(bubble, projects, "prj_m33", mode);
            Assert.That((r.Project, r.Reason, r.Ambiguous), Is.EqualTo(((JoinedProject?)null, AttributionReason.OtherTarget, false)), mode.ToString());
        }
        Assert.That(ProjectAttribution.SeparationDeg(bubble, projects[0]), Is.GreaterThan(30));
    }

    [Test]
    public void A_frame_of_the_target_still_goes_to_the_active_project_when_only_part_of_its_header_agrees()
    {
        var m33 = Project("prj_m33", "M33", M33);
        var projects = new[] { m33 };
        AttributionResult Fallback(FramePointing f) => ProjectAttribution.Attribute(f, projects, "prj_m33", AttributionMode.AutoWithActiveFallback);

        // Off the grid but within its half-diagonal plus a degree (a typed centre 20' out, a slightly different framing).
        Assert.That(Fallback(new FramePointing("M33_FRA600", 25.0, 30.66)).Reason, Is.EqualTo(AttributionReason.ActiveProject));
        // The mount lost its sync (RA/DEC 3 deg out) while OBJCTRA/OBJCTDEC stayed on the target: the target's position counts.
        var lostSync = FramePointing.FromHeader(new InitHeader { Object = "M33_UC131", Ra = 27.0, Dec = 31.5, ObjctRa = "01 33 51", ObjctDec = "+30 39 36" });
        Assert.That(Fallback(lostSync).Reason, Is.EqualTo(AttributionReason.ActiveProject));
        // OBJECT names the project: the name is taken at its word, wherever the mount says it was.
        Assert.That(Fallback(new FramePointing("M33", 120.0, -10.0)).Project!.ProjectId, Is.EqualTo("prj_m33"));
        // No position at all: nothing says it is elsewhere.
        Assert.That(Fallback(new FramePointing("Bubble Nebula", null, null)).Reason, Is.EqualTo(AttributionReason.ActiveProject));
        // A project without a grid cannot tell either.
        Assert.That(ProjectAttribution.IsOtherTarget(new FramePointing("x", 350, 61), Project("prj_n", "N", null)), Is.False);
    }

    [Test]
    public void The_on_target_radius_is_the_half_diagonal_plus_a_degree_and_at_least_three_degrees_while_the_grid_waits_for_first_light()
    {
        var m33 = Project("prj_m33", "M33", M33);
        Assert.That(ProjectAttribution.OnTargetRadiusDeg(m33), Is.EqualTo(1.0 + Math.Sqrt(3840.0 * 3840 + 2160.0 * 2160) / 2 * 1.1876 / 3600).Within(1e-9));
        var pending = m33 with { GridPending = true };
        Assert.That(ProjectAttribution.OnTargetRadiusDeg(pending), Is.EqualTo(3.0));
        var twoAndAHalfOut = new FramePointing("x", 23.47155, 30.66404 + 2.5);
        Assert.That(ProjectAttribution.IsOtherTarget(twoAndAHalfOut, m33), Is.True);
        Assert.That(ProjectAttribution.IsOtherTarget(twoAndAHalfOut, pending), Is.False, "a placeholder centre is given more room");
        // A mosaic's derived grid is large; its radius grows with it.
        var mosaic = Project("prj_mos", "Veil", MakeGrid(312.0, 31.0, 0, 2.0, 30000, 20000));
        Assert.That(ProjectAttribution.IsOtherTarget(new FramePointing("x", 312.0, 36.5), mosaic), Is.False);
    }

    [Test]
    public void Separation_is_the_angle_on_the_sky()
    {
        Assert.That(SkyMath.SeparationDeg(10, 0, 11, 0), Is.EqualTo(1.0).Within(1e-9));
        Assert.That(SkyMath.SeparationDeg(0, 60, 1, 60), Is.EqualTo(0.5).Within(1e-4), "RA shrinks by cos(dec)");
        Assert.That(SkyMath.SeparationDeg(359.5, 0, 0.5, 0), Is.EqualTo(1.0).Within(1e-9), "across RA 0");
        Assert.That(SkyMath.SeparationDeg(0, 89.9, 180, 89.9), Is.EqualTo(0.2).Within(1e-9), "over the pole");
        Assert.That(SkyMath.SeparationDeg(5, 5, 5, 5), Is.EqualTo(0.0));
    }

    [Test]
    public void The_header_gives_the_target_position_beside_the_mounts()
    {
        var f = FramePointing.FromHeader(new InitHeader { Ra = 27.0, Dec = 31.5, ObjctRa = "01 33 51", ObjctDec = "+30 39 36" });
        Assert.That((f.RaDeg, f.DecDeg), Is.EqualTo((27.0, 31.5)));
        Assert.That(f.TargetRaDeg, Is.EqualTo(23.4625).Within(1e-9));
        Assert.That(f.Positions().Count(), Is.EqualTo(2));
        var only = FramePointing.FromHeader(new InitHeader { ObjctRa = "01 33 51", ObjctDec = "+30 39 36" });
        Assert.That(only.Positions().Count(), Is.EqualTo(1), "the target's position is not counted twice");
    }

    [Test]
    public void No_position_and_no_name_means_not_attributed()
    {
        var projects = new[] { Project("prj_hh", "Horsehead Nebula", Horsehead) };
        var r = ProjectAttribution.Attribute(new FramePointing(null, null, null), projects);
        Assert.That((r.Project, r.Reason, r.Ambiguous), Is.EqualTo(((JoinedProject?)null, AttributionReason.None, false)));
        Assert.That(ProjectAttribution.Attribute(new FramePointing("x", 1, 1), Array.Empty<JoinedProject>()).Project, Is.Null);
    }

    [Test]
    public void A_nearby_but_outside_pointing_does_not_match_and_a_rotated_grid_is_respected()
    {
        // Rotated 90 degrees: wide along north-south. 0.55 deg east is outside (east spans only +-0.4167).
        var rotated = Project("prj_r", "Rotated", MakeGrid(100, 0, 90));
        Assert.That(ProjectAttribution.Attribute(new FramePointing("x", 100.55, 0), new[] { rotated }).Project, Is.Null);
        Assert.That(ProjectAttribution.Attribute(new FramePointing("x", 100, 0.55), new[] { rotated }).Project, Is.Not.Null);
    }
}
