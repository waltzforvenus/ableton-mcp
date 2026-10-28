"""Warp vision orchestration: from a clip's warp state to analysis and
proposals (Model layer, no wire access, no response text).

``AbletonService`` reads the clip with ``get_clip_warp_markers`` and hands
that raw state dict here; this module resolves it into a ``ClipContext``
(the file, the current warp mapping, the song grid), runs
``audio_analysis`` on the requested window, and returns plain dicts — plus
rendered image bytes under the ``image`` key, which the View turns into MCP
image content. presenters.py owns every word the model reads.

Two grids, kept straight here so callers never have to:

- CLIP beats are what Live's warp markers and start/end markers use;
- GRID beats are what the image labels and the window/beat parameters
  use: SONG beats for an Arrangement clip (clip beat + start_time -
  start_marker), clip beats for a Session clip. "Bar 9" in the picture is
  bar 9 of the song, and ``start_beat=32`` means the song's beat 32.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import audio_analysis as aa

# Response-size budget: the JSON half of an analysis has to fit beside a
# ~150-250 KB image inside one tool result, so long lists are capped and
# say so (``*_total`` fields carry the uncapped count).
MAX_TRANSIENTS = 150
MAX_MARKERS_LISTED = 120
DEFAULT_IMAGE_WIDTH = 1200
MAX_IMAGE_WIDTH = 2400

MODES = ("transients", "rewarp", "tempo")


@dataclass
class ClipContext:
    state: Dict[str, Any]
    path: str
    warping: bool
    markers: List[Dict[str, float]]
    beat_map: aa.BeatMap
    grid_offset: float           # grid beat = clip beat + grid_offset
    beats_per_bar: int
    song_tempo: float
    region: Tuple[float, float]  # file seconds the clip plays
    notes: List[str]

    def grid_beat(self, seconds: float) -> float:
        return self.beat_map.seconds_to_beat(seconds) + self.grid_offset

    def seconds_at(self, grid_beat: float) -> float:
        return self.beat_map.beat_to_seconds(grid_beat - self.grid_offset)


def clip_context(state: Dict[str, Any]) -> ClipContext:
    """Resolve the Remote Script's warp state into the mapping to draw."""
    if not state.get("is_audio_clip", True):
        raise aa.AnalysisError("audio analysis needs an audio clip; this is MIDI")
    path = state.get("file_path")
    if not path:
        raise aa.AnalysisError(
            "Live reported no file path for this clip (frozen, or a "
            "recording not yet written to disk?), so there is nothing to read")
    if not os.path.isfile(path):
        raise aa.AnalysisError(
            "the clip's audio file is not on this machine at %r" % path)
    tempo = float(state.get("song_tempo") or 120.0)
    spb = 60.0 / tempo
    warping = bool(state.get("warping"))
    notes: List[str] = []
    markers = [{"beat_time": float(m["beat_time"]),
                "sample_time": float(m["sample_time"])}
               for m in state.get("warp_markers") or []]
    arrangement = bool(state.get("arrangement"))
    start_marker = float(state.get("start_marker") or 0.0)
    end_marker = state.get("end_marker")
    if warping:
        if len(markers) < 2:
            notes.append("the clip has fewer than two warp markers, so the "
                         "warp rate outside them is assumed to be the song "
                         "tempo")
        beat_map = aa.BeatMap.from_markers(markers, spb)
        offset = (float(state.get("start_time") or 0.0) - start_marker
                  if arrangement else 0.0)
        region_start = beat_map.beat_to_seconds(start_marker)
        region_end = (beat_map.beat_to_seconds(float(end_marker))
                      if end_marker is not None else None)
    else:
        # Unwarped: the file plays at its own rate, its markers are in
        # seconds, and clip beat 0 is the start marker at the song tempo.
        notes.append("warping is OFF: the grid shown is the song tempo laid "
                     "over the file at its native rate; warp edits need "
                     "warping on (set_clip_warp)")
        beat_map = aa.BeatMap([(0.0, start_marker)], spb)
        offset = float(state.get("start_time") or 0.0) if arrangement else 0.0
        region_start = start_marker
        region_end = float(end_marker) if end_marker is not None else None
    duration = state.get("sample_seconds")
    region_start = max(0.0, region_start)
    if region_end is None or region_end <= region_start:
        region_end = float(duration) if duration else region_start + 600.0
    if duration:
        region_end = min(region_end, float(duration))
    return ClipContext(state=state, path=path, warping=warping,
                       markers=markers, beat_map=beat_map, grid_offset=offset,
                       beats_per_bar=int(state.get("signature_numerator") or 4),
                       song_tempo=tempo, region=(region_start, region_end),
                       notes=notes)


