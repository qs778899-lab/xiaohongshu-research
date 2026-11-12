"""Synthetic export tests: employment context never upgrades age or attribution."""
import json
import unittest

import test_bridge as fixture
import xhs_rounds as rounds


class EmploymentExportTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BridgeTests();self.f.setUp();self.p=self.f.project
        rounds.create(self.p,'employment-test',{'cutoff':'2026-11-05T00:00:00+08:00',
                      'doctors':['合成医生'],'employment_rule':'近期官方临床证据'})
        self.source=self.p.path/'synthetic-source.txt';self.source.write_text('合成患者原文；年龄及医生归属分别核查。')

    def tearDown(self):self.f.tearDown()

    def entry(self,**fields):
        return {'id':'S001','note_id':fixture.NOTE,'doctor':'原评价标签不应自动当作已核姓名',
                'url':fixture.URL,'source':self.source.name,'summary':'合成评价',
                'boundary':'原归属边界保持不变',**fields}

    def export(self,entry):
        manifest=self.p.path/'synthetic-evidence.json';manifest.write_text(json.dumps({'evaluation_entries':[entry]}))
        rounds.import_evidence(self.p,manifest)
        before=[tuple(row) for row in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]
        evidence_before=[tuple(row) for row in self.p.db.execute('SELECT * FROM evidence_versions')]
        result=rounds.export(self.p,'employment-test')
        self.assertEqual(before,[tuple(row) for row in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])
        self.assertEqual(evidence_before,[tuple(row) for row in self.p.db.execute('SELECT * FROM evidence_versions')])
        directory=self.p.path/'exports/employment-test'
        return ((directory/'总文档.md').read_text(),(directory/'成人专项.md').read_text(),
                json.loads((directory/'原帖索引.json').read_text())[0])

    def fields(self):
        return {'employment_status':'recent_official_clinical_affiliation_supported',
                'employment_verified':True,'employment_doctor':'合成医生',
                'employment_source_url':'https://hospital.example/official/2026-07.html',
                'employment_source_published_at':'2026-07-17','employment_checked_on':'2026-11-05',
                'employment_source_locator':'官方资料第二段',
                'employment_verification_scope':'近期临床任职支持；非当日排班核验',
                'employment_boundary':'不证明本条治疗发生在该院'}

    def test_supported_adult_entry_has_identical_dated_context_in_both_documents(self):
        full,adult,index=self.export(self.entry(adult_verified=True,**self.fields()))
        section=full[full.index('## S001'):]
        self.assertIn(section,adult)
        for text in ['任职背景（近期官方临床证据）：合成医生','资料发布日期：2026-07-17',
                     '本次核对日：2026-11-05','[官方／院方供稿来源](<https://hospital.example/official/2026-07.html>)',
                     '近期临床任职支持；非当日排班核验','核对日不代表当日实时在岗或实际排班']:
            self.assertIn(text,section)
        self.assertTrue(index['legacy_claim']);self.assertFalse(index['semantic_verified'])
        self.assertEqual(index['boundary'],'原归属边界保持不变')

    def test_missing_fields_remain_unknown_without_doctor_string_inference(self):
        full,adult,index=self.export(self.entry(adult_verified=True,
                    employment_status='recent_official_clinical_affiliation_supported'))
        for text in ['任职背景（近期官方临床证据）：待核','资料发布日期：待核；本次核对日：待核',
                     '官方资料：待核','核验范围：待核']:
            self.assertIn(text,full)
        self.assertIn('成人范围有依据，医生归属及现任条件另核',adult)
        self.assertNotIn('employment_verified',index)

    def test_known_employment_with_unknown_age_does_not_enter_adult_section(self):
        full,adult,index=self.export(self.entry(**self.fields()))
        self.assertIn('任职背景（近期官方临床证据）：合成医生',full)
        self.assertNotIn('## S001',adult)
        self.assertIn('S001：成人或现任条件未结构化核验',adult)
        self.assertIn('当前轮次的任职口径为“近期官方临床证据”支持',adult)
        self.assertNotIn('adult_verified',index)

    def test_confirmed_adult_start_is_retained_without_rewriting_legacy_review(self):
        full,adult,index=self.export(self.entry(adult_status='adult_start_confirmed',
                    adult_basis='原始记录明确27岁开始本阶段治疗',**self.fields()))
        self.assertIn(full[full.index('## S001'):],adult)
        self.assertEqual(index['adult_status'],'adult_start_confirmed')
        self.assertNotIn('adult_verified',index)
        self.assertTrue(index['legacy_claim'])
        self.assertFalse(index['semantic_verified'])

    def test_current_adult_age_does_not_prove_adult_treatment_start(self):
        _,adult,index=self.export(self.entry(current_age=30,treatment_start_age=None,
                    adult_status='current_age_only',**self.fields()))
        self.assertNotIn('## S001',adult)
        self.assertIn('S001：成人或现任条件未结构化核验',adult)
        self.assertIsNone(index['treatment_start_age'])

    def test_explicit_minor_start_is_excluded_with_its_source_not_called_unknown(self):
        full,adult,index=self.export(self.entry(adult_verified=False,age_at_start=16,
                    adult_status='minor_at_start_explicit',adult_basis='本人明确16岁开始正畸',
                    **self.fields()))
        self.assertIn('## S001',full)
        self.assertNotIn('## S001',adult)
        self.assertIn('S001：已确认未成年起治，不纳入成人起治核心',adult)
        self.assertIn('本人明确16岁开始正畸',adult)
        self.assertIn(f'[打开原帖]({fixture.URL})',adult)
        self.assertNotIn('S001：成人或现任条件未结构化核验',adult)
        self.assertEqual(index['age_at_start'],16)
        self.assertFalse(index['adult_verified'])

    def test_false_adult_flag_without_explicit_minor_evidence_is_not_minor(self):
        _,adult,index=self.export(self.entry(adult_verified=False,
                    adult_status='starting_age_unknown',**self.fields()))
        self.assertNotIn('## S001',adult)
        self.assertIn('S001：成人或现任条件未结构化核验',adult)
        self.assertNotIn('已确认未成年起治',adult)
        self.assertEqual(index['adult_status'],'starting_age_unknown')

    def test_child_consultation_is_excluded_without_inventing_start_age(self):
        _,adult,index=self.export(self.entry(adult_verified=False,age_at_start=None,
                    child_age_at_initial_post=4.5,
                    adult_status='explicit_child_consultation_start_not_confirmed',
                    adult_basis='原帖四岁半儿童咨询；实际起治未确认',**self.fields()))
        self.assertNotIn('## S001',adult)
        self.assertIn('已明确儿童咨询，不纳入成人范围；尚未确认起治',adult)
        self.assertNotIn('成人或现任条件未结构化核验',adult)
        self.assertIsNone(index['age_at_start'])
        self.assertEqual(index['child_age_at_initial_post'],4.5)

    def test_professional_content_is_not_mislabelled_as_age_unknown_patient(self):
        _,adult,index=self.export(self.entry(adult_verified=None,core_evaluation_eligible=False,
                    adult_status='not_applicable_professional_content_without_patient_evaluation',
                    **self.fields()))
        self.assertNotIn('## S001',adult)
        self.assertIn('专业账号内容，未取得独立患者评价，不纳入成人患者核心',adult)
        self.assertNotIn('成人或现任条件未结构化核验',adult)
        self.assertIsNone(index['adult_verified'])


if __name__=='__main__':unittest.main()
