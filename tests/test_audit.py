from iroptimizer import audit

SCHEME = "Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced) *"
PARK = "    Current AC Power Setting Index: 0x00000064\n    Current DC Power Setting Index: 0x00000005"
REG = {"AutoGameModeEnabled": 1, "HwSchMode": 2, "OverlayTestMode": None, "HistoricalCaptureEnabled": 0,
       "DirectXUserGlobalSettings": "SwapEffectUpgradeEnable=1;", "Enabled": 1}
SNAP = {"renderer_file": "rendererDX11Monitor.ini",
        "renderer": {"Graphics Options": {"MaxCarsToDraw": "40", "DynamicShadowMaps": "1", "SSAO": "1"},
                     "Replay Graphics": {"MaxCarsToDraw": "20", "DynamicShadowMaps": "1"}}}
NV = {"global": {0x1194F158: 0, 0x00A879CF: 0x60925292, 0x00198FFF: 1, 0x1057EB71: 0},
      "app": {0x10835002: 0, 0x0005F543: 2}}


def fake_run(args):
    return SCHEME if "/getactivescheme" in args else PARK


def fake_reg(root, path, name):
    return REG.get(name)


def sample():
    return audit.run(snapshot=lambda: SNAP,
                     windows=lambda: audit.windows_items(run=fake_run, reg=fake_reg, timer=lambda: 1.0),
                     nvidia=lambda: audit.nvidia_items(read=lambda: NV))


def by_name(items):
    return {i["name"]: i for i in items}


def test_windows_items_read_values():
    w = by_name(audit.windows_items(run=fake_run, reg=fake_reg, timer=lambda: 0.5))
    assert w["Power plan"]["value"] == "Balanced" and w["Power plan"]["level"] == "test"
    assert w["Power plan"]["test"]["auto"] == "powerplan"
    assert w["Core parking (minimum cores unparked)"]["value"] == "100%"
    assert w["Hardware-accelerated GPU scheduling"]["value"] == "On"
    assert w["Multiplane overlay (MPO)"]["value"] == "On (Windows default)"
    assert w["Game Bar background recording"]["level"] == "ok"
    assert w["Optimizations for windowed games"]["value"] == "On"
    assert w["Timer resolution"]["value"] == "0.50 ms"


def test_power_saver_is_bad_and_unreadable_is_not_flagged():
    saver = lambda a: "GUID: a1841308-3541-4fab-bc81-f71556f20b4a  (Power saver)" if "/getactivescheme" in a else ""
    w = by_name(audit.windows_items(run=saver, reg=fake_reg, timer=lambda: None))
    assert w["Power plan"]["level"] == "bad"
    assert w["Core parking (minimum cores unparked)"]["value"] == "Couldn't read"
    assert w["Timer resolution"]["value"] == "Couldn't read" and w["Timer resolution"]["level"] == "info"


def test_nvidia_items_prefer_the_iracing_profile_and_flag_shader_cache_off():
    n = by_name(audit.nvidia_items(read=lambda: NV))
    assert n["Low latency mode"]["value"] == "Ultra (iRacing profile)" and n["Low latency mode"]["test"]
    assert n["Vertical sync"]["value"] == "Use the app setting (global)"
    assert n["G-SYNC mode"]["value"] == "Off (global)"
    assert n["Max frame rate"]["value"] == "Off (iRacing profile)"
    fixed = by_name(audit.nvidia_items(read=lambda: {"global": {0x10A879CF: 4}, "app": {}}))
    assert fixed["Monitor technology"]["value"] == "Fixed refresh (global)"
    assert n["Texture filtering quality"]["value"] == "Couldn't read"
    nd = by_name(audit.nvidia_items(read=lambda: {"global": {}, "app": {}, "profile": "not found (NVAPI x)",
                                                 "codes": {0x10835002: [-160, None], 0x00198FFF: [-5, None]}}))
    assert nd["Max frame rate"]["value"] == "Driver default"
    assert nd["Shader cache"]["value"] == "Couldn't read" and "-5" in nd["Shader cache"]["note"]
    assert nd["iRacing driver profile"]["value"] == "Not found"
    off = by_name(audit.nvidia_items(read=lambda: {"global": {0x00198FFF: 0}, "app": {}}))
    assert off["Shader cache"]["level"] == "bad"
    err = audit.nvidia_items(read=lambda: {"error": "The NVIDIA driver's NVAPI isn't installed."})
    assert err[0]["value"].startswith("The NVIDIA driver") and err[0]["level"] == "info"


