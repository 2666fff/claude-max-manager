"""Deterministic contracts; fabricated credentials only, no network or live files."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from core import write, read
from enhanced import Store, AuthProblem, retry_seconds, directory_lock
from runner import classify


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'accounts'
        self.live = self.base / 'live'
        self.live.mkdir()
        self.store = Store(self.root, self.live, self.live / '.claude.json', process_check=lambda: [])
        for slot in [1, 2, 5]:
            write(self.store.profile(slot) / '.claude.json', {'oauthAccount': {'accountUuid': f'uuid-{slot}', 'emailAddress': f'account-{slot}@example.invalid'}})
            write(self.store.profile(slot) / '.credentials.json', {'claudeAiOauth': {
                'accessToken': f'fake-access-{slot}', 'refreshToken': f'fake-refresh-{slot}',
                'subscriptionType': 'max', 'expiresAt': int((time.time()+3600)*1000),
                'scopes': ['user:inference', 'user:profile']}, 'mcpOAuth': {'keep': 'fixture'}})
        write(self.live / '.claude.json', read(self.store.profile(1) / '.claude.json'))
        write(self.live / '.credentials.json', read(self.store.profile(1) / '.credentials.json'))

    def tearDown(self):
        self.temp.cleanup()

    def row(self, slot, five=10, week=20):
        return {'slot': slot, 'checked': time.time(), 'windows': {'five_hour': {'utilization': five}, 'seven_day': {'utilization': week}}}

    def test_week_cap_disabled_stale_and_unknown_excluded(self):
        self.assertEqual(self.store.choose([self.row(1, 10, 100), self.row(2, 40, 30), self.row(5, 0, 100)]), 2)
        self.store.set_meta(2, enabled=False)
        self.assertIsNone(self.store.choose([self.row(1, 10, 100), self.row(2, 0, 0)]))
        old = self.row(5);old['checked'] -= 700
        self.assertIsNone(self.store.choose([old, {'slot':1,'error':'network'}]))

    def test_model_specific_cap(self):
        row = self.row(2);row['scoped'] = [{'name':'Opus','utilization':100}]
        self.store.save_settings({'model':'opus'})
        self.assertIsNone(self.store.choose([row]))

    def test_refresh_preserves_scopes_and_other_credentials(self):
        response = io.BytesIO(json.dumps({'access_token':'new-fixture-access','refresh_token':'new-fixture-refresh','expires_in':3600,'scope':'user:profile user:inference','refresh_token_expires_in':86400}).encode())
        with patch('enhanced.urllib.request.urlopen', return_value=response):
            result = self.store.token(2,force=True)
        saved = read(self.store.profile(2)/'.credentials.json')
        self.assertEqual(result['accessToken'],'new-fixture-access')
        self.assertEqual(saved['mcpOAuth'],{'keep':'fixture'})
        self.assertIn('user:inference',result['scopes'])
        self.assertGreater(result['refreshTokenExpiresAt'],time.time()*1000)
        self.assertFalse((self.store.profile(2)/'.refresh-result.json').exists())

    def test_revoked_token_quarantined_but_429_is_not(self):
        error = urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"error":"invalid_grant"}'))
        with patch('enhanced.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(AuthProblem) as raised:self.store.token(2,force=True)
        self.assertTrue(raised.exception.permanent)
        self.assertTrue(self.store.meta(2)['dead_token'])
        error = urllib.error.HTTPError('https://example.invalid',429,'limited',{'Retry-After':'600'},io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'))
        with patch('enhanced.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(AuthProblem) as raised:self.store.token(5,force=True)
        self.assertFalse(raised.exception.permanent)
        self.assertGreaterEqual(self.store.meta(5)['refresh_retry_at'],time.time()+590)

    def test_cached_usage_no_network_and_failure_not_usable(self):
        good = self.row(2);good['next_poll']=time.time()+300
        write(self.store.profile(2)/'usage-cache.json',good)
        with patch('enhanced.urllib.request.urlopen',side_effect=AssertionError('unneeded network')):
            result=self.store.quota(2)
        self.assertTrue(result['cached'])
        self.store.invalidate(2)
        error=urllib.error.HTTPError('https://example.invalid',429,'limited',{'Retry-After':'800'},io.BytesIO(b'{}'))
        with patch('enhanced.urllib.request.urlopen',side_effect=error):
            result=self.store.quota(2)
        self.assertIn('error',result)
        self.assertGreater(result['next_poll'],time.time()+790)
        self.assertIsNone(self.store.choose([result]))

    def test_refresh_journal_recovery(self):
        import hashlib
        doc=read(self.store.profile(2)/'.credentials.json');old=doc['claudeAiOauth']
        new=dict(old,accessToken='recovered-fixture')
        digest=hashlib.sha256(json.dumps(old,sort_keys=True).encode()).hexdigest()
        write(self.store.profile(2)/'.refresh-result.json',{'before':digest,'oauth':new})
        self.assertEqual(self.store.token(2)['accessToken'],'recovered-fixture')

    def test_remove_restore_and_current_protection(self):
        with self.assertRaises(RuntimeError):self.store.remove(1)
        self.store.remove(2)
        self.assertNotIn(2,self.store.slots())
        self.store.restore_removed()
        self.assertEqual(self.store.identity(6)['accountUuid'],'uuid-2')

    def test_process_guard_and_credentials_preserved(self):
        self.store.process_check=lambda:[123]
        before=(self.live/'.credentials.json').read_bytes()
        with self.assertRaises(RuntimeError):self.store.switch(2)
        self.assertEqual(before,(self.live/'.credentials.json').read_bytes())

    def test_lock_contention_does_not_steal(self):
        path=self.root/'fixture.lock'
        with directory_lock(path):
            with self.assertRaises(RuntimeError):
                with directory_lock(path,timeout=.01):pass
            self.assertTrue(path.is_dir())

    def test_real_cli_limit_event_shape_and_normal_success(self):
        # Sanitized structural shape observed from installed 2.1.288 on 2026-10-03.
        events=[{'type':'assistant','error':'rate_limit'}, {'type':'result','is_error':True,'subtype':'success'}]
        self.assertEqual(classify(events,1),'limit')
        self.assertEqual(classify([{'type':'result','is_error':False}],0),'complete')
        self.assertEqual(classify([{'type':'assistant','text':'rate_limit'}],1),'error')
        self.assertEqual(classify([{'type':'result','is_error':True,'errors':['permission denied']}],1),'error')

    def test_retry_after_date(self):
        self.assertEqual(retry_seconds('60'),60)
        self.assertEqual(retry_seconds('Thu, 01 Jan 1970 00:10:00 GMT',now=0),600)

    def test_auto_waits_for_process_and_respects_cooldown(self):
        self.store.save_settings({'auto_enabled':True})
        rows=[self.row(1,100,20),self.row(2,10,20)]
        self.store.process_check=lambda:[8888]
        with patch('enhanced.runner_active',return_value=False), patch.object(self.store,'switch') as switch:
            self.assertIn('需退出',self.store.auto_step(rows))
            switch.assert_not_called()
            self.store.process_check=lambda:[]
            self.assertIn('已自动切换到账号 2',self.store.auto_step(rows))
            switch.assert_called_once_with(2)
            write(self.root/'rotation.json',{'last_switch':time.time()})
            self.assertIn('冷却',self.store.auto_step(rows))
            self.assertEqual(switch.call_count,1)

    def test_exhausted_pool_waits_and_runner_owns_rotation(self):
        self.store.save_settings({'auto_enabled':True})
        rows=[self.row(1,100,20),self.row(2,0,100),self.row(5,0,100)]
        with patch('enhanced.runner_active',return_value=False), patch.object(self.store,'switch') as switch:
            self.assertIn('没有额度充足',self.store.auto_step(rows))
            switch.assert_not_called()
        with patch('enhanced.runner_active',return_value=True):
            self.assertIn('受管任务正在管理',self.store.auto_step(rows))


if __name__=='__main__':
    unittest.main()
