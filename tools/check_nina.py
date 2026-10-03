"""Bringing a N.I.N.A. profile across.

    python tools/check_nina.py

Against a profile written here in the shape N.I.N.A. writes them - the same
namespaces, the same nesting, the same "No_Device" for an empty slot - so the
importer is exercised on the real format without depending on N.I.N.A. being
installed on the machine running the check.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import ninaimport                                # noqa: E402
from astrocontrol.config import Config                             # noqa: E402
from astrocontrol.equipment import EquipmentStore                  # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


P = "http://schemas.datacontract.org/2004/07/NINA.Profile"
E = "http://schemas.datacontract.org/2004/07/NINA.Core.Model.Equipment"
I = "http://www.w3.org/2001/XMLSchema-instance"


def filter_info(name, position, offset=0, focus=False):
    return (f'<a:FilterInfo><a:_autoFocusFilter>{"true" if focus else "false"}</a:_autoFocusFilter>'
            f'<a:_focusOffset>{offset}</a:_focusOffset><a:_name>{name}</a:_name>'
            f'<a:_position>{position}</a:_position></a:FilterInfo>')


PROFILE = f"""<Profile xmlns="{P}" xmlns:i="{I}">
<AstrometrySettings><Elevation>500</Elevation><Latitude>31.5475</Latitude><Longitude>-99.3822</Longitude></AstrometrySettings>
<CameraSettings><BayerPattern>Auto</BayerPattern><BitDepth>16</BitDepth><Gain>100</Gain><Id>ASCOM.ASICamera2.Camera</Id>
<LastDeviceName>ZWO ASI6200MM Pro (ASCOM)</LastDeviceName><MaxFlatExposureTime>20</MaxFlatExposureTime>
<MinFlatExposureTime>0</MinFlatExposureTime><Offset>50</Offset><PixelSize>3.76</PixelSize><Temperature>-10</Temperature></CameraSettings>
<DomeSettings><Id>No_Device</Id><LastDeviceName/></DomeSettings>
<FilterWheelSettings><FilterWheelFilters xmlns:a="{E}">{filter_info("L", 0)}{filter_info("R", 1, -20)}{filter_info("Ha", 2, 140, True)}</FilterWheelFilters>
<Id>ASCOM.EFW2.FilterWheel</Id><LastDeviceName>ZWO FilterWheel (ASCOM)</LastDeviceName></FilterWheelSettings>
<FlatDeviceSettings><Id>ASCOM.DeepSkyDad.FP.CoverCalibrator1</Id><LastDeviceName>ASCOM DeepSkyDad FP (ASCOM)</LastDeviceName></FlatDeviceSettings>
<FlatWizardSettings><HistogramMeanTarget>0.5</HistogramMeanTarget><HistogramTolerance>0.1</HistogramTolerance></FlatWizardSettings>
<FocuserSettings><AutoFocusCurveFitting>HYPERBOLIC</AutoFocusCurveFitting><AutoFocusExposureTime>4</AutoFocusExposureTime>
<AutoFocusInitialOffsetSteps>4</AutoFocusInitialOffsetSteps><AutoFocusMethod>STARHFR</AutoFocusMethod>
<AutoFocusNumberOfFramesPerPoint>1</AutoFocusNumberOfFramesPerPoint><AutoFocusStepSize>50</AutoFocusStepSize>
<AutoFocusTotalNumberOfAttempts>1</AutoFocusTotalNumberOfAttempts><BacklashIn>25</BacklashIn><BacklashOut>10</BacklashOut>
<Id>ASCOM.EAF.Focuser</Id><LastDeviceName>ZWO Focuser (ASCOM)</LastDeviceName><UseFilterWheelOffsets>true</UseFilterWheelOffsets></FocuserSettings>
<FramingAssistantSettings><CameraHeight>6388</CameraHeight><CameraWidth>9576</CameraWidth><LastRotationAngle>97.8</LastRotationAngle></FramingAssistantSettings>
<GuiderSettings><DitherPixels>5</DitherPixels><DitherRAOnly>false</DitherRAOnly><GuiderName>PHD2_Single</GuiderName>
<PHD2Path>C:\\Program Files (x86)\\PHDGuiding2\\phd2.exe</PHD2Path><PHD2ServerPort>4400</PHD2ServerPort><PHD2ServerUrl>localhost</PHD2ServerUrl>
<SettlePixels>1.5</SettlePixels><SettleTime>10</SettleTime><SettleTimeout>40</SettleTimeout></GuiderSettings>
<Id>abc-123</Id>
<ImageFileSettings><FilePath>C:\\Users\\me\\Dropbox\\frames\\</FilePath><FilePattern>$$TARGETNAME$$</FilePattern></ImageFileSettings>
<LastUsed>2026-09-13T18:43:13.6634193-07:00</LastUsed>
<MeridianFlipSettings><MinutesAfterMeridian>5</MinutesAfterMeridian><PauseTimeBeforeMeridian>0</PauseTimeBeforeMeridian><Recenter>true</Recenter></MeridianFlipSettings>
<Name>Default</Name>
<PlateSolveSettings><ASTAPLocation>C:\\Program Files\\astap\\astap.exe</ASTAPLocation><AstrometryAPIKey>sekret</AstrometryAPIKey>
<AstrometryURL>http://nova.astrometry.net</AstrometryURL><DownSampleFactor>0</DownSampleFactor><ExposureTime>5</ExposureTime>
<MaxObjects>500</MaxObjects><NumberOfAttempts>10</NumberOfAttempts><PlateSolverType>ASTAP</PlateSolverType>
<SearchRadius>30</SearchRadius><Threshold>1</Threshold></PlateSolveSettings>
<RotatorSettings><Id>No_Device</Id><LastDeviceName/></RotatorSettings>
<SafetyMonitorSettings><Id>No_Device</Id></SafetyMonitorSettings>
<SequenceSettings><CoolCameraAtSequenceStart>false</CoolCameraAtSequenceStart><DoMeridianFlip>true</DoMeridianFlip>
<ParkMountAtSequenceEnd>true</ParkMountAtSequenceEnd><WarmCamAtSequenceEnd>true</WarmCamAtSequenceEnd></SequenceSettings>
<SwitchSettings><Id>No_Device</Id></SwitchSettings>
<TelescopeSettings><FocalLength>389</FocalLength><Id>ASCOM.SoftwareBisque.Telescope</Id>
<LastDeviceName>Driver for telescope connected through TheSky (ASCOM)</LastDeviceName><SettleTime>0</SettleTime></TelescopeSettings>
</Profile>"""

folder = Path(tempfile.mkdtemp())
(folder / "abc-123.profile").write_text(PROFILE, encoding="utf-8")
(folder / "old.profile").write_text(
    PROFILE.replace("abc-123", "old").replace("2026-09-13", "2025-01-01"), encoding="utf-8")

print("-- finding profiles --")
found = ninaimport.profiles(folder)
case("every profile is found, most recently used first",
     [p["id"] for p in found] == ["abc-123", "old"], str([p["id"] for p in found]))
case("...nothing found where there is nothing", ninaimport.profiles(folder / "nope") == [])

print("\n-- reading one --")
plan = ninaimport.read(folder / "abc-123.profile")
s = plan["settings"]
case("the site comes across, and wins over the mount",
     s["site"]["latitude"] == 31.5475 and s["site"]["longitude"] == -99.3822
     and s["site"]["elevation"] == 500 and s["site"]["useMount"] is False)
case("focal length, pixel size and the sensor from three different places",
     s["optics"] == {"focalLength": 389.0, "pixelSize": 3.76,
                     "sensorWidth": 9576, "sensorHeight": 6388}, str(s["optics"]))
case("gain, offset and setpoint",
     s["camera"]["gain"] == 100 and s["camera"]["offset"] == 50
     and s["camera"]["setpoint"] == -10.0)
case("filters in slot order, folded to one letter, with offsets and the autofocus filter",
     s["camera"]["filterNames"] == ["L", "R", "H"]
     and s["sequencer"]["filterOffsets"] == {"R": -20, "H": 140}
     and s["sequencer"]["autofocusFilter"] == "H", str(s["camera"]["filterNames"]))
case("a Bayer pattern of Auto does not make the camera colour",
     "colour" not in s["camera"])
case("focus: exposure, step, points either side, curve, backlash, offsets on",
     s["sequencer"]["focusExposure"] == 4.0 and s["sequencer"]["focusStepSize"] == 50
     and s["sequencer"]["focusPoints"] == 9 and s["sequencer"]["focusMethod"] == "hyperbolic"
     and s["sequencer"]["focusBacklash"] == 25 and s["sequencer"]["useFilterOffsets"] is True,
     str({k: v for k, v in s["sequencer"].items() if k.startswith("focus")}))
case("the run: park at end, cool/warm, flip and its timing",
     s["sequencer"]["parkAtEnd"] is True and s["camera"]["coolAtStart"] is False
     and s["camera"]["warmAtEnd"] is True and s["sequencer"]["meridianFlipEnabled"] is True
     and s["sequencer"]["flipAfterMinutes"] == 5.0 and s["sequencer"]["flipSolve"] is True)
case("guiding: dither and settle, and PHD2's path",
     s["guiding"]["ditherPixels"] == 5.0 and s["guiding"]["settlePixels"] == 1.5
     and s["guiding"]["settleTime"] == 10.0 and s["guiding"]["settleTimeout"] == 40.0
     and s["guiding"]["phd2Path"].endswith("phd2.exe"))
case("plate solving: ASTAP, radius, attempts, tolerance, the key",
     s["solver"]["astapPath"].endswith("astap.exe") and s["solver"]["searchRadius"] == 30.0
     and s["solver"]["attempts"] == 10 and s["solver"]["tolerance"] == 1.0
     and s["solver"]["astrometryKey"] == "sekret" and "astrometryUrl" not in s["solver"],
     str(s["solver"]))
case("the image folder", s["capture"]["rootDirectory"] == "C:\\Users\\me\\Dropbox\\frames\\")
case("flats: the histogram target as ADU at the camera's bit depth",
     s["calibration"]["flatTargetAdu"] == round(0.5 * 65535)
     and s["calibration"]["flatTolerancePercent"] == 10.0
     and s["calibration"]["flatMaxExposure"] == 20.0 and "flatMinExposure" not in s["calibration"])

d = plan["devices"]
case("every ASCOM slot is remembered, and PHD2 by its address",
     d["camera"]["driverId"] == "ASCOM.ASICamera2.Camera"
     and d["filterwheel"]["driverId"] == "ASCOM.EFW2.FilterWheel"
     and d["focuser"]["driverId"] == "ASCOM.EAF.Focuser"
     and d["mount"]["driverId"] == "ASCOM.SoftwareBisque.Telescope"
     and d["flatpanel"]["driverId"] == "ASCOM.DeepSkyDad.FP.CoverCalibrator1"
     and d["guider"] == {"backend": "phd2", "driverId": "127.0.0.1:4400",
                         "name": "PHD2 (127.0.0.1:4400)"}, str(sorted(d)))
case("...and an empty slot is left empty", "rotator" not in d and "dome" not in d)
case("what is left out is said, with a reason",
     any("rotation angle" in line for line in plan["leftOut"])
     and any("File pattern" in line for line in plan["leftOut"]))
case("every mapped value says where it came from",
     all(f"{sec}.{key}" in plan["from"] for sec, vals in s.items() for key in vals))

print("\n-- carrying it out --")


class Rig:
    def __init__(self, config):
        self.id = "main"
        self.name = "Telescope 1"
        self.config = config


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("sequencer", {"filterOffsets": {"OIII": 90}})
equipment = EquipmentStore(Path(tempfile.mkdtemp()) / "equipment.json")
rig = Rig(config)
done = ninaimport.apply(plan, config, rig, equipment)
case("every section is written",
     sorted(done["settings"]) == sorted(s), str(sorted(done["settings"])))
case("...and reads back", config.get("optics", "focalLength") == 389.0
     and config.get("camera", "filterNames") == ["L", "R", "H"]
     and config.get("solver", "astapPath").endswith("astap.exe"))
case("focus offsets merge onto what was there, in the one spelling",
     config.get("sequencer", "filterOffsets") == {"O": 90, "R": -20, "H": 140},
     str(config.get("sequencer", "filterOffsets")))
remembered = equipment.rig("main")["devices"]
case("the drivers are remembered on the telescope, not connected",
     remembered["camera"]["driverId"] == "ASCOM.ASICamera2.Camera"
     and remembered["guider"]["backend"] == "phd2"
     and sorted(done["devices"]) == sorted(d), str(sorted(remembered)))

config2 = Config(Path(tempfile.mkdtemp()) / "settings.json")
part = ninaimport.apply(plan, config2, Rig(config2), equipment, ["guiding"], False)
case("a choice of sections writes only those, and no drivers",
     list(part["settings"]) == ["guiding"] and part["devices"] == []
     and config2.get("optics", "focalLength") is None)

# A plugin that keeps control characters in its settings (Sequencer Powerups'
# DockableExprs) leaves references like &#x1; in the file. N.I.N.A. reads them
# back; a conforming XML parser refuses the whole profile, which used to look
# like there being no profile at all.
plugged = Path(tempfile.mkdtemp())
(plugged / "abc.profile").write_text(PROFILE.replace(
    "</Profile>",
    '<PluginSettings><Value>max_cycles&#x1;Numeric&#x1;None&#x0;is_powered&#1;</Value>'
    "</PluginSettings></Profile>"), encoding="utf-8")
found = ninaimport.profiles(plugged)
case("a profile carrying control characters from a plugin is still found",
     len(found) == 1, str(found))
case("...and still read", ninaimport.read(plugged / "abc.profile")["settings"] != {})

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
