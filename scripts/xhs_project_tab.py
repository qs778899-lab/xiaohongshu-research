"""Prepare one new project home tab under a currently valid parent web lease.

Fresh manual-tab search, body and review attestation remain mandatory. Unknown
creation or inspection outcomes are never retried by changing project names.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

import xhs_bridge as bridge
import xhs_manual_tab_recovery as manual
import xhs_note_recovery as nr
import xhs_project as xp

HOME = 'https://www.xiaohongshu.com/explore'


def create_home(parent, meta):
    # Driver construction verifies the pinned runtime before this entry is used.
    runtime = parent['runtime']
    env = {k:v for k,v in os.environ.items() if not k.startswith('OPENCLI_')}
    env.update(OPENCLI_CONFIG_DIR=runtime['config_dir'], OPENCLI_CACHE_DIR=runtime['cache_dir'])
    cfg = {'packageRoot':runtime['package_root'], 'contextId':parent['context_id'],
           'extensionVersion':bridge.effective_extension_version(parent),
           'chromeTabId':parent['tabs']['detail']['chrome_tab_id'],
           'windowId':parent['tabs']['detail']['window_id'], 'expectedUrl':meta['url']}
    try:
        result = subprocess.run([runtime['node'],str(Path(__file__).with_suffix('.mjs'))],
            input=json.dumps(cfg),text=True,capture_output=True,env=env,timeout=45)
        packet=json.loads(result.stdout)
    except (subprocess.TimeoutExpired,ValueError,OSError):
        raise bridge.BridgeError('PROJECT_TAB_CREATE_OUTCOME_UNKNOWN') from None
    if not packet.get('ok'):
        raise bridge.BridgeError(packet.get('error','PROJECT_TAB_CREATE_OUTCOME_UNKNOWN'))
    return packet['data']


def completed_preparation(record, keys):
    """Home inspection is not enough: require the same fresh attested web grant."""
    done = record.with_suffix('.done.json')
    nr.require(done.exists(), 'PROJECT_TAB_PRIOR_OUTCOME_REQUIRES_DIAGNOSIS')
    result, _ = nr.read_json(done)
    inspected = manual.check_pointer(result['inspection'])
    grant_file = Path(result['inspection']['file']).parent / 'grant.json'
    nr.require(grant_file.exists(), 'PROJECT_TAB_FRESH_ATTESTATION_REQUIRED')
    grant, _ = nr.read_json(grant_file)
    b = bridge.load_binding(inspected['binding']['project'], grant['authority']['binding_name'])
    nr.require(b.get('manual_tab_revalidation') == manual.pointer(grant_file), 'PROJECT_TAB_GRANT_CHANGED')
    nr.require(b['tabs']['detail']['page'] == inspected['binding']['tabs']['detail']['page'],
               'PROJECT_TAB_ATTESTATION_TARGET_CHANGED')
    manual.web_exceptions(b['project'], b, keys)
    probe = manual.check_pointer(grant['probe'])
    search = manual.check_pointer(probe['inspection'])
    nr.require(search.get('initial_inspection') == result['inspection'], 'PROJECT_TAB_INSPECTION_CHANGED')


def prepare(project, parent_project, binding, authorization, creator=create_home):
    project=Path(project).resolve(); parent_project=Path(parent_project).resolve()
    xp.legacy.research_path(project)
    nr.require(project!=parent_project,'DISTINCT_PROJECT_REQUIRED')
    nr.require(isinstance(authorization,str) and authorization.strip(),'AUTHORIZATION_BASIS_REQUIRED')
    parent=bridge.load_binding(parent_project,binding)
    slot=parent.get('tabs',{}).get('detail',{})
    nr.require(type(slot.get('chrome_tab_id')) is int and type(slot.get('window_id')) is int,
               'REGISTERED_NATIVE_TAB_REQUIRED')
    # A new project is not an access-recovery mechanism. Parent authorization,
    # STOP bytes and live identity must all remain valid before creating a tab.
    with bridge.lease(parent_project,parent) as keys:
        stops=nr.current_stops(keys)
        root=xp.legacy.account_root()/'project-tab-preparations'
        root.mkdir(parents=True,exist_ok=True)
        key=xp.digest({'context':parent['context_id'],'project':str(project)})
        record=root/(key+'.json')
        nr.require(not record.exists(),'PROJECT_TAB_ALREADY_ATTEMPTED')
        for old in root.glob('*.json'):
            if old.name.endswith('.done.json'):continue
            value,_=nr.read_json(old)
            prior_parent = manual.check_pointer(value['parent'])
            if (value.get('context_id')==parent['context_id']
                    or prior_parent.get('account_uid')==parent['account_uid']):
                completed_preparation(old, keys)
        driver=bridge.Driver(parent['runtime'])
        try:
            meta=driver.call('metadata',parent,'detail')
            ident=driver.call('identity',parent,'detail')
            nr.require(meta.get('page')==slot['page'] and ident.get('page')==slot['page']
                       and ident.get('uid')==parent['account_uid'] and ident.get('url')==meta.get('url'),
                       'PROJECT_TAB_PARENT_CHANGED')
            bridge.safe_url(meta['url'])
            nr.require(nr.current_stops(keys)==stops,'PROJECT_TAB_STOP_CHANGED')
            folder=project/'bridge'/'project-tab-preparation';folder.mkdir(parents=True,exist_ok=True)
            intent={'context_id':parent['context_id'],'account_uid':parent['account_uid'],'project':str(project),'parent_project':str(parent_project),
                    'parent_binding':binding,'authorization':authorization,'at':xp.legacy.now(),
                    'stops':stops,'parent':manual.evidence(folder/'parent.json',parent),
                    'identity':manual.evidence(folder/'parent-identity.json',ident)}
            manual.evidence(record,intent)
            manual.evidence(folder/'intent.json',intent)
            created=creator(parent,meta)
            manual.evidence(folder/'created.json',created)
            nr.require(created.get('url')==HOME and created.get('active') is True
                       and type(created.get('tabId')) is int and created['tabId']!=slot['chrome_tab_id']
                       and created.get('windowId')==slot['window_id'],'PROJECT_TAB_CREATE_MISMATCH')
            nr.require(nr.current_stops(keys)==stops,'PROJECT_TAB_STOP_CHANGED')
            nr.require(bridge.load_binding(parent_project,binding)==parent,'PROJECT_TAB_PARENT_CHANGED')
            user={'user_statement':authorization,'processed_at':xp.legacy.now(),
                  'normal_other_posts_statement':'Tool verified the original public page identity before creating this home tab; new research access remains unverified.',
                  'statement_origin':'automated_observation_not_user_restoration_claim',
                  'preparation':manual.pointer(folder/'intent.json')}
            manual.evidence(folder/'request.json',user)
        except bridge.BridgeError as exc:
            if exc.code == 'ACCESS_STOP':
                bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
            raise
        finally:driver.close()
    # inspect obtains its own account lock and does not inherit parent access.
    try:
        result=manual.inspect(project,binding,folder/'created.json',folder/'request.json',parent_project)
    except bridge.BridgeError as exc:
        if exc.code == 'ACCESS_STOP':
            with nr.locked_stops(parent['profile_guard_key'], parent['account_uid']) as keys:
                bridge.identity.stop_all(keys, 'Observed browser login/verification/access restriction')
        raise
    manual.evidence(record.with_suffix('.done.json'),{'inspection':manual.pointer(result['inspection']),
                    'collection_permitted':False,'at':xp.legacy.now()})
    return dict(result,new_project_tab=True,fresh_search_and_body_required=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,required=True);p.add_argument('--parent-project',type=Path,required=True)
    p.add_argument('--binding',required=True);p.add_argument('--authorization',required=True)
    a=p.parse_args()
    print(json.dumps(prepare(a.project,a.parent_project,a.binding,a.authorization),ensure_ascii=False))

if __name__=='__main__':main()
