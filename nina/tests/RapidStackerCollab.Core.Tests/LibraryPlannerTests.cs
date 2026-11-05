// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Library;

namespace RapidStackerCollab.Core.Tests;

public class LibraryPlannerTests
{
    private static readonly LibraryRigProfile Rig = new("ASI2600MM", 1, 64, 48);

    private static int counter;

    private static LibraryCandidate File(string path, string imageTyp, double? exposure = 120, double? gain = 100, double? offset = 50, double? setTemp = -10,
        string? instrume = "ASI2600MM", int binning = 1, int w = 64, int h = 48, string? dateObs = "2026-10-12T02:00:00", long mtime = 1000) =>
        new(path, 6144, mtime + counter++, new InitHeader
        {
            ImageTyp = imageTyp, ExpTime = exposure, Gain = gain, Offset = offset, SetTemp = setTemp, Instrume = instrume, XBinning = binning, YBinning = binning,
            Naxis1 = w, Naxis2 = h, DateObs = dateObs,
        });

    private static IEnumerable<LibraryCandidate> Darks(int n, string prefix = "d", double exposure = 120, double? gain = 100, double? setTemp = -10) =>
        Enumerable.Range(1, n).Select(i => File($"/lib/{prefix}{i}.fits", "Dark", exposure, gain, setTemp: setTemp));

    private static string[] Paths(LibraryPlan p) => p.Upload.Select(i => Path.GetFileName(i.File.Path)).ToArray();

    [Test]
    public void A_usable_master_is_sent_instead_of_the_raw_frames_of_its_group_and_only_the_newest()
    {
        var files = Darks(10).Concat(new[]
        {
            File("/lib/master_old.fits", "Master Dark", dateObs: "2026-09-01T00:00:00"),
            File("/lib/master_new.fits", "Master Dark", dateObs: "2026-10-01T00:00:00"),
        });
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(Paths(plan), Is.EqualTo(new[] { "master_new.fits" }));
        Assert.That(plan.Upload.Single().IsMaster);
        Assert.That(plan.Skipped.Count(s => s.Reason.Contains("older master")), Is.EqualTo(1));
        Assert.That(plan.Skipped.Count(s => s.Reason.Contains("master is sent instead")), Is.EqualTo(10));
        Assert.That(plan.Groups.Single().Chosen, Is.EqualTo("master"));
    }

    [Test]
    public void Without_a_master_the_raw_frames_go_when_there_are_at_least_three()
    {
        var plan = LibraryPlanner.Plan(Darks(5), Rig);
        Assert.That(plan.Upload, Has.Count.EqualTo(5));
        Assert.That(plan.Upload.All(i => !i.IsMaster));
        Assert.That(plan.Groups.Single().Chosen, Is.EqualTo("raw"));
    }

    [Test]
    public void Fewer_than_three_raw_frames_are_skipped_with_a_note()
    {
        var plan = LibraryPlanner.Plan(Darks(2), Rig);
        Assert.That(plan.Upload, Is.Empty);
        Assert.That(plan.Skipped, Has.Count.EqualTo(2));
        Assert.That(plan.Skipped[0].Reason, Does.Contain("at least 3"));
        Assert.That(plan.Groups.Single().Chosen, Is.EqualTo("none"));
    }

    [Test]
    public void Prefer_raw_sends_the_raw_frames_even_when_a_master_exists_and_falls_back_to_the_master_when_there_are_too_few()
    {
        var withMaster = Darks(4).Append(File("/lib/master.fits", "Master Dark"));
        var raw = LibraryPlanner.Plan(withMaster, Rig, new LibraryPlannerOptions(PreferRaw: true));
        Assert.That(raw.Upload, Has.Count.EqualTo(4));
        Assert.That(raw.Upload.All(i => !i.IsMaster));

        var few = Darks(2).Append(File("/lib/master.fits", "Master Dark"));
        var fallback = LibraryPlanner.Plan(few, Rig, new LibraryPlannerOptions(PreferRaw: true));
        Assert.That(Paths(fallback), Is.EqualTo(new[] { "master.fits" }));
    }

    [Test]
    public void A_master_without_INSTRUME_is_not_usable_so_the_raw_frames_go_instead()
    {
        var files = Darks(4).Append(File("/lib/master.fits", "Master Dark", instrume: null));
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(plan.Upload, Has.Count.EqualTo(4));
        Assert.That(plan.Skipped.Single().Reason, Does.Contain("INSTRUME"));
    }

    [Test]
    public void A_master_without_GAIN_is_not_usable_when_the_raw_frames_of_the_same_settings_carry_one()
    {
        var files = Darks(4).Append(File("/lib/master.fits", "Master Dark", gain: null));
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(plan.Upload, Has.Count.EqualTo(4));
        Assert.That(plan.Upload.All(i => !i.IsMaster));
        Assert.That(plan.Skipped.Single().Reason, Does.Contain("no GAIN"));
    }

    [Test]
    public void A_master_without_GAIN_is_usable_when_the_frames_of_that_camera_have_none()
    {
        var files = Darks(4, gain: null).Append(File("/lib/master.fits", "Master Dark", gain: null));
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(Paths(plan), Is.EqualTo(new[] { "master.fits" }));
    }