def resolve_window(ctx: ClipContext, start_seconds: Optional[float] = None,
                   end_seconds: Optional[float] = None,
                   start_beat: Optional[float] = None,
                   end_beat: Optional[float] = None) -> Tuple[float, float]:
    """File-seconds window from either unit; default = what the clip plays."""
    lo, hi = ctx.region
    w0 = (float(start_seconds) if start_seconds is not None
          else ctx.seconds_at(float(start_beat)) if start_beat is not None
          else lo)
    w1 = (float(end_seconds) if end_seconds is not None
          else ctx.seconds_at(float(end_beat)) if end_beat is not None
          else hi)
    w0 = max(0.0, w0)
    if w1 <= w0:
        raise aa.AnalysisError(
            "empty window: end (%.3f s) is not after start (%.3f s)" % (w1, w0))
    if w1 - w0 > aa.MAX_ANALYSIS_SECONDS:
        ctx.notes.append("window capped at %d s" % aa.MAX_ANALYSIS_SECONDS)
        w1 = w0 + aa.MAX_ANALYSIS_SECONDS
    return w0, w1


def _clip_summary(ctx: ClipContext) -> Dict[str, Any]:
    s = ctx.state
    out = {k: s[k] for k in ("track_index", "track_name", "clip_index",
                             "clip_name", "arrangement", "warping",
                             "warp_mode", "file_path", "start_time",
                             "start_marker", "end_marker")
           if k in s}
    out.update({"song_tempo": ctx.song_tempo,
                "beats_per_bar": ctx.beats_per_bar,
                "grid_offset_beats": round(ctx.grid_offset, 6)})
    return out


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def _transient_rows(ctx: ClipContext, analysis: aa.Analysis,
                    limit: int) -> List[Dict[str, Any]]:
    rows = analysis.transients
    if len(rows) > limit:
        keep = sorted(rows, key=lambda t: -t.weight)[:limit]
        rows = sorted(keep, key=lambda t: t.time)
    out = []
    for t in rows:
        g = ctx.grid_beat(t.time)
        target = round(g * 4) / 4.0  # nearest 16th
        out.append({
            "t": _r(t.time), "beat": _r(g, 3),
            "off_16th_ms": _r((t.time - ctx.seconds_at(target)) * 1000.0, 1),
            "strength": _r(t.strength, 2), "band": t.band,
        })
    return out


def _marker_rows(ctx: ClipContext, markers: List[Dict[str, float]],
                 window: Tuple[float, float]) -> List[Dict[str, Any]]:
    w0, w1 = window
    rows = []
    last = len(markers) - 1
    for i, m in enumerate(markers):
        if w0 - 1e-6 <= m["sample_time"] <= w1 + 1e-6:
            row = {"beat_time": _r(m["beat_time"], 6),
                   "sample_time": _r(m["sample_time"], 6),
                   "grid_beat": _r(m["beat_time"] + ctx.grid_offset, 4)}
            if i == last and last > 0:
                row["hidden_end_marker"] = True
            rows.append(row)
    return rows


def _title(ctx: ClipContext, window: Tuple[float, float], extra: str = "") -> str:
    s = ctx.state
    mode = "warp ON" if ctx.warping else "warp OFF"
    return ("%s > %s  |  %.2f-%.2f s  |  %s  |  song %.2f BPM%s"
            % (s.get("track_name", "?"), s.get("clip_name", "?"),
               window[0], window[1], mode, ctx.song_tempo, extra))


