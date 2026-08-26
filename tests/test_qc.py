"""QC decides what is shippable, so its thresholds get tested directly."""

from dailyfive.qc import QCMetrics, _silence, trim_points, verdict


def good(**kw):
    base = dict(duration_s=196.0, lufs_i=-11.2, true_peak_db=-0.9, clip_samples=0,
                silence_ratio=0.03, file_bytes=5_000_000, measured=True)
    return QCMetrics(**{**base, **kw})


def test_healthy_track_passes():
    assert verdict(good()) == ("pass", [])


def test_unmeasured_always_fails():
    v, reasons = verdict(QCMetrics(measured=False))
    assert v == "fail" and "could not be measured" in reasons[0]


def test_clipping_is_caught():
    v, reasons = verdict(good(true_peak_db=0.4))
    assert v == "fail" and any("clipping" in r for r in reasons)


def test_near_silent_render_is_caught():
    v, reasons = verdict(good(lufs_i=-34.0))
    assert v == "fail" and any("near-silent" in r for r in reasons)


def test_crushed_render_is_caught():
    v, reasons = verdict(good(lufs_i=-2.0))
    assert v == "fail" and any("crushed" in r for r in reasons)


def test_truncated_full_song_is_caught():
    v, reasons = verdict(good(duration_s=47.0))
    assert v == "fail" and any("under the 90s floor" in r for r in reasons)


def test_short_cut_has_its_own_floor():
    assert verdict(good(duration_s=44.0), slot_type="short")[0] == "pass"
    assert verdict(good(duration_s=44.0), slot_type="full")[0] == "fail"


def test_duration_far_from_request_is_caught():
    v, reasons = verdict(good(duration_s=20.0), slot_type="short",
                         requested_duration_s=45)
    assert v == "fail" and any("off the 45s requested" in r for r in reasons)


def test_dead_air_is_caught():
    v, reasons = verdict(good(silence_ratio=0.6))
    assert v == "fail" and any("silence" in r for r in reasons)


def test_long_tail_is_caught():
    v, reasons = verdict(good(tail_silence_s=20.0))
    assert v == "fail" and any("dead air at the end" in r for r in reasons)


def test_tiny_file_is_caught():
    v, reasons = verdict(good(file_bytes=1000))
    assert v == "fail" and any("bytes" in r for r in reasons)


def test_every_failure_reason_names_a_number():
    _, reasons = verdict(good(duration_s=47.0, true_peak_db=0.4, lufs_i=-34.0))
    assert len(reasons) >= 3
    assert all(any(ch.isdigit() for ch in r) for r in reasons)


def test_unclosed_silence_runs_to_end_of_file():
    m = QCMetrics(duration_s=100.0)
    _silence("silence_start: 0.0\nsilence_end: 3.2 | silence_duration: 3.2\n"
             "silence_start: 88.0\n", m)
    assert m.lead_silence_s == 3.2
    assert m.tail_silence_s == 12.0
    assert 0.15 < m.silence_ratio < 0.16


def test_trim_points_are_conservative():
    m = QCMetrics(duration_s=100.0, lead_silence_s=3.2, tail_silence_s=12.0)
    start, end = trim_points(m)
    assert start < 3.2          # never cuts into the first transient
    assert end is not None and end > 88.0


def test_no_trim_when_edges_are_tight():
    assert trim_points(QCMetrics(duration_s=100.0, lead_silence_s=0.2,
                                 tail_silence_s=0.3)) == (0.0, None)


def test_decoded_duration_beats_a_lying_container_header():
    """Suno's streaming MP3s over-report length — one by 45%.

    Trusting the header breaks the duration gate in both directions: a
    truncated song passes, and a short-form cut is rejected for a length it
    does not have.
    """
    from dailyfive.qc import _decoded_duration
    stderr = ("frame= 100 time=00:01:00.00 bitrate=N/A\n"
              "frame= 200 time=00:04:13.80 bitrate=N/A\n")
    assert _decoded_duration(stderr) == 253.8


def test_decoded_duration_is_none_when_ffmpeg_printed_no_progress():
    from dailyfive.qc import _decoded_duration
    assert _decoded_duration("no timestamps here") is None


def test_a_header_far_from_the_decode_is_recorded_as_a_note():
    m = QCMetrics(duration_s=253.8, container_duration_s=369.6, lufs_i=-14.4,
                  true_peak_db=-2.0, file_bytes=5_000_000, measured=True)
    v, reasons = verdict(m, slot_type="full")
    assert v == "pass", "a lying header is not itself a reason to reject the audio"
    assert m.duration_s == 253.8, "the decode must win"


def test_the_duration_gate_uses_the_decoded_value():
    """A 47s song whose header claims 200s must still be cut."""
    m = QCMetrics(duration_s=47.0, container_duration_s=200.0, lufs_i=-14.0,
                  true_peak_db=-1.0, file_bytes=5_000_000, measured=True)
    v, reasons = verdict(m, slot_type="full")
    assert v == "fail"
    assert any("47.0s under the 90s floor" in r for r in reasons)
