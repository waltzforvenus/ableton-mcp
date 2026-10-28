"""Model: measure and verify a rendered audio file — loudness, peaks, silence,
clipping and whether the tail was cut — without anyone having to listen.

Bounces went out quieter than the mix sounded in Live, truncated before the
effect tails, clipped, or silent from a stray solo, and every check was an
ad-hoc ffmpeg/pyloudnorm script. This module is the one check:

- ``ffprobe`` for the container facts (duration, rate, channels, bit depth);
- one ``ffmpeg`` pass with ``ebur128=peak=true`` (integrated loudness, LRA,
  true peak — ITU-R BS.1770 as ffmpeg implements it), ``astats`` (sample
  peak, RMS) and ``silencedetect`` (leading/trailing silence, silence %);
- a second short pass on the last 50 ms (``-sseof``): sound still above
  -60 dBFS at the very end means the render stopped mid-tail;
- when numpy is available (the ``analysis`` extra), the warp-vision
  decoder (``audio_analysis.read_frames``) at full rate for the clipped-
  sample count and the L/R balance and correlation, and as the fallback
  for peak / RMS / silence / tail when ffmpeg is missing.

Subprocesses only, local files only: no network, nothing leaves the machine.
The ffmpeg binary is found by the composition root (or PATH, or the usual
Homebrew prefixes) and passed in.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

RunResult = Tuple[int, str, str]
Runner = Callable[[Sequence[str], float], RunResult]

SILENCE_DB = -60.0          # below this counts as silence
SILENCE_MIN_S = 0.05        # shortest gap reported as silence
TAIL_WINDOW_S = 0.05        # "still sounding at the cut" is measured here
CLIP_LEVEL = 0.99997        # |x| at or above this counts as a clipped sample

_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _default_runner(args: Sequence[str], timeout: float) -> RunResult:
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


def find_tool(name: str, override: Optional[str] = None) -> Optional[str]:
    """An executable path for ``name`` (ffmpeg/ffprobe): the override, PATH,
    then the usual Homebrew prefixes — an MCP server launched by an agent
    often has a minimal PATH."""
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        return override
    found = shutil.which(name)
    if found:
        return found
    for folder in _COMMON_BIN_DIRS:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _num(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text)
    if not m:
        return None
    raw = m.group(1)
    if raw.lower() in ("-inf", "inf", "nan"):
        return float(raw.lower().replace("nan", "inf")) if "inf" in raw.lower() else None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_ffmpeg_summary(stderr: str) -> Dict[str, Any]:
    """The numbers out of one ``ebur128,astats,silencedetect`` pass."""
    summary = stderr.split("Summary:", 1)[1] if "Summary:" in stderr else ""
    silences: List[Dict[str, float]] = []
    start = None
    for line in stderr.splitlines():
        s = _num(r"silence_start:\s*([-\d.]+)", line)
        if s is not None:
            start = s
        e = _num(r"silence_end:\s*([-\d.]+)", line)
        if e is not None:
            silences.append({"start": max(0.0, start or 0.0), "end": e})
            start = None
    if start is not None:
        silences.append({"start": max(0.0, start), "end": None})
    return {
        "lufs_i": _num(r"I:\s*([-\d.]+|-inf)\s*LUFS", summary),
        "lra": _num(r"LRA:\s*([-\d.]+)\s*LU\b", summary),
        "true_peak_dbtp": _num(r"Peak:\s*([-\d.]+|-inf)\s*dBFS", summary),
        "peak_dbfs": _num(r"Peak level dB:\s*([-\d.]+|-inf)", stderr),
        "rms_db": _num(r"RMS level dB:\s*([-\d.]+|-inf)", stderr),
        "silences": silences,
    }


def _probe(ffprobe: str, path: str, run: Runner) -> Dict[str, Any]:
    code, out, err = run([ffprobe, "-v", "error", "-show_entries",
                          "format=duration:stream=sample_rate,channels,"
                          "bits_per_sample,bits_per_raw_sample,sample_fmt,codec_name",
                          "-select_streams", "a:0", "-of", "json", path], 30.0)
    if code != 0:
        raise ValueError(f"ffprobe could not read {path}: {err.strip() or out.strip()}")
    data = json.loads(out or "{}")
    stream = (data.get("streams") or [{}])[0]
    bits = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
    try:
        bits = int(bits) or None
    except (TypeError, ValueError):
        bits = None
    if bits is None and str(stream.get("sample_fmt", "")).startswith("flt"):
        bits = 32
    return {
        "duration_s": float((data.get("format") or {}).get("duration") or 0.0),
        "sample_rate": int(stream.get("sample_rate") or 0) or None,
        "channels": int(stream.get("channels") or 0) or None,
        "bit_depth": bits,
        "codec": stream.get("codec_name"),
    }


def _numpy_stats(path: str, start: float, end: Optional[float]) -> Optional[Dict[str, Any]]:
    """Clipping, balance and correlation (plus fallback levels) through the
    warp-vision decoder at full rate; None without numpy."""
    try:
        from . import audio_analysis
        np = audio_analysis._np()
        rate, frames, channels = audio_analysis.file_info(path)
    except Exception:
        return None
    first = int(start * rate)
    last = frames if end is None else min(frames, int(end * rate))
    if last <= first:
        return None
    data = audio_analysis.read_frames(path, first, last - first)
    absx = np.abs(data)
    peak = float(absx.max()) if absx.size else 0.0
    mono = data.mean(axis=1)
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
    out: Dict[str, Any] = {
        "clipped_samples": int((absx >= CLIP_LEVEL).sum()),
        "peak_dbfs": 20 * math.log10(peak) if peak > 0 else float("-inf"),
        "rms_db": 20 * math.log10(rms) if rms > 0 else float("-inf"),
    }
    tail = absx[-max(1, int(TAIL_WINDOW_S * rate)):]
    tail_peak = float(tail.max()) if tail.size else 0.0
    out["tail_peak_dbfs"] = 20 * math.log10(tail_peak) if tail_peak > 0 else float("-inf")
    threshold = 10 ** (SILENCE_DB / 20.0)
    loud = np.where(absx.max(axis=1) > threshold)[0]
    out["leading_silence_s"] = (float(loud[0]) / rate) if loud.size else (last - first) / rate
    out["trailing_silence_s"] = (float(len(absx) - 1 - loud[-1]) / rate) if loud.size else (last - first) / rate
    if data.shape[1] >= 2:
        left, right = data[:, 0].astype(np.float64), data[:, 1].astype(np.float64)
        el, er = float(np.sum(left ** 2)), float(np.sum(right ** 2))
        out["lr_balance_db"] = (10 * math.log10(el / er)) if el > 0 and er > 0 else None
        denom = math.sqrt(el * er)
        out["lr_correlation"] = float(np.sum(left * right) / denom) if denom > 0 else None
    return out


def _finite(x: Optional[float]) -> Optional[float]:
    """JSON has no -inf: report "no signal" levels as None-with-a-flag."""
    if x is None or math.isinf(x) or math.isnan(x):
        return None
    return round(x, 2)


def analyze(path: str, start_s: Optional[float] = None,
            end_s: Optional[float] = None, ffmpeg: Optional[str] = None,
            ffprobe: Optional[str] = None,
            runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Every measurement for ``path`` (or ``[start_s, end_s]`` of it)."""
    path = os.path.expanduser(path or "")
    if not os.path.isfile(path):
        raise ValueError(f"no such audio file: {path!r}")
    run = runner or _default_runner
    ffmpeg_bin = find_tool("ffmpeg", ffmpeg)
    ffprobe_bin = find_tool("ffprobe", ffprobe)
    result: Dict[str, Any] = {"path": path, "engine": [], "warnings": []}
    if ffprobe_bin:
        result.update(_probe(ffprobe_bin, path, run))
        result["engine"].append("ffprobe")
    window: List[str] = []
    if start_s:
        window += ["-ss", str(float(start_s))]
    if end_s is not None:
        window += ["-to", str(float(end_s))]
    span_start = float(start_s or 0.0)
    span_end = float(end_s) if end_s is not None else result.get("duration_s")
    stats: Dict[str, Any] = {}
    if ffmpeg_bin:
        code, _out, err = run([ffmpeg_bin, "-hide_banner", "-nostats"] + window +
                              ["-i", path, "-af",
                               "ebur128=peak=true,astats=measure_perchannel=none,"
                               f"silencedetect=n={SILENCE_DB}dB:d={SILENCE_MIN_S}",
                               "-f", "null", "-"], 600.0)
        if code == 0:
            stats = parse_ffmpeg_summary(err)
            result["engine"].append("ffmpeg")
            code, _o, tail_err = run([ffmpeg_bin, "-hide_banner", "-nostats",
                                      "-sseof", f"-{TAIL_WINDOW_S}", "-i", path,
                                      "-af", "astats=measure_perchannel=none",
                                      "-f", "null", "-"], 60.0)
            if code == 0 and end_s is None:
                stats["tail_peak_dbfs"] = _num(r"Peak level dB:\s*([-\d.]+|-inf)", tail_err)
        else:
            last = err.strip().splitlines()[-1] if err.strip() else "no output"
            result["warnings"].append("ffmpeg failed: " + last)
    else:
        result["warnings"].append("ffmpeg not found: no LUFS or true peak")
    np_stats = _numpy_stats(path, span_start, end_s)
    if np_stats:
        result["engine"].append("numpy")
        for key in ("peak_dbfs", "rms_db", "tail_peak_dbfs"):
            if stats.get(key) is None:
                stats[key] = np_stats.get(key)
        for key in ("clipped_samples", "lr_balance_db", "lr_correlation",
                    "leading_silence_s", "trailing_silence_s"):
            stats.setdefault(key, np_stats.get(key))

    # Silence from silencedetect when ffmpeg ran (numpy's figures otherwise).
    span = (span_end - span_start) if span_end else None
    silences = stats.pop("silences", None)
    if silences is not None and span:
        total = 0.0
        lead = trail = 0.0
        for gap in silences:
            g_end = gap["end"] if gap["end"] is not None else span
            total += max(0.0, g_end - gap["start"])
            if gap["start"] <= 0.001:
                lead = g_end
            if gap["end"] is None or g_end >= span - 0.001:
                trail = g_end - gap["start"]
        stats["leading_silence_s"] = round(lead, 3)
        stats["trailing_silence_s"] = round(trail, 3)
        stats["silence_pct"] = round(100.0 * total / span, 1) if span > 0 else None
    tail_raw = stats.get("tail_peak_dbfs")  # before -inf becomes None
    for key in ("lufs_i", "lra", "true_peak_dbtp", "peak_dbfs", "rms_db",
                "tail_peak_dbfs", "lr_balance_db"):
        if key in stats:
            raw = stats[key]
            stats[key] = _finite(raw)
            # Only the whole-file peak decides "no signal"; a -inf tail peak
            # is a clean, fully decayed ending.
            if key == "peak_dbfs" and raw is not None and math.isinf(raw) and raw < 0:
                stats["no_signal"] = True
    result.update(stats)

    if result.get("no_signal") or (result.get("silence_pct") or 0) >= 99.5:
        result["warnings"].append("silent: no signal above -60 dBFS")
    if (result.get("clipped_samples") or 0) > 0 or (
            result.get("true_peak_dbtp") is not None and result["true_peak_dbtp"] > 0.0):
        result["warnings"].append("clipping: samples at full scale or true peak above 0 dBTP")
    tail = result.get("tail_peak_dbfs")
    result["tail_ok"] = None if tail_raw is None else bool(tail_raw <= SILENCE_DB)
    if result["tail_ok"] is False:
        result["warnings"].append(
            f"tail cut: the last {int(TAIL_WINDOW_S * 1000)} ms still peak at "
            f"{tail} dBFS — the render probably ends mid-reverb; extend the range")
    return result


