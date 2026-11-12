"""Closed filters must not turn sort verification into a new search."""
import unittest
from unittest.mock import patch

import test_bridge as fixture
import xhs_bridge as bridge
import xhs_project as project


class SearchDriver(fixture.Fake):
    def __init__(self,sort='综合',top=600,clicked_selection_visible=True):
        super().__init__('https://www.xiaohongshu.com/search_result?keyword='+fixture.NOTE)
        self.sort=sort;self.top=top;self.panel=False;self.sort_clicks=0;self.opens=0
        self.clicked_selection_visible=clicked_selection_visible

    def call(self,action,binding=None,role=None,**fields):
        if action in {'capture','advance','activate','scroll_diagnose'}:
            self.calls.append((action,binding['context_id'],role,fields))
            if action=='capture':
                return {'url':self.url,'uid':fixture.UID,'selected':[self.sort] if self.panel and (not self.sort_clicks or self.clicked_selection_visible) else [],
                        'cards':[{'url':fixture.URL,'text':'保留当前结果'}],
                        'position':{'scrollTop':self.top,'scrollHeight':5000,'viewportHeight':600},'explicitEnd':False}
            if action=='advance':
                if fields.get('sort'):
                    if not self.panel:self.panel=True;self.opens+=1;return {'action':'filter_opened'}
                    self.sort_clicks+=1;self.sort={'general':'综合','latest':'最新','popular':'最多点赞'}[fields['sort']]
                    self.top=0;return {'action':'sort_clicked'}
                if fields.get('dismissFilters'):self.panel=False;return {'action':'filter_closed'}
                self.top=fields['resumeTop'] if 'resumeTop' in fields else self.top+500
                return {'action':'scroll','after':self.top}
            return {}
        return super().call(action,binding,role,**fields)


class SortResumeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        task=self.p.task(self.f.tid);task['payload'].update(stage='search',sort='general')
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(project.dumps(task['payload']),task['id']));self.p.db.commit()
        self.binding={**self.f.binding,'tabs':{'search':{'page':'search','owned':True}}}
        self.sleep=patch.object(bridge.time,'sleep',lambda _:None);self.sleep.start()

    def tearDown(self):self.sleep.stop();self.f.tearDown()

    def test_three_closed_panel_units_keep_sort_and_advance_without_research(self):
        driver=SearchDriver()
        for _ in range(3):bridge.collect_one(self.p,self.binding,driver,self.p.task(self.f.tid),force_advance=True)
        self.assertEqual(driver.opens,3);self.assertEqual(driver.sort_clicks,0)
        self.assertEqual(driver.top,2100)
        self.assertEqual(self.p.task(self.f.tid)['payload']['bridge']['cursor']['scrollTop'],2100)

    def test_different_sort_is_selected_once_then_not_reclicked(self):
        driver=SearchDriver(sort='最新')
        bridge.collect_one(self.p,self.binding,driver,self.p.task(self.f.tid),force_advance=True)
        bridge.collect_one(self.p,self.binding,driver,self.p.task(self.f.tid),force_advance=True)
        self.assertEqual(driver.sort_clicks,1);self.assertEqual(driver.sort,'综合')
        self.assertEqual(driver.top,1000)

    def test_unconfirmed_click_is_not_repeated(self):
        driver=SearchDriver(sort='最新',clicked_selection_visible=False)
        with self.assertRaisesRegex(bridge.BridgeError,'SORT_UNCONFIRMED'):
            bridge.collect_one(self.p,self.binding,driver,self.p.task(self.f.tid))
        self.assertEqual(driver.sort_clicks,1)

    def test_pagination_repair_checks_existing_sort_without_clicking_it(self):
        task=self.p.task(self.f.tid);task['payload']['bridge']={'cursor':{'scrollTop':600},'fault':{'code':'PAGINATION_STALLED','hold':True,'recovery':{}}}
        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',(project.dumps(task['payload']),task['id']));self.p.db.commit()
        driver=SearchDriver();result=bridge.repair_pagination(self.p,self.binding,driver,self.p.task(task['id']))
        self.assertTrue(result['recovered']);self.assertEqual(driver.sort_clicks,0);self.assertEqual(driver.top,1100)


if __name__=='__main__':unittest.main()
