"""Read-only selected-target reload risk comparisons; never load/reenable authority."""
import copy
import threading
import time
import uuid
from .automation import fingerprint
from .arrangement_edits import media_hash


class AutomationReload:
    def __init__(self, clock=time.monotonic, hash_file=media_hash):
        self._clock, self._hash = clock, hash_file
        self._plans = {}
        self._mutex = threading.Lock()

    @staticmethod
    def _known(row):
        state = row.get('automation_state')
        return (row.get('supported') is True and row.get('source') == 'live'
                and row.get('coverage') == 'state_only' and type(state) is int
                and state in (0, 1, 2) and row.get('overridden') is (state == 2)
                and row.get('envelope_present') is (state != 0))

    @staticmethod
    def _observe(targets, observe):
        rows = []
        for target in targets:
            try:
                row = observe(copy.deepcopy(target))
                if not isinstance(row, dict):
                    raise ValueError('observation is not structured')
                rows.append(copy.deepcopy(row))
            except Exception as exc:
                rows.append({'supported': False, 'reason': str(exc)})
        return rows

    def plan(self, copy_path, targets, observe):
        if (not isinstance(targets, list) or not 1 <= len(targets) <= 64
                or any(not isinstance(t, dict) or 'saved_path' in t for t in targets)):
            raise ValueError('provide1..64 live targets, not saved-copy targets')
        targets = copy.deepcopy(targets)
        started = self._clock()
        digest = self._hash(copy_path)
        rows = self._observe(targets, observe)
        if self._hash(copy_path) != digest or self._clock() - started > 300:
            raise ValueError('copy changed or observation window expired')
        plan = dict(plan_id=uuid.uuid4().hex, copy_path=copy_path, copy_sha256=digest,
                    targets=targets, observations=rows, observed_at=started,
                    expires_at=started+300, reload_authorized=False,
                    identity_verified=False, scope='selected_targets_non_atomic',
                    override_risk_indices=[i for i, r in enumerate(rows) if r.get('overridden') is True],
                    unknown_indices=[i for i, r in enumerate(rows) if not self._known(r)])
        plan['fingerprint'] = fingerprint(plan)
        with self._mutex:
            if len(self._plans) >= 64:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan['plan_id']] = copy.deepcopy(plan)
        return plan

    def reconcile(self, plan_id, expected_fingerprint, observe):
        with self._mutex:
            plan = copy.deepcopy(self._plans.get(plan_id))
        if plan is None or plan['fingerprint'] != expected_fingerprint:
            raise ValueError('unknown/stale reload plan')
        if self._clock() > plan['expires_at'] or self._hash(plan['copy_path']) != plan['copy_sha256']:
            raise ValueError('stale copy or expired reload plan')
        rows = self._observe(plan['targets'], observe)
        if self._clock() > plan['expires_at'] or self._hash(plan['copy_path']) != plan['copy_sha256']:
            raise ValueError('copy changed or plan expired during observation')
        unknown, changed, reenabled = [], [], []
        for i, (old, new) in enumerate(zip(plan['observations'], rows)):
            if not self._known(old) or not self._known(new):
                unknown.append(i)
                continue
            if old != new:
                changed.append(i)
            if old['overridden'] is True and new['overridden'] is False:
                reenabled.append(i)
        return dict(plan_id=plan_id, comparison='unknown' if unknown else ('changed' if changed else 'unchanged'),
                    observations=rows, changed_indices=changed, unknown_indices=unknown,
                    possible_reenabled_indices=reenabled, reload_authorized=False,
                    reconciliation_required=bool(unknown or changed), identity_verified=False,
                    scope='selected_targets_non_atomic', observed_at=self._clock(),
                    warning='No reload performed or certified; unchanged selected states do not prove safe reload or complete identity')