def test_iracing_items_show_replay_differences():
    i = by_name(audit.iracing_items(SNAP))
    assert i["MaxCarsToDraw"]["value"] == "40 race, 20 replay" and i["MaxCarsToDraw"]["level"] == "warn"
    assert "close iRacing, then press Read again" in i["Replay and race graphics differ (settings file)"]["note"]
    assert i["DynamicShadowMaps"]["value"] == "1" and i["DynamicShadowMaps"]["level"] == "test"
    assert i["SSAO"]["test"] == {"a": "SSAO 1", "b": "SSAO lower"}
    assert audit.iracing_items(None)[0]["value"] == "Couldn't read"


def test_run_counts_and_survives_a_failing_part():
    r = sample()
    assert r["counts"]["warn"] == 1 and r["counts"]["attention"] == 1 and r["counts"]["bad"] == 0
    assert r["counts"]["test"] >= 5
    def boom():
        raise RuntimeError("x")
    r = audit.run(snapshot=lambda: SNAP, windows=boom, nvidia=lambda: [])
    assert r["items"][0]["name"] == "Audit error"


def test_parsers():
    assert audit.parse_active_scheme(SCHEME) == ("381b4222-f694-41f0-9685-ff5bb260df2e", "Balanced")
    assert audit.parse_active_scheme("") == (None, None)
    assert audit.parse_ac_value(PARK) == 100 and audit.parse_ac_value("nothing") is None
    assert audit.read_nvidia_profiles().get("error") or True  # never raises, on any OS


def test_api_audit(tmp_path):
    from test_ui import make_api
    api = make_api(tmp_path, settings_snapshot=lambda: SNAP, audit_parts={
        "windows": lambda: audit.windows_items(run=fake_run, reg=fake_reg, timer=lambda: 1.0),
        "nvidia": lambda: audit.nvidia_items(read=lambda: NV)})
    r = api.audit()
    assert r["ok"] and any(i["name"] == "MaxCarsToDraw" for i in r["items"])


def test_mismatch_ignores_differences_that_cant_matter():
    from iroptimizer import config_reader
    race = {"MotionBlurStrength": "0", "MotionBlurDrivingCams": "1", "MotionBlurBroadcastCams": "0",
            "DynamicShadowMaps": "0", "DynamicShadowTrim": "1", "LODMinFPSTarget": "80",
            "HideCockpitObstructions": "1", "DriverHands": "0", "SteeringWheel": "1", "MaxCarsToDraw": "20"}
    rep = {**race, "MotionBlurDrivingCams": "0", "MotionBlurBroadcastCams": "1", "DynamicShadowTrim": "0",
           "LODMinFPSTarget": "60", "HideCockpitObstructions": "0", "DriverHands": "1", "SteeringWheel": "0"}
    snap = {"renderer": {"Graphics Options": race, "Replay Graphics": rep}}
    assert config_reader.replay_mismatch(snap, low1=85) == []
    assert [d["key"] for d in config_reader.replay_mismatch(snap, low1=70)] == ["LODMinFPSTarget"]
    assert [d["key"] for d in config_reader.replay_mismatch(snap)] == ["LODMinFPSTarget"]  # 1% low unknown
    assert {d["key"] for d in config_reader.view_toggle_diffs(snap)} == {"HideCockpitObstructions", "DriverHands",
                                                                         "SteeringWheel"}
    # with blur or dynamic shadows on, the dependent keys count again
    on = {"renderer": {"Graphics Options": {**race, "MotionBlurStrength": "2", "DynamicShadowMaps": "1"},
                       "Replay Graphics": {**rep, "MotionBlurStrength": "2", "DynamicShadowMaps": "1"}}}
    keys = {d["key"] for d in config_reader.replay_mismatch(on, low1=85)}
    assert keys == {"MotionBlurDrivingCams", "MotionBlurBroadcastCams", "DynamicShadowTrim"}
    assert "settings file" in config_reader.mismatch_text(config_reader.replay_mismatch(on, low1=85))
    items = {i["name"]: i for i in audit.iracing_items(snap, low1=85)}
    assert "Replay and race graphics differ (settings file)" not in items
    assert items["Cockpit view toggles differ in replays"]["level"] == "info"
