#!/usr/bin/env python3
"""Plan and verify sample-based audio alignment to the first decoded picture.

Input audio must already be continuous. Timing correction here is limited to
trimming leading samples, adding leading silence, and making the audio span
exactly the pictures: an overrun is clipped and a source whose audio ends
before its last picture is padded with trailing silence. No asynchronous
resampling or interior gap repair is used.
"""
import argparse
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys


def probe(path, *args):
    return json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', *args, '-of', 'json', str(path)], text=True))


def metadata(path):
    return probe(path, '-show_streams', '-show_format')


def picture_bounds(path, meta):
    video = next(s for s in meta['streams'] if s['codec_type'] == 'video')
    entries = 'frame=best_effort_timestamp_time,pkt_duration_time'
    first_frames = probe(path, '-select_streams', 'v:0', '-read_intervals', '%+#128',
                         '-show_frames', '-show_entries', entries)['frames']
    if not first_frames or 'best_effort_timestamp_time' not in first_frames[0]:
        raise ValueError('first decoded picture has no usable timestamp')
    first = Fraction(first_frames[0]['best_effort_timestamp_time'])
    duration = video.get('duration', meta.get('format', {}).get('duration'))
    if duration in (None, 'N/A'):
        raise ValueError('cannot locate the final picture without a usable video/container duration')
    seek = max(first, first + Fraction(duration) - 10)
    # Seeking even to the first picture can land on a later MPEG-2 GOP and
    # reconstruct a different timeline when PES timestamps are sparse. If the
    # tail window starts at the beginning, decode from there without seeking.
    tail_options = ('-read_intervals', f'{float(seek):.6f}%') if seek > first else ()
    tail = probe(path, '-select_streams', 'v:0', *tail_options,
                 '-show_frames', '-show_entries', entries)['frames']
    rate = Fraction(video['r_frame_rate'])
    # A picture decoded without any timestamp still occupies the timeline:
    # the encode shows it one picture after the one before it. MPEG-2 in a
    # program stream can end on such a picture, because a missing timestamp
    # is reconstructed only when a later anchor picture arrives, and none
    # follows the last.
    ends = []
    for f in tail:
        length = Fraction(f.get('pkt_duration_time') or str(1 / rate))
        if 'best_effort_timestamp_time' in f:
            ends.append(Fraction(f['best_effort_timestamp_time']) + length)
        elif ends:
            ends.append(ends[-1] + length)
    if not ends or max(ends) <= first:
        raise ValueError('could not determine the last decoded picture boundary')
    return first, max(ends), rate


def audio_frames(path, stream):
    frames = probe(path, '-select_streams', 'a:0', '-show_frames', '-show_entries',
                   'frame=best_effort_timestamp,nb_samples,sample_rate')['frames']
    rate = int(stream['sample_rate'])
    tb = Fraction(stream['time_base'])
    # Raw DV labels audio in video-frame ticks; MKV labels it in milliseconds.
    # Sample counts provide the finer clock, but comparison must respect the
    # precision actually available in the container's timestamps.
    tolerance = max(tb, Fraction(1, rate)) + Fraction(1, 1000000)
    count = 0
    first = None
    previous = None
    for frame in frames:
        if 'best_effort_timestamp' not in frame or int(frame.get('nb_samples', 0)) <= 0:
            raise ValueError('audio frame has missing timestamp or sample count')
        if int(frame.get('sample_rate', rate)) != rate:
            raise ValueError('audio sample rate changes inside the recording')
        pts = int(frame['best_effort_timestamp']) * tb
        if first is None:
            first = pts
        if previous is not None and pts <= previous:
            raise ValueError(f'non-increasing audio timestamps at {float(pts):.6f}s')
        deviation = pts - first - Fraction(count, rate)
        if abs(deviation) > tolerance:
            raise ValueError(f'discontinuous audio at {float(pts):.6f}s: '
                             f'{float(deviation):+.6f}s from the sample clock; repair the input first')
        count += int(frame['nb_samples'])
        previous = pts
    if first is None:
        raise ValueError('audio stream contains no decoded samples')
    return first, count, rate, tolerance


