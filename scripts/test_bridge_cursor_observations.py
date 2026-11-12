"""Restoration observations are not failed forward pagination attempts."""
import copy
import unittest
from unittest.mock import patch

import test_bridge as fixture
import test_bridge_sort_resume as sort_fixture
import xhs_bridge as b
import xhs_project as xp

NEW_NOTE='4'*24


class ClampedDriver(sort_fixture.SearchDriver):
    def __init__(self,top=208,maximum=371,anchor_top=None,new_after_restore=False,stage='search'):
        super().__init__(top=top)
        self.maximum=maximum;self.anchor_top=anchor_top;self.new_after_restore=new_after_restore
        self.restores=[];self.forward_count=0;self.restored=False;self.explicit_end=False
        if stage=='author':self.url='https://www.xiaohongshu.com/user/profile/'+fixture.NOTE

    def call(self,action,binding=None,role=None,**fields):
        if action=='capture':
            value=super().call(action,binding,role,**fields)
            value['position'].update(scrollHeight=self.maximum+208,viewportHeight=208,
                                     firstNoteId=fixture.NOTE,lastNoteId=fixture.NOTE)
            value['explicitEnd']=self.explicit_end
            if self.new_after_restore and self.restored:
                value['cards'].append({'url':fixture.URL.replace(fixture.NOTE,NEW_NOTE),'text':'New rendered note'})
            return value
        if action=='advance' and not fields.get('sort') and not fields.get('dismissFilters'):
            self.calls.append((action,binding['context_id'],role,fields))
            before=self.top
            if 'resumeTop' in fields:
                self.restores.append(dict(fields));self.restored=True
                self.top=self.anchor_top if self.anchor_top is not None else min(self.maximum,fields['resumeTop'])
                return {'action':'scroll','before':before,'after':self.top,'anchorRestored':self.anchor_top is not None}
            self.forward_count+=1;self.top=min(self.maximum,self.top+500)
            return {'action':'scroll','before':before,'after':self.top,'anchorRestored':False}
        return super().call(action,binding,role,**fields)


