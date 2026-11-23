import datetime
import io
import subprocess

import pytest

from pipeline_tools import check_dv_date
from pipeline_tools import dv_recording_time as dvmeta


def bcd(value):
    return ((value // 10) << 4) | (value % 10)


def date_pack(pack_id, value):
    return bytes(
        [pack_id, 0xFF, bcd(value.day), bcd(value.month), bcd(value.year % 100)]
    )


def time_pack(pack_id, value):
    return bytes(
        [pack_id, 0xFF, bcd(value.second), bcd(value.minute), bcd(value.hour)]
    )


def make_frame(value, source_values=None):
    frame = bytearray(dvmeta.FRAME_BYTES_525_60)
    frame[:4] = b"\x1f\x07\x00\x3f"
    positions = {name: [] for name in dvmeta.SOURCES}
    for name, offset in dvmeta._pack_positions(0, dvmeta.SEQUENCES_525_60):
        positions[name].append(offset)

    if source_values is None:
        source_values = {name: value for name in dvmeta.SOURCES}
    for name, source_value in source_values.items():
        date_id, time_id = dvmeta.SOURCE_PACK_IDS[name]
        frame[positions[name][0]:positions[name][0] + 5] = date_pack(
            date_id, source_value
        )
        frame[positions[name][1]:positions[name][1] + 5] = time_pack(
            time_id, source_value
        )
    return frame


def make_empty_frame():
    frame = bytearray(dvmeta.FRAME_BYTES_525_60)
    frame[:4] = b"\x1f\x07\x00\x3f"
    return frame


def test_inspect_frame_keeps_the_raw_pack_that_produced_the_value():
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    frame = make_frame(value, {"VAUX": value})
    offsets = [
        offset
        for name, offset in dvmeta._pack_positions(0, dvmeta.SEQUENCES_525_60)
        if name == "VAUX"
    ]
    # Put an invalid/default date before the valid one. The diagnostic should
    # show the pack it decoded, not an earlier invalid pack or a later repeat.
    frame[offsets[0]:offsets[0] + 5] = bytes(
        [dvmeta.PACK_VIDEO_DATE, 0xFF, 0x01, 0x01, 0x70]
    )
    frame[offsets[1]:offsets[1] + 5] = date_pack(
        dvmeta.PACK_VIDEO_DATE, value
    )
    frame[offsets[2]:offsets[2] + 5] = date_pack(
        dvmeta.PACK_VIDEO_DATE, datetime.datetime(2005, 5, 24)
    )

    source = dvmeta.inspect_frame(frame, dvmeta.SEQUENCES_525_60)["VAUX"]

    assert source["date"] == (2004, 4, 23)
    assert source["date_offset"] == offsets[1]
    assert source["date_raw"] == date_pack(dvmeta.PACK_VIDEO_DATE, value)


def test_scan_stream_reports_normal_progression_without_a_discontinuity():
    start = datetime.datetime(2004, 4, 23, 17, 34, 19)
    frames = [make_frame(start) for _ in range(30)]
    frames.append(make_frame(start + datetime.timedelta(seconds=1)))

    report = check_dv_date.scan_stream(io.BytesIO(b"".join(frames)))

    assert report["frames"] == 31
    assert report["conflict_frames"] == 0
    assert report["agreement_frames"] == 31
    assert all(
        not report["sources"][name]["discontinuities"]
        for name in dvmeta.SOURCES
    )


def test_scan_stream_reports_clock_jump_and_source_disagreement():
    start = datetime.datetime(2004, 4, 23, 17, 34, 19)
    jumped = start + datetime.timedelta(seconds=10)
    frame_zero = make_frame(start)
    frame_one = make_frame(
        jumped,
        {"VAUX": jumped, "SSYB": jumped, "AAUX": start},
    )

    report = check_dv_date.scan_stream(io.BytesIO(frame_zero + frame_one))

    assert report["conflict_frames"] == 1
    assert report["first_conflict"][0] == 1
    assert report["sources"]["VAUX"]["discontinuities"][0]["frame"] == 1
    assert report["sources"]["SSYB"]["discontinuities"][0]["frame"] == 1
    assert report["sources"]["AAUX"]["discontinuities"] == []


def test_a_source_without_a_current_time_pack_is_not_a_false_conflict():
    start = datetime.datetime(2004, 4, 23, 17, 34, 19)
    next_second = start + datetime.timedelta(seconds=1)
    frame_zero = make_frame(start)
    frame_one = make_frame(next_second, {"VAUX": next_second})

    report = check_dv_date.scan_stream(io.BytesIO(frame_zero + frame_one))

    assert report["conflict_frames"] == 0
    assert report["agreement_frames"] == 1


def test_sources_that_never_overlap_are_compared_on_the_media_timeline():
    start = datetime.datetime(2004, 4, 23, 17, 34, 19)
    frame_zero = make_frame(start, {"VAUX": start})
    frame_one = make_frame(
        start + datetime.timedelta(seconds=10),
        {"AAUX": start + datetime.timedelta(seconds=10)},
    )

    report = check_dv_date.scan_stream(io.BytesIO(frame_zero + frame_one))

    assert report["conflict_frames"] == 0
    assert report["agreement_frames"] == 0
    assert report["timeline_conflicts"][0]["source"] == "AAUX"


def test_scan_stream_rejects_a_partial_final_frame():
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    payload = bytes(make_frame(value)) + b"partial"

    with pytest.raises(check_dv_date.AuditError, match="final partial DV frame"):
        check_dv_date.scan_stream(io.BytesIO(payload))


def test_scan_stream_rejects_a_misaligned_complete_frame():
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    second_frame = make_frame(value)
    second_frame[0] = 0

    with pytest.raises(check_dv_date.AuditError, match="header at frame 1"):
        check_dv_date.scan_stream(io.BytesIO(make_frame(value) + second_frame))


def test_container_demux_preserves_packet_payload_instead_of_remuxing_dv():
    command = check_dv_date.container_command("capture with spaces.avi")

    assert command[-3:] == ["-f", "data", "pipe:1"]
    assert command[command.index("-c:v") + 1] == "copy"
    assert "dv" not in command


def test_unknown_option_is_a_command_line_error(capsys):
    with pytest.raises(SystemExit) as raised:
        check_dv_date.parse_args(["--not-an-option"])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "[ERROR] unknown option: --not-an-option" in error
    assert "Try '" in error and "--help" in error


def test_fast_probe_and_full_scan_share_the_same_frame_parser(tmp_path):
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    capture = tmp_path / "capture.dv"
    capture.write_bytes(make_frame(value))

    assert dvmeta.recording_datetimes(capture) == {
        "VAUX": "2004-04-23 17:34:19",
        "SSYB": "2004-04-23 17:34:19",
        "AAUX": "2004-04-23 17:34:19",
    }
    report = check_dv_date.scan_stream(io.BytesIO(capture.read_bytes()))
    assert all(
        report["sources"][name]["first"][1] == value for name in dvmeta.SOURCES
    )


def test_fast_probe_recovers_metadata_after_an_empty_head(tmp_path):
    value = datetime.datetime(2007, 4, 14, 11, 13, 9)
    capture = tmp_path / "late-metadata.dv"
    capture.write_bytes(b"".join(
        [make_empty_frame() for _ in range(45)] + [make_frame(value)]
    ))

    found, frame_number, head_state = dvmeta.recording_evidence(capture)

    assert frame_number == 45
    assert head_state == "missing"
    assert set(found.values()) == {"2007-04-14 11:13:09"}
    assert set(found) == set(dvmeta.SOURCES)


def test_fast_probe_looks_past_a_transient_head_conflict(tmp_path):
    wrong = datetime.datetime(2007, 9, 30, 18, 31, 16)
    right = datetime.datetime(2007, 11, 24, 14, 53, 14)
    conflicting = make_frame(
        right, {"VAUX": wrong, "SSYB": right, "AAUX": wrong}
    )
    capture = tmp_path / "transient-conflict.dv"
    capture.write_bytes(conflicting + make_frame(right))

    found, frame_number, head_state = dvmeta.recording_evidence(capture)

    assert frame_number == 1
    assert head_state == "conflict"
    assert set(found.values()) == {"2007-11-24 14:53:14"}
    assert set(found) == set(dvmeta.SOURCES)


def test_fast_probe_demuxes_non_riff_containers_with_the_data_muxer(
    tmp_path, monkeypatch
):
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    wrapped = tmp_path / "capture.mkv"
    wrapped.write_bytes(b"\x1aE\xdf\xa3container header")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=bytes(make_frame(value)))

    monkeypatch.setattr(dvmeta.subprocess, "run", run)

    assert dvmeta.recording_datetime(wrapped) == "2004-04-23 17:34:19"
    assert calls[0][-3:] == ["-f", "data", "-"]
    assert "-frames:v" in calls[0]
    assert "dv" not in calls[0]


def test_fast_probe_rejects_partial_output_from_a_failed_demux(
    tmp_path, monkeypatch
):
    value = datetime.datetime(2004, 4, 23, 17, 34, 19)
    wrapped = tmp_path / "capture.mkv"
    wrapped.write_bytes(b"\x1aE\xdf\xa3container header")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=bytes(make_frame(value)))

    monkeypatch.setattr(dvmeta.subprocess, "run", run)

    assert dvmeta.recording_datetimes(wrapped) == {}
