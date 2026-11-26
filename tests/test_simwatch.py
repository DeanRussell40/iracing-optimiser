from iroptimizer import simwatch


def test_redact_keeps_paths_and_switches_hides_tokens():
    args = [r"C:\Program Files (x86)\iRacing\iRacingSim64DX11.exe", "-playreplay",
            r"C:\Users\x\Documents\iRacing\replay\subses12345678.rpy", "-authToken", "abc",
            "session=xyz", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef", "-fullscreen"]
    out, hid = simwatch.redact(args)
    assert hid
    assert out[0] == args[0] and out[1] == "-playreplay" and out[2] == args[2]
    assert out[4].startswith("<redacted") and out[5] == "session=<redacted>" and out[6].startswith("<redacted")
    assert out[7] == "-fullscreen"
    assert simwatch.redact(["a.exe", "-x"]) == (["a.exe", "-x"], False)


def test_scan_logs_each_sim_process_once(tmp_path):
    seen = set()
    procs = lambda: iter([(10, ["sim.exe", "-playreplay", "r.rpy"])])  # noqa: E731
    assert len(simwatch.scan(seen, tmp_path, procs)) == 1
    assert simwatch.scan(seen, tmp_path, procs) == []
    text = (tmp_path / simwatch.LOG).read_text(encoding="utf-8")
    assert "pid=10" in text and "-playreplay" in text and text.count("\n") == 1