def rounded(value):
    # Symmetric nearest-sample rounding, avoiding binary floating-point ties.
    if value < 0:
        return -rounded(-value)
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def plan(args):
    meta = metadata(args.input)
    start, end, video_rate = picture_bounds(args.input, meta)
    span = end - start
    if args.test:
        span = min(span, Fraction(30))
    result = dict(input=str(Path(args.input).resolve()), picture_start_seconds=str(start),
                  picture_end_seconds=str(end), picture_span_seconds=str(span),
                  video_frame_seconds=str(1 / video_rate), test=args.test)
    audio = next((s for s in meta['streams'] if s['codec_type'] == 'audio'), None)
    if audio is None:
        result.update(audio_present=False, ffmpeg_audio_args=['-an'])
        return result
    first, count, rate, tolerance = audio_frames(args.input, audio)
    shift = rounded((first - start) * rate)
    trim, pad = max(0, -shift), max(0, shift)
    picture_samples = rounded(span * rate)
    available = max(0, count - trim) + pad
    if available <= 0:
        raise ValueError('source audio has no samples overlapping the selected pictures')
    # The master's audio spans exactly the pictures. Audio running past the
    # last picture is clipped; audio ending before it (a DVD recorder stops
    # the audio track a few hundred milliseconds before the picture) is padded
    # with silence, so every later length check compares two streams of one
    # extent instead of failing on a source-shaped shortfall.
    expected = picture_samples
    tail_pad = max(0, picture_samples - available)
    codec = audio['codec_name']
    preserve_pcm = args.pcm and codec.startswith('pcm_')
    copy = (not args.test and shift == 0 and
            ((codec == 'aac' and picture_samples <= count <= picture_samples + 1023) or
             (preserve_pcm and count == picture_samples)))
    if copy:
        audio_args = ['-c:a', 'copy']
    else:
        filters = ['asetpts=N/SR/TB']
        if trim:
            filters.append(f'atrim=start_sample={trim}')
        if pad:
            filters.append(f'adelay={pad}S:all=1')
        if tail_pad:
            filters.append(f'apad=whole_len={picture_samples}')
        filters += [f'atrim=end_sample={picture_samples}', 'asetpts=N/SR/TB']
        audio_args = ['-af', ','.join(filters), '-c:a', codec if preserve_pcm else 'aac']
        if not preserve_pcm:
            audio_args += ['-b:a', '192k']
    result.update(audio_present=True, source_audio_start_seconds=str(first),
                  source_offset_seconds=str(first - start), source_samples=count,
                  sample_rate=rate, trim_samples=trim, pad_samples=pad,
                  tail_pad_samples=tail_pad, expected_samples=expected,
                  picture_samples=picture_samples,
                  output_codec=codec if preserve_pcm else 'aac', copy=copy,
                  ffmpeg_audio_args=audio_args)
    return result


def verify(args):
    planned = json.loads(Path(args.plan).read_text())
    meta = metadata(args.output)
    video_start, video_end, _ = picture_bounds(args.output, meta)
    video_tb = Fraction(next(s for s in meta['streams'] if s['codec_type'] == 'video')['time_base'])
    if abs(video_start) > video_tb:
        raise ValueError(f'first master picture is not at zero: {float(video_start):.6f}s')
    if abs(video_end - Fraction(planned['picture_span_seconds'])) > Fraction(planned['video_frame_seconds']) + video_tb:
        raise ValueError('master picture end differs from the selected source span')
    audio = next((s for s in meta['streams'] if s['codec_type'] == 'audio'), None)
    if not planned['audio_present']:
        if audio is not None:
            raise ValueError('master unexpectedly contains audio')
        return dict(verified=True, audio_present=False)
    if audio is None:
        raise ValueError('master lost its audio stream')
    first, count, rate, tolerance = audio_frames(args.output, audio)
    if rate != planned['sample_rate']:
        raise ValueError('master audio sample rate differs from the plan')
    if audio['codec_name'] != planned['output_codec']:
        raise ValueError('master audio codec differs from the plan')
    if abs(first) > tolerance:
        raise ValueError(f'first decoded master audio is not at zero: {float(first):.6f}s')
    # AAC decoding can expose the unused samples in the final 1024-sample
    # block. Encoder priming must already be removed by the container/decoder.
    padding = 1023 if audio['codec_name'] == 'aac' else 0
    expected = planned['expected_samples']
    if not expected <= count <= expected + padding:
        raise ValueError(f'master has {count} decoded audio samples; expected {expected}'
                         f' plus at most {padding} final AAC padding samples')
    return dict(verified=True, audio_present=True, decoded_samples=count,
                expected_samples=expected, trailing_padding_samples=count - expected,
                decoded_audio_start_seconds=float(first), picture_end_seconds=float(video_end),
                trim_samples=planned['trim_samples'], pad_samples=planned['pad_samples'],
                tail_pad_samples=planned['tail_pad_samples'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('input')
    p.add_argument('--output', required=True)
    p.add_argument('--pcm', action='store_true')
    p.add_argument('--test', action='store_true')
    v = sub.add_parser('verify')
    v.add_argument('--plan', required=True)
    v.add_argument('--output', required=True)
    v.add_argument('--report', required=True)
    args = parser.parse_args()
    try:
        result = plan(args) if args.command == 'plan' else verify(args)
        target = args.output if args.command == 'plan' else args.report
        Path(target).write_text(json.dumps(result, indent=2) + '\n')
        if result.get('audio_present'):
            print(f"[INFO] Audio timing {args.command}: trim {result['trim_samples']} leading samples; "
                  f"add {result['pad_samples']} leading and {result['tail_pad_samples']} "
                  f"trailing silence samples.")
            if args.command == 'verify':
                print(f"[INFO] Audio verified: {result['decoded_samples']} decoded samples; "
                      f"{result['trailing_padding_samples']} samples of final AAC padding.")
        else:
            print('[INFO] Audio timing: no audio stream.')
        return 0
    except (ValueError, KeyError, StopIteration, OSError, subprocess.CalledProcessError) as exc:
        print(f'[ERROR] Audio timing {args.command} failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
