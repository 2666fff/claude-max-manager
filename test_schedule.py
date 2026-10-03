"""Request counts and official reset deadlines; synthetic accounts only."""
import datetime as dt
import io
import json
import time
import unittest
from unittest.mock import patch

from core import write
import test_features


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_features.ContractTests()
        self.fixture.setUp()
        self.store = self.fixture.store
        self.now = time.time()

    def tearDown(self):
        self.fixture.tearDown()

    def reset(self, seconds):
        return dt.datetime.fromtimestamp(self.now + seconds, dt.timezone.utc).isoformat()

    def seed(self, slot, five=20, week=30, age=700, five_reset=1800, week_reset=7200):
        row = {'slot': slot, 'checked': self.now - age, 'next_poll': self.now - 1,
               'windows': {'five_hour': {'utilization': five, 'resets_at': self.reset(five_reset)},
                           'seven_day': {'utilization': week, 'resets_at': self.reset(week_reset)}}}
        write(self.store.profile(slot) / 'usage-cache.json', row)
        return row

    def response(self, request, **kwargs):
        slot = int(request.get_header('Authorization').rsplit('-', 1)[1])
        self.calls.append(slot)
        return io.BytesIO(json.dumps({'five_hour': {'utilization': 10, 'resets_at': self.reset(5000)},
                                     'seven_day': {'utilization': 20, 'resets_at': self.reset(7200)}}).encode())

    def test_random_deadline_persisted_once_per_success(self):
        self.calls = []
        with patch('enhanced.time.time', return_value=self.now), \
             patch('enhanced.urllib.request.urlopen', side_effect=self.response), \
             patch('enhanced.random.randint', return_value=480) as sample:
            row = self.store.quota(1, background=True)
            self.assertEqual(row['next_poll'] - row['checked'], 480)
            self.store.quota(1, background=True)
            self.assertEqual(self.calls, [1])
            sample.assert_called_once_with(300, 480)
        self.store.invalidate(1)
        with patch('enhanced.time.time', return_value=self.now), \
             patch('enhanced.urllib.request.urlopen', side_effect=self.response), \
             patch('enhanced.random.randint', return_value=300):
            row = self.store.quota(1)
            self.assertEqual(row['next_poll'] - row['checked'], 300)

    def test_background_only_fetches_current_before_standby_reset(self):
        self.seed(1)
        self.seed(2)
        self.seed(5, five=100)
        self.calls = []
        with patch('enhanced.time.time', return_value=self.now), \
             patch('enhanced.urllib.request.urlopen', side_effect=self.response):
            rows = [self.store.quota(s, background=True) for s in [1, 2, 5]]
            self.assertEqual(self.calls, [1])
            self.assertEqual(rows[1]['query_policy'], 'standby')
            self.assertEqual(rows[2]['query_policy'], 'waiting_reset')
            self.assertGreater(self.store.refresh_deadline(rows[1], active=False), self.now + 1000)

    def test_multiple_exhausted_windows_wait_for_last_reset_even_manual(self):
        row = self.seed(1, five=100, week=100, five_reset=1800, week_reset=7200)
        with patch('enhanced.time.time', return_value=self.now), \
             patch.object(self.store, 'token', side_effect=AssertionError('must not refresh OAuth')), \
             patch('enhanced.urllib.request.urlopen', side_effect=AssertionError('must not query usage')):
            self.assertAlmostEqual(self.store.blocked_until(row), self.now + 7200, places=5)
            self.assertAlmostEqual(self.store.refresh_deadline(row, active=True), self.now + 7200, places=5)
            self.assertTrue(self.store.quota(1)['cached'])
            self.assertTrue(self.store.quota(1, background=True)['cached'])

    def test_standby_fetches_once_after_reset_then_waits_next_reset(self):
        self.seed(2, five=100, five_reset=60)
        self.calls = []
        with patch('enhanced.time.time', return_value=self.now + 61), \
             patch('enhanced.urllib.request.urlopen', side_effect=self.response):
            row = self.store.quota(2, background=True)
            self.assertEqual(self.calls, [2])
            self.assertAlmostEqual(self.store.refresh_deadline(row, active=False), self.now + 5000, places=5)
            self.store.quota(2, background=True)
            self.assertEqual(self.calls, [2])

    def test_stale_candidate_validated_but_exhausted_current_not_queried(self):
        rows = [self.seed(1, five=100), self.seed(2), self.seed(5, week=100)]
        self.calls = []
        with patch('enhanced.time.time', return_value=self.now), \
             patch('enhanced.runner_active', return_value=False), \
             patch('enhanced.urllib.request.urlopen', side_effect=self.response), \
             patch.object(self.store, 'switch') as switch:
            self.store.auto_step(rows)
            self.assertEqual(self.calls, [2])
            switch.assert_called_once_with(2, allow_running=True)
            self.assertEqual(rows[1]['checked'], self.now)

    def test_model_blocker_used_only_when_selected_and_invalid_reset_not_guessed(self):
        row = self.seed(2)
        row['scoped'] = [{'name': 'Opus', 'utilization': 100, 'resets_at': self.reset(9000)}]
        self.assertIsNone(self.store.blocked_until(row))
        self.store.save_settings({'model': 'opus'})
        self.assertAlmostEqual(self.store.blocked_until(row), self.now + 9000, places=5)
        row['scoped'][0]['resets_at'] = '2026-10-03T20:00:00'  # No authoritative timezone.
        self.assertIsNone(self.store.blocked_until(row))


if __name__ == '__main__':
    unittest.main()
