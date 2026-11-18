"""A3a: guards run before import; fake LOM only, no Live or sockets."""
import pytest
from fake_ableton import FakeClip, make_harness
from ableton_mcp.services import AbletonService
from ableton_mcp import audio_metrics


class Gate:
    def require(self, *args, **kwargs):
        pass


def setup(tmp_path):
    h = make_harness()
    track = h.song.tracks[2]
    track.arrangement_clips = [FakeClip('Original', 4, midi=False, start_time=8)]
    writes = []
    track.create_audio_clip = lambda *args: writes.append(args)
    path = tmp_path / 'source.wav'
    path.write_bytes(b'fixture decoded through injected probe')
    return h, track, writes, str(path)


def call(h, **params):
    return h.process({'type': 'create_arrangement_audio_clip', 'params': params})


@pytest.mark.parametrize('start,end,hits', [(0,16,1),(6,10,1),(10,14,1),(8,12,1),(4,8,0),(12,16,0)])
def test_full_half_open_interval(tmp_path, start, end, hits):
    h, track, writes, path = setup(tmp_path)
    assert len(h.mcp._arrangement_overlaps(track, start, end)) == hits
    assert writes == []


def test_unproven_native_footprint_never_imports(tmp_path):
    h, track, writes, path = setup(tmp_path)
    result = call(h, track_index=2, path=path, start_time=0,
                  source_span={'duration_seconds': 8, 'decoded': True})
    assert result['status'] == 'success', result
    assert result['result']['supported'] is False
    assert 'footprint' in result['result']['reason']
    assert writes == []


@pytest.mark.parametrize('options', [
    {'source_offset_seconds': -1}, {'source_offset_seconds': float('nan')},
    {'source_offset_seconds': 9}, {'duration_seconds': 0},
    {'duration_seconds': 9}, {'destination_length_beats': float('inf')},
    {'start_time': -1}, {'warping': 'false'},
])
def test_invalid_source_parameters_zero_writes(tmp_path, options):
    h, track, writes, path = setup(tmp_path)
    params = dict(track_index=2, path=path, start_time=0,
                  source_span={'duration_seconds': 8, 'decoded': True})
    params.update(options)
    response = call(h, **params)
    assert response['status'] == 'error'
    assert writes == []


def test_trim_and_requested_beats_do_not_prove_footprint(tmp_path):
    h, track, writes, path = setup(tmp_path)
    response = call(h, track_index=2, path=path, start_time=0,
                    source_offset_seconds=2, duration_seconds=3,
                    destination_length_beats=6, warping=False,
                    source_span={'duration_seconds': 8, 'decoded': True})
    assert response['status'] == 'success', response
    plan = response['result']
    assert plan['supported'] is False
    assert plan['source_offset_seconds'] == 2
    assert plan['duration_seconds'] == 3
    assert plan['destination_length_beats'] is None
    assert writes == []


@pytest.mark.parametrize('span', [None, {}, {'duration_seconds': 0, 'decoded': True},
    {'duration_seconds': float('nan'), 'decoded': True},
    {'duration_seconds': 8, 'decoded': False}])
def test_missing_or_invalid_probe_refuses(tmp_path, span):
    h, track, writes, path = setup(tmp_path)
    response = call(h, track_index=2, path=path, source_span=span)
    assert response['status'] == 'error'
    assert writes == []


def test_service_injects_decoded_probe_without_accepting_beat_claim(tmp_path):
    h, track, writes, path = setup(tmp_path)
    class Client:
        def send_command(self, command, params):
            response = h.process({'type': command, 'params': params})
            if response['status'] == 'error':
                raise ValueError(response['message'])
            return response['result']
    service = AbletonService(Client(), Gate(), source_span_probe=lambda p: {
        'duration_seconds': 8, 'decoded': True})
    plan = service.create_arrangement_audio_clip(2, path, 0, destination_length_beats=1)
    assert plan['supported'] is False
    assert plan['duration_seconds'] == 8
    assert writes == []


def test_probe_failure_sends_nothing():
    class Client:
        def send_command(self, *args):
            pytest.fail('probe failure must not send a modifying command')
    def fail(path):
        raise ValueError('decoder failed')
    service = AbletonService(Client(), Gate(), source_span_probe=fail)
    with pytest.raises(ValueError, match='decoder failed'):
        service.create_arrangement_audio_clip(2, '/fixture.wav', 0)


def test_decoded_span_rejects_short_decoder_read(monkeypatch):
    from ableton_mcp import audio_analysis
    monkeypatch.setattr(audio_analysis, 'file_info', lambda p: (10, 20, 1))
    monkeypatch.setattr(audio_analysis, 'read_frames', lambda *args: [0] * 19)
    with pytest.raises(ValueError, match='decoded'):
        audio_metrics.probe_source_span('/fixture.wav')


def test_decoded_span_counts_actual_frames(monkeypatch):
    from ableton_mcp import audio_analysis
    monkeypatch.setattr(audio_analysis, 'file_info', lambda p: (10, 20, 1))
    monkeypatch.setattr(audio_analysis, 'read_frames', lambda p, first, count: [0] * count)
    span = audio_metrics.probe_source_span('/fixture.wav')
    assert span == {'duration_seconds': 2.0, 'decoded': True, 'sample_rate': 10, 'frames': 20}


@pytest.mark.parametrize('change', ['tempo', 'topology'])
def test_interleaved_state_change_during_decode_never_writes(tmp_path, change):
    h, track, writes, path = setup(tmp_path)
    class Client:
        def send_command(self, command, params):
            return call(h, **params)['result']
    def probe(path):
        if change == 'tempo':
            h.song.tempo = 90
        else:
            track.arrangement_clips.append(FakeClip('Human edit', 16, start_time=0))
        return {'duration_seconds': 8, 'decoded': True}
    service = AbletonService(Client(), Gate(), source_span_probe=probe)
    plan = service.create_arrangement_audio_clip(2, path, 0)
    assert plan['supported'] is False
    assert plan['destination_length_beats'] is None
    assert writes == []


@pytest.mark.parametrize('start,end', [(0, float('nan')), (4, 4), (6, 4)])
def test_interval_engine_rejects_unresolved_span(tmp_path, start, end):
    h, track, writes, path = setup(tmp_path)
    with pytest.raises(ValueError):
        h.mcp._arrangement_overlaps(track, start, end)
    assert writes == []


def test_unknown_existing_clip_extent_refuses(tmp_path):
    h, track, writes, path = setup(tmp_path)
    track.arrangement_clips[0].end_time = float('nan')
    with pytest.raises(ValueError):
        h.mcp._arrangement_overlaps(track, 0, 16)
    assert writes == []
