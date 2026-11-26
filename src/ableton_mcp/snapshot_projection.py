"""Opt-in snapshot projection across native versions; absent data stays unknown."""
import copy


def normalize(snapshot, scope):
    out = copy.deepcopy(snapshot)

    def boolean(row, key):
        if type(row.get(key)) is not bool:
            row[key] = None

    def collection(row, key, visit):
        if key not in row:
            row[key] = None
        value = row[key]
        if value is None:
            return
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ValueError('malformed snapshot collection: ' + key)
        for item in value:
            visit(item)

    def clip(row):
        boolean(row, 'muted')
        count = row.get('note_count')
        if type(count) is not int or count < 0:
            row['note_count'] = None
        if not scope['include_notes']:
            row.pop('notes', None)
        elif 'notes' not in row:
            row['notes'] = None
        if not scope['include_warp_markers']:
            row.pop('warp_markers', None)

    def slot(row):
        value = row.get('clip')
        if isinstance(value, dict):
            clip(value)
        elif value is not None:
            raise ValueError('malformed snapshot clip')

    def device(row):
        boolean(row, 'is_active')
        boolean(row, 'device_on')
        if not scope['include_params']:
            row.pop('parameters', None)
        elif 'parameters' not in row:
            row['parameters'] = None
        if scope['include_rack_chains']:
            # Missing chains does not establish a rack or an empty chain list.
            if 'chains' in row:
                collection(row, 'chains', chain)
        else:
            row.pop('chains', None)

    def chain(row):
        collection(row, 'devices', device)

    def track(row):
        state = row.get('monitoring_state')
        if type(state) is not int or state not in (0, 1, 2):
            row['monitoring_state'] = None
        collection(row, 'devices', device)
        collection(row, 'clip_slots', slot)
        collection(row, 'arrangement_clips', clip)

    collection(out, 'tracks', track)
    collection(out, 'return_tracks', track)
    master = out.get('master_track')
    if isinstance(master, dict):
        track(master)
    elif master is not None:
        raise ValueError('malformed snapshot master_track')
    else:
        out['master_track'] = None
    return out
