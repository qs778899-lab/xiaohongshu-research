import unittest,tempfile,json,hashlib,copy
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import patch
import xhs_fork_role_recovery as r
import xhs_current_page_recovery as current

class Driver:
    def __init__(self,error=None,page='C'*32):self.calls=[];self.error=error;self.page=page
    def call(self,action,b,role):
        self.calls.append(action);page=self.page
        if self.error=='unknown' and action=='create':raise r.bridge.BridgeError('COMMAND_OUTCOME_UNKNOWN')
        if action=='create':return {'page':page}
        if action=='metadata':return {'page':page,'url':'' if self.error=='blank' else 'https://www.xiaohongshu.com/explore'}
        return {'page':'D'*32 if self.error=='page' else page,'uid':'wrong' if self.error=='uid' else 'account','url':'https://www.xiaohongshu.com/explore'}

class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Path(self.tmp.name);(self.p/'bridge/bindings').mkdir(parents=True)
        self.home={'roles':{'search':{'page':'A'*32,'session':'lease-search'},'detail':{'page':'B'*32,'session':'lease-detail'}}}
        self.b={'binding_name':'child','project':str(self.p),'format':'test','session':'lease','account_uid':'account','context_id':'context','extension_version':'1.0.26','profile_guard_key':'guard',
                'fork_home_verification':{'file':'original','sha256':'originalsha'},'tabs':{'detail':{'page':'B'*32,'session':'lease-detail','owned':True}},
                'retired_targets':[{'role':'search','tab':{'page':'A'*32,'session':'lease-search','owned':True},'proof':{'state':'registered_target_absent','page':'A'*32}}]}
        f=self.p/'authority.json';f.write_text(json.dumps({'parent_name':'parent','stop_hashes':{'stop':'sha'}}));self.b['web_parent_authorization']={'file':str(f),'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        homefile=self.p/'home.json';homefile.write_text(json.dumps(self.home));self.b['fork_home_verification']={'file':str(homefile),'sha256':hashlib.sha256(homefile.read_bytes()).hexdigest()}
        probe=self.p/'probe.json';probe.write_text(json.dumps({'stops':{'stop':{'sha256':'sha'}}}))
        grant=self.p/'grant.json';grant.write_text(json.dumps({'probe_file':str(probe),'probe_sha256':hashlib.sha256(probe.read_bytes()).hexdigest()}))
        self.b['current_page_revalidation']={'file':str(grant),'sha256':hashlib.sha256(grant.read_bytes()).hexdigest()}
        self.saved=[];self.ps=[]
        replacements=[patch.object(r.xp,'project_lock',lambda p:nullcontext()),patch.object(r.bridge,'load_binding',lambda p,n:copy.deepcopy(self.b) if n=='child' else {}),
            patch.object(r.nr,'locked_stops',lambda p,b:nullcontext(['key'])),patch.object(current,'web_exceptions',return_value={'stop':'sha'}),
            patch.object(r.nr,'completed_fork_home',return_value=(self.home,{})),patch.object(r.nr.native,'open_lookup_project',return_value=type('P',(),{'close':lambda self:None})()),
            patch.object(r.bridge,'save_binding',lambda p,n,b:self.saved.append(copy.deepcopy(b)))]
        for p in replacements:p.start();self.ps.append(p)
    def tearDown(self):
        for p in reversed(self.ps):p.stop()
        self.tmp.cleanup()
    def test_valid_missing_role_append_proof_and_same_session(self):
        d=Driver();o=r.recover(self.p,'child','search',d);self.assertTrue(o['home_verified']);self.assertEqual(d.calls,['create','metadata','identity']);self.assertEqual(self.saved[0]['tabs']['search']['session'],'lease-search');self.assertEqual(self.saved[0]['fork_home_verification'],self.b['fork_home_verification']);r.effective_roles(self.p,self.saved[0],self.home)
    def test_no_absence_proof_refuses_before_create(self):
        self.b['retired_targets']=[];d=Driver()
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])
    def test_wrong_absence_id_refuses(self):
        self.b['retired_targets'][0]['proof']['page']='F'*32;d=Driver()
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])
    def test_unknown_create_consumes_and_never_retries(self):
        d=Driver('unknown')
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,['create']);self.assertEqual(self.saved,[])
    def test_wrong_uid_no_adoption(self):
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',Driver('uid'))
        self.assertEqual(self.saved,[])
    def test_wrong_actual_page_no_adoption(self):
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',Driver('page'))
        self.assertEqual(self.saved,[])
    def test_grant_historical_projection_only_exact_role(self):
        r.recover(self.p,'child','search',Driver());b=self.saved[0]
        historical=r.grant_validation_binding(self.p,b)
        self.assertEqual(historical['tabs']['search']['page'],'A'*32)
        self.assertEqual(historical['tabs']['detail'],b['tabs']['detail'])
        self.assertEqual(b['tabs']['search']['page'],'C'*32)
    def test_changed_uid_or_grant_cannot_project(self):
        r.recover(self.p,'child','search',Driver());b=self.saved[0]
        b['account_uid']='wrong'
        with self.assertRaises(Exception):r.grant_validation_binding(self.p,b)
    def test_wrong_retired_session_cannot_create(self):
        self.b['retired_targets'][0]['tab']['session']='wrong';d=Driver()
        with self.assertRaises(Exception):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])
    def test_known_created_resume_never_creates_again(self):
        first=Driver('blank')
        with self.assertRaises(Exception):r.recover(self.p,'child','search',first)
        self.assertEqual(first.calls,['create','metadata']);self.assertEqual(self.saved,[])
        second=Driver();r.recover(self.p,'child','search',second,resume_known_created='C'*32)
        self.assertEqual(second.calls,['metadata','identity']);self.assertEqual(len(self.saved),1)
    def test_unknown_create_cannot_resume(self):
        with self.assertRaises(Exception):r.recover(self.p,'child','search',Driver('unknown'))
        d=Driver()
        with self.assertRaises(Exception):r.recover(self.p,'child','search',d,resume_known_created='C'*32)
        self.assertEqual(d.calls,[])
    def test_resume_wrong_created_id_refuses(self):
        with self.assertRaises(Exception):r.recover(self.p,'child','search',Driver('blank'))
        d=Driver()
        with self.assertRaises(Exception):r.recover(self.p,'child','search',d,resume_known_created='E'*32)
        self.assertEqual(d.calls,[])
    def test_resume_changed_beforebinding_refuses(self):
        with self.assertRaises(Exception):r.recover(self.p,'child','search',Driver('blank'))
        self.b['state']='changed';d=Driver()
        with self.assertRaises(Exception):r.recover(self.p,'child','search',d,resume_known_created='C'*32)
        self.assertEqual(d.calls,[])
    def test_tampered_proof_refused(self):
        r.recover(self.p,'child','search',Driver());b=self.saved[0];f=Path(b['fork_role_replacements']['search']['file']);f.write_text(f.read_text()+' ')
        with self.assertRaises(Exception):r.effective_roles(self.p,b,self.home)

    def retire_first_replacement(self):
        r.recover(self.p,'child','search',Driver())
        self.b=copy.deepcopy(self.saved[-1]);slot=self.b['tabs'].pop('search')
        self.b['retired_targets'].append({'role':'search','tab':slot,
            'proof':{'state':'registered_target_absent','page':slot['page']}})
        self.saved.clear()

    def test_second_generation_chains_tip_preserves_v1_and_projects_original_home(self):
        self.retire_first_replacement()
        previous=copy.deepcopy(self.b['fork_role_replacements']['search'])
        old_bytes=Path(previous['file']).read_bytes()
        # Missing real final slot is never acceptable to ordinary callers.
        with self.assertRaises(r.bridge.BridgeError):r.effective_roles(self.p,self.b,self.home)
        d=Driver(page='E'*32);out=r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,['create','metadata','identity'])
        self.assertTrue(out['home_verified']);b=self.saved[-1]
        pointer=b['fork_role_replacements']['search'];record=json.loads(Path(pointer['file']).read_text())
        self.assertEqual(record['kind'],'guarded_fork_role_replacement_v2')
        self.assertEqual(record['previous_replacement'],previous)
        self.assertEqual(Path(pointer['file']).name,'search-'+'C'*32+'.proof.json')
        self.assertEqual(record['retirement']['tab']['page'],'C'*32)
        self.assertEqual(Path(previous['file']).read_bytes(),old_bytes)
        self.assertEqual(r.effective_roles(self.p,b,self.home)['search']['page'],'E'*32)
        historical=r.grant_validation_binding(self.p,b)
        self.assertEqual(historical['tabs']['search']['page'],'A'*32)
        self.assertEqual(b['tabs']['search']['page'],'E'*32)
        # Validation only projected the missing tip in memory, never adopted it.
        self.assertNotIn('search',self.b['tabs'])

    def test_second_generation_missing_tip_retirement_refuses_before_create(self):
        self.retire_first_replacement();self.b['retired_targets'].pop();d=Driver(page='E'*32)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])

    def test_second_generation_wrong_or_duplicate_retired_tip_refuses(self):
        self.retire_first_replacement();tip=self.b['retired_targets'][-1];d=Driver(page='E'*32)
        tip['tab']['session']='other'
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        tip['tab']['session']='lease-search';self.b['retired_targets'].append(copy.deepcopy(tip))
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])

    def test_second_generation_unknown_create_is_never_repeated(self):
        self.retire_first_replacement();d=Driver('unknown',page='E'*32)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        with self.assertRaises(Exception):r.recover(self.p,'child','search',d,resume_known_created='E'*32)
        self.assertEqual(d.calls,['create']);self.assertEqual(self.saved,[])

    def test_second_generation_known_create_resume_keeps_previous_pointer(self):
        self.retire_first_replacement();previous=copy.deepcopy(self.b['fork_role_replacements']['search'])
        with self.assertRaises(Exception):r.recover(self.p,'child','search',Driver('blank',page='E'*32))
        d=Driver(page='E'*32);r.recover(self.p,'child','search',d,resume_known_created='E'*32)
        self.assertEqual(d.calls,['metadata','identity'])
        record=json.loads(Path(self.saved[-1]['fork_role_replacements']['search']['file']).read_text())
        self.assertEqual(record['previous_replacement'],previous)

    def test_second_generation_reusing_original_or_other_role_page_refuses_adoption(self):
        self.retire_first_replacement();d=Driver(page='A'*32)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(self.saved,[])

    def test_second_generation_current_stop_mismatch_refuses_before_create(self):
        self.retire_first_replacement();d=Driver(page='E'*32)
        with patch.object(current,'web_exceptions',return_value={'stop':'new-stop'}):
            with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])

    def test_second_generation_old_chain_uid_grant_and_session_changes_refuse(self):
        self.retire_first_replacement();original=copy.deepcopy(self.b)
        for key,value in [('account_uid','wrong'),('session','changed'),
                          ('current_page_revalidation',{'file':'wrong','sha256':'0'*64})]:
            self.b=copy.deepcopy(original);self.b[key]=value;d=Driver(page='E'*32)
            with self.assertRaises(Exception):r.recover(self.p,'child','search',d)
            self.assertEqual(d.calls,[])

    def test_cycle_detection_and_wrong_role_in_link(self):
        self.retire_first_replacement();r.recover(self.p,'child','search',Driver(page='E'*32));b=self.saved[-1]
        pointer=b['fork_role_replacements']['search'];record=json.loads(Path(pointer['file']).read_text())
        # Real SHA checks already reject rewritten bytes. Also exercise the
        # recursion guard independently of cryptographic fixed-point impossibility.
        record['previous_replacement']=copy.deepcopy(pointer)
        real_read=r.nr.read_json
        def cycle_read(path,sha=None):
            if str(path)==pointer['file']:return record,pointer['sha256']
            return real_read(path,sha)
        with patch.object(r.nr,'read_json',side_effect=cycle_read):
            with self.assertRaisesRegex(r.bridge.BridgeError,'FORK_REPLACEMENT_CHAIN_CYCLE'):
                r.effective_roles(self.p,b,self.home)
        record['role']='detail'
        with patch.object(r.nr,'read_json',side_effect=cycle_read):
            with self.assertRaises(r.bridge.BridgeError):r.effective_roles(self.p,b,self.home)

    def test_second_generation_old_proof_tamper_refuses(self):
        self.retire_first_replacement();previous=copy.deepcopy(self.b['fork_role_replacements']['search'])
        r.recover(self.p,'child','search',Driver(page='E'*32));b=self.saved[-1]
        f=Path(previous['file']);f.write_text(f.read_text()+' ')
        with self.assertRaises(r.bridge.BridgeError):r.effective_roles(self.p,b,self.home)

    def test_third_generation_resolves_complete_chain(self):
        self.retire_first_replacement();r.recover(self.p,'child','search',Driver(page='E'*32))
        self.b=copy.deepcopy(self.saved[-1]);slot=self.b['tabs'].pop('search')
        self.b['retired_targets'].append({'role':'search','tab':slot,
            'proof':{'state':'registered_target_absent','page':slot['page']}})
        self.saved.clear();r.recover(self.p,'child','search',Driver(page='F'*32))
        b=self.saved[-1];roles,chains=r._resolve_roles(self.p,b,self.home)
        self.assertEqual(roles['search']['page'],'F'*32);self.assertEqual(len(chains['search']),3)
        self.assertEqual(r.grant_validation_binding(self.p,b)['tabs']['search']['page'],'A'*32)

    def test_archived_before_binding_tamper_rejected(self):
        self.retire_first_replacement()
        pointer=self.b['fork_role_replacements']['search'];proof=json.loads(Path(pointer['file']).read_text())
        intent=json.loads(Path(proof['intent']['file']).read_text())
        f=Path(intent['before_binding']['file']);f.write_text(f.read_text()+' ');d=Driver(page='E'*32)
        with self.assertRaises(r.bridge.BridgeError):r.recover(self.p,'child','search',d)
        self.assertEqual(d.calls,[])

if __name__=='__main__':unittest.main()
