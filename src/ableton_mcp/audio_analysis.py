"""Warp vision: look at a clip's audio file the way a person warping it would.

Runs in the MCP server process, on the clip's audio file as it sits on the
local disk (Live reports the path; the server and Live share a machine,
because the socket is loopback-only). Nothing here talks to Live and nothing
leaves the machine — the file is read, analysed in memory, and the result
goes back to the user's own MCP client.

What a human warping a take looks at, and what this module computes:

- the waveform and a log-frequency spectrogram (kick in the bottom rows,
  hats at the top) — rendered to an image a vision model can read;
- transients (onsets), each placed at its attack rather than at an STFT
  frame centre, because a warp marker a few milliseconds late flams;
- a tempo and beat-grid estimate of the source material in its own time;
- per-band energy (low / mid / high) over time, for text-only callers;
- the clip's CURRENT warp mapping (``BeatMap``, built from Live's
  ``warp_markers``), which turns "where is the project grid inside this
  file" into arithmetic — and so turns "is this warped right" into a
  number (``grid_alignment``) that can be re-measured after an edit;
- proposed warp-marker edits that pull strong transients onto the grid
  (``suggest_edits``), in exactly the add/move/remove shape the Remote
  Script's ``edit_clip_warp_markers`` command applies.

Dependencies are the optional ``analysis`` extra (numpy, soundfile, Pillow).
They are imported lazily so the server, and every other tool, still works
without them; ``AnalysisUnavailable`` carries the install hint. Plain PCM
WAV files decode through the standard library when soundfile is missing.

Units, which are the whole game here:

- ``seconds`` are positions in the SOURCE FILE (Live's warp-marker
  ``sample_time`` is in the same unit);
- ``beats`` are CLIP beats (Live's warp-marker ``beat_time``; the clip's
  ``start_marker``/``loop_*`` are in these too while warping is on);
- ``grid_offset`` converts clip beats to song beats for an Arrangement clip
  (``start_time - start_marker``), so the drawn grid and the snapping
  targets are the SONG's bars, not the clip's private ones.
"""

from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Analysis resolution. Long files are decimated to ~22 kHz first: transients
# and the bands that matter for warping all live below 11 kHz, and halving
# the sample count halves every cost below. 1024/256 at 22.05 kHz is a
# 46 ms window stepped every 11.6 ms; attack refinement then works on the
# decimated waveform itself at ~0.05 ms resolution.
TARGET_SR = 22050
N_FFT = 1024
HOP = 256

# Hard ceiling on how much audio one call decodes. A full album side would
# otherwise be decoded into memory because a caller forgot a window.
MAX_ANALYSIS_SECONDS = 900.0

# Spectrogram frequency range (log axis). Below 30 Hz is rumble, and the
# decimated rate caps the top.
SPEC_FMIN = 30.0

BANDS = (("low", 0.0, 250.0), ("mid", 250.0, 4000.0), ("high", 4000.0, 1e9))

INSTALL_HINT = (
    "audio analysis needs the optional 'analysis' extra: "
    "`uv pip install 'ableton-mcp[analysis]'` "
    "(or `pip install numpy soundfile pillow` into the server's environment)"
)


class AnalysisUnavailable(RuntimeError):
    """The optional analysis dependencies are not installed."""


class AnalysisError(ValueError):
    """The audio could not be analysed (missing file, empty window, ...)."""


def _np():
    try:
        import numpy  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without numpy
        raise AnalysisUnavailable(INSTALL_HINT) from e
    return numpy


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # pragma: no cover - exercised only without Pillow
        raise AnalysisUnavailable(INSTALL_HINT) from e
    return Image, ImageDraw, ImageFont


def dependencies_available() -> Dict[str, bool]:
    """Which optional dependencies import — for diagnostics and test skips."""
    found = {}
    for name in ("numpy", "soundfile", "PIL"):
        try:
            __import__(name)
            found[name] = True
        except Exception:
            found[name] = False
    return found


# ── Decoding ─────────────────────────────────────────────────────────────────

@dataclass
class AudioWindow:
    """Mono, decimated audio for ``[start, start + len/sr)`` of the file."""

    samples: Any            # numpy float32, mono
    sr: int                 # rate of ``samples`` (after decimation)
    start: float            # file seconds of samples[0]
    file_duration: float    # whole file, seconds
    native_sr: int
    channels: int

    @property
    def end(self) -> float:
        return self.start + len(self.samples) / float(self.sr)


def _file_info(path: str) -> Tuple[int, int, int]:
    """(native sample rate, total frames, channels)."""
    try:
        import soundfile as sf
    except ImportError:
        sf = None
    if sf is not None:
        try:
            info = sf.info(path)
            return int(info.samplerate), int(info.frames), int(info.channels)
        except Exception as e:
            raise AnalysisError("cannot read audio file %r: %s" % (path, e))
    try:
        with wave.open(path, "rb") as reader:
            return (reader.getframerate(), reader.getnframes(),
                    reader.getnchannels())
    except Exception as e:
        raise AnalysisError(
            "cannot read %r without soundfile (only PCM WAV decodes through "
            "the standard library): %s; %s" % (path, e, INSTALL_HINT))


def _read_frames(path: str, first: int, count: int):
    """Frames [first, first+count) as float32 array shaped (n, channels)."""
    np = _np()
    try:
        import soundfile as sf
    except ImportError:
        sf = None
    if sf is not None:
        data, _ = sf.read(path, start=first, stop=first + count,
                          dtype="float32", always_2d=True)
        return data
    with wave.open(path, "rb") as reader:
        width = reader.getsampwidth()
        channels = reader.getnchannels()
        reader.setpos(first)
        raw = reader.readframes(count)
    if width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        data = ints.astype(np.float32) / float(1 << 23)
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise AnalysisError("unsupported WAV sample width %d" % width)
    return data.reshape(-1, channels)