def analyze(state: Dict[str, Any], start_seconds: Optional[float] = None,
            end_seconds: Optional[float] = None,
            start_beat: Optional[float] = None,
            end_beat: Optional[float] = None, sensitivity: float = 0.5,
            include_image: bool = True,
            image_width: int = DEFAULT_IMAGE_WIDTH,
            max_transients: int = MAX_TRANSIENTS,
            band_points: int = 48) -> Dict[str, Any]:
    """Analyse what the clip plays (or a window of it) against its warping."""
    ctx = clip_context(state)
    window = resolve_window(ctx, start_seconds, end_seconds, start_beat,
                            end_beat)
    an = aa.analyse(ctx.path, window, sensitivity=sensitivity,
                    prior_bpm=ctx.song_tempo, band_points=band_points)
    window = an.window
    mid = (window[0] + window[1]) / 2.0
    tempo = None
    if an.tempo is not None:
        tempo = {"bpm": _r(an.tempo.bpm, 2),
                 "confidence": _r(an.tempo.confidence, 2),
                 "first_beat_seconds": _r(an.tempo.first_beat),
                 "candidates_bpm": an.tempo.candidates}
    markers = _marker_rows(ctx, ctx.markers, window)
    transients = _transient_rows(ctx, an, max(1, int(max_transients)))
    result: Dict[str, Any] = {
        "clip": _clip_summary(ctx),
        "window": {"start_seconds": _r(window[0]), "end_seconds": _r(window[1]),
                   "start_beat": _r(ctx.grid_beat(window[0]), 3),
                   "end_beat": _r(ctx.grid_beat(window[1]), 3)},
        "source_tempo_estimate": tempo,
        "warped_bpm_at_window_middle": _r(ctx.beat_map.bpm_at(mid), 3),
        "grid_alignment": {
            "beat": aa.grid_alignment(an.transients, ctx.beat_map, 1.0,
                                      ctx.grid_offset),
            "sixteenth": aa.grid_alignment(an.transients, ctx.beat_map, 0.25,
                                           ctx.grid_offset),
        },
        "warp_markers": markers[:MAX_MARKERS_LISTED],
        "warp_markers_in_window": len(markers),
        "transients": transients,
        "transients_total": len(an.transients),
        "band_energy_db": an.bands,
        "notes": ctx.notes,
    }
    if include_image:
        result["image"] = aa.render(
            an.audio, an.mag, window,
            [aa.Panel("current", ctx.beat_map, ctx.markers)],
            an.transients, _title(ctx, window), grid_offset=ctx.grid_offset,
            beats_per_bar=ctx.beats_per_bar, tempo=an.tempo,
            width=_width(image_width))
    return result


def _width(w: Any) -> int:
    return max(400, min(MAX_IMAGE_WIDTH, int(w or DEFAULT_IMAGE_WIDTH)))


@dataclass
class Proposal:
    ctx: ClipContext
    analysis: aa.Analysis
    mode: str
    subdivision: float
    edits: List[Dict[str, Any]]
    markers_after: List[Dict[str, float]]
    skipped: Dict[str, int]


def propose(state: Dict[str, Any], mode: str = "transients",
            subdivision: float = 1.0, tolerance: Optional[float] = None,
            min_strength: float = 0.3, max_edits: int = 32,
            sensitivity: float = 0.5,
            start_seconds: Optional[float] = None,
            end_seconds: Optional[float] = None,
            start_beat: Optional[float] = None,
            end_beat: Optional[float] = None) -> Proposal:
    mode = (mode or "transients").lower()
    if mode not in MODES:
        raise aa.AnalysisError("mode must be one of %s (got %r)"
                               % (", ".join(MODES), mode))
    subdivision = float(subdivision)
    if subdivision <= 0:
        raise aa.AnalysisError("subdivision must be positive (beats)")
    ctx = clip_context(state)
    if not ctx.warping:
        raise aa.AnalysisError(
            "warping is off, and warp markers only exist on a warped clip: "
            "turn it on with set_clip_warp(warping=True), then re-run")
    window = resolve_window(ctx, start_seconds, end_seconds, start_beat,
                            end_beat)
    an = aa.analyse(ctx.path, window, sensitivity=sensitivity,
                    prior_bpm=ctx.song_tempo)
    max_edits = max(1, min(int(max_edits), 256))
    if mode == "transients":
        out = aa.suggest_edits(
            an.transients, ctx.markers, ctx.beat_map, an.window,
            subdivision=subdivision, grid_offset=ctx.grid_offset,
            tolerance=0.35 if tolerance is None else float(tolerance),
            min_strength=min_strength, max_edits=max_edits)
    elif mode == "rewarp":
        out = aa.rewarp_edits(
            an.transients, ctx.markers, ctx.beat_map, an.window,
            subdivision=subdivision, grid_offset=ctx.grid_offset,
            tolerance=0.3 if tolerance is None else float(tolerance),
            min_strength=min_strength, max_edits=max_edits, tempo=an.tempo)
    else:
        if an.tempo is None:
            raise aa.AnalysisError(
                "no tempo could be estimated in this window (too short or "
                "no clear pulse); try mode='rewarp' or a longer window")
        out = aa.tempo_edits(an.tempo, ctx.markers, ctx.beat_map, an.window,
                             an.transients, grid_offset=ctx.grid_offset,
                             beats_per_bar=ctx.beats_per_bar)
    return Proposal(ctx, an, mode, subdivision, out["edits"],
                    out["markers_after"], out["skipped"])


