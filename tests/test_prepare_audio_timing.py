"""Real media regressions for picture-relative audio preparation."""
import array
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_audio', ROOT / 'pipeline_tools/prepare_audio_timing.py')
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def strip_video_timestamps(path):
    """Clear PTS/DTS from every video PES header of a program stream but the first.

    FFmpeg rebuilds each picture's timestamp from the packet that follows it,
    so only the final anchor picture decodes without one, as on a disc whose
    recorder stamps few pictures. The cleared bytes become header stuffing.
    """
    data = bytearray(path.read_bytes())
    pos, first = 0, True
    while pos + 6 <= len(data):
        assert data[pos:pos + 3] == b'\0\0\1', f'no start code at byte {pos}'
        stream_id = data[pos + 3]
        if stream_id == 0xBA:  # pack header and its stuffing
            pos += 14 + (data[pos + 13] & 7)
            continue
        if stream_id == 0xB9:  # program end
            break
        flags = data[pos + 7]
        if stream_id == 0xE0 and flags & 0x80:
            if first:
                first = False
            elif not flags & 0x3F:  # nothing follows the timestamps
                size = 10 if flags & 0x40 else 5
                data[pos + 7] = 0
                data[pos + 9:pos + 9 + size] = b'\xff' * size
        pos += 6 + int.from_bytes(data[pos + 4:pos + 6], 'big')
    path.write_bytes(data)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
class PreparationAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='prepare-audio-test-')
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def media(self, video_delay=0, audio_delay=0, gap=False, duration=3, dv=False):
        source = self.d / '-source & café.mkv'
        video_filter = f'setpts=PTS+{video_delay}/TB'
        audio_filter = f'asetpts=PTS+{audio_delay}/TB'
        if gap:
            audio_filter += '+gte(T\\,1)*0.032/TB'
        args = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                (f'testsrc2=size={"720x480" if dv else "128x96"}:'
                 f'rate={"30000/1001" if dv else "25"}:duration={duration}'),
                '-f', 'lavfi', '-i', f'anoisesrc=sample_rate=48000:amplitude=0.2:seed=823:duration={duration}',
                '-vf', video_filter, '-af', audio_filter,
                '-c:v', 'dvvideo' if dv else 'ffv1', '-c:a', 'pcm_s16le']
        if dv:
            source = self.d / 'source.dv'
            args += ['-pix_fmt', 'yuv411p', '-ac', '2', '-f', 'dv']
        subprocess.run(args + [str(source)], check=True)
        return source

    def prepare(self, source, *opts, success=True):
        result = subprocess.run(['bash', str(ROOT / 'prepare_video.sh'), '--yes',
                                 '--output-dir', str(self.d / 'masters'), '--log-dir', str(self.d / 'logs'),
                                 *opts, str(source)], capture_output=True, text=True,
                                env={**os.environ, 'VIDEO_PRESERVATION_ROOT': str(self.d / 'preservation')})
        if not success:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(list((self.d / 'masters').glob('*')))
            return result
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-7000:])
        self.output = result.stdout + result.stderr
        master, = (self.d / 'masters').glob('*')
        plan = json.loads((self.d / 'logs/audio_timing_plan.json').read_text())
        report = json.loads((self.d / 'logs/audio_timing_verified.json').read_text())
        self.assertTrue(report['verified'])
        return master, plan, report

    def pcm(self, source):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(source),
                                        '-map', '0:a:0', '-c:a', 'pcm_s16le', '-f', 's16le', '-'])

    def test_early_pcm_retained_samples_are_bit_exact(self):
        source = self.media(video_delay=0.12)
        master, plan, _ = self.prepare(source, '--pcm')
        self.assertEqual(plan['trim_samples'], 5760)
        self.assertEqual(plan['pad_samples'], 0)
        # Trimming the lead-in leaves the audio 5760 samples short of the
        # last picture; the master fills that with silence.
        self.assertEqual(plan['tail_pad_samples'], 5760)
        self.assertEqual(plan['expected_samples'], plan['picture_samples'])
        expected = (self.pcm(source)[5760 * 2:] + bytes(5760 * 2))[:plan['expected_samples'] * 2]
        self.assertEqual(self.pcm(master), expected)

    def test_short_aac_tail_is_padded_not_copied(self):
        # Aligned AAC is normally stream-copied. Audio that stops before the
        # last picture must be padded instead, or the pipeline's duration
        # reconciliation and final A/V check reject the master.
        source = self.d / 'short-tail.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=128x96:rate=25:duration=3',
                        '-f', 'lavfi', '-i', 'anoisesrc=sample_rate=48000:amplitude=0.2:seed=823:duration=2.6',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
        master, plan, report = self.prepare(source)
        self.assertFalse(plan['copy'])
        self.assertEqual(plan['trim_samples'], 0)
        self.assertEqual(plan['pad_samples'], 0)
        self.assertGreaterEqual(plan['tail_pad_samples'], int(0.4 * 48000) - 1024)
        self.assertEqual(plan['expected_samples'], plan['picture_samples'])
        self.assertEqual(report['expected_samples'], 3 * 48000)
        self.assertLessEqual(report['trailing_padding_samples'], 1023)
        tail = array.array('h', self.pcm(master)[-int(0.3 * 48000) * 2:])
        self.assertEqual(max(abs(v) for v in tail), 0)
        # Silence the source does not hold is stated, not slipped in: once in
        # the pre-flight summary, once in the completion report, and in the
        # master's own comment tag so the fact survives with the file.
        added = plan['tail_pad_samples']
        self.assertEqual(self.output.count('SILENCE APPENDED'), 2)
        self.assertIn(f'ends {added} samples', self.output)
        comment = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=comment',
                                           '-of', 'default=nw=1:nk=1', str(master)], text=True)
        self.assertIn('prepare_video.sh master', comment)
        self.assertIn(f'{added} samples ({round(added / 48)} ms) of silence appended', comment)

    def test_aligned_audio_reports_no_silence(self):
        master, plan, _ = self.prepare(self.media(duration=1.001, dv=True), '--pcm')
        self.assertEqual(plan['tail_pad_samples'], 0)
        self.assertNotIn('SILENCE', self.output)
        comment = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format_tags=comment',
                                           '-of', 'default=nw=1:nk=1', str(master)], text=True)
        self.assertNotIn('audio:', comment)

    def test_late_pcm_has_exact_leading_silence(self):
        source = self.media(audio_delay=0.125)
        master, plan, _ = self.prepare(source, '--pcm')
        self.assertEqual(plan['pad_samples'], 6000)
        expected = (bytes(6000 * 2) + self.pcm(source))[:plan['expected_samples'] * 2]
        self.assertEqual(self.pcm(master), expected)

    def test_aligned_raw_dv_pcm_is_unchanged(self):
        source = self.media(duration=1.001, dv=True)
        master, plan, _ = self.prepare(source, '--pcm')
        self.assertEqual(plan['trim_samples'], 0)
        self.assertEqual(plan['pad_samples'], 0)
        self.assertEqual(self.pcm(source), self.pcm(master))

    def test_open_gop_mpeg2_with_unstamped_final_picture(self):
        # A DVD program cut at an open GOP, as the trimmed disc MPGs are. Its
        # two leading B-frames are packets that decode to nothing, and its
        # final anchor picture decodes without a timestamp. The master holds
        # every decoded picture, so the planned span and the frame-count check
        # must include that last picture and leave out the dropped ones.
        # One encoder worker keeps the PES layout stable across hosts and
        # exposes the timestamp ambiguity when this program stream is sought.
        from fractions import Fraction
        whole = self.d / 'whole.mpg'
        source = self.d / 'open-gop cut.mpg'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=128x96:rate=30000/1001:duration=4',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
                        '-c:v', 'mpeg2video', '-threads:v', '1', '-bf', '2', '-g', '15',
                        '-flags', '+ilme+ildct', '-top', '1',
                        '-c:a', 'ac3', '-f', 'vob', str(whole)], check=True)
        # The audio is re-encoded so the cut leaves no seam in it; seeking
        # into the AC3 stream reports a decode error for the partial frame.
        subprocess.run(['ffmpeg', '-v', 'error', '-ss', '1', '-i', str(whole), '-c:v', 'copy',
                        '-c:a', 'ac3', '-f', 'vob', str(source)], check=True, capture_output=True)
        strip_video_timestamps(source)
        stream, = timing.probe(source, '-select_streams', 'v:0', '-count_frames', '-count_packets',
                               '-show_entries', 'stream=nb_read_frames,nb_read_packets')['streams']
        pictures = int(stream['nb_read_frames'])
        self.assertGreater(int(stream['nb_read_packets']), pictures)
        frames = timing.probe(source, '-select_streams', 'v:0', '-show_frames',
                              '-show_entries', 'frame=best_effort_timestamp_time')['frames']
        self.assertEqual(len(frames), pictures)
        self.assertNotIn('best_effort_timestamp_time', frames[-1])
        self.assertTrue(all('best_effort_timestamp_time' in f for f in frames[:-1]))
        master, plan, _ = self.prepare(source)
        span = Fraction(plan['picture_span_seconds']) / Fraction(plan['video_frame_seconds'])
        self.assertEqual(round(span), pictures)
        # The timeline has no gaps, so the exact two-frames-per-picture rule
        # applies rather than the duration-derived fallback.
        self.assertNotIn('gapped timestamps', self.output)
        self.assertIn(f'Frame-count check: {2 * pictures} frames (expected {2 * pictures})', self.output)

    def test_late_aac_content_is_aligned(self):
        source = self.media(audio_delay=0.125)
        master, plan, report = self.prepare(source)
        expected = array.array('h', (bytes(6000 * 2) + self.pcm(source))[:plan['expected_samples'] * 2])
        actual = array.array('h', self.pcm(master))
        # Broadband seeded noise makes a displacement unambiguous. Compare
        # independently computed PCM against lossy decoded AAC at several
        # positions, including immediately after the preserved delay.
        for start in [6000, 24000, 96000]:
            x = expected[start:start + 1024]
            scores = []
            for lag in range(-8, 9):
                y = actual[start + lag:start + lag + 1024]
                scores.append(sum(a * b for a, b in zip(x, y)))
            self.assertEqual(scores.index(max(scores)) - 8, 0)
        self.assertLessEqual(report['trailing_padding_samples'], 1023)

    def test_discontinuity_is_refused(self):
        result = self.prepare(self.media(gap=True), success=False)
        self.assertIn('discontinuous audio', result.stdout + result.stderr)

    def test_test_mode_checks_30_second_output(self):
        master, plan, _ = self.prepare(self.media(duration=31, video_delay=0.12), '--test', '--pcm')
        self.assertEqual(plan['expected_samples'], 30 * 48000)
        self.assertEqual(len(self.pcm(master)), 30 * 48000 * 2)


class RoundingTests(unittest.TestCase):
    def test_offsets_round_symmetrically_to_samples(self):
        from fractions import Fraction
        self.assertEqual(timing.rounded(Fraction(3395184, 1000)), 3395)
        self.assertEqual(timing.rounded(-Fraction(3395184, 1000)), -3395)
        self.assertEqual(timing.rounded(Fraction(1, 2)), 1)
        self.assertEqual(timing.rounded(-Fraction(1, 2)), -1)


if __name__ == '__main__':
    unittest.main()
