"""Readiness observations must not turn a collapsed grid into reload/sort retries."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xhs_bridge as b


class Capture:
    def __init__(self,sequence):self.sequence=iter(sequence);self.calls=[]
    def call(self,action,*args,**kwargs):
        self.calls.append(action)
        if action=='reload':return {'reloadSubmitted':True}
        if action!='capture':raise AssertionError('Unexpected action: '+action)
        value=next(self.sequence)
        if isinstance(value,str):raise b.BridgeError(value)
        return value


class BackgroundLayoutCapture(unittest.TestCase):
    def setUp(self):
        self.project=SimpleNamespace(path=Path('/synthetic-never-written'))
        self.args={'stage':'search','query':'Synthetic query','expectedPath':'/search_result/','backgroundText':True}

    def run_capture(self,driver):return b.ready_capture(self.project,{'binding_name':'synthetic'},driver,'search',self.args)

    def test_three_read_only_observations_preserve_specific_gap_without_reload_or_sort(self):
        driver=Capture(['BACKGROUND_LAYOUT_UNSETTLED']*3)
        with patch.object(b.time,'sleep') as sleep,patch.object(b,'verify_tab') as verify,patch.object(b,'save_binding') as save:
            with self.assertRaisesRegex(b.BridgeError,'^BACKGROUND_LAYOUT_UNSETTLED$'):self.run_capture(driver)
        self.assertEqual(driver.calls,['capture']*3);self.assertEqual([x.args for x in sleep.call_args_list],[(1,),(1,)])
        verify.assert_not_called();save.assert_not_called();self.assertIn('BACKGROUND_LAYOUT_UNSETTLED',b.LOCAL_FAULTS)

    def test_later_real_layout_can_be_captured_within_three_observations(self):
        ready={'cards':[{'text':'Synthetic actual card'}]};driver=Capture(['BACKGROUND_LAYOUT_UNSETTLED']*2+[ready])
        with patch.object(b.time,'sleep'):self.assertEqual(self.run_capture(driver),ready)
        self.assertEqual(driver.calls,['capture']*3)

    def test_loading_then_collapsed_layout_still_does_not_reload(self):
        driver=Capture(['CONTENT_LOADING','BACKGROUND_LAYOUT_UNSETTLED','BACKGROUND_LAYOUT_UNSETTLED'])
        with patch.object(b.time,'sleep'):
            with self.assertRaisesRegex(b.BridgeError,'^BACKGROUND_LAYOUT_UNSETTLED$'):self.run_capture(driver)
        self.assertEqual(driver.calls,['capture']*3)

    def test_stop_account_and_target_errors_are_not_retried_or_masked(self):
        for code in ['ACCESS_STOP','ACCOUNT_MISMATCH','EXEC_TARGET_MISMATCH','TARGET_MISMATCH']:
            with self.subTest(code=code):
                driver=Capture(['BACKGROUND_LAYOUT_UNSETTLED',code])
                with patch.object(b.time,'sleep'):
                    with self.assertRaisesRegex(b.BridgeError,'^'+code+'$'):self.run_capture(driver)
                self.assertEqual(driver.calls,['capture']*2)

    def test_existing_real_loading_single_reload_contract_is_unchanged(self):
        ready={'cards':[{'text':'Synthetic loaded card'}]};driver=Capture(['CONTENT_LOADING']*3+[ready])
        with patch.object(b.time,'sleep'),patch.object(b,'save_binding'),patch.object(b,'verify_tab',return_value={
                'url':'https://www.xiaohongshu.com/search_result/?keyword=Synthetic','visible':False}):
            self.assertEqual(self.run_capture(driver),ready)
        self.assertEqual(driver.calls,['capture']*3+['reload','capture'])


if __name__=='__main__':unittest.main()
