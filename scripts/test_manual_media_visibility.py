import copy,unittest
from unittest.mock import patch,Mock
import xhs_bridge as bridge
import xhs_bridge_media as media

class VisibleManualTests(unittest.TestCase):
 def setUp(self):
  self.note='1'*24;self.url='https://www.xiaohongshu.com/explore/'+self.note;self.expected='/explore/'+self.note
  self.binding={'manual_single_tab':True,'transport_surface':'browser','account_uid':'2'*24,'context_id':'test',
    'tabs':{'detail':{'page':'A'*32,'owned':False,'chrome_tab_id':31,'window_id':21}}}
  self.meta={'page':'A'*32,'url':self.url,'visible':True};self.driver=Mock()
 def test_visible_manual_does_not_select_or_claim_read(self):
  with patch.object(bridge,'verify_tab',return_value=self.meta) as check:
   proof=media.prepare_carousel_view(self.driver,self.binding,self.expected)
  self.driver.call.assert_not_called();self.assertEqual(check.call_count,2)
  self.assertTrue(media.confirmed_view_preparation(proof));self.assertTrue(proof['visual_verification_required'])
  self.assertNotIn('reviewed',proof);self.assertIsNone(proof['activated_at'])
 def test_background_or_unconfirmed_manual_does_not_activate(self):
  for value in [False,None]:
   with self.subTest(value=value),patch.object(bridge,'verify_tab',return_value=dict(self.meta,visible=value)):
    with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_ACTIVATION_UNCONFIRMED'):media.prepare_carousel_view(self.driver,self.binding,self.expected)
  self.driver.call.assert_not_called()
 def test_changed_visibility_or_target_fails(self):
  for after in [dict(self.meta,visible=False),dict(self.meta,page='B'*32),dict(self.meta,url='https://www.xiaohongshu.com/explore/'+'3'*24)]:
   with self.subTest(after=after),patch.object(bridge,'verify_tab',side_effect=[self.meta,after]):
    with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_ACTIVATION_UNCONFIRMED'):media.prepare_carousel_view(self.driver,self.binding,self.expected)
 def test_not_manually_bound_fails_even_if_visible(self):
  binding=copy.deepcopy(self.binding);binding['manual_single_tab']=False
  with patch.object(bridge,'verify_tab',return_value=self.meta):
   with self.assertRaises(bridge.BridgeError):media.prepare_carousel_view(self.driver,binding,self.expected)
 def test_owned_flow_still_selects_once(self):
  binding=copy.deepcopy(self.binding);binding['tabs']['detail']['owned']=True
  self.driver.call.return_value={'activated':'A'*32,'windowFocusRequested':False}
  with patch.object(bridge,'verify_tab',return_value=self.meta):proof=media.prepare_carousel_view(self.driver,binding,self.expected)
  self.driver.call.assert_called_once_with('activate',binding,'detail');self.assertTrue(media.confirmed_view_preparation(proof))
 def test_incomplete_visible_proof_cannot_be_reused(self):
  with patch.object(bridge,'verify_tab',return_value=self.meta):proof=media.prepare_carousel_view(self.driver,self.binding,self.expected)
  for field in ['visible_before','visible_after','selectionSubmitted']:
   invalid=copy.deepcopy(proof);invalid['response'].pop(field)
   self.assertFalse(media.confirmed_view_preparation(invalid))
 def test_navigation_to_background_user_tab_is_local_gap_without_selection(self):
  from types import SimpleNamespace
  self.binding['binding_name']='manual'
  initial=dict(self.meta,url='https://www.xiaohongshu.com/explore/'+'4'*24)
  for stage in ['body','images','video']:
   self.binding['tabs']['detail'].pop('navigation_intent',None)
   self.driver.reset_mock()
   task={'payload':{'stage':stage,'target':self.note}}
   with self.subTest(stage=stage),patch.object(bridge,'observed_url',return_value=self.url), \
        patch.object(bridge,'verify_tab',side_effect=[initial,dict(self.meta,visible=False)]), \
        patch.object(bridge,'save_binding'),patch.object(bridge.time,'sleep'):
    with self.assertRaisesRegex(bridge.BridgeError,'MEDIA_ACTIVATION_UNCONFIRMED'):
     media.prepare_media_target(SimpleNamespace(path='/unused'),self.binding,self.driver,task)
   self.driver.call.assert_called_once_with('navigate',self.binding,'detail',url=self.url)
 def test_visible_manual_preflight_uses_normal_target_guards(self):
  from types import SimpleNamespace
  task={'payload':{'stage':'images','target':self.note}}
  with patch.object(bridge,'observed_url',return_value=self.url),patch.object(bridge,'verify_tab',return_value=self.meta):
   result=media.prepare_media_target(SimpleNamespace(path='/unused'),self.binding,self.driver,task)
  self.assertEqual(result[1],'detail');self.assertNotIn('backgroundText',result[2]);self.driver.call.assert_not_called()
 def test_preflight_preserves_access_failures(self):
  with patch.object(bridge,'prepare_target',side_effect=bridge.BridgeError('ACCESS_STOP')):
   with self.assertRaisesRegex(bridge.BridgeError,'ACCESS_STOP'):
    media.prepare_media_target(None,self.binding,self.driver,{'payload':{'stage':'images'}})
  self.driver.call.assert_not_called()
 def test_owned_preflight_keeps_original_driver(self):
  binding=copy.deepcopy(self.binding);binding['tabs']['detail']['owned']=True
  task={'payload':{'stage':'images','target':self.note}}
  with patch.object(bridge,'prepare_target') as prepare:
   media.prepare_media_target(None,binding,self.driver,task)
  prepare.assert_called_once_with(None,binding,self.driver,task)
if __name__=='__main__':unittest.main()