def verify(path: str, expected_duration_s: Optional[float] = None,
           tolerance_s: float = 0.05, sample_rate: Optional[int] = None,
           bit_depth: Optional[int] = None,
           max_true_peak_dbtp: Optional[float] = None,
           min_lufs: Optional[float] = None, max_lufs: Optional[float] = None,
           allow_tail_cut: bool = False, **kw: Any) -> Dict[str, Any]:
    """``analyze`` plus pass/fail checks; ``ok`` is True only if every
    requested check passed and the file is neither silent nor clipped."""
    m = analyze(path, **kw)
    failures: List[str] = []
    if expected_duration_s is not None:
        d = m.get("duration_s")
        if d is None or abs(d - expected_duration_s) > tolerance_s:
            failures.append(f"duration {d} s, expected {expected_duration_s} s ±{tolerance_s}")
    if sample_rate is not None and m.get("sample_rate") != sample_rate:
        failures.append(f"sample rate {m.get('sample_rate')}, expected {sample_rate}")
    if bit_depth is not None and m.get("bit_depth") not in (None, bit_depth):
        failures.append(f"bit depth {m.get('bit_depth')}, expected {bit_depth}")
    tp = m.get("true_peak_dbtp")
    if max_true_peak_dbtp is not None and tp is not None and tp > max_true_peak_dbtp:
        failures.append(f"true peak {tp} dBTP above {max_true_peak_dbtp}")
    lufs = m.get("lufs_i")
    if min_lufs is not None and (lufs is None or lufs < min_lufs):
        failures.append(f"integrated loudness {lufs} LUFS below {min_lufs}")
    if max_lufs is not None and lufs is not None and lufs > max_lufs:
        failures.append(f"integrated loudness {lufs} LUFS above {max_lufs}")
    for w in m.get("warnings", []):
        if w.startswith("silent") or w.startswith("clipping"):
            failures.append(w)
        if w.startswith("tail cut") and not allow_tail_cut:
            failures.append(w)
    m["failures"] = failures
    m["ok"] = not failures
    return m