def wire_edits(proposal: Proposal) -> List[Dict[str, Any]]:
    """The edits in the exact shape edit_clip_warp_markers takes."""
    keep = ("op", "beat_time", "sample_time", "new_beat_time", "distance")
    return [dict((k, e[k]) for k in keep if k in e) for e in proposal.edits]


def proposal_result(proposal: Proposal, include_image: bool = True,
                    image_width: int = DEFAULT_IMAGE_WIDTH,
                    applied: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Summarise a proposal (and, after apply, the read-back) for the View.

    The "after" alignment is MEASURED on the markers Live reports after an
    apply, and PREDICTED from the proposal on a dry run — the result says
    which, because only one of them is evidence.
    """
    ctx, an = proposal.ctx, proposal.analysis
    sub = proposal.subdivision
    before = aa.grid_alignment(an.transients, ctx.beat_map, sub, ctx.grid_offset)
    if applied is not None:
        after_markers = [{"beat_time": float(m["beat_time"]),
                          "sample_time": float(m["sample_time"])}
                         for m in applied.get("warp_markers_after") or []]
        after_kind = "measured_from_live_readback"
    else:
        after_markers = proposal.markers_after
        after_kind = "predicted"
    after_map = aa.BeatMap.from_markers(after_markers, ctx.beat_map.default_spb)
    after = aa.grid_alignment(an.transients, after_map, sub, ctx.grid_offset)
    result: Dict[str, Any] = {
        "clip": _clip_summary(ctx),
        "mode": proposal.mode,
        "subdivision_beats": sub,
        "window": {"start_seconds": _r(an.window[0]),
                   "end_seconds": _r(an.window[1]),
                   "start_beat": _r(ctx.grid_beat(an.window[0]), 3),
                   "end_beat": _r(ctx.grid_beat(an.window[1]), 3)},
        "source_tempo_estimate_bpm": (_r(an.tempo.bpm, 2)
                                      if an.tempo is not None else None),
        "alignment_before": before,
        "alignment_after": after,
        "alignment_after_is": after_kind,
        "edit_count": len(proposal.edits),
        "edits": wire_edits(proposal),
        "edit_evidence": [
            {k: e[k] for k in ("op", "beat_time", "transient_time",
                               "shift_ms", "strength") if k in e}
            for e in proposal.edits],
        "skipped": proposal.skipped,
        "notes": ctx.notes,
    }
    if applied is not None:
        result["applied"] = {k: applied.get(k) for k in (
            "requested", "applied", "failed", "not_attempted", "verified",
            "mismatches")}
        result["applied"]["failures"] = [
            r for r in applied.get("results") or [] if not r.get("ok")]
    if include_image:
        result["image"] = aa.render(
            an.audio, an.mag, an.window,
            [aa.Panel("before", ctx.beat_map, ctx.markers),
             aa.Panel("after" if applied is not None else "propose",
                      after_map, after_markers, "proposed")],
            an.transients,
            _title(ctx, an.window, "  |  %s, %d edits%s" % (
                proposal.mode, len(proposal.edits),
                " APPLIED" if applied is not None else " (dry run)")),
            grid_offset=ctx.grid_offset, beats_per_bar=ctx.beats_per_bar,
            tempo=an.tempo, proposed=proposal.edits,
            width=_width(image_width))
    return result