class CursorObservationTests(unittest.TestCase):
    def setUp(self):
        self.f=sort_fixture.SortResumeTests();self.f.setUp();self.p=self.f.p;self.tid=self.f.f.tid
        self.binding=self.f.binding
        self.seed={'url':'https://www.xiaohongshu.com/search_result?keyword='+fixture.NOTE,
                   'uid':fixture.UID,'selected':['综合'],'cards':[{'url':fixture.URL,'text':'Known note'}],
                   'position':{'scrollTop':1066,'scrollHeight':2000,'viewportHeight':208,
                               'firstNoteId':fixture.NOTE,'lastNoteId':fixture.NOTE},'explicitEnd':False}
        b.store_capture(self.p,self.p.task(self.tid),self.seed)
        self.original_cursor=copy.deepcopy(self.progress()['cursor']);self.original_hashes=list(self.progress()['hashes'])

    def tearDown(self):self.f.tearDown()
    def progress(self):return self.p.task(self.tid)['payload']['bridge']

    def test_208_viewport_old_1066_current_max_371_restores_once_and_counts_only_forward(self):
        driver=ClampedDriver()
        with patch.object(b,'store_capture',wraps=b.store_capture) as capture:
            b.collect_one(self.p,self.binding,driver,self.p.task(self.tid))
        self.assertEqual(len(driver.restores),1);self.assertEqual(driver.forward_count,1)
        self.assertEqual([c.kwargs.get('count_stall',True) for c in capture.call_args_list],[False,False,False,True])
        pr=self.progress();self.assertEqual(pr['cursor'],self.original_cursor)
        self.assertEqual(pr['last_observed_position']['scrollTop'],371)
        self.assertEqual(pr['last_observed_position']['viewportHeight'],208)
        self.assertEqual(pr['stalled_count'],1);self.assertNotIn('fault',pr);self.assertFalse(pr['collection_end'])
        observation=pr['cursor_restoration_observations'][-1]
        self.assertEqual(observation['reason'],'current_document_maximum_clamped')
        self.assertEqual(observation['current_maximum'],371);self.assertFalse(observation['platform_end_inferred'])
        self.assertIn(observation['snapshot'].split('/')[-1].removesuffix('.json'),pr['hashes'])
        self.assertTrue(set(self.original_hashes)<=set(pr['hashes']))
        self.assertEqual(pr.get('reviewed_hashes',[]),[])

    def test_repeated_observation_and_restore_never_manufacture_three_failures(self):
        data=copy.deepcopy(self.seed);data['position']['scrollTop']=208
        for _ in range(9):b.store_capture(self.p,self.p.task(self.tid),data,count_stall=False)
        pr=self.progress();self.assertEqual(pr['stalled_count'],0);self.assertNotIn('fault',pr)
        self.assertEqual(pr['cursor'],self.original_cursor);self.assertEqual(pr['last_observed_position']['scrollTop'],208)

    def test_three_real_forward_failures_still_raise_pagination_hold(self):
        driver=ClampedDriver()
        for count in range(1,4):
            b.collect_one(self.p,self.binding,driver,self.p.task(self.tid),force_advance=True)
            self.assertEqual(self.progress()['stalled_count'],count)
        pr=self.progress();self.assertEqual(len(driver.restores),3);self.assertEqual(driver.forward_count,3)
        self.assertEqual(pr['fault']['code'],'PAGINATION_STALLED');self.assertTrue(pr['fault']['hold'])
        self.assertTrue(pr['awaiting_review']);self.assertFalse(pr['collection_end'])
        self.assertEqual(pr['cursor'],self.original_cursor)

    def test_direct_store_default_still_counts_repeated_no_progress(self):
        for count in range(1,4):
            b.store_capture(self.p,self.p.task(self.tid),copy.deepcopy(self.seed))
            self.assertEqual(self.progress()['stalled_count'],count)
        self.assertEqual(self.progress()['fault']['code'],'PAGINATION_STALLED')

    def test_new_id_in_restoration_is_retained_and_resets_old_stall(self):
        task=self.p.task(self.tid);task['payload']['bridge']['stalled_count']=2
        task['payload']['bridge']['fault']={'code':'PAGINATION_STALLED','hold':True,'recovery':{}}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()
        driver=ClampedDriver(new_after_restore=True)
        b.collect_one(self.p,self.binding,driver,self.p.task(self.tid),force_advance=True)
        pr=self.progress();self.assertEqual(set(pr['observed_note_ids']),{fixture.NOTE,NEW_NOTE})
        self.assertEqual(pr['stalled_count'],1);self.assertNotIn('fault',pr)
        self.assertEqual(pr['cursor'],self.original_cursor);self.assertEqual(pr.get('reviewed_hashes',[]),[])

    def test_confirmed_end_observation_is_progress_even_when_not_counting_stalls(self):
        data=copy.deepcopy(self.seed)
        for _ in range(2):b.store_capture(self.p,self.p.task(self.tid),data)
        data['position']['scrollTop']=371;data['explicitEnd']=True
        b.store_capture(self.p,self.p.task(self.tid),data,count_stall=False)
        pr=self.progress();self.assertEqual(pr['stalled_count'],0);self.assertTrue(pr['collection_end'])
        self.assertEqual(pr['cursor'],self.original_cursor)

    def test_restart_anchor_is_preserved_and_real_forward_below_old_high_water_advances(self):
        driver=ClampedDriver(maximum=2000,anchor_top=300)
        b.collect_one(self.p,self.binding,driver,self.p.task(self.tid))
        pr=self.progress();self.assertEqual(len(driver.restores),1)
        self.assertEqual(driver.restores[0]['anchor'],fixture.NOTE);self.assertEqual(driver.restores[0]['resumeTop'],1066)
        self.assertEqual(pr['last_observed_position']['scrollTop'],800)
        self.assertEqual(pr['cursor'],self.original_cursor);self.assertEqual(pr['stalled_count'],0)
        self.assertEqual(pr['cursor_restoration_observations'][-1]['reason'],'observed_anchor_restored')
        self.assertFalse(pr['collection_end'])

    def test_author_restoration_uses_same_noncounting_and_clamp_rules(self):
        task=self.p.task(self.tid);task['payload'].update(stage='author',bridge_url='https://www.xiaohongshu.com/user/profile/'+fixture.NOTE)
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),self.tid));self.p.db.commit()
        self.binding={'context_id':self.binding['context_id'],'account_uid':self.binding['account_uid'],
                      'tabs':{'detail':{'page':'detail','owned':True}}}
        driver=ClampedDriver(stage='author')
        b.collect_one(self.p,self.binding,driver,self.p.task(self.tid))
        pr=self.progress();self.assertEqual(len(driver.restores),1);self.assertEqual(pr['stalled_count'],1)
        self.assertEqual(pr['cursor'],self.original_cursor);self.assertFalse(pr['collection_end'])


if __name__=='__main__':unittest.main()
