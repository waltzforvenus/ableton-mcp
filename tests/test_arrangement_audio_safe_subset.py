"""Offline native-return contract tests; no live connection or media decode."""
import json
import pytest
from fake_ableton import make_harness, FakeClip
from ableton_mcp import presenters, audio_metrics, audio_analysis
from ableton_mcp.services import AbletonService


def fixture(tmp_path):
    h = make_harness()
    t = h.song.tracks[2]
    t.arrangement_clips = []
    t.is_frozen = False
    h.song.record_mode = False
    h.song.session_record = False
    p = tmp_path / 'source.wav'
    p.write_bytes(b'local decoder fixture')
    calls = []
    native = t.create_audio_clip
    def create(path, start):
        calls.append((path, start))
        return native(path, start)
    t.create_audio_clip = create
    class Gate:
        def require(self, *a, **k): pass
    class Client:
        def send_command(self, cmd, params):
            result = h.process({'type': cmd, 'params': params})
            if result['status'] == 'error': raise ValueError(result['message'])
            return result['result']
    decoded = []
    def probe(path):
        decoded.append(path)
        return {'duration_seconds': 4, 'decoded': True}
    service = AbletonService(Client(), Gate(), source_span_probe=probe)
    return h, t, str(p), calls, decoded, service


def test_unsupported_presenter_is_not_success():
    result = {'supported': False, 'mutated': False, 'reason': 'footprint unknown',
              'destination_length_beats': None, 'requested_destination_length_beats': 2}
    text = presenters.create_arrangement_audio_clip(result)
    assert 'Placed' not in text
    assert json.loads(text) == result


@pytest.mark.parametrize('warping', [False, True])
def test_empty_track_whole_file_uses_returned_clip(tmp_path, warping):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    result = service.create_arrangement_audio_clip(2, p, 16, warping=warping)
    assert result['supported'] is True
    assert result['start_time'] == 16 and result['end_time'] == 24
    assert result['warping'] is warping
    assert result['gain'] == t.arrangement_clips[0].gain
    assert result['placement_mode'] == 'empty_track_whole_file'
    assert len(calls) == len(decoded) == 1
    assert 'Placed' in presenters.create_arrangement_audio_clip(result)


@pytest.mark.parametrize('reason', ['occupied','trim','beats','frozen','recording','midi','api'])
def test_predictable_refusal_never_decodes_or_writes(tmp_path, reason):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    options = {}
    if reason == 'occupied': t.arrangement_clips = [FakeClip('Human', 4)]
    if reason == 'trim': options['source_offset_seconds'] = 1
    if reason == 'beats': options['destination_length_beats'] = 8
    if reason == 'frozen': t.is_frozen = True
    if reason == 'recording': h.song.record_mode = True
    if reason == 'midi': t.has_midi_input = True
    if reason == 'api': t.create_audio_clip = None
    result = service.create_arrangement_audio_clip(2, p, 16, **options)
    assert result['supported'] is False
    assert decoded == calls == []


@pytest.mark.parametrize('change', ['topology','identity','file','frozen','tempo'])
def test_decode_interleaving_revalidated(tmp_path, change):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    def probe(path):
        if change == 'topology': t.arrangement_clips.append(FakeClip('Human',4))
        if change == 'identity': h.song.tracks[2] = h.song.tracks[1]
        if change == 'file': open(path, 'ab').write(b'changed')
        if change == 'frozen': t.is_frozen = True
        if change == 'tempo': h.song.tempo = 90
        return {'duration_seconds': 4, 'decoded': True}
    service._source_span_probe = probe
    result = service.create_arrangement_audio_clip(2, p, 16)
    if change == 'tempo':
        # Empty-track placement does not claim a tempo conversion.
        assert result['supported'] is True
    else:
        assert result['supported'] is False
        assert calls == []


@pytest.mark.parametrize('failure', ['raise_after','none','wrong_path','extent','topology','gain'])
def test_uncertain_native_mutation_reports_reconciliation(tmp_path, failure):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    create = t.create_audio_clip
    def broken(path, start):
        clip = create(path, start)
        if failure == 'raise_after': raise RuntimeError('native failure')
        if failure == 'none': return None
        if failure == 'wrong_path': clip.file_path = '/wrong.wav'
        if failure == 'extent': clip.end_time = float('nan')
        if failure == 'topology': t.arrangement_clips.append(FakeClip('Unexpected', 4))
        if failure == 'gain': clip.gain = float('nan')
        return clip
    t.create_audio_clip = broken
    result = service.create_arrangement_audio_clip(2, p, 16)
    assert result['reconciliation_required'] is True
    assert result['mutation_state'] == 'unknown'
    assert len(calls) == 1 and len(t.arrangement_clips) >= 1
    assert 'Placed' not in presenters.create_arrangement_audio_clip(result)


