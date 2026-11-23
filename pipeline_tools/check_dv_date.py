#!/usr/bin/env python3
"""Audit recording-date metadata throughout a native DV capture.

This is the full-file companion to ``dv_recording_time.py``.  The batch tool
reads only the first frame because it needs a fast timestamp for sorting;
this diagnostic reads every frame when that first-frame evidence disagrees or
when a tape may contain a recording-time discontinuity.

Container input is demuxed with FFmpeg's data muxer.  Its DV muxer must not be
used here: it regenerates VAUX and subcode metadata even with ``-c:v copy`` and
would make the diagnostic inspect different timestamp packs from those stored
in the source file.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

try:
    from . import dv_recording_time as dvmeta
except ImportError:  # Direct execution puts pipeline_tools/ on sys.path.
    import dv_recording_time as dvmeta


__version__ = "1.0.0"
NTSC_FPS = 30000 / 1001
PAL_FPS = 25.0
# DV timestamps have one-second resolution.  A normal clock tick can therefore
# be displaced from its ideal frame by almost one frame, but not by a second.
DISCONTINUITY_SLACK_SECONDS = 1.1


class AuditError(RuntimeError):
    """A source could not be audited reliably."""


class ArgumentParser(argparse.ArgumentParser):
    def format_help(self):
        return super().format_help().replace("usage:", "Usage:", 1)

    def format_usage(self):
        return super().format_usage().replace("usage:", "Usage:", 1)

    def error(self, message):
        self.exit(
            2,
            f"[ERROR] {message}\n"
            f"Try '{self.prog} --help' for more information.\n",
        )


def build_parser():
    parser = ArgumentParser(
        prog=Path(sys.argv[0]).name,
        usage="%(prog)s [OPTIONS] FILE",
        description=(
            "Description:\n"
            "  Inspect VAUX, SSYB and AAUX recording-date packs in every frame "
            "of a\n"
            "  native DV or Digital8 capture. Report source agreement, pack "
            "coverage\n"
            "  and recording-time discontinuities."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s dv.20040423-173419.avi\n"
            "  %(prog)s -- capture.dv\n\n"
            "Dependencies:\n"
            "  ffprobe; ffmpeg for a DV stream stored in a container"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    arguments = parser.add_argument_group("Arguments")
    arguments.add_argument("file", metavar="FILE", help="DV or DV-in-container capture")
    options = parser.add_argument_group("Options")
    options.add_argument("-h", "--help", action="help", help="show this help and exit")
    options.add_argument(
        "-V",
        "--version",
        action="version",
        version="%(prog)s " + __version__,
        help="show version and exit",
    )
    return parser


def parse_args(argv=None):
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    for argument in arguments:
        if argument == "--":
            break
        if argument.startswith("-") and argument not in {
            "-h",
            "--help",
            "-V",
            "--version",
        }:
            parser.error(f"unknown option: {argument}")
    return parser.parse_args(arguments)


def _require_command(name):
    if shutil.which(name) is None:
        raise AuditError(f"required command not found: {name}")


def _probe_dv_codec(path):
    _require_command("ffprobe")
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                os.fspath(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditError(f"ffprobe could not inspect {path}: {exc}") from exc

    codecs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise AuditError(f"ffprobe could not inspect {path}: {detail}")
    if codecs != ["dvvideo"]:
        found = ", ".join(codecs) if codecs else "no video stream"
        raise AuditError(f"expected one DV video stream in {path}; found {found}")


def container_command(path):
    """Return the metadata-preserving command used for container input."""
    return [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        os.fspath(path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-f",
        "data",
        "pipe:1",
    ]


def _timestamp(parts):
    date_value, time_value = parts
    if date_value is None or time_value is None:
        return None
    return datetime.datetime(*(date_value + time_value))


def _new_source_stats():
    return {
        "date": None,
        "time": None,
        "first": None,
        "last": None,
        "usable_frames": 0,
        "date_pack_frames": 0,
        "time_pack_frames": 0,
        "previous_change": None,
        "discontinuities": [],
    }


def scan_stream(stream: BinaryIO):
    """Scan a raw DV byte stream and return its metadata audit."""
    first_four = stream.read(4)
    if len(first_four) < 4:
        raise AuditError("no complete DV stream found")
    if dvmeta._frame_offset(first_four) != 0:
        raise AuditError("the stream does not start with a DV frame header")

    pal = bool(first_four[3] & 0x80)
    if pal:
        system = "PAL"
        frame_size = dvmeta.FRAME_BYTES_625_50
        sequences = dvmeta.SEQUENCES_625_50
        fps = PAL_FPS
    else:
        system = "NTSC"
        frame_size = dvmeta.FRAME_BYTES_525_60
        sequences = dvmeta.SEQUENCES_525_60
        fps = NTSC_FPS

    remainder = stream.read(frame_size - 4)
    if len(remainder) != frame_size - 4:
        raise AuditError("incomplete first DV frame")

    frame = first_four + remainder
    frame_number = 0
    first_frame_sources = None
    stats = {name: _new_source_stats() for name in dvmeta.SOURCES}
    agreement_frames = 0
    conflict_frames = 0
    first_conflict = None

    while True:
        if dvmeta._frame_offset(frame[:4]) != 0:
            raise AuditError(f"invalid DV frame header at frame {frame_number}")
        sources = dvmeta.inspect_frame(frame, sequences)
        if first_frame_sources is None:
            first_frame_sources = sources

        frame_values = {}
        for name in dvmeta.SOURCES:
            source = sources[name]
            source_stats = stats[name]
            if source["date"] is not None:
                source_stats["date"] = source["date"]
                source_stats["date_pack_frames"] += 1
            if source["time"] is not None:
                source_stats["time"] = source["time"]
                source_stats["time_pack_frames"] += 1

            value = _timestamp((source_stats["date"], source_stats["time"]))
            if value is None:
                continue

            source_stats["usable_frames"] += 1
            if source_stats["first"] is None:
                source_stats["first"] = (frame_number, value)
            source_stats["last"] = (frame_number, value)

            # A carried date is meaningful alongside a newly observed time,
            # but a wholly carried timestamp is not current evidence.  Using
            # it for agreement would call a source contradictory as soon as it
            # stopped emitting packs and another source advanced one second.
            if source["time"] is not None:
                frame_values[name] = value
                previous = source_stats["previous_change"]
                if previous is None:
                    source_stats["previous_change"] = (frame_number, value)
                elif value != previous[1]:
                    media_delta = (frame_number - previous[0]) / fps
                    clock_delta = (value - previous[1]).total_seconds()
                    if abs(clock_delta - media_delta) > DISCONTINUITY_SLACK_SECONDS:
                        source_stats["discontinuities"].append(
                            {
                                "frame": frame_number,
                                "previous_frame": previous[0],
                                "previous": previous[1],
                                "current": value,
                                "clock_delta": clock_delta,
                                "media_delta": media_delta,
                            }
                        )
                    source_stats["previous_change"] = (frame_number, value)

        if len(frame_values) > 1:
            if len(set(frame_values.values())) > 1:
                conflict_frames += 1
                if first_conflict is None:
                    first_conflict = (frame_number, dict(frame_values))
            else:
                agreement_frames += 1

        frame_number += 1
        frame = stream.read(frame_size)
        if not frame:
            break
        if len(frame) != frame_size:
            raise AuditError(
                f"final partial DV frame: {len(frame)} of {frame_size} bytes"
            )

    first_values = sorted(
        (source["first"][0], name, source["first"][1])
        for name, source in stats.items()
        if source["first"] is not None
    )
    timeline_conflicts = []
    if first_values:
        anchor_frame, anchor_name, anchor_value = first_values[0]
        for frame_no, name, value in first_values[1:]:
            media_delta = (frame_no - anchor_frame) / fps
            clock_delta = (value - anchor_value).total_seconds()
            if abs(clock_delta - media_delta) > DISCONTINUITY_SLACK_SECONDS:
                timeline_conflicts.append(
                    {
                        "source": name,
                        "frame": frame_no,
                        "value": value,
                        "anchor_source": anchor_name,
                        "anchor_frame": anchor_frame,
                        "anchor_value": anchor_value,
                        "clock_delta": clock_delta,
                        "media_delta": media_delta,
                    }
                )

    return {
        "system": system,
        "frame_size": frame_size,
        "fps": fps,
        "frames": frame_number,
        "first_frame_sources": first_frame_sources,
        "sources": stats,
        "agreement_frames": agreement_frames,
        "conflict_frames": conflict_frames,
        "first_conflict": first_conflict,
        "timeline_conflicts": timeline_conflicts,
    }


def _scan_container(path):
    _require_command("ffmpeg")
    stderr_file = tempfile.TemporaryFile()
    try:
        try:
            process = subprocess.Popen(
                container_command(path), stdout=subprocess.PIPE, stderr=stderr_file
            )
        except OSError as exc:
            raise AuditError(f"ffmpeg could not read {path}: {exc}") from exc

        assert process.stdout is not None
        try:
            report = scan_stream(process.stdout)
        except BaseException:
            process.stdout.close()
            process.terminate()
            process.wait()
            raise

        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            stderr_file.seek(0)
            detail = stderr_file.read().decode("utf-8", errors="replace").strip()
            detail = detail or f"exit status {return_code}"
            raise AuditError(f"ffmpeg could not read {path}: {detail}")
        return report
    finally:
        stderr_file.close()


def audit_path(path):
    path = Path(path)
    if not path.is_file():
        raise AuditError(f"file not found: {path}")
    source_path = path.resolve()
    _probe_dv_codec(source_path)

    if path.suffix.lower() == ".dv":
        try:
            with source_path.open("rb") as stream:
                report = scan_stream(stream)
        except OSError as exc:
            raise AuditError(f"could not read {path}: {exc}") from exc
        input_description = "raw DV"
    else:
        report = _scan_container(source_path)
        input_description = "DV packet payload copied from container"

    report["file"] = os.fspath(path)
    report["input"] = input_description
    return report


def _format_value(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_first_frame_source(name, source):
    date_value = source["date"]
    time_value = source["time"]
    if date_value is None and time_value is None:
        value = "not found"
    elif date_value is None:
        value = "[no valid date] %02d:%02d:%02d" % time_value
    elif time_value is None:
        value = "%04d-%02d-%02d [no valid time]" % date_value
    else:
        value = "%04d-%02d-%02d %02d:%02d:%02d" % (date_value + time_value)

    lines = [f"  {name:5s}: {value}"]
    for kind in ("date", "time"):
        raw = source[f"{kind}_raw"]
        if raw is not None:
            lines.append(
                f"      {kind.upper()} pack: offset {source[f'{kind}_offset']}, "
                f"{raw.hex(' ')}"
            )
    return lines


def print_report(report):
    print(f"File:                    {report['file']}")
    print(f"Input:                   {report['input']}")
    print(f"DV system:               {report['system']}")
    print(f"Frame size:              {report['frame_size']} bytes")
    print(f"Frames scanned:          {report['frames']}")

    print("\nFirst-frame timestamp packs:")
    for name in dvmeta.SOURCES:
        for line in _format_first_frame_source(
            name, report["first_frame_sources"][name]
        ):
            print(line)

    print("\nFirst valid recording timestamp from each source:")
    first_values = []
    for name in dvmeta.SOURCES:
        first = report["sources"][name]["first"]
        if first is None:
            print(f"  {name:5s}: none")
        else:
            frame, value = first
            first_values.append((frame, name, value))
            print(f"  {name:5s}: {_format_value(value)}  (first at frame {frame})")

    review_reasons = []
    print("\nResult:")
    if not first_values:
        print("  No valid recording date/time found.")
        review_reasons.append("no valid recording date/time was found")
    elif report["conflict_frames"] or report["timeline_conflicts"]:
        print("  Valid DV metadata sources do not share one timeline; no source was chosen.")
        if report["conflict_frames"]:
            frame, values = report["first_conflict"]
            print(f"  First simultaneous disagreement at frame {frame}:")
            for name, value in values.items():
                print(f"    {name:5s}: {_format_value(value)}")
            print(f"  Frames with simultaneous disagreement: {report['conflict_frames']}")
        for item in report["timeline_conflicts"]:
            print(
                f"  {item['source']} first appears at frame {item['frame']} with "
                f"{_format_value(item['value'])}; relative to "
                f"{item['anchor_source']} at frame {item['anchor_frame']}, "
                f"clock {item['clock_delta']:+.3f}s versus media "
                f"{item['media_delta']:+.3f}s"
            )
        review_reasons.append("valid metadata sources disagree")
    else:
        first_frame, _, first_value = min(first_values)
        valid_names = [name for name in dvmeta.SOURCES if report["sources"][name]["first"]]
        print(f"  Recording date/time:   {_format_value(first_value)}")
        print(f"  First valid at frame:  {first_frame}")
        print(f"  Source(s):             {', '.join(valid_names)}")
        if len(valid_names) == 1:
            status = "one valid metadata source"
        elif report["agreement_frames"]:
            status = (
                "standard metadata sources agree in "
                f"{report['agreement_frames']} jointly observed frames"
            )
        else:
            status = "multiple sources align, with no jointly observed time packs"
        print(f"  Status:                {status}")

    print("\nCoverage:")
    for name in dvmeta.SOURCES:
        source = report["sources"][name]
        print(
            f"  {name:5s}: valid DATE packs in {source['date_pack_frames']:8d} frames; "
            f"TIME packs in {source['time_pack_frames']:8d}; "
            f"carried timestamp usable in {source['usable_frames']:8d}"
        )

    print("\nLast timestamp from each source:")
    for name in dvmeta.SOURCES:
        last = report["sources"][name]["last"]
        if last is None:
            print(f"  {name:5s}: none")
        else:
            frame, value = last
            print(f"  {name:5s}: {_format_value(value)}  (frame {frame})")

    print("\nRecording-time discontinuities:")
    found_discontinuity = False
    for name in dvmeta.SOURCES:
        discontinuities = report["sources"][name]["discontinuities"]
        if not discontinuities:
            print(f"  {name:5s}: none")
            continue
        found_discontinuity = True
        print(f"  {name:5s}:")
        for item in discontinuities:
            print(
                f"    frame {item['frame']}: {_format_value(item['previous'])} -> "
                f"{_format_value(item['current'])}; clock {item['clock_delta']:+.3f}s, "
                f"media +{item['media_delta']:.3f}s since frame "
                f"{item['previous_frame']}"
            )
    if found_discontinuity:
        review_reasons.append("recording-time discontinuities were found")

    if review_reasons:
        print(
            "[WARNING] Review required: " + "; ".join(review_reasons) + ".",
            file=sys.stderr,
        )
    else:
        print("\n[INFO] DV recording-date audit passed.")
    return not review_reasons


def main(argv=None):
    args = parse_args(argv)
    try:
        report = audit_path(args.file)
    except AuditError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0 if print_report(report) else 1


if __name__ == "__main__":
    sys.exit(main())
