"""Schema-qualified value-only XML copies, not a native Live format writer.

Never reserializes XML: only selected FloatEvent Value payloads are replaced.
Unknown bytes outside those payloads (including comments/CDATA) remain identical.
"""
import hashlib
import os
import re
from xml.parsers import expat
from .arrangement_edits import media_hash


class SavedAutomationWriter:
    SCHEMA = 'leaf-float-values-v1'
    LIMIT = 8 * 1024 * 1024

    def _read(self, path):
        with open(path, 'rb') as stream:
            data = stream.read(self.LIMIT + 1)
        if len(data) > self.LIMIT:
            raise ValueError('saved XML exceeds byte budget')
        if data.startswith(b'\x1f\x8b') or b'<!DOCTYPE' in data or b'<!ENTITY' in data:
            raise ValueError('writer requires plain UTF-8 XML without DTD/entities')
        try:
            data.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('writer requires UTF-8 XML') from exc
        declaration = re.match(br'\s*<\?xml[^?]*encoding\s*=\s*[\'"]([^\'"]+)', data)
        if declaration and declaration[1].lower() not in (b'utf-8', b'utf8'):
            raise ValueError('writer requires UTF-8 XML declaration')
        return data

    def _spans(self, data, pointee_id):
        parser = expat.ParserCreate('UTF-8')
        stack, envelopes = [], []
        current = None

        def start(name, attrs):
            nonlocal current
            stack.append(name)
            if name == 'AutomationEnvelope':
                if current is not None:
                    raise ValueError('nested envelope layout unsupported')
                current = {'targets': [], 'events': [], 'containers': 0}
            if current is None:
                return
            if stack[-3:] == ['AutomationEnvelope', 'EnvelopeTarget', 'PointeeId']:
                current['targets'].append(attrs.get('Value'))
            if stack[-3:] == ['AutomationEnvelope', 'Automation', 'Events']:
                current['containers'] += 1
            if stack[-4:] == ['AutomationEnvelope', 'Automation', 'Events', 'FloatEvent']:
                current['events'].append((parser.CurrentByteIndex, dict(attrs)))

        def end(name):
            nonlocal current
            if name == 'AutomationEnvelope':
                envelopes.append(current)
                current = None
            stack.pop()

        parser.StartElementHandler, parser.EndElementHandler = start, end
        parser.Parse(data, True)
        selected = [e for e in envelopes if str(pointee_id) in e['targets']]
        if len(selected) != 1 or selected[0]['targets'] != [str(pointee_id)] or selected[0]['containers'] != 1:
            raise ValueError('ambiguous envelope layout')
        spans = []
        for offset, attrs in selected[0]['events']:
            end = data.find(b'/>', offset)
            if end == -1:
                raise ValueError('writer requires self-closing leaf events')
            tag = data[offset:end+2]
            if set(attrs) - {'Id', 'Time', 'Value'} or not {'Time', 'Value'} <= set(attrs):
                raise ValueError('unsupported event attributes')
            if 'Id' in attrs and not attrs['Id'].isdigit():
                raise ValueError('unsupported event Id')
            match = re.search(br"\bValue\s*=\s*(['\"])([^'\"]*)\1", tag)
            if not match or b'<' in tag[1:] or b'>' in tag[:-1]:
                raise ValueError('unsupported event lexical layout')
            spans.append((offset+match.start(2), offset+match.end(2)))
        return spans

    def _parent_binding(self, output_path, fd=None):
        parent = os.path.dirname(output_path)
        resolved = os.path.realpath(parent)
        stat = os.stat(parent) if fd is None else os.fstat(fd)
        visible = os.stat(resolved)
        if (visible.st_dev, visible.st_ino) != (stat.st_dev, stat.st_ino):
            raise ValueError('destination parent changed while resolving')
        return {'resolved_parent': resolved, 'parent_device': stat.st_dev,
                'parent_inode': stat.st_ino, 'filename': os.path.basename(output_path)}

    def prepare(self, target, before, proposed, operation):
        if target.get('schema') != self.SCHEMA:
            raise ValueError('unsupported writer schema')
        if operation != 'replace' or [p['time'] for p in before] != [p['time'] for p in proposed]:
            raise ValueError('writer only replaces values at identical existing times')
        source, backup, output = (target[k] for k in ('saved_path','backup_path','output_path'))
        if not all(isinstance(p, str) and os.path.isabs(p) for p in (source, backup, output)):
            raise ValueError('writer paths must be absolute')
        if os.path.samefile(source, backup):
            raise ValueError('backup must be a distinct source copy')
        if os.path.lexists(output) or os.path.realpath(output) in (os.path.realpath(source), os.path.realpath(backup)):
            raise ValueError('output must be a new distinct file')
        data = self._read(source)
        digest = hashlib.sha256(data).hexdigest()
        if media_hash(backup) != digest:
            raise ValueError('backup must match source bytes')
        spans = self._spans(data, target['pointee_id'])
        if len(spans) != len(proposed):
            raise ValueError('event mapping incomplete')
        output_data = data
        for (start, end), point in reversed(list(zip(spans, proposed))):
            output_data = output_data[:start] + repr(point['value']).encode('ascii') + output_data[end:]
        return {'source_sha256': digest, 'backup_sha256': digest,
                'output_sha256': hashlib.sha256(output_data).hexdigest(),
                'destination': self._parent_binding(output)}, output_data

    def publish(self, output_path, data, destination):
        # Opening may follow a parent symlink, but the opened object's identity
        # must equal the held plan. All file IO thereafter is relative to that fd.
        parent_fd = os.open(os.path.dirname(output_path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            if self._parent_binding(output_path, parent_fd) != destination:
                raise ValueError('destination parent identity changed')
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(destination['filename'], flags, 0o600, dir_fd=parent_fd)
            with os.fdopen(fd, 'w+b') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                stream.seek(0)
                observed = stream.read(self.LIMIT + 1)
                if observed != data:
                    raise ValueError('output byte readback mismatch')
                visible = os.stat(destination['filename'], dir_fd=parent_fd, follow_symlinks=False)
                actual = os.fstat(stream.fileno())
                if (visible.st_dev, visible.st_ino) != (actual.st_dev, actual.st_ino):
                    raise ValueError('output directory entry changed')
            if self._parent_binding(output_path) != destination:
                raise ValueError('destination parent changed after publication; inspect bound directory')
            return observed
        finally:
            os.close(parent_fd)