def test_probe_frame_budget_refuses_before_decoder(monkeypatch):
    monkeypatch.setattr(audio_analysis, 'file_info', lambda p: (48000, 10**12, 2))
    monkeypatch.setattr(audio_analysis, 'read_frames', lambda *a: pytest.fail('must not decode'))
    with pytest.raises(ValueError, match='budget'):
        audio_metrics.probe_source_span('/fixture.wav')


@pytest.mark.parametrize('failure', ['warp','gain','state','identity'])
def test_property_phase_failure_is_never_success_or_retried(tmp_path, failure):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    native = t.create_audio_clip
    class WarpClip(FakeClip):
        @property
        def warping(self): return getattr(self, '_warp', False)
        @warping.setter
        def warping(self, value):
            if getattr(self, 'ready', False):
                if failure == 'warp': raise ValueError('warp refused')
                if failure == 'gain': self.gain = .25
                if failure == 'state': t.arm = not t.arm
                if failure == 'identity': h.song.tracks[2] = h.song.tracks[1]
            self._warp = value
    def create(path, start):
        calls.append((path, start))
        clip = WarpClip('Source', 8, midi=False, start_time=start)
        clip.file_path = path
        clip.ready = True
        t.arrangement_clips.append(clip)
        return clip
    t.create_audio_clip = create
    result = service.create_arrangement_audio_clip(2, p, 16)
    assert result['reconciliation_required'] is True
    assert len(calls) == 1


def test_uncertain_exception_blocks_fresh_retry_even_when_empty(tmp_path):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    def fail(*args):
        calls.append(args)
        raise RuntimeError('unknown native outcome')
    t.create_audio_clip = fail
    first = service.create_arrangement_audio_clip(2, p, 16)
    second = service.create_arrangement_audio_clip(2, p, 16)
    assert first['reconciliation_required'] is True
    assert second['reconciliation_required'] is True
    assert len(calls) == 1


def test_decoder_failure_only_preflight_runs(tmp_path):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    def fail(path): raise ValueError('decoder failed')
    service._source_span_probe = fail
    with pytest.raises(ValueError, match='decoder failed'):
        service.create_arrangement_audio_clip(2, p, 16)
    assert calls == []


def test_probe_deadline_checks_between_chunks(monkeypatch):
    monkeypatch.setattr(audio_analysis, 'file_info', lambda p: (48000, 65537, 1))
    clock = iter([0, 0, 16])
    monkeypatch.setattr(audio_metrics.time, 'monotonic', lambda: next(clock))
    reads = []
    def decode(p, first, count):
        reads.append(first)
        return [0] * count
    monkeypatch.setattr(audio_analysis, 'read_frames', decode)
    with pytest.raises(ValueError, match='deadline'):
        audio_metrics.probe_source_span('/fixture.wav')
    assert reads == [0]


def test_apply_connection_loss_is_reconciliation_not_plain_error():
    class Gate:
        def require(self, *a, **k): pass
    class Client:
        def send_command(self, command, params):
            if params.get('preflight'): return {'eligible': True, 'preflight_token': 'fixture'}
            raise RuntimeError('connection lost after send')
    service = AbletonService(Client(), Gate(), source_span_probe=lambda p: {'decoded':True,'duration_seconds':4})
    result = service.create_arrangement_audio_clip(2, '/fixture.wav', 16)
    assert result['reconciliation_required'] is True
    assert result['retry_safe'] is False
    assert 'Placed' not in presenters.create_arrangement_audio_clip(result)


@pytest.mark.parametrize('options', [{'start_time':1576801}, {'start_time':float('nan')},
    {'source_offset_seconds':-1}, {'duration_seconds':0}, {'warping':'false'},
    {'expect_track_name':'Wrong'}])
def test_invalid_request_refuses_before_decode(tmp_path, options):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    start = options.pop('start_time', 16)
    with pytest.raises(ValueError): service.create_arrangement_audio_clip(2, p, start, **options)
    assert decoded == calls == []


def test_wire_apply_token_is_single_use(tmp_path):
    h, t, p, calls, decoded, service = fixture(tmp_path)
    params = {'track_index':2, 'path':p, 'start_time':16}
    first = h.mcp._create_arrangement_audio_clip(**dict(params, preflight=True))
    params.update(preflight_token=first['preflight_token'], source_span={'decoded':True,'duration_seconds':4})
    result = h.mcp._create_arrangement_audio_clip(**params)
    assert result['supported'] is True
    t.arrangement_clips = []  # simulate a later human deletion, never automatic rollback
    replay = h.mcp._create_arrangement_audio_clip(**params)
    assert replay['supported'] is False
    assert len(calls) == 1