def _decimate(x, factor: int):
    """Low-pass (windowed sinc) then keep every ``factor``-th sample."""
    np = _np()
    if factor <= 1:
        return x
    taps = 64 * factor + 1
    n = np.arange(taps) - (taps - 1) / 2.0
    cutoff = 0.45 / factor  # cycles/sample, a little under the new Nyquist
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    h = (h / h.sum()).astype(np.float32)
    # FFT convolution: np.convolve is O(n * taps), which for a long take at
    # 96 kHz is the slowest step of the whole analysis.
    size = len(x) + taps - 1
    nfft = 1 << (size - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(h, nfft), nfft)[:size]
    y = y[(taps - 1) // 2:(taps - 1) // 2 + len(x)]
    return y[::factor].astype(np.float32)


def load_audio(path: str, start: float = 0.0, end: Optional[float] = None,
               pad: float = 0.0) -> AudioWindow:
    """Decode ``[start - pad, end + pad]`` seconds of ``path`` to mono.

    ``pad`` exists for detection at the window's edges: an onset at the very
    first sample of a window has no "before" to compare against, so callers
    decode a little either side and crop the results.
    """
    np = _np()
    native_sr, total_frames, channels = _file_info(path)
    if native_sr <= 0 or total_frames <= 0:
        raise AnalysisError("audio file %r is empty" % path)
    file_duration = total_frames / float(native_sr)
    start = max(0.0, float(start) - pad)
    stop = file_duration if end is None else min(file_duration, float(end) + pad)
    if stop - start > MAX_ANALYSIS_SECONDS:
        stop = start + MAX_ANALYSIS_SECONDS
    if stop <= start:
        raise AnalysisError(
            "empty analysis window %.3f-%.3f s (file is %.3f s long)"
            % (start, stop, file_duration))
    first = int(round(start * native_sr))
    count = max(1, int(round((stop - start) * native_sr)))
    data = _read_frames(path, first, count)
    mono = data.mean(axis=1).astype(np.float32) if data.shape[1] > 1 \
        else data[:, 0].astype(np.float32)
    factor = max(1, int(native_sr // TARGET_SR))
    mono = _decimate(mono, factor)
    return AudioWindow(samples=mono, sr=native_sr // factor if factor > 1 else native_sr,
                       start=first / float(native_sr),
                       file_duration=file_duration,
                       native_sr=native_sr, channels=channels)


# ── Spectral analysis ────────────────────────────────────────────────────────

def stft_magnitude(x, n_fft: int = N_FFT, hop: int = HOP):
    """|STFT| as float32 (frames, bins), centred frames: frame i sits at
    i * hop / sr seconds from the first sample. Scaled so a full-scale sine
    peaks near 1.0 (roughly dBFS)."""
    np = _np()
    pad = n_fft // 2
    xp = np.pad(np.asarray(x, dtype=np.float32), (pad, pad))
    if len(xp) < n_fft:
        xp = np.pad(xp, (0, n_fft - len(xp)))
    window = np.hanning(n_fft).astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(xp, n_fft)[::hop]
    out = np.empty((frames.shape[0], n_fft // 2 + 1), dtype=np.float32)
    # Blocks bound the complex intermediate (a 5-minute take is ~26k frames).
    for s in range(0, frames.shape[0], 4096):
        block = frames[s:s + 4096] * window
        out[s:s + 4096] = np.abs(np.fft.rfft(block, axis=1))
    out *= 2.0 / window.sum()
    return out


def onset_envelope(mag):
    """Spectral flux on log magnitude, half-wave rectified, normalised to a
    0..1 peak. One value per STFT frame; frame 0 is 0."""
    np = _np()
    logmag = np.log1p(100.0 * mag)
    flux = np.zeros(mag.shape[0], dtype=np.float32)
    if mag.shape[0] > 1:
        diff = np.diff(logmag, axis=0)
        flux[1:] = np.maximum(diff, 0.0).sum(axis=1)
    peak = float(flux.max()) if flux.size else 0.0
    if peak > 0:
        flux /= peak
    return flux


def band_onset_envelopes(mag, sr: int) -> Dict[str, Any]:
    """Spectral flux per band (low/mid/high), each normalised to its own
    peak. What tells a kick from a hat at the same instant — and what the
    beat-phase search leans on, because the beat is usually where the low
    end hits, not where the brightest (and loudest-in-flux) hat is."""
    np = _np()
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    logmag = np.log1p(100.0 * mag)
    diff = np.zeros_like(logmag)
    if mag.shape[0] > 1:
        diff[1:] = np.maximum(np.diff(logmag, axis=0), 0.0)
    out = {}
    for name, lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        env = diff[:, sel].sum(axis=1) if sel.any() else np.zeros(mag.shape[0])
        peak = float(env.max()) if env.size else 0.0
        out[name] = env / peak if peak > 0 else env
    return out


def _moving(op, x, half: int):
    """Centred moving max/mean over 2*half+1 samples (edges padded)."""
    np = _np()
    if half <= 0:
        return x.copy()
    padded = np.pad(x, (half, half), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1)
    return op(view, axis=1)


def _refine_attack(samples, sr: int, t_guess: float,
                   before: float = 0.05, after: float = 0.03) -> float:
    """Pull a frame-level onset back to the attack itself.

    Spectral flux peaks somewhere inside a 46 ms window, which is far too
    coarse for a warp marker. In the waveform around the guess: take the
    1 ms RMS envelope, find its peak, and walk back to where it first rose
    above 20 % of the way from the pre-attack floor to that peak — the
    point Live's own transient markers sit on.
    """
    np = _np()
    a = max(0, int((t_guess - before) * sr))
    b = min(len(samples), int((t_guess + after) * sr))
    if b - a < 8:
        return t_guess
    seg = samples[a:b].astype(np.float64)
    k = max(1, int(sr * 0.001))
    env = np.sqrt(np.convolve(seg * seg, np.ones(k) / k, mode="same"))
    p = int(np.argmax(env))
    peak = env[p]
    floor = float(env[:p].min()) if p > 0 else 0.0
    if peak <= floor:
        return t_guess
    thr = floor + 0.2 * (peak - floor)
    # Search FORWARD from the quietest point before the peak. Walking back
    # from the peak instead stops early on low material: a 1 ms RMS of a
    # 60 Hz kick ripples at every zero crossing, and the first dip below
    # threshold put markers ~4 ms late.
    i = int(np.argmin(env[:p])) if p > 0 else 0
    while i < p and env[i] < thr:
        i += 1
    return (a + i) / float(sr)


@dataclass
class Transient:
    time: float       # file seconds, at the attack
    strength: float   # 0..1, relative to the strongest in the analysed audio
    band: str = "mid"  # where the onset's energy jump is largest: low/mid/high
    low: float = 0.0   # 0..1 low-band onset strength (kick/bass weight)

    @property
    def weight(self) -> float:
        """Ranking for "which hits define the beat": strength, with low-end
        hits favoured — on most material the kick is the beat and the hat
        between kicks is the loudest thing in a spectral-flux sense."""
        return self.strength * (0.6 + 0.8 * self.low)

    def is_strong(self, threshold: float) -> bool:
        """Strong overall OR strong in the low band: a kick under a bright
        mix can sit at 0.15 full-band strength and still be the downbeat."""
        return self.strength >= threshold or self.low >= threshold


def detect_transients(audio: AudioWindow, mag=None, flux=None,
                      sensitivity: float = 0.5,
                      min_gap: float = 0.05) -> List[Transient]:
    """Peak-pick the onset envelope and refine each pick to its attack.

    ``sensitivity`` 0..1 lowers the adaptive threshold as it rises (0.5 is a
    sane default for drums and most full mixes; raise it for soft material).
    """
    np = _np()
    if mag is None:
        mag = stft_magnitude(audio.samples)
    if flux is None:
        flux = onset_envelope(mag)
    if flux.size < 3 or float(flux.max()) <= 0:
        return []
    bands = band_onset_envelopes(mag, audio.sr)
    names = [name for name, _, _ in BANDS]
    fps = audio.sr / float(HOP)
    local_max = _moving(np.max, flux, max(1, int(round(0.03 * fps))))
    local_mean = _moving(np.mean, flux, max(1, int(round(0.10 * fps))))
    sens = min(1.0, max(0.0, float(sensitivity)))
    delta = 0.02 + 0.13 * (1.0 - sens)
    floor = 0.02 + 0.10 * (1.0 - sens)
    picks = np.nonzero((flux >= local_max) & (flux >= local_mean + delta)
                       & (flux >= floor))[0]
    out: List[Transient] = []
    for i in picks:
        t_rel = i * HOP / float(audio.sr)
        t_rel = _refine_attack(audio.samples, audio.sr, t_rel)
        t = audio.start + t_rel
        s = float(flux[i])
        # Band values are read as the max over the pick's neighbourhood:
        # a low band's flux can peak a frame later than a bright one's.
        lo_i, hi_i = max(0, i - 1), min(flux.size, i + 2)
        per = [float(bands[n][lo_i:hi_i].max()) for n in names]
        tr = Transient(t, s, names[per.index(max(per))], per[0])
        if out and t - out[-1].time < min_gap:
            if s > out[-1].strength:
                out[-1] = tr
            continue
        out.append(tr)
    return out


@dataclass
class TempoEstimate:
    bpm: float
    confidence: float          # peak prominence; < ~1.5 means "a guess"
    first_beat: float          # file seconds of the grid phase
    beat_period: float         # seconds
    candidates: List[float] = field(default_factory=list)

    def beats_between(self, start: float, end: float) -> List[float]:
        if self.beat_period <= 0:
            return []
        k0 = math.ceil((start - self.first_beat) / self.beat_period)
        k1 = math.floor((end - self.first_beat) / self.beat_period)
        return [self.first_beat + k * self.beat_period for k in range(k0, k1 + 1)]


def estimate_tempo(flux, sr: int, start: float = 0.0,
                   bpm_min: float = 60.0, bpm_max: float = 200.0,
                   prior_bpm: float = 120.0,
                   phase_env=None) -> Optional[TempoEstimate]:
    """Autocorrelation tempo with a log-normal prior around ``prior_bpm``,
    then a fine period/phase search of a beat comb over the whole envelope.

    The prior is what settles the octave question (90 vs 180) the way a
    listener would; pass the project tempo as ``prior_bpm`` to bias toward
    the octave the song is actually in. ``phase_env`` (the low-band flux)
    is mixed into the phase search so the grid lands on the kicks rather
    than on the off-beat hats that dominate full-band flux.
    """
    np = _np()
    fps = sr / float(HOP)
    env = np.asarray(flux, dtype=np.float64)
    if env.size < int(fps * 2) or env.max() <= 0:
        return None
    x = env - env.mean()
    n = 1 << (2 * x.size - 1).bit_length()
    ac = np.fft.irfft(np.abs(np.fft.rfft(x, n)) ** 2, n)[:x.size]
    if ac[0] <= 0:
        return None
    ac /= ac[0]
    lag_min = max(1, int(math.floor(60.0 * fps / bpm_max)))
    lag_max = min(x.size - 2, int(math.ceil(60.0 * fps / bpm_min)))
    if lag_max <= lag_min + 2:
        return None
    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / max(1.0, prior_bpm)) / 0.9) ** 2)
    score = ac[lags] * prior
    order = np.argsort(score)[::-1]
    best = int(lags[order[0]])
    candidates = []
    for j in order:
        b = round(float(bpms[j]), 1)
        if all(abs(b - c) > 3.0 for c in candidates):
            candidates.append(b)
        if len(candidates) == 3:
            break

    # Fine search: period within +-3 % of the coarse lag, every phase.
    comb_env = env
    if phase_env is not None and len(phase_env) == len(env):
        comb_env = env + 1.5 * np.asarray(phase_env, dtype=np.float64)
    smooth = np.convolve(comb_env, np.array([0.25, 0.5, 1.0, 0.5, 0.25]),
                         mode="same")
    best_score, best_p, best_phase = -1.0, float(best), 0.0
    for p in np.linspace(best * 0.97, best * 1.03, 121):
        count = int((smooth.size - 1) / p)
        if count < 2:
            continue
        k = np.arange(count) * p
        phases = np.arange(0.0, p, 0.5)
        idx = phases[:, None] + k[None, :]
        valid = idx < smooth.size - 1
        lo = np.floor(np.where(valid, idx, 0)).astype(int)
        frac = np.where(valid, idx, 0) - lo
        vals = smooth[lo] * (1 - frac) + smooth[np.minimum(lo + 1, smooth.size - 1)] * frac
        vals = np.where(valid, vals, 0.0)
        per = vals.sum(axis=1) / np.maximum(valid.sum(axis=1), 1)
        j = int(np.argmax(per))
        if per[j] > best_score:
            best_score, best_p, best_phase = float(per[j]), float(p), float(phases[j])
    mean_env = float(smooth.mean()) or 1e-9
    confidence = best_score / mean_env
    period = best_p / fps
    return TempoEstimate(
        bpm=60.0 / period, confidence=confidence,
        first_beat=start + best_phase / fps, beat_period=period,
        candidates=candidates)


def band_energy(mag, sr: int, start: float, points: int = 64
                ) -> Dict[str, Any]:
    """Mean power per band, pooled into ``points`` time bins, in dB."""
    np = _np()
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    power = mag.astype(np.float64) ** 2
    frames = power.shape[0]
    points = max(1, min(points, frames))
    edges = np.linspace(0, frames, points + 1).astype(int)
    out: Dict[str, Any] = {
        "times": [round(start + ((edges[i] + edges[i + 1]) / 2.0) * HOP / sr, 3)
                  for i in range(points)],
    }
    for name, lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        if not sel.any():
            out[name + "_db"] = [-120.0] * points
            continue
        band = power[:, sel].sum(axis=1)
        pooled = [band[edges[i]:max(edges[i] + 1, edges[i + 1])].mean()
                  for i in range(points)]
        out[name + "_db"] = [round(10 * math.log10(v + 1e-12), 1) for v in pooled]
    return out


# ── The clip's warp mapping ──────────────────────────────────────────────────

class BeatMap:
    """Live's warp mapping: piecewise-linear clip beats <-> file seconds.

    Built from ``warp_markers`` (beat_time, sample_time) pairs. Outside the
    first/last marker Live keeps the neighbouring segment's rate, and so does
    this; with a single marker the rate is ``seconds_per_beat`` (the project
    tempo — the best available guess, and flagged as such by callers).
    """

    def __init__(self, markers: Sequence[Tuple[float, float]],
                 seconds_per_beat: float = 0.5):
        pts = sorted((float(b), float(s)) for b, s in markers)
        if not pts:
            pts = [(0.0, 0.0)]
        self.beats = [p[0] for p in pts]
        self.seconds = [p[1] for p in pts]
        self.default_spb = float(seconds_per_beat) if seconds_per_beat > 0 else 0.5

    @classmethod
    def from_markers(cls, markers: Sequence[Dict[str, float]],
                     seconds_per_beat: float = 0.5) -> "BeatMap":
        return cls([(m["beat_time"], m["sample_time"]) for m in markers],
                   seconds_per_beat)

    def _segment_spb(self, i: int) -> float:
        db = self.beats[i + 1] - self.beats[i]
        ds = self.seconds[i + 1] - self.seconds[i]
        return ds / db if db > 0 else self.default_spb

    def beat_to_seconds(self, beat: float) -> float:
        b, s = self.beats, self.seconds
        if len(b) == 1:
            return s[0] + (beat - b[0]) * self.default_spb
        if beat <= b[0]:
            return s[0] + (beat - b[0]) * self._segment_spb(0)
        if beat >= b[-1]:
            return s[-1] + (beat - b[-1]) * self._segment_spb(len(b) - 2)
        i = _bisect(b, beat)
        return s[i] + (beat - b[i]) * self._segment_spb(i)

    def seconds_to_beat(self, sec: float) -> float:
        b, s = self.beats, self.seconds
        if len(b) == 1:
            return b[0] + (sec - s[0]) / self.default_spb
        if sec <= s[0]:
            spb = self._segment_spb(0)
            return b[0] + (sec - s[0]) / spb
        if sec >= s[-1]:
            spb = self._segment_spb(len(b) - 2)
            return b[-1] + (sec - s[-1]) / spb
        i = _bisect(s, sec)
        return b[i] + (sec - s[i]) / self._segment_spb(i)

    def bpm_at(self, sec: float) -> float:
        """The source tempo the mapping implies at ``sec`` (Live's Seg. BPM)."""
        b, s = self.beats, self.seconds
        if len(b) == 1:
            return 60.0 / self.default_spb
        i = min(max(_bisect(s, sec), 0), len(b) - 2)
        spb = self._segment_spb(i)
        return 60.0 / spb if spb > 0 else 0.0


def _bisect(values: Sequence[float], x: float) -> int:
    """Index i with values[i] <= x < values[i+1] (clamped to segments)."""
    lo, hi = 0, len(values) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if values[mid] <= x:
            lo = mid
        else:
            hi = mid
    return lo


def grid_alignment(transients: Sequence[Transient], beat_map: BeatMap,
                   subdivision: float = 0.25, grid_offset: float = 0.0,
                   min_strength: float = 0.3, tolerance: float = 0.35
                   ) -> Dict[str, Any]:
    """How far strong transients sit from the nearest grid line, in ms.

    Only transients within ``tolerance`` of a subdivision count — a
    syncopated ghost note between grid lines is not an error. This is the
    number to compare before and after a warp edit.
    """
    errors = []
    for tr in transients:
        if not tr.is_strong(min_strength):
            continue
        beat = beat_map.seconds_to_beat(tr.time)
        song = beat + grid_offset
        target = round(song / subdivision) * subdivision
        if abs(song - target) > tolerance * subdivision:
            continue
        target_sec = beat_map.beat_to_seconds(target - grid_offset)
        errors.append((tr.time - target_sec) * 1000.0)
    if not errors:
        return {"count": 0, "subdivision": subdivision}
    absolute = sorted(abs(e) for e in errors)
    return {
        "count": len(errors),
        "subdivision": subdivision,
        "median_abs_ms": round(absolute[len(absolute) // 2], 2),
        "p90_abs_ms": round(absolute[min(len(absolute) - 1,
                                         int(0.9 * len(absolute)))], 2),
        "max_abs_ms": round(absolute[-1], 2),
        "mean_signed_ms": round(sum(errors) / len(errors), 2),
    }


# ── Proposals ────────────────────────────────────────────────────────────────

def suggest_edits(transients: Sequence[Transient],
                  markers: Sequence[Dict[str, float]],
                  beat_map: BeatMap,
                  window: Tuple[float, float],
                  subdivision: float = 1.0,
                  grid_offset: float = 0.0,
                  tolerance: float = 0.35,
                  min_strength: float = 0.3,
                  min_shift_ms: float = 3.0,
                  max_edits: int = 32,
                  marker_snap_ms: float = 25.0,
                  ) -> Dict[str, Any]:
    """Propose warp-marker edits that put strong transients on the grid.

    The manual Live workflow, mechanised: for each strong transient near a
    grid line (strongest first), work out which grid line it belongs to under
    the CURRENT warping, and how far off it is. If it is off by more than
    ``min_shift_ms``:

    - an existing marker already sitting on the transient (within
      ``marker_snap_ms``) is MOVED to the grid line — exactly dragging it;
    - otherwise a marker is ADDED at the transient, pinned to the grid line
      (an existing marker parked on that grid line elsewhere in the audio is
      REMOVED first, since a beat can carry only one marker).

    Every proposal is kept only if the resulting marker set stays strictly
    increasing in both beat and sample time — Live refuses anything else.
    Returns ``{"edits": [...], "skipped": {...}, "markers_after": [...]}``
    where edits are wire-ready for edit_clip_warp_markers.
    """
    w0, w1 = window
    cands = [t for t in transients
             if w0 <= t.time <= w1 and t.is_strong(min_strength)]
    cands.sort(key=lambda t: -t.weight)

    # Working marker set: beat -> seconds. Keys are exact floats from Live.
    # Live's last warp marker is hidden in its UI and only sets the final
    # segment's rate (LOM docs), so it is never dragged or deleted here.
    current = {float(m["beat_time"]): float(m["sample_time"])
               for m in markers[:-1]} if len(markers) > 1 else \
        {float(m["beat_time"]): float(m["sample_time"]) for m in markers}
    hidden = dict((float(m["beat_time"]), float(m["sample_time"]))
                  for m in markers[-1:]) if len(markers) > 1 else {}
    planned: Dict[float, float] = dict(current)
    claimed_beats = set()
    edits: List[Dict[str, Any]] = []
    skipped = {"off_grid": 0, "already_aligned": 0, "conflict": 0,
               "limit": 0}

    def monotonic(trial: Dict[float, float]) -> bool:
        merged = dict(hidden)
        merged.update(trial)
        return _strictly_increasing(merged)

    for tr in cands:
        beat = beat_map.seconds_to_beat(tr.time)
        song = beat + grid_offset
        target_song = round(song / subdivision) * subdivision
        if abs(song - target_song) > tolerance * subdivision:
            skipped["off_grid"] += 1
            continue
        target = round(target_song - grid_offset, 6)
        shift_ms = (tr.time - beat_map.beat_to_seconds(target)) * 1000.0
        if abs(shift_ms) < min_shift_ms:
            skipped["already_aligned"] += 1
            continue
        if target in claimed_beats or target in hidden:
            skipped["conflict"] += 1
            continue
        if len(edits) >= max_edits:
            skipped["limit"] += 1
            continue

        trial = dict(planned)
        new_edits: List[Dict[str, Any]] = []
        # A marker already on this transient? Drag it.
        on_transient = [b for b, s in planned.items()
                        if b in current and b not in claimed_beats
                        and abs(s - tr.time) * 1000.0 <= marker_snap_ms]
        if on_transient:
            src = min(on_transient, key=lambda b: abs(planned[b] - tr.time))
            if abs(src - target) < 1e-6:
                # Marker is on the grid line already but not on the attack:
                # re-pin it to the attack (remove + add at the same beat).
                trial[target] = tr.time
                new_edits = [{"op": "remove", "beat_time": src},
                             {"op": "add", "beat_time": target,
                              "sample_time": round(tr.time, 6)}]
            else:
                if target in trial and target != src:
                    del trial[target]
                    new_edits.append({"op": "remove", "beat_time": target})
                sec = trial.pop(src)
                if abs(sec - tr.time) * 1000.0 > 0.5:
                    # Dragging keeps the marker's sample position, and this
                    # one is a few ms off the attack: replace it outright
                    # rather than move-then-re-pin, which _order_edits would
                    # reorder into a remove of a beat not yet occupied.
                    trial[target] = tr.time
                    new_edits += [{"op": "remove", "beat_time": src},
                                  {"op": "add", "beat_time": target,
                                   "sample_time": round(tr.time, 6)}]
                else:
                    trial[target] = sec
                    new_edits.append({"op": "move", "beat_time": src,
                                      "new_beat_time": target})
        else:
            if target in trial:
                new_edits.append({"op": "remove", "beat_time": target})
            trial[target] = tr.time
            new_edits.append({"op": "add", "beat_time": target,
                              "sample_time": round(tr.time, 6)})
        if not monotonic(trial):
            skipped["conflict"] += 1
            continue
        planned = trial
        claimed_beats.add(target)
        for e in new_edits:
            e["transient_time"] = round(tr.time, 6)
            e["shift_ms"] = round(shift_ms, 2)
            e["strength"] = round(tr.strength, 3)
        edits.extend(new_edits)

    planned.update(hidden)
    markers_after = [{"beat_time": b, "sample_time": round(s, 6)}
                     for b, s in sorted(planned.items())]
    return {"edits": _order_edits(edits), "skipped": skipped,
            "markers_after": markers_after}


def _strictly_increasing(state: Dict[float, float]) -> bool:
    """Live's warp invariant: beat order and sample order agree, with no
    two markers on the same sample position."""
    pts = sorted(state.items())
    return all(pts[i + 1][1] > pts[i][1] for i in range(len(pts) - 1))


def rewarp_edits(transients: Sequence[Transient],
                 markers: Sequence[Dict[str, float]],
                 beat_map: BeatMap,
                 window: Tuple[float, float],
                 subdivision: float = 1.0,
                 grid_offset: float = 0.0,
                 tolerance: float = 0.3,
                 min_strength: float = 0.2,
                 max_edits: int = 64,
                 tempo: Optional[TempoEstimate] = None,
                 ) -> Dict[str, Any]:
    """Re-warp the window from scratch by tracking the beat left to right.

    For takes whose current warping DRIFTS (a live drummer, or Live's
    Auto-Warp guessing 120 for a 123 BPM file): snapping each transient to
    its nearest grid line under the current mapping fails there, because
    after a few bars the nearest line is the wrong one. A person fixes that
    by warping from the left, one marker at a time, each marker correcting
    the tempo used to find the next; so does this:

    1. anchor on the first strong low-weighted transient within tolerance
       of a grid line under the current mapping;
    2. predict the next grid line's time from the last anchor and the local
       beat period (seeded from the tempo estimate when it agrees in octave
       with the current mapping, else from the mapping itself);
    3. take the strongest transient within ``tolerance`` of the prediction,
       pin a marker there, and update the period from the last two anchors.

    Existing markers between the first and last new marker are removed
    (they are the warping being replaced); markers outside are kept, and
    proposals that would cross them are dropped from the end.
    """
    w0, w1 = window
    cands = sorted((t for t in transients
                    if w0 <= t.time <= w1 and t.is_strong(min_strength)),
                   key=lambda t: t.time)
    empty = {"edits": [], "skipped": {}, "markers_after": list(markers)}
    if not cands:
        empty["skipped"] = {"no_transients": 1}
        return empty

    def snap(beat: float) -> float:
        song = beat + grid_offset
        return round(song / subdivision) * subdivision - grid_offset

    anchor = None
    for tr in sorted(cands[:16], key=lambda t: -t.weight):
        beat = beat_map.seconds_to_beat(tr.time)
        if abs(beat - snap(beat)) <= tolerance * subdivision:
            if anchor is None or tr.time < anchor[1]:
                anchor = (round(snap(beat), 6), tr.time)
    if anchor is None:
        empty["skipped"] = {"no_anchor": 1}
        return empty

    spb = 60.0 / max(1e-6, beat_map.bpm_at(anchor[1]))
    if tempo is not None and tempo.confidence >= 1.5:
        est = tempo.beat_period
        for mult in (0.5, 1.0, 2.0):
            if abs(est * mult / spb - 1.0) < 0.15:
                spb = est * mult
                break

    placed = [anchor]
    b, t = anchor
    misses = 0
    while True:
        b_next = round(b + subdivision, 6)
        t_pred = t + subdivision * spb
        if t_pred > w1:
            break
        reach = tolerance * subdivision * spb
        near = [c for c in cands if abs(c.time - t_pred) <= reach]
        if near:
            best = max(near, key=lambda c: c.weight
                       * (1.0 - 0.5 * abs(c.time - t_pred) / reach))
            placed.append((b_next, best.time))
            if len(placed) >= 2:
                (bp, tp), (bq, tq) = placed[-2], placed[-1]
                spb = 0.5 * spb + 0.5 * (tq - tp) / (bq - bp)
            b, t = b_next, best.time
            misses = 0
        else:
            # No hit where the beat should be (a rest, a fill): carry the
            # prediction forward on the current period.
            b, t = b_next, t_pred
            misses += 1
            if misses > 8:
                break

    hidden_beat = float(markers[-1]["beat_time"]) if len(markers) > 1 else None
    if hidden_beat is not None:
        # Nothing at or past Live's hidden end marker: it owns its beat, and
        # thinning below must not pick a placement that is then refused.
        hidden_sec = float(markers[-1]["sample_time"])
        placed = [p for p in placed if p[0] < hidden_beat and p[1] < hidden_sec]
        if not placed:
            empty["skipped"] = {"conflict": 1}
            return empty
    if len(placed) > max_edits:
        step = len(placed) / float(max_edits)
        placed = [placed[int(i * step)] for i in range(max_edits - 1)] + [placed[-1]]

    first_t, last_t = placed[0][1], placed[-1][1]
    first_b, last_b = placed[0][0], placed[-1][0]
    edits: List[Dict[str, Any]] = []
    state = {float(m["beat_time"]): float(m["sample_time"]) for m in markers}
    for m in markers:
        mb, ms = float(m["beat_time"]), float(m["sample_time"])
        if mb == hidden_beat:
            continue
        inside = (first_t - 1e-4 <= ms <= last_t + 1e-4
                  or first_b - 1e-6 <= mb <= last_b + 1e-6)
        if inside:
            edits.append({"op": "remove", "beat_time": mb})
            state.pop(mb, None)
    adds = []
    for pb, pt in placed:
        trial = dict(state)
        trial[pb] = pt
        if pb == hidden_beat or not _strictly_increasing(trial):
            break
        state = trial
        adds.append({"op": "add", "beat_time": pb, "sample_time": round(pt, 6),
                     "transient_time": round(pt, 6)})
    if not adds:
        empty["skipped"] = {"conflict": 1}
        return empty
    edits.extend(adds)
    return {"edits": edits,
            "skipped": {"dropped_at_end": len(placed) - len(adds)},
            "markers_after": [{"beat_time": k, "sample_time": round(v, 6)}
                              for k, v in sorted(state.items())]}


def _order_edits(edits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Removes first, then moves, then adds — the order in which a batch can
    never trip over its own intermediate state (a move onto a beat that a
    later remove frees, an add between two markers a later move separates).
    Moves run in the direction that keeps them from crossing: rightward
    moves from the right end, leftward from the left."""
    removes = [e for e in edits if e["op"] == "remove"]
    moves = [e for e in edits if e["op"] == "move"]
    adds = [e for e in edits if e["op"] == "add"]
    # A remove that a later add on the same beat restores is part of a
    # re-pin; keep each remove exactly once.
    seen = set()
    unique_removes = []
    for e in removes:
        if e["beat_time"] not in seen:
            seen.add(e["beat_time"])
            unique_removes.append(e)
    right = sorted((m for m in moves if m["new_beat_time"] > m["beat_time"]),
                   key=lambda m: -m["beat_time"])
    left = sorted((m for m in moves if m["new_beat_time"] <= m["beat_time"]),
                  key=lambda m: m["beat_time"])
    adds.sort(key=lambda a: a["beat_time"])
    return unique_removes + right + left + adds


def apply_edits_to_markers(markers: Sequence[Dict[str, float]],
                           edits: Sequence[Dict[str, Any]]
                           ) -> List[Dict[str, float]]:
    """What the marker list becomes after ``edits`` — the dry-run model of
    the Remote Script's batch command, used for the "after" preview."""
    state = {float(m["beat_time"]): float(m["sample_time"]) for m in markers}
    for e in edits:
        op = e.get("op")
        b = float(e.get("beat_time", 0.0))
        if op == "remove":
            state.pop(b, None)
        elif op == "move":
            new_b = float(e["new_beat_time"]) if "new_beat_time" in e \
                else b + float(e.get("distance", 0.0))
            if b in state:
                state[new_b] = state.pop(b)
        elif op == "add":
            if "sample_time" in e and e["sample_time"] is not None:
                state[b] = float(e["sample_time"])
    return [{"beat_time": b, "sample_time": s} for b, s in sorted(state.items())]


def tempo_edits(tempo: TempoEstimate, markers: Sequence[Dict[str, float]],
                beat_map: BeatMap, window: Tuple[float, float],
                transients: Sequence[Transient] = (),
                grid_offset: float = 0.0, beats_per_bar: int = 4,
                snap_ms: float = 30.0) -> Dict[str, Any]:
    """Replace the window's warping with the estimated tempo: two markers,
    one on the first strong downbeat and one a whole number of bars later,
    pinned to consecutive grid bars. Every existing marker strictly inside
    the window is removed. For material recorded to a click or a machine
    where the tempo is constant — use mode="transients" for live playing.
    """
    w0, w1 = window
    beats = tempo.beats_between(w0, w1)
    if len(beats) < beats_per_bar + 1:
        return {"edits": [], "skipped": {"too_short": 1},
                "markers_after": list(markers)}

    def snap(t: float) -> float:
        near = [tr.time for tr in transients if abs(tr.time - t) * 1000 <= snap_ms]
        return min(near, key=lambda x: abs(x - t)) if near else t

    t0 = snap(beats[0])
    bars = (len(beats) - 1) // beats_per_bar
    t1 = snap(beats[bars * beats_per_bar])
    b0 = round(beat_map.seconds_to_beat(t0) + grid_offset) - grid_offset
    b1 = b0 + bars * beats_per_bar
    edits: List[Dict[str, Any]] = []
    # The last marker is Live's hidden rate marker; leave it alone.
    for m in (markers[:-1] if len(markers) > 1 else markers):
        if w0 <= m["sample_time"] <= w1 or b0 <= m["beat_time"] <= b1:
            edits.append({"op": "remove", "beat_time": float(m["beat_time"])})
    edits.append({"op": "add", "beat_time": round(b0, 6), "sample_time": round(t0, 6)})
    edits.append({"op": "add", "beat_time": round(b1, 6), "sample_time": round(t1, 6)})
    after = apply_edits_to_markers(markers, edits)
    pts = sorted((m["beat_time"], m["sample_time"]) for m in after)
    if any(pts[i + 1][1] <= pts[i][1] for i in range(len(pts) - 1)):
        return {"edits": [], "skipped": {"conflict": 1},
                "markers_after": list(markers)}
    return {"edits": edits, "skipped": {}, "markers_after": after}


# ── Rendering ────────────────────────────────────────────────────────────────

# A perceptual dark-to-bright ramp (black, indigo, magenta-red, orange,
# pale yellow). Hand-picked anchors, not copied from any colormap table.
_RAMP = ((0.0, (4, 4, 12)), (0.25, (52, 16, 104)), (0.5, (170, 40, 92)),
         (0.75, (246, 120, 36)), (1.0, (252, 250, 180)))

COLOURS = {
    "bg": (18, 18, 24), "panel": (26, 27, 34), "text": (220, 220, 228),
    "dim": (130, 132, 145), "wave": (120, 170, 220), "beat": (70, 72, 86),
    "bar": (150, 152, 170), "marker": (255, 210, 40), "transient": (60, 230, 230),
    "proposed": (255, 70, 200), "est": (120, 220, 120),
    "low": (230, 90, 70), "mid": (240, 200, 80), "high": (110, 180, 255),
}


def _lut():
    np = _np()
    xs = [a for a, _ in _RAMP]
    lut = np.zeros((256, 3), dtype=np.uint8)
    t = np.linspace(0, 1, 256)
    for c in range(3):
        lut[:, c] = np.interp(t, xs, [col[c] for _, col in _RAMP]).astype(np.uint8)
    return lut


def _font(size: int = 11):
    _, _, ImageFont = _pil()
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: bitmap default only
        return ImageFont.load_default()


@dataclass
class Panel:
    """One waveform strip with its own grid/marker overlays (before/after)."""
    title: str
    beat_map: BeatMap
    markers: Sequence[Dict[str, float]]
    marker_colour: str = "marker"


def render(audio: AudioWindow, mag, window: Tuple[float, float],
           panels: Sequence[Panel], transients: Sequence[Transient],
           title: str, grid_offset: float = 0.0, beats_per_bar: int = 4,
           tempo: Optional[TempoEstimate] = None,
           proposed: Sequence[Dict[str, Any]] = (),
           width: int = 1200, spec_height: int = 240,
           wave_height: int = 110, fmt: str = "png") -> bytes:
    """Waveform panel(s) + band strip + log-frequency spectrogram + time axis.

    Overlays, each in one colour throughout: the song grid (bars bright,
    beats dim, bar numbers on top), warp markers (yellow, beat label on the
    flag), detected transients (cyan ticks, height = strength), the
    estimated source beat grid (green dots on the axis), and proposed
    markers (magenta).
    """
    np = _np()
    Image, ImageDraw, _ = _pil()
    w0, w1 = window
    span = max(1e-6, w1 - w0)
    left, right = 46, 12
    plot_w = max(100, int(width) - left - right)
    font = _font(11)
    small = _font(10)
    header_h, band_h, axis_h, gap = 34, 30, 22, 6
    height = (header_h + len(panels) * (wave_height + gap) + band_h + gap
              + spec_height + axis_h)
    img = Image.new("RGB", (left + plot_w + right, height), COLOURS["bg"])
    draw = ImageDraw.Draw(img)

    def x_of(t: float) -> float:
        return left + (t - w0) / span * plot_w

    # Header: title and legend.
    draw.text((left, 3), title, fill=COLOURS["text"], font=font)
    legend = [("warp marker", "marker"), ("transient", "transient"),
              ("bar", "bar"), ("est. source beat", "est")]
    if proposed:
        legend.insert(1, ("proposed marker", "proposed"))
    lx = left
    for label, key in legend:
        draw.rectangle([lx, 22, lx + 8, 30], fill=COLOURS[key])
        draw.text((lx + 12, 19), label, fill=COLOURS["dim"], font=small)
        lx += 12 + int(draw.textlength(label, font=small)) + 16

    # Column ranges of the decimated audio for the waveform.
    sr = audio.sr
    s_idx0 = int((w0 - audio.start) * sr)
    s_idx1 = int((w1 - audio.start) * sr)
    cols = np.linspace(s_idx0, s_idx1, plot_w + 1).astype(int)
    x = audio.samples
    mins = np.zeros(plot_w)
    maxs = np.zeros(plot_w)
    for c in range(plot_w):
        a, b = max(0, cols[c]), min(len(x), max(cols[c] + 1, cols[c + 1]))
        if b > a:
            seg = x[a:b]
            mins[c], maxs[c] = float(seg.min()), float(seg.max())
    peak = max(1e-6, float(max(abs(mins.min()), abs(maxs.max()))))

    def grid_lines(beat_map: BeatMap):
        """(x, is_bar, bar_number) for song-grid beats inside the window."""
        b0 = beat_map.seconds_to_beat(w0) + grid_offset
        b1 = beat_map.seconds_to_beat(w1) + grid_offset
        nbeats = b1 - b0
        step = 1
        if nbeats > plot_w / 6:
            step = beats_per_bar  # beats too dense: bars only
            while nbeats / step > plot_w / 30:
                step *= 2
        out = []
        for song_beat in range(int(math.ceil(b0)), int(math.floor(b1)) + 1):
            is_bar = song_beat % beats_per_bar == 0
            if song_beat % step:
                continue
            t = beat_map.beat_to_seconds(song_beat - grid_offset)
            out.append((x_of(t), is_bar, song_beat // beats_per_bar + 1))
        return out

    y = header_h
    for panel in panels:
        top, bottom = y, y + wave_height
        draw.rectangle([left, top, left + plot_w, bottom], fill=COLOURS["panel"])
        lines = grid_lines(panel.beat_map)
        label_every = max(1, int(len([l for l in lines if l[1]]) / 24) + 1)
        bar_seen = 0
        for gx, is_bar, bar_no in lines:
            draw.line([gx, top, gx, bottom],
                      fill=COLOURS["bar" if is_bar else "beat"], width=1)
            if is_bar:
                if bar_seen % label_every == 0:
                    draw.text((gx + 2, top + 1), str(bar_no),
                              fill=COLOURS["bar"], font=small)
                bar_seen += 1
        mid = (top + bottom) / 2.0
        half = (wave_height - 16) / 2.0
        for c in range(plot_w):
            draw.line([left + c, mid - maxs[c] / peak * half,
                       left + c, mid - mins[c] / peak * half],
                      fill=COLOURS["wave"])
        for tr in transients:
            if w0 <= tr.time <= w1:
                tx = x_of(tr.time)
                h = 4 + tr.strength * 14
                draw.line([tx, bottom - h, tx, bottom], fill=COLOURS["transient"],
                          width=1)
        last_label = -1e9
        for m in panel.markers:
            if w0 <= m["sample_time"] <= w1:
                mx = x_of(m["sample_time"])
                draw.line([mx, top, mx, bottom], fill=COLOURS[panel.marker_colour],
                          width=2)
                draw.polygon([(mx - 4, top), (mx + 4, top), (mx, top + 6)],
                             fill=COLOURS[panel.marker_colour])
                # Dense markers (a rewarp puts one on every beat) would
                # print over each other; label only where there is room.
                label = "%g" % round(m["beat_time"], 3)
                if mx - last_label >= draw.textlength(label, font=small) + 8:
                    draw.text((mx + 3, top + 14), label,
                              fill=COLOURS[panel.marker_colour], font=small)
                    last_label = mx
        draw.text((4, top + 2), panel.title[:7], fill=COLOURS["dim"], font=small)
        y = bottom + gap

    # Band strip: low / mid / high energy as three thin heat rows.
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    f0 = int(max(0, (w0 - audio.start) * sr / HOP))
    f1 = int(min(mag.shape[0], max(f0 + 1, (w1 - audio.start) * sr / HOP)))
    win = mag[f0:f1]
    fcols = np.linspace(0, win.shape[0], plot_w + 1).astype(int)
    row_h = band_h // 3
    for r, (name, lo, hi) in enumerate(BANDS):
        sel = (freqs >= lo) & (freqs < hi)
        band = (win[:, sel] ** 2).sum(axis=1) if sel.any() else np.zeros(win.shape[0])
        db = 10 * np.log10(band + 1e-12)
        top_db = float(db.max()) if db.size else 0.0
        base = COLOURS[name]
        for c in range(plot_w):
            a, b = fcols[c], max(fcols[c] + 1, fcols[c + 1])
            v = float(db[a:b].max()) if b <= db.size and b > a else -120.0
            k = min(1.0, max(0.0, (v - (top_db - 50.0)) / 50.0))
            draw.line([left + c, y + r * row_h, left + c, y + (r + 1) * row_h - 1],
                      fill=tuple(int(ch * k) for ch in base))
        draw.text((4, y + r * row_h - 1), name, fill=base, font=small)
    y += band_h + gap

    # Spectrogram, log-frequency rows, 80 dB range below the window's peak.
    top, bottom = y, y + spec_height
    nyq = sr / 2.0
    row_f = np.geomspace(SPEC_FMIN, nyq * 0.98, spec_height)[::-1]
    row_bins = row_f / nyq * (mag.shape[1] - 1)
    pooled = np.zeros((plot_w, mag.shape[1]), dtype=np.float32)
    for c in range(plot_w):
        a, b = fcols[c], max(fcols[c] + 1, fcols[c + 1])
        if b <= win.shape[0] and b > a:
            pooled[c] = win[a:b].max(axis=0)
    db = 20 * np.log10(pooled + 1e-9)
    ref = float(db.max()) if db.size else 0.0
    norm = np.clip((db - (ref - 80.0)) / 80.0, 0, 1)
    lo_bin = np.floor(row_bins).astype(int)
    frac = row_bins - lo_bin
    hi_bin = np.minimum(lo_bin + 1, mag.shape[1] - 1)
    rows = norm[:, lo_bin] * (1 - frac) + norm[:, hi_bin] * frac  # (plot_w, H)
    lut = _lut()
    rgb = lut[(rows.T * 255).astype(np.uint8)]
    img.paste(Image.fromarray(rgb, "RGB"), (left, top))
    for f in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
        if SPEC_FMIN <= f < nyq:
            fy = top + (1 - math.log(f / SPEC_FMIN) / math.log(nyq * 0.98 / SPEC_FMIN)) * spec_height
            draw.text((2, fy - 6), ("%dk" % (f // 1000)) if f >= 1000 else str(f),
                      fill=COLOURS["dim"], font=small)
            draw.line([left - 3, fy, left, fy], fill=COLOURS["dim"])
    last = panels[-1]
    for gx, is_bar, _ in grid_lines(last.beat_map):
        if is_bar:
            draw.line([gx, top, gx, top + 8], fill=COLOURS["bar"])
            draw.line([gx, bottom - 8, gx, bottom], fill=COLOURS["bar"])
    for m in last.markers:
        if w0 <= m["sample_time"] <= w1:
            mx = x_of(m["sample_time"])
            draw.line([mx, top, mx, bottom], fill=COLOURS[last.marker_colour], width=1)
    for p in proposed:
        if p.get("op") == "add" and w0 <= p.get("sample_time", -1) <= w1:
            px = x_of(p["sample_time"])
            draw.line([px, top, px, bottom], fill=COLOURS["proposed"], width=1)
    y = bottom

    # Time axis + the estimated source beat grid as dots.
    draw.line([left, y, left + plot_w, y], fill=COLOURS["dim"])
    step = _nice_step(span, plot_w / 90.0)
    t = math.ceil(w0 / step) * step
    while t <= w1 + 1e-9:
        tx = x_of(t)
        draw.line([tx, y, tx, y + 4], fill=COLOURS["dim"])
        draw.text((tx + 2, y + 4), _fmt_time(t, step), fill=COLOURS["dim"], font=small)
        t += step
    if tempo is not None:
        for bt in tempo.beats_between(w0, w1):
            bx = x_of(bt)
            draw.ellipse([bx - 1.5, y - 2, bx + 1.5, y + 1], fill=COLOURS["est"])

    buf = io.BytesIO()
    if fmt == "jpeg":
        img.save(buf, format="JPEG", quality=85, optimize=True)
    else:
        # A 256-colour palette: a real drum stem's spectrogram is noise to a
        # truecolour PNG (~450 KB at 1200 px); quantised it is ~130 KB with
        # every line and label still crisp, which JPEG cannot promise.
        img = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE) \
            if hasattr(Image, "Quantize") else img.quantize(colors=256)
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _nice_step(span: float, max_ticks: float) -> float:
    raw = span / max(1.0, max_ticks)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def _fmt_time(t: float, step: float) -> str:
    if step >= 60:
        return "%d:%02d" % (int(t // 60), int(t % 60))
    if step >= 1:
        return ("%d:%02d" % (int(t // 60), int(t % 60))) if t >= 60 else "%ds" % int(round(t))
    decimals = max(0, -int(math.floor(math.log10(step))))
    return ("%." + str(decimals) + "fs") % t


# ── One-call orchestration ───────────────────────────────────────────────────

@dataclass
class Analysis:
    audio: AudioWindow
    mag: Any
    window: Tuple[float, float]
    transients: List[Transient]
    tempo: Optional[TempoEstimate]
    bands: Dict[str, Any]


def analyse(path: str, window: Tuple[float, float],
            sensitivity: float = 0.5, prior_bpm: float = 120.0,
            band_points: int = 64) -> Analysis:
    """Decode the window (padded), run every detector, crop to the window."""
    w0, w1 = window
    audio = load_audio(path, w0, w1, pad=0.5)
    w0 = max(w0, audio.start)
    w1 = min(w1, audio.end)
    if w1 - w0 < 0.05:
        raise AnalysisError("analysis window is shorter than 50 ms")
    mag = stft_magnitude(audio.samples)
    flux = onset_envelope(mag)
    transients = [t for t in detect_transients(audio, mag, flux, sensitivity)
                  if w0 <= t.time <= w1]
    f0 = int(max(0, (w0 - audio.start) * audio.sr / HOP))
    f1 = int(min(len(flux), (w1 - audio.start) * audio.sr / HOP + 1))
    low = band_onset_envelopes(mag, audio.sr)["low"]
    tempo = estimate_tempo(flux[f0:f1], audio.sr,
                           start=audio.start + f0 * HOP / float(audio.sr),
                           prior_bpm=prior_bpm, phase_env=low[f0:f1])
    bands = band_energy(mag[f0:f1], audio.sr,
                        audio.start + f0 * HOP / float(audio.sr), band_points)
    return Analysis(audio, mag, (w0, w1), transients, tempo, bands)
