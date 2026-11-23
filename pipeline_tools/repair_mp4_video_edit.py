#!/usr/bin/env python3
"""Extend an assembled MP4's single normal video edit to expose every sample.

GPAC 2.0 -cat can align samples to an audio endpoint without extending the
video edit by the corresponding gap. Preserve media_time, sample timestamps,
audio edits and chapters; only extend duration fields. This is for complete
pipeline assemblies, not intentionally trimmed MP4s. Unsupported layouts fail
before writing. The replacement is atomic and does not resize any boxes.
"""
import argparse
import os
from pathlib import Path
import shutil
import struct
import tempfile


def boxes(f, start, end):
    while start < end:
        if end - start < 8:
            raise ValueError('truncated MP4 box')
        f.seek(start)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            size = struct.unpack('>Q', f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError('invalid MP4 box size')
        yield kind, start + header, start + size
        start += size


def one(items, kind):
    matches = [(start, end) for name, start, end in items if name == kind]
    if len(matches) != 1:
        raise ValueError(f'expected one {kind.decode()} box')
    return matches[0]


def read(f, bounds):
    f.seek(bounds[0])
    return f.read(bounds[1] - bounds[0])


def duration_field(f, bounds, track=False):
    data = read(f, bounds)
    version = data[0]
    if version not in (0, 1):
        raise ValueError('unsupported duration box version')
    offset = (28 if version else 20) if track else (24 if version else 16)
    fmt = '>Q' if version else '>I'
    return bounds[0] + offset, fmt, struct.unpack_from(fmt, data, offset)[0]


def timescale(f, bounds):
    data = read(f, bounds)
    value = struct.unpack_from('>I', data, 20 if data[0] else 12)[0]
    if not value:
        raise ValueError('zero timescale')
    return value


def runs(data, signed=False):
    count = struct.unpack_from('>I', data, 4)[0]
    if len(data) != 8 + 8 * count:
        raise ValueError('invalid sample timing table')
    for offset in range(8, len(data), 8):
        n, value = struct.unpack_from('>Ii' if signed else '>II', data, offset)
        if not n:
            raise ValueError('empty sample timing run')
        yield n, value


def presentation_bounds(f, table):
    durations = runs(read(f, one(table, b'stts')))
    ctts = any(kind == b'ctts' for kind, _, _ in table)
    offsets = iter(())
    if ctts:
        data = read(f, one(table, b'ctts'))
        if data[0] not in (0, 1):
            raise ValueError('unsupported composition offset version')
        offsets = iter(runs(data, signed=data[0] == 1))
    remaining = 0
    dts = end = 0
    first = None
    for count, delta in durations:
        if delta <= 0:
            raise ValueError('nonpositive sample duration')
        while count:
            if ctts and not remaining:
                remaining, offset = next(offsets)  # mismatched tables fail closed
            elif not ctts:
                remaining, offset = count, 0
            n = min(count, remaining)
            first = dts + offset if first is None else min(first, dts + offset)
            end = max(end, dts + n * delta + offset)
            dts += n * delta
            count -= n
            remaining -= n
    if ctts and (remaining or next(offsets, None) is not None):
        raise ValueError('sample timing counts disagree')
    if not dts:
        raise ValueError('empty video timing table')
    return first, end


def presentation_end(f, table):
    return presentation_bounds(f, table)[1]


def repair_plan(path):
    """Inspect timing metadata only; return byte patches for a short end edit.

    A hidden leading sample is ambiguous (possibly an intentional trim), so
    reject it instead of changing the shared audio/video time origin.
    """
    path = Path(path)
    patches = []
    with path.open('rb') as f:
        top = list(boxes(f, 0, os.fstat(f.fileno()).st_size))
        if any(kind == b'moof' for kind, _, _ in top):
            raise ValueError('fragmented MP4 is unsupported')
        movie = list(boxes(f, *one(top, b'moov')))
        if any(kind == b'mvex' for kind, _, _ in movie):
            raise ValueError('fragmented MP4 is unsupported')
        mvhd = one(movie, b'mvhd')
        movie_ts = timescale(f, mvhd)
        movie_duration = duration_field(f, mvhd)[2]
        videos = 0
        for kind, start, end in movie:
            if kind != b'trak':
                continue
            track = list(boxes(f, start, end))
            media = list(boxes(f, *one(track, b'mdia')))
            if read(f, one(media, b'hdlr'))[8:12] != b'vide':
                continue
            videos += 1
            if not any(kind == b'edts' for kind, _, _ in track):
                continue
            edit = one(list(boxes(f, *one(track, b'edts'))), b'elst')
            data = read(f, edit)
            version = data[0]
            if version not in (0, 1) or struct.unpack_from('>I', data, 4)[0] != 1:
                raise ValueError('expected a single normal video edit')
            fmt = '>Q' if version else '>I'
            segment, media_time, rate = struct.unpack_from('>Qqi' if version else '>Iii', data, 8)
            if media_time < 0 or rate != 65536:
                raise ValueError('unsupported video edit start or rate')
            media_ts = timescale(f, one(media, b'mdhd'))
            table = list(boxes(f, *one(list(boxes(f, *one(media, b'minf'))), b'stbl')))
            first_pts, end_pts = presentation_bounds(f, table)
            if first_pts < media_time:
                raise ValueError('video edit hides leading samples; cannot safely repair the time origin')
            required = max(0, ((end_pts - media_time) * movie_ts + media_ts - 1) // media_ts)
            if required <= segment:
                continue
            patches.append((edit[0] + 8, fmt, required))
            pos, tkfmt, old = duration_field(f, one(track, b'tkhd'), track=True)
            patches.append((pos, tkfmt, max(old, required)))
            movie_duration = max(movie_duration, required)
        if videos != 1:
            raise ValueError('expected one video track')
        if patches:
            pos, fmt, old = duration_field(f, mvhd)
            patches.append((pos, fmt, max(old, movie_duration)))
    # Pack before creating the copy: overflow must not partially change a file.
    return [(pos, struct.pack(fmt, value)) for pos, fmt, value in patches]


def repair(path):
    path = Path(path)
    patches = repair_plan(path)
    if not patches:
        return False
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    os.close(fd)
    try:
        shutil.copyfile(path, temp)
        with open(temp, 'r+b') as f:
            for pos, data in patches:
                f.seek(pos)
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(path, temp)
        if repair_plan(temp):
            raise ValueError('video presentation boundary repair did not verify')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mp4', type=Path)
    args = parser.parse_args()
    try:
        changed = repair(args.mp4)
    except (OSError, ValueError, struct.error, StopIteration) as exc:
        parser.exit(1, f'Video edit repair failed: {exc}\n')
    print('Video edit window extended to include every picture.' if changed else
          'Video edit window already includes every picture.')
