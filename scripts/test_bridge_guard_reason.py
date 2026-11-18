"""Current lease failures cannot inherit an earlier task-local error."""
import copy
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import xhs_bridge as bridge
import xhs_native as native
import xhs_project as xp


class Driver:
    def __init__(self):
        self.metrics = {'calls': 0, 'process_starts': 0}
        self.closed = False

    def call(self, *args, **kwargs):
        raise AssertionError('A failed lease must not reach the browser')

    def close(self):
        self.closed = True


class GuardReasonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = xp.Project(self.root / 'project', create=True)
        source = self.root / 'scope.json'
        source.write_text(json.dumps({'query': 'Synthetic person'}))
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'units': [{
            'stage': 'search', 'target': 'Synthetic person', 'sort': 'general',
            'sources': [str(source)], 'next': 'Read the actual result page'}]}))
        self.tid = native.import_manifest(self.p, manifest)['task_ids'][0]
        self.binding = {'binding_name': 'test', 'runtime': {}, 'state': 'ready',
                        'last_error': {'code': 'OBSERVED_SOURCE_URL_REQUIRED', 'at': 'earlier'}}
        self.driver = Driver()
        self.stop = self.root / 'STOP'
        self.stop.write_bytes(b'{"reason":"synthetic account verification"}\n')
        self.grant = self.root / 'grant.json'
        self.grant.write_bytes(b'{"status":"historical recovery evidence"}\n')

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def failed_lease(self, exc):
        @contextmanager
        def lease(*args):
            raise exc
            yield  # pragma: no cover
        return lease

    def run_work(self, lease, collect=None):
        with patch.object(bridge, 'load_binding', return_value=self.binding), \
             patch.object(bridge, 'Driver', return_value=self.driver), \
             patch.object(bridge, 'lease', lease), \
             patch.object(bridge, 'collect_one', side_effect=collect) as collector, \
             patch.object(bridge, 'save_binding') as save, \
             patch.object(bridge.time, 'sleep'):
            result = bridge.work(self.p.path, 'test', units=1, max_seconds=30,
                                 interval=3, task_ids=[self.tid])
        return result, collector, save

    def test_account_stop_does_not_become_a_stale_local_hold(self):
        before_task = self.p.task(self.tid)
        before_binding = copy.deepcopy(self.binding)
        stop, grant = self.stop.read_bytes(), self.grant.read_bytes()
        result, collector, save = self.run_work(
            self.failed_lease(bridge.legacy.StopRun('Current account recovery invalid')))
        self.assertEqual(result['event'], 'blocked')
        self.assertEqual(result['reason'], 'SHARED_STOP')
        self.assertEqual(result['processed_units'], 0)
        self.assertEqual(result['metrics']['calls'], 0)
        collector.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.p.task(self.tid), before_task)
        self.assertEqual(self.binding, before_binding)
        self.assertEqual(self.stop.read_bytes(), stop)
        self.assertEqual(self.grant.read_bytes(), grant)
        self.assertTrue(self.driver.closed)
        handoff = json.loads((self.p.path / 'bridge' / 'handoff.json').read_text())
        self.assertEqual(handoff['reason'], 'SHARED_STOP')

    def test_stale_target_change_does_not_trigger_home_restoration(self):
        self.binding['last_error']['code'] = 'UNEXPECTED_TARGET_CHANGE'
        self.binding['allow_home_restore'] = {'authorization': 'synthetic preexisting consent'}
        with patch.object(bridge, 'restore_owned_home') as restore:
            result, collector, _ = self.run_work(
                self.failed_lease(bridge.legacy.StopRun('Current account stop')))
        self.assertEqual(result['reason'], 'SHARED_STOP')
        restore.assert_not_called()
        collector.assert_not_called()

    def test_untyped_research_error_does_not_inherit_old_error(self):
        result, collector, _ = self.run_work(
            self.failed_lease(bridge.legacy.ResearchError('Current connector failure')))
        self.assertEqual(result['event'], 'blocked')
        self.assertEqual(result['reason'], 'CONNECTOR_ERROR')
        self.assertNotIn('fault', self.p.task(self.tid)['payload'].get('bridge', {}))
        collector.assert_not_called()

    def test_current_typed_access_error_stays_blocking(self):
        result, collector, _ = self.run_work(
            self.failed_lease(bridge.BridgeError('ACCESS_STOP')))
        self.assertEqual(result['event'], 'blocked')
        self.assertEqual(result['reason'], 'ACCESS_STOP')
        self.assertNotIn('fault', self.p.task(self.tid)['payload'].get('bridge', {}))
        collector.assert_not_called()

    def test_current_typed_local_error_keeps_its_actual_hold(self):
        result, _, _ = self.run_work(
            lambda *args: nullcontext([]), bridge.BridgeError('SORT_UNCONFIRMED'))
        self.assertEqual(result['processed_units'], 1)
        fault = self.p.task(self.tid)['payload']['bridge']['fault']
        self.assertEqual(fault['code'], 'SORT_UNCONFIRMED')
        self.assertTrue(fault['hold'])
        self.assertEqual(self.binding['last_error']['code'], 'SORT_UNCONFIRMED')


if __name__ == '__main__':
    unittest.main()
