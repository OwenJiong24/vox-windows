"""VAD segmentation for incremental streaming preview.

Why this module exists
----------------------
The streaming preview used to re-transcribe the ENTIRE accumulated recording on every
tick. With a 500 ms tick that is quadratic work: a 20 s dictation costs
`sum(0.5 * i * RTF for i in 1..40)` ≈ **410 s of audio pushed through the model**, which
at the measured CPU RTF of 0.042 is ~17 s of CPU inside a 20 s recording — a whole core,
continuously, for a preview.

The fix is to stop re-reading what has already been read: find the last CONFIRMED
speech segment (speech followed by a real pause), transcribe it ONCE, remember its
text, and thereafter only transcribe the short unsettled tail. Work per tick becomes
O(tail) instead of O(everything so far).

This module is deliberately pure numpy with no Vox imports, so it is trivially testable
and safe to import anywhere.
"""

from __future__ import annotations

import numpy as np

# Defaults chosen to match AudioPipeline.vad_trim's existing frame/energy conventions
# (frame_ms=30, threshold_rms=0.003) but with a slightly higher floor: the preview only
# needs to find OBVIOUS pauses, and being conservative here means we simply transcribe
# a bit more tail — never that we cut a word in half.
FRAME_MS = 30
SILENCE_RMS = 0.006
MIN_SILENCE_MS = 260      # a pause this long confirms the segment before it
MIN_SEGMENT_MS = 400      # ignore blips shorter than this


def frame_rms(audio, sr, frame_ms=FRAME_MS):
    """Per-frame RMS energy. Vectorised reshape — no Python loop over frames."""
    try:
        arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    except Exception:
        return np.zeros(0, dtype=np.float32)
    frame_len = int(sr * frame_ms / 1000.0)
    if frame_len <= 0 or arr.size < frame_len:
        return np.zeros(0, dtype=np.float32)
    n_frames = arr.size // frame_len
    trimmed = arr[: n_frames * frame_len].reshape(n_frames, frame_len)
    return np.sqrt(np.mean(trimmed * trimmed, axis=1)).astype(np.float32)


def last_confirmed_segment_end(audio, sr, frame_ms=FRAME_MS,
                               silence_rms=SILENCE_RMS,
                               min_silence_ms=MIN_SILENCE_MS,
                               min_segment_ms=MIN_SEGMENT_MS):
    """End of the last definitively-finished speech run, or None.

    Returns a sample index such that ``audio[:end]`` is safe to transcribe once and
    forget. None means "nothing is settled yet — keep the whole thing as tail".

    The index returned is the end of the last SPEECH frame (not the end of the
    following silence), so the audio handed to the ASR is never truncated.
    """
    rms = frame_rms(audio, sr, frame_ms)
    if rms.size == 0:
        return None

    frame_len = int(sr * frame_ms / 1000.0)
    silence_frames = max(1, int(round(min_silence_ms / float(frame_ms))))
    speech_frames = max(1, int(round(min_segment_ms / float(frame_ms))))

    # Walk back over any trailing silence first; a pause only confirms a segment if
    # there is speech BEFORE it (otherwise the user simply has not started talking).
    i = rms.size - 1
    while i >= 0 and rms[i] < silence_rms:
        i -= 1
    if i < 0:
        return None                                  # all silence so far
    if (rms.size - 1 - i) < silence_frames:
        return None                                  # pause not long enough yet

    # Measure the speech run that this pause terminates.
    j = i
    while j >= 0 and rms[j] >= silence_rms:
        j -= 1
    if (i - j) < speech_frames:
        return None                                  # too short to trust

    return (i + 1) * frame_len


def split_new_segments(audio, sr, already_done=0, **kw):
    """Split the unsettled region into (newly_confirmed, tail).

    ``already_done`` is a sample index into ``audio`` that has been finalised before.
    Returns ``(end_of_confirmed, tail_start)``; ``end_of_confirmed`` equals
    ``already_done`` when nothing new is settled.
    """
    start = max(0, int(already_done))
    if start >= len(audio):
        return start, start
    end = last_confirmed_segment_end(audio[start:], sr, **kw)
    if end is None:
        return start, start
    return start + end, start + end