    [Test]
    public void Another_camera_other_binning_or_other_size_is_skipped_with_the_reason_and_the_camera_compare_ignores_case_and_spaces()
    {
        var files = new[]
        {
            File("/lib/other_cam.fits", "Master Dark", instrume: "QHY268M"),
            File("/lib/bin2.fits", "Master Dark", binning: 2, w: 32, h: 24),
            File("/lib/small.fits", "Master Dark", w: 32, h: 24),
            File("/lib/ok.fits", "Master Bias", exposure: 0, instrume: "  asi2600mm "),
        };
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(Paths(plan), Is.EqualTo(new[] { "ok.fits" }));
        Assert.That(plan.Skipped.Single(s => s.Path.EndsWith("other_cam.fits")).Reason, Does.Contain("QHY268M"));
        Assert.That(plan.Skipped.Single(s => s.Path.EndsWith("bin2.fits")).Reason, Does.Contain("binning 2"));
        Assert.That(plan.Skipped.Single(s => s.Path.EndsWith("small.fits")).Reason, Does.Contain("sensor size"));
    }

    [Test]
    public void A_rig_that_records_no_sensor_size_skips_no_file_for_its_size_and_a_binned_header_matches_the_binned_sensor()
    {
        var plan = LibraryPlanner.Plan(new[] { File("/lib/m.fits", "Master Dark", w: 123, h: 45) }, Rig with { WidthPx = null, HeightPx = null });
        Assert.That(plan.Upload, Has.Count.EqualTo(1));

        var binned = LibraryPlanner.Plan(new[] { File("/lib/b.fits", "Master Dark", binning: 2, w: 32, h: 24) }, new LibraryRigProfile("ASI2600MM", 2, 64, 48));
        Assert.That(binned.Upload, Has.Count.EqualTo(1), "a 2x2 file is half the sensor in each direction");
    }

    [Test]
    public void Lights_and_flats_are_ignored_and_counted_not_listed()
    {
        var files = new[] { File("/lib/l.fits", "Light"), File("/lib/f.fits", "Flat"), File("/lib/mf.fits", "Master Flat"), File("/lib/s.fits", "Snapshot") }
            .Concat(Darks(3));
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(plan.Ignored, Is.EqualTo(4));
        Assert.That(plan.Upload, Has.Count.EqualTo(3));
        Assert.That(plan.Skipped, Is.Empty);
    }

    [Test]
    public void Exposure_to_a_hundredth_and_set_point_to_a_degree_split_and_merge_groups()
    {
        var files = new[]
        {
            // 120.0 and 120.004 are one group; -10.2 and -9.8 are one set-point; -12 and 300 s are others.
            File("/lib/a1.fits", "Dark", exposure: 120.0, setTemp: -10.2),
            File("/lib/a2.fits", "Dark", exposure: 120.004, setTemp: -9.8),
            File("/lib/a3.fits", "Dark", exposure: 120.0, setTemp: -10.0),
            File("/lib/b1.fits", "Dark", exposure: 120.0, setTemp: -12),
            File("/lib/b2.fits", "Dark", exposure: 120.0, setTemp: -12),
            File("/lib/b3.fits", "Dark", exposure: 120.0, setTemp: -12),
            File("/lib/c1.fits", "Dark", exposure: 300),
            File("/lib/c2.fits", "Dark", exposure: 300),
        };
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(plan.Groups, Has.Count.EqualTo(3));
        Assert.That(Paths(plan), Is.EquivalentTo(new[] { "a1.fits", "a2.fits", "a3.fits", "b1.fits", "b2.fits", "b3.fits" }), "the 300 s group has only two frames");
        Assert.That(plan.Groups.Select(g => g.Chosen), Is.EquivalentTo(new[] { "raw", "raw", "none" }));
    }

    [Test]
    public void Gain_offset_and_family_keep_groups_apart()
    {
        var files = Darks(3, gain: 100)
            .Concat(Darks(3, "g", gain: 200))
            .Concat(Enumerable.Range(1, 3).Select(i => File($"/lib/bias{i}.fits", "Bias", 0)))
            .Concat(Enumerable.Range(1, 3).Select(i => File($"/lib/df{i}.fits", "Dark Flat", 3)));
        var plan = LibraryPlanner.Plan(files, Rig);
        Assert.That(plan.Groups.Select(g => g.Family).OrderBy(x => x), Is.EqualTo(new[] { "Bias", "Dark", "Dark", "DarkFlat" }));
        Assert.That(plan.Upload, Has.Count.EqualTo(12));
    }

    [Test]
    public void The_plan_does_not_depend_on_the_order_the_files_are_listed_in()
    {
        var files = Darks(6).Concat(new[] { File("/lib/m1.fits", "Master Bias", 0), File("/lib/m2.fits", "Master Bias", 0, dateObs: "2026-10-01T00:00:00") }).ToList();
        var a = LibraryPlanner.Plan(files, Rig);
        var b = LibraryPlanner.Plan(Enumerable.Reverse(files), Rig);
        Assert.That(b.Upload.Select(i => i.File.Path), Is.EqualTo(a.Upload.Select(i => i.File.Path)));
        Assert.That(b.Skipped.Select(s => s.Reason), Is.EquivalentTo(a.Skipped.Select(s => s.Reason)));
    }
}
