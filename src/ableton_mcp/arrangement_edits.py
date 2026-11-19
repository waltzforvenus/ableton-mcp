"""Server-held region manifests; never derive authority from caller JSON."""
import copy
import hashlib
import json
import os
import threading
import uuid


def media_hash(path):
    if not os.path.isfile(path) or os.path.getsize(path) > 256 * 1024 * 1024:
        raise ValueError('source media missing or exceeds bounded hash budget')
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        remaining = 256 * 1024 * 1024 + 1
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                return digest.hexdigest()
            remaining -= len(chunk)
            digest.update(chunk)
    raise ValueError('source media exceeds bounded hash budget')


class ArrangementEdits:
    def __init__(self, hash_media=media_hash):
        self._hash = hash_media
        self._plans = {}
        self._mutex = threading.Lock()

    def register(self, native, request):
        manifest = copy.deepcopy(native)
        manifest['source_media_hashes'] = {p: self._hash(p) for p in native.get('source_media', [])} if native.get('supported') else {}
        manifest['rollback_checkpoint'] = {'status': 'required_before_apply', 'method': 'checkpoint_set'}
        manifest['manifest_id'] = uuid.uuid4().hex
        manifest['fingerprint'] = hashlib.sha256(json.dumps(manifest, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self._mutex:
            if len(self._plans) >= 64:
                self._plans.pop(next(iter(self._plans)))
            self._plans[manifest['manifest_id']] = (copy.deepcopy(manifest), copy.deepcopy(native), copy.deepcopy(request))
        return manifest

    def apply(self, manifest_id, fingerprint, checkpoint, dispatch):
        with self._mutex:
            saved = self._plans.get(manifest_id)
            if not saved or saved[0]['fingerprint'] != fingerprint:
                raise ValueError('unknown or stale region manifest')
            manifest, native, request = saved
            if not native.get('supported'):
                return dict(supported=False, applied=False, missing_capabilities=native.get('missing_capabilities', []))
            for path, expected in manifest['source_media_hashes'].items():
                if self._hash(path) != expected:
                    raise ValueError('source media changed')
            # Consume before any possible save/write: uncertain attempts never replay.
            del self._plans[manifest_id]
        backup = checkpoint(request)
        save = backup.get('save') or {}
        evidence = save.get('verified') or {}
        path = evidence.get('set_path')
        if (not backup.get('checkpointed') or not backup.get('sha256') or not backup.get('checkpoint_path')
                or save.get('saved') is not True or not path
                or evidence.get('document_before') != path or evidence.get('document_after') != path
                or evidence.get('unsaved_after') is not False or not evidence.get('mtime_advanced')
                or evidence.get('modal_after')):
            return {'applied': False, 'supported': True, 'reason': 'verified checkpoint required', 'checkpoint': backup}
        if self._hash(backup['checkpoint_path']) != backup['sha256']:
            raise ValueError('checkpoint hash mismatch')
        for path, expected in manifest['source_media_hashes'].items():
            if self._hash(path) != expected:
                raise ValueError('source media changed after checkpoint')
        try:
            result = dispatch(native)
        except Exception as exc:
            result = {'applied': None, 'reconciliation_required': True, 'reason': str(exc)}
        return dict(result, checkpoint=backup, recovery='explicit checkpoint-based recovery only; no global undo', retry_safe=False)
