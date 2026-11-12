#!/usr/bin/env python3
"""Explicit-profile, explicit-tab XHS bridge. Never clears STOP or copies login data."""
from __future__ import annotations
from xhs_extension_compatibility import effective_extension_version
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import queue
import threading
import atexit
import time
import uuid
import sqlite3
from datetime import datetime
from urllib.parse import urlparse, parse_qs, quote, urljoin

import xhs_research as legacy
import xhs_native as native
import xhs_project as xp
import xhs_account_identity as identity

VERSION = 'xhs-bridge-v1'
STAGES = {'search', 'body', 'roots', 'replies', 'author', 'images', 'video'}


class BridgeError(legacy.ResearchError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def classify_note_error(url,navigation_intent):
    """Conservative offline diagnosis only; it never grants access or changes STOP."""
    blocked={'classification':'ACCESS_STOP','collection_permitted':False,'retry_allowed':False,
             'identity_verified':False,'fresh_identity_verification_required_before_recovery':True,
             'stop_changed':False}
    def reject(reason):return dict(blocked,reason=reason)
    if not isinstance(navigation_intent,dict) or not navigation_intent.get('at'):
        return reject('current_navigation_intent_required')
    def note_id(value):
        safe_url(value)
        path=urlparse(value).path.rstrip('/')
        match=re.fullmatch(r'/(?:explore|search_result)/([a-f0-9]{24})|/user/profile/[a-f0-9]{24}/([a-f0-9]{24})',path)
        return next((g for g in match.groups() if g),None) if match else None
    try:
        expected=note_id(navigation_intent.get('url'))
        if not expected:return reject('navigation_intent_is_not_an_exact_note')
        parsed=urlparse(url)
        if (parsed.scheme!='https' or parsed.netloc!='www.xiaohongshu.com'
                or parsed.path!='/404' or parsed.fragment):return reject('not_exact_public_note_error_page')
        query=parse_qs(parsed.query,keep_blank_values=True)
        if set(query)-{'source','error_code','error_msg','verifyMsg','uuid'} or any(len(v)!=1 for v in query.values()):
            return reject('ambiguous_or_unknown_error_parameters')
        if query.get('error_code')!=['300031'] or query.get('error_msg')!=['当前笔记暂时无法浏览']:
            return reject('not_the_exact_unavailable_note_message')
        if query.get('verifyMsg', [''])!=['']:return reject('verification_message_present')
        sources=query.get('source',[])
        if len(sources)!=1:return reject('one_nested_source_required')
        source=urlparse(sources[0])
        if source.scheme or source.netloc or source.fragment or not re.fullmatch(r'/404/sec_[A-Za-z0-9_-]+',source.path):
            return reject('unrecognized_nested_error_source')
        nested=parse_qs(source.query,keep_blank_values=True)
        if set(nested)!={'redirectPath'} or len(nested['redirectPath'])!=1:
            return reject('one_nested_redirect_required')
        actual=note_id(nested['redirectPath'][0])
        if actual!=expected:return reject('redirect_does_not_match_current_note_intent')
    except (BridgeError,ValueError,TypeError,AttributeError):return reject('invalid_or_unsafe_error_or_intent_url')
    return dict(blocked,classification='NOTE_UNAVAILABLE',note_id=expected,
                reason='exact_unavailable_note_error_bound_to_current_navigation_intent',
                message='当前原帖无法浏览；此诊断不能证明账号登录失效，也不解除采集阻断。')


def diagnose_error(project,name,role,source_file):
    """Read an existing exact-tab metadata file; no browser, account lock or mutation."""
    source=Path(source_file)
    if not source.is_absolute() or not source.is_file() or source.is_symlink():
        raise BridgeError('EXISTING_METADATA_JSON_REQUIRED')
    raw=source.read_bytes()
    try:meta=json.loads(raw)
    except (ValueError,UnicodeError):raise BridgeError('VALID_METADATA_JSON_REQUIRED')
    b=load_binding(project,name);slot=b.get('tabs',{}).get(role,{})
    intent=slot.get('navigation_intent')
    diagnostic=classify_note_error(meta.get('url') if isinstance(meta,dict) else None,intent)
    if not isinstance(meta,dict) or not slot.get('page') or meta.get('page')!=slot['page']:
        diagnostic.update(classification='ACCESS_STOP',reason='metadata_does_not_match_exact_registered_tab')
        diagnostic.pop('note_id',None);diagnostic.pop('message',None)
    return dict(diagnostic,source_file=str(source),source_sha256=hashlib.sha256(raw).hexdigest(),
                binding_name=name,role=role,metadata_is_existing_observation_not_new_live_check=True,
                binding_state_unchanged=True,
                next='Keep all existing stops. Any later recovery must actually reverify the account on an authorized normal public page; no navigation is authorized by this diagnostic.')


def digest_tree(root):
    root = Path(root)
    entries = [(str(f.relative_to(root)), hashlib.sha256(f.read_bytes()).hexdigest())
               for base in ('dist', 'clis') for f in sorted((root/base).rglob('*')) if f.is_file()]
    entries.append(('package.json', hashlib.sha256((root/'package.json').read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def safe_url(url):
    try:
        p = urlparse(url)
        valid = p.scheme == 'https' and p.hostname == 'www.xiaohongshu.com' and not p.port and not p.username and not p.password
        if valid and (p.path.startswith('/website-login/') or p.path.rstrip('/')=='/login' or 'error_code' in parse_qs(p.query)):
            raise BridgeError('ACCESS_STOP')
        valid = valid and re.fullmatch(r'/(?:explore|search_result)(?:/[a-f0-9]{24})?/?|/user/profile/[a-f0-9]{24}(?:/[a-f0-9]{24})?/?', p.path)
    except (ValueError, TypeError):
        valid = False
    if not valid: raise BridgeError('SCOPE_CHANGED')
    if 'error_code' in parse_qs(p.query): raise BridgeError('ACCESS_STOP')
    return url


class Driver:
    def __init__(self, runtime):
        self.runtime = dict(runtime)
        if digest_tree(runtime['package_root']) != runtime['package_tree_sha256']:
            raise BridgeError('RUNTIME_CHANGED_RECHECK_REQUIRED')
        if hashlib.sha256(Path(runtime['node']).read_bytes()).hexdigest() != runtime['node_sha256']:
            raise BridgeError('NODE_CHANGED_RECHECK_REQUIRED')
        self.process = None
        self.responses = queue.Queue()
        self.sequence = 0
        self.metrics = {'calls': 0, 'process_starts': 0, 'elapsed_seconds': 0}
        atexit.register(self.close)

    def close(self):
        process, self.process = self.process, None
        if process:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            for stream in (process.stdin, process.stdout):
                if stream: stream.close()

    def exchange(self, cfg, env):
        if self.process is None:
            self.responses = queue.Queue()
            self.process = subprocess.Popen([self.runtime['node'],str(Path(__file__).with_name('xhs_bridge_driver.mjs')),'--stream'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,env=env)
            self.metrics['process_starts'] += 1
            def read_lines(process, responses):
                try:
                    for line in process.stdout: responses.put(line)
                finally: responses.put(None)
            threading.Thread(target=read_lines,args=(self.process,self.responses),daemon=True).start()
        self.sequence += 1
        cfg = dict(cfg,requestId=self.sequence)
        try:
            self.process.stdin.write(json.dumps(cfg)+'\n'); self.process.stdin.flush()
            line=self.responses.get(timeout=50)
            if line is None: raise BridgeError('COMMAND_OUTCOME_UNKNOWN')
            packet=json.loads(line)
            if packet.get('requestId') != self.sequence: raise BridgeError('COMMAND_OUTCOME_UNKNOWN')
            return packet
        except (queue.Empty,BrokenPipeError,OSError,ValueError):
            self.close()
            raise BridgeError('COMMAND_OUTCOME_UNKNOWN') from None

    def call(self, action, binding=None, role=None, **fields):
        cfg = {'action': action, 'packageRoot': self.runtime['package_root'], **fields}
        if binding:
            cfg.update(contextId=binding['context_id'], session=binding['session'], uid=binding['account_uid'])
            if binding.get('transport_surface') == 'browser':
                cfg['surface'] = 'browser'
            cfg['extensionVersion'] = effective_extension_version(binding)
            if role:
                slot=binding['tabs'][role]
                if not slot.get('session'):raise BridgeError('ROLE_SESSION_REQUIRED')
                cfg['session']=slot['session']
                if slot.get('page'):cfg['tab']=slot['page']
        env = dict(os.environ)
        # Remove user defaults and alternate transports; never repurpose HOME.
        for key in list(env):
            if key.startswith('OPENCLI_'): env.pop(key)
        env.update(OPENCLI_CONFIG_DIR=self.runtime['config_dir'], OPENCLI_CACHE_DIR=self.runtime['cache_dir'],
                   OPENCLI_BROWSER_CONNECT_TIMEOUT='10', OPENCLI_BROWSER_COMMAND_TIMEOUT='30')
        started=time.monotonic()
        try: packet=self.exchange(cfg,env)
        finally:
            self.metrics['calls']+=1
            self.metrics['elapsed_seconds']+=time.monotonic()-started
        if not packet.get('ok'):
            if binding and binding.get('project'):
                legacy.write_json(Path(binding['project'])/'bridge'/'last-driver-error.json',{'action':action,'role':role,'error':packet.get('error',{}),'at':legacy.now()})
            raise BridgeError(packet.get('error', {}).get('code', 'CONNECTOR_ERROR'))
        data = packet.get('data')
        if isinstance(data, dict) and data.get('error'): raise BridgeError(data['error'])
        return data


def connected_profiles(driver):
    status = driver.call('profiles')
    if not status.get('daemonRunning'):
        raise BridgeError('CONNECTOR_OFFLINE')
    return status.get('profiles', [])


def binding_path(project, name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', name): raise BridgeError('INVALID_BINDING_NAME')
    path = Path(project)/'bridge'/'bindings'/(name+'.json')
    if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)):
        raise BridgeError('UNSAFE_BINDING_PATH')
    return path


def load_binding(project, name):
    path = binding_path(project, name)
    b = legacy.read_json(path, {})
    if b.get('format') != VERSION or b.get('project') != str(Path(project).resolve()):
        raise BridgeError('BINDING_NOT_FOUND')
    if b.get('manual_single_tab'):
        slots = b.get('tabs', {})
        if set(slots) != {'search', 'detail'} or slots['search'] != slots['detail']:
            raise BridgeError('MANUAL_SHARED_TAB_STATE_DIVERGED')
        b['tabs']['search'] = b['tabs']['detail']
    return b


def save_binding(project, name, b):
    legacy.write_json(binding_path(project, name), b)


def assert_web_project_access(project, account_uid):
    # Rendered public web only: never change the raw/API STOP or other channels.
    from xhs_native_recovery import native_project_stop_applies, valid_rows
    db = native.open_lookup_project(Path(project))
    try:
        stop = db.get_meta('stop')
        if native_project_stop_applies(db) or native.channel_health(db).get('access', {}).get('hold'):
            raise legacy.StopRun('Project web access remains stopped')
        if stop and not any(r.get('project_stop_digest') == xp.digest(stop)
                            and r.get('account_uid') == account_uid
                            and r.get('original_account_uid') == account_uid for _, r in valid_rows(db)):
            raise legacy.StopRun('Project web recovery account does not match binding')
    finally:
        db.close()


def attach_web_authorization(project, b, authorization_path):
    # This is an explicit reference to existing user consent, not an inferred grant.
    path = Path(authorization_path)
    if not path.is_file() or path.is_symlink(): raise BridgeError('WEB_AUTHORIZATION_INVALID_FILE')
    raw = path.read_bytes()
    try: value = json.loads(raw)
    except ValueError: raise BridgeError('WEB_AUTHORIZATION_INVALID_FILE') from None
    if (value.get('scope') != 'rendered_public_web_only' or value.get('approved') is not True
            or value.get('context_id') != b['context_id'] or value.get('account_uid') != b['account_uid']
            or not value.get('user_text') or not value.get('recorded_at')
            or not isinstance(value.get('preserved_media_stops'), dict)):
        raise BridgeError('WEB_AUTHORIZATION_MISMATCH')
    target = Path(project)/'bridge'/'web-channel-authorization.json'
    if target.is_symlink() or (target.exists() and target.read_bytes() != raw):
        raise BridgeError('WEB_AUTHORIZATION_CHANGED')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists(): target.write_bytes(raw)
    b['web_channel_authorization'] = hashlib.sha256(raw).hexdigest()


def web_authorization_exceptions(project,b):
    exceptions={}
    grant=b.get('web_channel_authorization')
    if grant:
        path=Path(project)/'bridge'/'web-channel-authorization.json'
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=grant:raise BridgeError('WEB_AUTHORIZATION_CHANGED')
        value=legacy.read_json(path,{})
        if value.get('scope')!='rendered_public_web_only' or value.get('context_id')!=b['context_id'] or value.get('account_uid')!=b['account_uid'] or value.get('approved') is not True:
            raise BridgeError('WEB_AUTHORIZATION_MISMATCH')
        exceptions=value.get('preserved_media_stops',{})
    return exceptions


@contextmanager
def lease(project, b):
    project = Path(project)
    assert_web_project_access(project, b['account_uid'])
    exceptions=web_authorization_exceptions(project,b)
    from xhs_note_recovery import FORK_KIND,fork_path,fork_projection
    is_fork=bool(b.get('web_parent_authorization') or b.get('binding_kind')==FORK_KIND
                 or fork_path(project,'names',b['binding_name']).exists())
    note_recovery={'project':str(project),'binding':b['binding_name']} if b.get('note_unavailable_recovery') or is_fork or b.get('manual_tab_revalidation') else None
    with identity.account_lease(b['profile_guard_key'], b['account_uid'],web_stop_exceptions=exceptions,web_note_recovery=note_recovery) as keys:
        if (project/'bridge'/'PAUSE').exists(): raise BridgeError('USER_PAUSED')
        if is_fork:
            current=load_binding(project,b['binding_name'])
            if (fork_projection(b)!=fork_projection(current) or b.get('web_parent_authorization')!=current.get('web_parent_authorization')
                    or b.get('fork_home_verification')!=current.get('fork_home_verification')
                    or {r:(s.get('page'),s.get('session'),s.get('owned')) for r,s in b.get('tabs',{}).items()}
                       !={r:(s.get('page'),s.get('session'),s.get('owned')) for r,s in current.get('tabs',{}).items()}):
                raise BridgeError('FORK_IN_MEMORY_AUTHORITY_CHANGED')
        if b.get('fork_role_replacements'):
            from xhs_note_recovery import read_json
            from xhs_fork_role_recovery import effective_roles
            pointer=b['fork_home_verification'];home,_=read_json(pointer['file'],pointer['sha256'])
            effective_roles(project,b,home)
        yield keys


def save_failure(project, name, b, exc, keys=None):
    code = getattr(exc, 'code', 'SHARED_STOP' if isinstance(exc, legacy.StopRun) else 'CONNECTOR_ERROR')
    b['state'] = 'blocked'; b['last_error'] = {'code': code, 'at': legacy.now()}
    save_binding(project, name, b)
    if code == 'ACCESS_STOP' and keys:
        identity.stop_all(keys, 'Observed browser login/verification/access restriction')


def fork_binding(project,parent_name,name,intent,authorization,driver=None):
    """Create independent home tabs; delegate only a currently valid typed web grant.

    Existing parent tabs are never read, selected, navigated, or closed. An intent
    is consumed before create; unknown outcomes cannot be retried under a new name.
    """
    import xhs_note_recovery as nr
    project=Path(project).resolve();binding_path(project,name);binding_path(project,parent_name)
    if name==parent_name:raise BridgeError('FORK_DISTINCT_BINDING_REQUIRED')
    if not isinstance(authorization,str) or not authorization.strip():raise BridgeError('AUTHORIZATION_BASIS_REQUIRED')
    record_file=nr.fork_path(project,'intents',intent);name_file=nr.fork_path(project,'names',name)
    with xp.project_lock(project):
        if record_file.exists():
            prior,sha=nr.read_json(record_file)
            if prior.get('parent_name')!=parent_name or prior.get('child_name')!=name or prior.get('authorization')!=authorization:
                raise BridgeError('FORK_INTENT_ALREADY_CONSUMED')
            b=load_binding(project,name)
            if b.get('state')=='ready':
                with lease(project,b):pass
                return {'binding_name':name,'state':'ready','duplicate':True,'scope':'rendered_public_web_only','tabs':b['tabs']}
            # A known owned page can finish initial guards. An uncertain create
            # or a later operational/page change cannot use this bootstrap path.
            slots=b.get('tabs',{})
            if (b.get('last_error',{}).get('code') not in {None,'SCOPE_CHANGED','CONTENT_LOADING','IDENTITY_UNCONFIRMED'}
                    or b.get('fork_home_verification') or record_file.with_suffix('.home.json').exists()
                    or record_file.with_suffix('.ready.json').exists() or set(slots) not in ({'search'},{'search','detail'})):
                raise BridgeError('FORK_OUTCOME_REQUIRES_DIAGNOSIS')
            for role,slot in slots.items():
                if (slot.get('owned') is not True or slot.get('state')!='created'
                        or not re.fullmatch(r'[a-fA-F0-9]{32}',slot.get('page','')) or slot.get('navigation_intent')
                        or slot.get('last_url')!='https://www.xiaohongshu.com/explore'
                        or slot.get('session')!=b.get('session','')+'-'+role):
                    raise BridgeError('FORK_OUTCOME_REQUIRES_DIAGNOSIS')
            baseline=hashlib.sha256(binding_path(project,name).read_bytes()).hexdigest()
            parent=load_binding(project,parent_name)
            with lease(project,parent) as keys:
                if hashlib.sha256(binding_path(project,name).read_bytes()).hexdigest()!=baseline:
                    raise BridgeError('FORK_BOOTSTRAP_BINDING_CHANGED')
                p=native.open_lookup_project(project)
                try:
                    nr.fork_authority(p,b,keys,home_required=False)
                    _finish_binding_fork(p,b,prior,sha,keys,driver,resuming=True)
                finally:p.close()
            return {'binding_name':name,'state':'ready','duplicate':False,'resumed_initialization':True,
                    'scope':'rendered_public_web_only','tabs':b['tabs'],'parent_binding_unchanged':True,
                    'stop_bytes_unchanged':True,'body_validation_still_required':True}
        if binding_path(project,name).exists() or name_file.exists():raise BridgeError('FORK_BINDING_ALREADY_EXISTS')
        parent=load_binding(project,parent_name)
        if (parent.get('web_parent_authorization') or parent.get('binding_kind')==nr.FORK_KIND
                or nr.fork_path(project,'names',parent_name).exists()):raise BridgeError('FORK_RECURSION_FORBIDDEN')
        if not parent.get('note_unavailable_recovery'):raise BridgeError('FORK_DIRECT_TYPED_PARENT_REQUIRED')
        # A different caller-supplied intent must not hide an unknown create.
        for prior_file in record_file.parent.glob('*.json'):
            if prior_file.name.endswith(('.home.json','.ready.json')):continue
            prior,prior_sha=nr.read_json(prior_file)
            if prior.get('parent_name')==parent_name and prior.get('parent_grant')==parent['note_unavailable_recovery']:
                try:nr.completed_fork_home(project,prior_file,prior,prior_sha)
                except (legacy.ResearchError,OSError,KeyError,TypeError):
                    raise BridgeError('FORK_PRIOR_OUTCOME_REQUIRES_DIAGNOSIS') from None
        with lease(project,parent) as keys:
            fresh=load_binding(project,parent_name)
            if nr.fork_projection(fresh)!=nr.fork_projection(parent) or fresh.get('note_unavailable_recovery')!=parent.get('note_unavailable_recovery'):
                raise BridgeError('FORK_PARENT_AUTHORITY_CHANGED')
            p=native.open_lookup_project(project)
            try:
                stop_hashes=nr.direct_web_exceptions(p,fresh,keys)
                session='xhs-'+uuid.uuid4().hex
                b={k:fresh[k] for k in ('format','project','profile_label','context_id','account_uid','profile_guard_key','runtime','extension_version')}
                b.update(binding_name=name,session=session,authorization=authorization,state='provisioning',tabs={},
                         binding_kind=nr.FORK_KIND,created_at=legacy.now())
                if fresh.get('web_channel_authorization'):b['web_channel_authorization']=fresh['web_channel_authorization']
                record={'kind':nr.FORK_KIND,'scope':'rendered_public_web_only','project':str(project),'intent_id':intent,
                        'parent_name':parent_name,'child_name':name,'authorization':authorization,'created_at':b['created_at'],
                        'parent_projection':nr.fork_projection(fresh),'child_projection':nr.fork_projection(b),
                        'parent_grant':fresh['note_unavailable_recovery'],'stop_hashes':stop_hashes,
                        'parent_binding_sha256_at_intent':hashlib.sha256(binding_path(project,parent_name).read_bytes()).hexdigest()}
                sha=nr.immutable(record_file,record);pointer={'file':str(record_file),'sha256':sha}
                nr.immutable(name_file,pointer);b['web_parent_authorization']=pointer;save_binding(project,name,b)
                _finish_binding_fork(p,b,record,sha,keys,driver)
            finally:p.close()
    return {'binding_name':name,'state':'ready','duplicate':False,'scope':'rendered_public_web_only',
            'tabs':b['tabs'],'parent_binding_unchanged':True,'stop_bytes_unchanged':True,'body_validation_still_required':True}


def _finish_binding_fork(p,b,record,sha,keys,driver=None,resuming=False):
    """Guarded initial home setup; never recreates an already journaled role."""
    import xhs_note_recovery as nr
    project=p.path;name=b['binding_name'];session=b['session'];owned_driver=driver is None
    record_file=nr.fork_path(project,'intents',record['intent_id'])
    pointer={'file':str(record_file),'sha256':sha}
    journal=record_file.parent.parent/'initialization'/record['intent_id']
    if journal.is_symlink() or journal.parent.is_symlink():raise BridgeError('FORK_UNSAFE_RECORD_PATH')
    existing_roles=set(b.get('tabs',{})) if resuming else set()
    def role_record(role,page,method):
        slot=b['tabs'][role];path=journal/(role+'-target.json')
        value={'authorization':pointer,'role':role,'page':page,'session':slot['session'],
               'home_url':'https://www.xiaohongshu.com/explore'}
        if path.exists():
            prior,_=nr.read_json(path)
            if any(prior.get(k)!=v for k,v in value.items()):raise BridgeError('FORK_BOOTSTRAP_TARGET_CHANGED')
        else:
            nr.immutable(path,dict(value,method=method,at=legacy.now(),
                binding_sha256=hashlib.sha256(binding_path(project,name).read_bytes()).hexdigest()))
    def observation(action,role,result=None,exc=None):
        at=legacy.now();path=journal/('observation-'+uuid.uuid4().hex+'.json')
        value={'authorization':pointer,'action':action,'role':role,'at':at,
               'expected_page':b['tabs'].get(role,{}).get('page'),'expected_home_url':'https://www.xiaohongshu.com/explore'}
        if exc is None:value['result']=result
        else:value['error']={'code':getattr(exc,'code',type(exc).__name__)}
        digest=nr.immutable(path,value)
        b.setdefault('initialization_observations',[]).append({'file':str(path),'sha256':digest,'action':action,'role':role,'at':at})
        save_binding(project,name,b)
    try:
        taken_pages=set();taken_sessions=set()
        # Registration metadata only, never a browser-wide inventory.
        for path in (project/'bridge'/'bindings').glob('*.json'):
            if path==binding_path(project,name):continue
            old,_=nr.read_json(path)
            slots=list(old.get('tabs',{}).values())+[x.get('tab',{}) for x in old.get('quarantined_tabs',[])]
            taken_pages.update(x.get('page').lower() for x in slots if isinstance(x.get('page'),str))
            taken_sessions.update(x.get('session') for x in slots if x.get('session'))
            if old.get('session'):taken_sessions.add(old['session'])
        if session in taken_sessions:raise BridgeError('FORK_SESSION_COLLISION')
        # Freeze known bootstrap pages before reading them. Never adopt a page
        # whose role/page/session was subsequently changed by another caller.
        for role in existing_roles:
            slot=b['tabs'][role]
            if slot['page'].lower() in taken_pages or slot['session'] in taken_sessions:
                raise BridgeError('FORK_TARGET_OR_SESSION_REUSED')
            role_record(role,slot['page'],'existing_owned_created_journal')
        driver=driver or Driver(b['runtime'])
        profiles=connected_profiles(driver)
        connected=[x for x in profiles if x.get('contextId')==b['context_id']]
        if len(connected)!=1:raise BridgeError('SPECIFIED_PROFILE_OFFLINE')
        if connected[0].get('extensionVersion')!=effective_extension_version(b):raise BridgeError('EXTENSION_CHANGED_RECHECK_REQUIRED')
        class NewHomeDriver:
            def call(self,action,binding=None,role=None,**fields):
                try:result=driver.call(action,binding,role,**fields)
                except Exception as exc:
                    if action in {'metadata','identity','create'}:observation(action,role,exc=exc)
                    raise
                if action in {'metadata','identity','create'}:observation(action,role,result)
                if action=='create':
                    page=result.get('page','');slot=binding['tabs'][role]
                    if (not isinstance(page,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',page)
                            or page.lower() in taken_pages or slot['session'] in taken_sessions):
                        slot['rejected_creation_observation']={'page':page,'at':legacy.now()};save_binding(project,name,binding)
                        raise BridgeError('FORK_TARGET_OR_SESSION_REUSED')
                    role_record(role,page,'guarded_create_result')
                if role in existing_roles and action in {'metadata','identity'}:
                    # This is only a same-home initialization resume, not an
                    # authorization to navigate or adopt a different page.
                    slot=binding['tabs'][role]
                    safe_url(result.get('url'))
                    if result.get('page')!=slot['page'] or urlparse(result['url']).path.rstrip('/')!='/explore':
                        raise BridgeError('FORK_BOOTSTRAP_TARGET_CHANGED')
                    if action=='identity' and result.get('uid')!=binding['account_uid']:raise BridgeError('ACCOUNT_MISMATCH')
                return result
        home_driver=NewHomeDriver();roles={}
        for role in ('search','detail'):
            if (project/'bridge'/'PAUSE').exists():raise BridgeError('USER_PAUSED')
            nr.fork_authority(p,b,keys,home_required=False)
            meta=provision_role(project,name,b,home_driver,role);slot=b['tabs'][role]
            if slot['page'].lower() in taken_pages or slot['session'] in taken_sessions:raise BridgeError('FORK_TARGET_OR_SESSION_REUSED')
            actual=home_driver.call('identity',b,role)
            for obs in (meta,actual):
                safe_url(obs.get('url'))
                if obs.get('page')!=slot['page'] or urlparse(obs['url']).path.rstrip('/')!='/explore' or obs.get('error'):
                    raise BridgeError('FORK_ACTUAL_HOME_TARGET_REQUIRED')
            if actual.get('uid')!=b['account_uid']:raise BridgeError('ACCOUNT_MISMATCH')
            roles[role]={'page':slot['page'],'session':slot['session'],'metadata':meta,'identity':actual}
            taken_pages.add(slot['page'].lower());taken_sessions.add(slot['session']);save_binding(project,name,b)
        nr.fork_authority(p,b,keys,home_required=False)
        home={'kind':'guarded_new_binding_home_identity_v1','authorization':pointer,'binding_name':name,
              'context_id':b['context_id'],'account_uid':b['account_uid'],'session':session,'observed_at':legacy.now(),'roles':roles}
        home_file=record_file.with_suffix('.home.json');home_sha=nr.immutable(home_file,home)
        b['fork_home_verification']={'file':str(home_file),'sha256':home_sha}
        nr.immutable(record_file.with_suffix('.ready.json'),{'authorization':pointer,'home':b['fork_home_verification']})
        nr.fork_authority(p,b,keys)
        b['state']='ready'
        if b.get('last_error'):b.setdefault('resolved_initialization_errors',[]).append(b.pop('last_error'))
        save_binding(project,name,b)
    except Exception as exc:
        if b.get('last_error'):
            prior=dict(b['last_error']);history=b.setdefault('initialization_failures',[])
            if prior not in history:history.append(prior)
        save_failure(project,name,b,exc,keys);raise
    finally:
        if owned_driver and driver is not None:driver.close()


def bind(project, name, runtime, context_id, profile_label, account_uid, profile_guard_key, authorization, web_authorization=None):
    legacy.research_path(project)
    existed=binding_path(project,name).exists()
    try:
        return _bind(project,name,runtime,context_id,profile_label,account_uid,profile_guard_key,authorization,web_authorization)
    except legacy.ResearchError as exc:
        # A preflight STOP can occur before any tab is created. Persist that
        # specific failure instead of leaving a misleading provisioning state.
        if not existed and binding_path(project,name).exists():
            value=load_binding(project,name);save_failure(project,name,value,exc)
        raise


def _bind(project, name, runtime, context_id, profile_label, account_uid, profile_guard_key, authorization, web_authorization=None):
    identity.uid_key(account_uid)
    if binding_path(project, name).exists(): raise BridgeError('BINDING_ALREADY_EXISTS_USE_RECOVER')
    if not re.fullmatch(r'[\w-]{1,128}', context_id): raise BridgeError('INVALID_CONTEXT_ID')
    if not authorization.strip(): raise BridgeError('AUTHORIZATION_BASIS_REQUIRED')
    driver = Driver(runtime)
    driver.call('doctor')
    profiles = connected_profiles(driver)
    connected = [p for p in profiles if p['contextId'] == context_id]
    if len(connected) != 1: raise BridgeError('SPECIFIED_PROFILE_OFFLINE')
    if connected[0].get('extensionVersion') != '1.0.26': raise BridgeError('EXTENSION_CHANGED_RECHECK_REQUIRED')
    identity.associate(account_uid, profile_guard_key)
    b = {'format': VERSION, 'binding_name':name, 'project': str(Path(project).resolve()), 'profile_label': profile_label,
         'context_id': context_id, 'account_uid': account_uid, 'profile_guard_key': profile_guard_key,
         'session': 'xhs-'+uuid.uuid4().hex, 'runtime': runtime, 'extension_version':'1.0.26',
         'tabs': {}, 'authorization': authorization, 'state':'provisioning', 'created_at':legacy.now()}
    if web_authorization is not None:
        attach_web_authorization(project, b, web_authorization)
    # Journal each tab immediately; uncertain creation is never blindly retried.
    save_binding(project,name,b)
    with lease(project,b) as keys:
        try:
            for role in ('search','detail'):
                provision_role(project,name,b,driver,role)
            b['state']='ready';save_binding(project,name,b)
        except Exception as exc:
            save_failure(project,name,b,exc,keys);raise
    return {k:b[k] for k in ('profile_label','context_id','session','state','tabs')}


def provision_role(project,name,b,driver,role):
    if b.get('web_parent_authorization') and b.get('fork_home_verification'):
        # A replacement is not covered by a different page's home UID evidence.
        from xhs_note_recovery import read_json
        pointer=b['fork_home_verification'];home,_=read_json(pointer.get('file'),pointer.get('sha256'))
        from xhs_fork_role_recovery import effective_roles
        slot=b.get('tabs',{}).get(role,{})
        if not slot.get('page'):
            raise BridgeError('FORK_HOME_REVERIFICATION_REQUIRED')
        proof=effective_roles(project,b,home).get(role,{})
        if not proof or slot.get('page')!=proof.get('page') or slot.get('session')!=proof.get('session'):
            raise BridgeError('FORK_HOME_REVERIFICATION_REQUIRED')
    if role in b['tabs']:
        # A pending creation may already have happened. Never repeat it.
        if not b['tabs'][role].get('page'):raise BridgeError('TAB_CREATE_OUTCOME_UNKNOWN')
    else:
        # Missing role means no creation intention was ever journaled. This is
        # safe to resume only inside the caller's verified profile/account lease.
        b['tabs'][role]={'session':b['session']+'-'+role,'owned':False,'state':'creation_pending'}
        save_binding(project,name,b)
        tab=driver.call('create',b,role)
        if any(t.get('page')==tab['page'] for t in b['tabs'].values()):raise BridgeError('DUPLICATE_OWNED_TARGET')
        b['tabs'][role].update(page=tab['page'],owned=True,state='created',last_url='https://www.xiaohongshu.com/explore')
        save_binding(project,name,b)
    slot=b['tabs'][role]
    def initializing():
        return (slot.get('owned') is True and slot.get('state')=='created'
                and not slot.get('verified_at') and not slot.get('navigation_intent')
                and slot.get('last_url')=='https://www.xiaohongshu.com/explore')
    if not initializing():return verify_tab(driver,b,role)
    for attempt in range(4):
        try:return verify_tab(driver,b,role)
        except BridgeError as exc:
            if not initializing() or attempt==3 or exc.code not in {'IDENTITY_UNCONFIRMED','CONTENT_LOADING','SCOPE_CHANGED'}:raise
            if exc.code=='SCOPE_CHANGED':
                # Only a never-verified owned tab may be in its initial page
                # transition. Check this exact ID without reading page content.
                meta=driver.call('metadata',b,role)
                if meta.get('page')!=slot['page'] or meta.get('url') not in {
                    'about:blank','https://www.xiaohongshu.com','https://www.xiaohongshu.com/',
                    'https://www.xiaohongshu.com/explore'}:raise
            time.sleep(1)


def target_path(path):
    return re.sub(r'^/user/profile/[a-f0-9]{24}/([a-f0-9]{24})$',r'/explore/\1',re.sub(r'^/search_result/([a-f0-9]{24})$',r'/explore/\1',path.rstrip('/')))


def verify_tab(driver,b,role):
    if role not in b['tabs'] or not b['tabs'][role].get('page'): raise BridgeError('PROVISIONING_INCOMPLETE')
    meta=driver.call('metadata',b,role)
    if meta['page'] != b['tabs'][role]['page']: raise BridgeError('TARGET_MISMATCH')
    slot=b['tabs'][role]
    if meta['url'] in {'about:blank','https://www.xiaohongshu.com/','https://www.xiaohongshu.com'} and slot.get('owned') and slot.get('state')=='created' and slot.get('last_url')=='https://www.xiaohongshu.com/explore' and not slot.get('verified_at'):
        raise BridgeError('CONTENT_LOADING')
    safe_url(meta['url'])
    def target(value):
        parsed=urlparse(value)
        return (target_path(parsed.path),parse_qs(parsed.query).get('keyword') if parsed.path.rstrip('/')=='/search_result' else None)
    allowed=[v for v in (slot.get('last_url'),slot.get('navigation_intent',{}).get('url')) if v]
    if allowed and target(meta['url']) not in [target(v) for v in allowed]:
        raise BridgeError('UNEXPECTED_TARGET_CHANGE')
    identity_result=driver.call('identity',b,role)
    if identity_result.get('uid') != b['account_uid']: raise BridgeError('ACCOUNT_MISMATCH')
    if target(identity_result.get('url',''))!=target(meta['url']):raise BridgeError('TARGET_MISMATCH')
    if slot.get('navigation_intent'):
        if target(meta['url'])!=target(slot['navigation_intent']['url']):raise BridgeError('CONTENT_LOADING')
        slot.pop('navigation_intent')
    meta['visible']=identity_result.get('visible')
    b['tabs'][role].update(last_url=meta['url'],verified_at=legacy.now())
    # CDP metadata may omit the native window ID. Absence is not evidence
    # that the already verified window changed; retain its original binding.
    if meta.get('windowId') is not None:
        if (b.get('manual_single_tab') and slot.get('window_id') is not None
                and slot['window_id']!=meta['windowId']):
            raise BridgeError('TARGET_MISMATCH')
        slot['window_id']=meta['windowId']
    return meta


def ready_capture(project,b,driver,role,args):
    """Retry observation, not navigation. One persisted reload per failed target."""
    layout_unsettled=False
    for attempt in range(3):
        try:return driver.call('capture',b,role,**args)
        except BridgeError as exc:
            if exc.code=='BACKGROUND_LAYOUT_UNSETTLED':layout_unsettled=True
            elif exc.code not in {'CONTENT_LOADING','IDENTITY_UNCONFIRMED'}:raise
            if attempt<2:time.sleep(1)
    if layout_unsettled:
        # A newly navigated background search may have decoded cards but no
        # masonry layout yet. Select only the registered owned tab once; keep
        # the same identity/target/layout guards and never request OS focus.
        if (args.get('backgroundText') is not True or
                b.get('tabs',{}).get(role,{}).get('owned') is not True):
            raise BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
        meta=verify_tab(driver,b,role)
        key=hashlib.sha256((role+'\0'+meta['url']).encode()).hexdigest()
        attempts=b.setdefault('layout_activation_attempts',{})
        if key in attempts:
            raise BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
        attempts[key]={'at':legacy.now(),'page':meta['page'],'url':meta['url']}
        save_binding(project.path,b['binding_name'],b)
        activated=driver.call('activate',b,role)
        if (activated.get('activated')!=meta['page'] or
                activated.get('windowFocusRequested') is not False):
            raise BridgeError('TARGET_RELATION_UNCONFIRMED')
        verify_tab(driver,b,role)
        for attempt in range(3):
            time.sleep(1)
            try:return driver.call('capture',b,role,**args)
            except BridgeError as exc:
                if exc.code not in {'BACKGROUND_LAYOUT_UNSETTLED','CONTENT_LOADING'}:raise
        raise BridgeError('BACKGROUND_LAYOUT_UNSETTLED')
    meta=verify_tab(driver,b,role)
    key=hashlib.sha256(meta['url'].encode()).hexdigest()
    activations=b.setdefault('activation_attempts',{})
    if args.get('backgroundText') is not True and meta.get('visible') is False and key not in activations:
        activations[key]=legacy.now();save_binding(project.path,b['binding_name'],b)
        driver.call('activate',b,role)
        time.sleep(1)
        try:return driver.call('capture',b,role,**args)
        except BridgeError as exc:
            if exc.code not in {'CONTENT_LOADING','IDENTITY_UNCONFIRMED'}:raise
    reloads=b.setdefault('reload_attempts',{})
    if key in reloads:raise BridgeError('CONTENT_LOADING_AFTER_SINGLE_RELOAD')
    reloads[key]=legacy.now()
    save_binding(project.path,b['binding_name'],b)
    driver.call('reload',b,role)
    time.sleep(1)
    return driver.call('capture',b,role,**args)


def recover_owned_role(project,name,b,driver,role):
    try:
        return provision_role(project,name,b,driver,role)
    except BridgeError as exc:
        if exc.code=='UNEXPECTED_TARGET_CHANGE':return restore_owned_home(project,name,b,driver,role)
        if exc.code!='TARGET_MISSING':raise
    old=dict(b['tabs'][role])
    if not old.get('owned'):raise BridgeError('TARGET_MISSING')
    proof=driver.call('probe_registered_target',b,role)
    if proof.get('state')!='registered_target_absent' or proof.get('page')!=old.get('page'):
        raise BridgeError('TARGET_RELATION_UNCONFIRMED')
    # Preserve the obsolete target and same project role/session. The existing
    # provisioning journal prevents repeated creation after an unknown result.
    b.setdefault('retired_targets',[]).append({'role':role,'tab':old,'proof':proof,'at':legacy.now()})
    del b['tabs'][role]
    save_binding(project,name,b)
    # A fork's absent role needs an append-only home/identity proof before
    # ordinary provisioning or collection can use a replacement tab.
    if b.get('fork_home_verification'):
        raise BridgeError('FORK_REPLACEMENT_HOME_REVERIFICATION_REQUIRED')
    return provision_role(project,name,b,driver,role)


def restore_owned_home(project,name,b,driver,role):
    slot=b['tabs'][role];meta=driver.call('metadata',b,role)
    if not slot.get('owned') or meta.get('page')!=slot['page'] or meta.get('url')!='https://www.xiaohongshu.com/explore':
        raise BridgeError('UNEXPECTED_TARGET_CHANGE')
    if not b.get('allow_home_restore'):raise BridgeError('HOME_RESTORE_NOT_AUTHORIZED')
    lease_state=driver.call('lease_status',b,role)
    if lease_state.get('matching')!=1 or lease_state.get('leaseTabCount')!=1:raise BridgeError('TARGET_RELATION_UNCONFIRMED')
    ident=driver.call('identity',b,role)
    if ident.get('uid')!=b['account_uid']:raise BridgeError('ACCOUNT_MISMATCH')
    target=slot.get('last_url');safe_url(target)
    if target=='https://www.xiaohongshu.com/explore':raise BridgeError('HOME_RESTORE_NO_TARGET')
    key=hashlib.sha256(target.encode()).hexdigest();attempts=b.setdefault('home_restore_attempts',{})
    if key in attempts:raise BridgeError('HOME_RESTORE_ALREADY_ATTEMPTED')
    attempts[key]={'at':legacy.now(),'before':meta,'target':target}
    save_binding(project,name,b)
    driver.call('navigate',b,role,url=target);time.sleep(1)
    verified=verify_tab(driver,b,role);save_binding(project,name,b)
    return verified


def recover(project,name,release=False):
    b=load_binding(project,name);driver=Driver(b['runtime'])
    with lease(project,b) as keys:
        try:
            profiles=connected_profiles(driver)
            match=[p for p in profiles if p['contextId']==b['context_id']]
            if len(match)!=1: raise BridgeError('SPECIFIED_PROFILE_OFFLINE')
            if match[0].get('extensionVersion')!=effective_extension_version(b): raise BridgeError('EXTENSION_CHANGED_RECHECK_REQUIRED')
            if not release:
                for role in ('search','detail'):recover_owned_role(project,name,b,driver,role)
            for role in b['tabs']:
                try:verify_tab(driver,b,role)
                except BridgeError as exc:
                    if exc.code!='UNEXPECTED_TARGET_CHANGE' or release:raise
                    restore_owned_home(project,name,b,driver,role)
                if release and b['tabs'][role].get('owned'):
                    driver.call('close',b,role)
                    b['tabs'][role]['released']=True
                    save_binding(project,name,b)
            b['state']='released' if release else 'ready';b.pop('last_error',None)
            save_binding(project,name,b)
        except Exception as exc:
            save_failure(project,name,b,exc,keys);raise
    return {'state':b['state'],'tabs':b['tabs']}


def text_mode(project,name,mode):
    """Persist one explicit collection-mode flag under the original account lease."""
    if mode not in ('background','foreground'):raise BridgeError('TEXT_MODE_REQUIRED')
    project=Path(project).resolve()
    with xp.project_lock(project):
        b=load_binding(project,name)
        with lease(project,b):
            b['background_text_collection']=mode=='background'
            save_binding(project,name,b)
    return {'binding':name,'mode':mode,'background_text_collection':b['background_text_collection']}


def window_state(project,name,role):
    """Read one registered role's native window; never prepare or activate it."""
    if role not in {'search','detail'}:raise BridgeError('ROLE_REQUIRED')
    project=Path(project).resolve()
    with xp.project_lock(project):
        b=load_binding(project,name);driver=Driver(b['runtime'])
        try:
            with lease(project,b) as keys:
                try:
                    before=verify_tab(driver,b,role)
                    observed=driver.call('registered_window_state',b,role)
                    after=verify_tab(driver,b,role)
                    if not isinstance(observed,dict):raise BridgeError('WINDOW_STATE_UNCONFIRMED')
                    if observed.get('page')!=before['page'] or after['page']!=before['page']:
                        raise BridgeError('TARGET_MISMATCH')
                    if observed.get('uid')!=b['account_uid']:raise BridgeError('ACCOUNT_MISMATCH')
                    if observed.get('url')!=before['url'] or after['url']!=before['url']:
                        raise BridgeError('UNEXPECTED_TARGET_CHANGE')
                    if (observed.get('readOnly') is not True or observed.get('targetVerified') is not True
                            or observed.get('windowFocusRequested') is not False
                            or observed.get('windowRestoreRequested') is not False):
                        raise BridgeError('WINDOW_STATE_UNCONFIRMED')
                    return dict(observed,binding=name,role=role)
                except Exception as exc:
                    save_failure(project,name,b,exc,keys);raise
        finally:driver.close()


def exact_target_observed_urls(data,stage,target,source_sha256=None):
    """Resolve observed links only; a named author href may be origin-relative.

    Relative paths are never inferred from IDs or prose. Keep both the raw href
    and its JSON key path so attachment audits can locate the original bytes.
    """
    if not re.fullmatch(r'[a-f0-9]{24}',target or ''):return []
    observations=[]
    def walk(value,path):
        if isinstance(value,dict):
            for key,child in value.items():walk(child,path+[key])
        elif isinstance(value,list):
            for i,child in enumerate(value):walk(child,path+[i])
        elif isinstance(value,str):
            # URL parsers can silently strip whitespace/control characters.
            if re.search(r'[\s\x00-\x1f\x7f\\]',value):return
            normalized=value;kind='absolute_observed_url'
            if not value.startswith('https://www.xiaohongshu.com/'):
                if stage!='author' or not path or path[-1]!='authorHrefRaw':return
                if not value.startswith('/user/profile/'):return
                # Exact raw path rejects protocol-relative, dot/encoded paths,
                # private routes, lookalike IDs and author-note subroutes.
                try:parts=urlparse(value)
                except ValueError:return
                if (parts.scheme or parts.netloc or parts.fragment or parts.params
                    or parts.path!='/user/profile/'+target):return
                normalized=urljoin('https://www.xiaohongshu.com',value)
                kind='observed_author_href_fixed_origin'
            try:safe_url(normalized)
            except (BridgeError,ValueError):return
            resolved_path=urlparse(normalized).path.rstrip('/')
            if stage=='author':matches=resolved_path=='/user/profile/'+target
            else:matches=(resolved_path in {'/explore/'+target,'/search_result/'+target}
                or bool(re.fullmatch(r'/user/profile/[a-f0-9]{24}/'+target,resolved_path)))
            if matches:
                observations.append({'original_href':value,'keypath':path,
                    'normalized_url':normalized,'normalization':kind,
                    'source_sha256':source_sha256,'content_acquired':False})
    walk(data,[])
    return observations


def observed_url(project,task):
    p=task['payload'];stage=p['stage'];target=p['target']
    if stage=='search': return 'https://www.xiaohongshu.com/search_result?keyword='+quote(target)+'&type=51'
    candidates=exact_target_observed_urls({'bridge_url':p.get('bridge_url')},stage,target)
    for source in p.get('provenance',[]):
        archive=Path(project)/source.get('archive','')
        if archive.is_file() and not archive.is_symlink():
            raw=archive.read_bytes();sha=hashlib.sha256(raw).hexdigest()
            if sha!=source.get('sha256'):continue
            try:candidates.extend(exact_target_observed_urls(json.loads(raw),stage,target,sha))
            except (ValueError,UnicodeError):pass
    if candidates:return candidates[0]['normalized_url']
    raise BridgeError('OBSERVED_SOURCE_URL_REQUIRED')


def attach_source(project,task_id,source_file):
    """Attach an observed exact-target JSON source without acquiring any page."""
    db=xp.Project(project)
    try:
        task=db.task(task_id);payload=task['payload'];stage=payload.get('stage');target=payload.get('target')
        if task.get('kind')!='native' or stage not in STAGES-{'search'} or not re.fullmatch(r'[a-f0-9]{24}',target or ''):
            raise BridgeError('SOURCE_ATTACHMENT_TARGET_REQUIRED')
        source=Path(source_file)
        if not source.is_absolute() or not source.is_file() or source.is_symlink():
            raise BridgeError('EXISTING_JSON_SOURCE_REQUIRED')
        raw=source.read_bytes();sha=hashlib.sha256(raw).hexdigest()
        try:data=json.loads(raw)
        except (ValueError,UnicodeError):raise BridgeError('VALID_JSON_SOURCE_REQUIRED')
        observations=exact_target_observed_urls(data,stage,target,sha)
        urls=list(dict.fromkeys(row['normalized_url'] for row in observations))
        if not urls:raise BridgeError('EXACT_TARGET_SOURCE_URL_REQUIRED')
        # Freeze the already validated bytes before import; later changes to the
        # caller's file cannot replace the evidence imported in this operation.
        source_archive,archived_sha=native.archive(db,source)
        if archived_sha!=sha:raise BridgeError('SOURCE_CHANGED_DURING_ATTACHMENT')
        directory=db.path/'bridge'/'source-attachments'
        if directory.is_symlink():raise BridgeError('SOURCE_ATTACHMENT_ARCHIVE_INVALID')
        directory.mkdir(parents=True,exist_ok=True)
        manifest={'units':[{'stage':stage,'target':target,'next':payload.get('next') or 'Read the attached exact-target source; preserve previous progress.',
            'priority':task['priority'],'sources':[str(db.path/source_archive)]}]}
        if 'sort' in payload:manifest['units'][0]['sort']=payload['sort']
        encoded=xp.dumps(manifest).encode();manifest_sha=hashlib.sha256(encoded).hexdigest()
        manifest_path=directory/(task_id+'-'+manifest_sha+'-manifest.json')
        if manifest_path.exists() and (manifest_path.is_symlink() or manifest_path.read_bytes()!=encoded):
            raise BridgeError('SOURCE_ATTACHMENT_ARCHIVE_INVALID')
        if not manifest_path.exists():manifest_path.write_bytes(encoded)
        imported=native.import_manifest(db,manifest_path)
        if imported['task_ids']!=[task_id]:raise BridgeError('SOURCE_ATTACHMENT_TASK_CHANGED')
        current=db.task(task_id);result_url=observed_url(db.path,current)
        b=current['payload'].get('bridge',{});fault=b.get('fault',{})
        resolved=fault.get('code')=='OBSERVED_SOURCE_URL_REQUIRED'
        audit={'task_id':task_id,'stage':stage,'target':target,'source_file':str(source),
            'source_archive':source_archive,'source_sha256':sha,'observed_urls':urls,
            'url_observations':observations,'resolved_url':result_url,'content_acquired':False,
            'manifest_file':str(manifest_path),'manifest_sha256':manifest_sha,
            'basis':'Existing JSON contains an observed exact-target public URL, or a named authorHrefRaw normalized only to the fixed XHS origin; original href, keypath and raw SHA are retained. No live content was acquired and this is not a new page observation.'}
        audit_raw=xp.dumps(audit).encode();audit_sha=hashlib.sha256(audit_raw).hexdigest()
        audit_path=directory/(task_id+'-'+audit_sha+'-audit.json')
        if audit_path.exists() and (audit_path.is_symlink() or audit_path.read_bytes()!=audit_raw):
            raise BridgeError('SOURCE_ATTACHMENT_ARCHIVE_INVALID')
        if not audit_path.exists():audit_path.write_bytes(audit_raw)
        reference={'audit_file':str(audit_path),'audit_sha256':audit_sha,'source_sha256':sha,
                   'source_archive':source_archive,'url':result_url}
        refs=current['payload'].setdefault('source_attachments',[])
        if reference not in refs:refs.append(reference)
        if resolved:
            b.setdefault('resolved_source_faults',[]).append(dict(reference,prior_fault=b.pop('fault'),resolved_at=legacy.now()))
        db.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',(xp.dumps(current['payload']),legacy.now(),task_id));db.db.commit()
        return {'task_id':task_id,'url':result_url,'source_sha256':sha,'audit_file':str(audit_path),
                'source_fault_resolved':resolved,'status':current['status'],'content_acquired':False}
    finally:db.close()


COMMENT_BODY_GAP_FAULT='COMMENT_REPLY_BODY_UNAVAILABLE'


def comment_collection_state(comments):
    """Parent pagination ending is independent of all discovered text being read."""
    threads=comments.get('threads',{})
    if isinstance(threads,list):threads={t.get('rootKey',''):t for t in threads}
    unresolved=set(comments.get('unresolvedRootKeys',[]))
    for key,thread in threads.items():
        if (thread.get('state') not in {'visible_replies_read','previously_read'}
                or thread.get('contentCaptured') is False or thread.get('replyContentCaptured') is False
                or thread.get('replyIdentityComplete') is False or thread.get('replyBodyGaps')
                or thread.get('replyBodyVerificationPending') or thread.get('identityAmbiguous')
                or thread.get('scanIncomplete') or thread.get('pendingReplyExpansion')):
            unresolved.add(key)
    root_end=comments.get('rootPaginationState')=='complete'
    complete=root_end and not unresolved and not comments.get('missingExpectedRootIds')
    terminal=[]
    for key in sorted(unresolved):
        thread=threads.get(key,{})
        gaps=[g for g in thread.get('replyBodyGaps',[]) if
              g.get('reason')=='reply_content_not_rendered' and g.get('renderedReplyPrefixOnly') is True
              and g.get('retainedPriorBody') is not True]
        if (thread.get('state')!='stalled' or not gaps or thread.get('expanders')
                or thread.get('unsafeExpanders') or thread.get('scanIncomplete')
                or thread.get('pendingReplyExpansion')):
            terminal=[];break  # Other runnable floors must retain their chance to progress.
        terminal.append({'rootKey':key,'parentCommentId':thread.get('commentId'),
                         'commentIds':[g.get('commentId') for g in gaps]})
    return {'root_end':root_end,'complete':complete,
            'unresolved':sorted(unresolved),'terminal_body_gaps':terminal if root_end else []}


def apply_comment_collection_state(progress,state,source,h):
    progress['root_collection_end']=state['root_end']
    progress['collection_end']=state['complete']
    if state['terminal_body_gaps'] and not progress.get('fault'):
        progress['fault']={'code':COMMENT_BODY_GAP_FAULT,'hold':True,'at':legacy.now(),
            'recovery':{'exhausted':True,'explicit_versioned_probe_required':True},
            'source_snapshot':str(source),'source_sha256':h,'threads':state['terminal_body_gaps']}


def reviewed_comment_state(project,task):
    """Recheck the linked current bytes, including legacy optimistic end booleans."""
    progress=task['payload'].get('bridge',{});comments=progress.get('comments',{})
    source=progress.get('last_snapshot')
    if not source and progress.get('hashes'):
        source=str(project.path/'bridge'/'raw'/(progress['hashes'][-1]+'.json'))
    h=Path(source or '').stem
    if h not in progress.get('hashes',[]):
        if comments:return comment_collection_state(comments)
        return None  # Legacy non-deep collectors retain their existing stage contract.
    raw=comment_scope_source(project,h,task['payload']['target'])
    coverage=raw.get('deepCoverage')
    if not isinstance(coverage,dict):return comment_collection_state(comments) if comments else None
    from xhs_browser import merge_browser_progress
    merged=merge_browser_progress(comments,{'coverage':coverage,'data':raw.get('results',[])},str(project.path/'bridge'/'raw'/(h+'.json')))
    # A contradictory remaining-floor list cannot disappear behind a legacy status flag.
    for thread in coverage.get('remainingThreads',[]):
        key=thread.get('rootKey')
        if key and key not in merged['unresolvedRootKeys']:merged['unresolvedRootKeys'].append(key)
    return comment_collection_state(merged)


def root_pagination_ended(progress):
    comments=progress.get('comments',{})
    return comments.get('rootPaginationState')=='complete' if comments else bool(progress.get('collection_end'))


def store_capture(project,task,data,*,count_stall=True):
    """Append immutable acquisition; no automatic semantic review or task completion."""
    p=task['payload'];progress=p.get('bridge',{})
    reply_marker=data.get('deepCoverage',{}).get('discoveredReplyScope')
    if reply_marker is not None:
        validate_discovered_reply_marker(project,task,reply_marker)
        if (reply_marker.get('parentPaginationAttempted') is not False or reply_marker.get('rootScrollsSubmitted')!=0
                or data.get('deepCoverage',{}).get('scrollRounds',0)!=0):raise BridgeError('DISCOVERED_REPLY_SCOPE_VIOLATION')
    # Cursor and visibility are operational state, not new evidence. Do not
    # append another source merely because the viewport moved or was activated.
    evidence={k:v for k,v in data.items() if k not in {'position','visible'}}
    if isinstance(evidence.get('sortVerification'),dict):
        evidence['sortVerification']={k:v for k,v in evidence['sortVerification'].items() if k!='at'}
    raw=json.dumps(evidence,ensure_ascii=False,sort_keys=True).encode()
    h=hashlib.sha256(raw).hexdigest();directory=project.path/'bridge'/'raw';directory.mkdir(parents=True,exist_ok=True)
    path=directory/(h+'.json')
    if path.is_symlink() or path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=h:
        raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
    if not path.exists():path.write_bytes(raw)
    from xhs_rounds import semantic
    stable_cards=[{'text':r.get('text'),'path':target_path(urlparse(r.get('url','')).path)} for r in data.get('cards',[])]
    signature=hashlib.sha256(json.dumps({'body':data.get('body'),'cards':stable_cards,
        'results':[semantic(r) for r in data.get('results',[])],'end':data.get('explicitEnd')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    duplicate=h in progress.get('hashes',[])
    semantic_duplicate=signature==progress.get('content_signature')
    position=data.get('position') or {}
    # The historical high-water cursor is a resume hint, not this document's
    # current position. A real forward scroll can advance below an old maximum.
    previous_position=progress.get('last_observed_position',progress.get('cursor',{}))
    moved=position.get('scrollTop',0)>previous_position.get('scrollTop',0)
    card_ids=[re.search(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/([a-f0-9]{24})(?:[/?]|$)',r.get('url','')) for r in data.get('cards',[])]
    new_ids={m[1] for m in card_ids if m}-set(progress.get('observed_note_ids',[]))
    no_progress=(not new_ids and not moved and data.get('explicitEnd') is not True and bool(progress.get('content_signature'))) if p['stage'] in {'search','author'} else signature==progress.get('content_signature') and not moved
    genuine_progress=not no_progress
    if p['stage'] in {'search','author'} and not count_stall:
        # Restoring an old position is not another failed pagination attempt.
        # New IDs or an actual end still count as evidence of progress.
        genuine_progress=bool(new_ids) or data.get('explicitEnd') is True
    if no_progress and count_stall:progress['stalled_count']=progress.get('stalled_count',0)+1
    elif genuine_progress:
        progress['stalled_count']=0
        if progress.get('fault',{}).get('code')=='PAGINATION_STALLED':progress.pop('fault',None)
    else:progress.setdefault('stalled_count',0)
    progress['content_signature']=signature
    if duplicate:pass
    else:
        progress.setdefault('hashes',[]).append(h)
        receipt={'target':p['target'],'action':'read','outcome':'ready','observed_at':legacy.now(),
                 'summary':'Bridge acquired rendered content; Codex reading and attribution pending.',
                 'source_file':str(path),'url':data['url'],'target_verified':True,'content_ready':True,
                 'view_consistent':True,'content_observed':True,'reviewed':False,'complete':False,
                 'positions':['bridge/raw/'+h+'.json'],'gaps':['semantic_reading_pending'],
                 'next':'Codex reads acquired text/media; resume saved collection cursor.'}
        if p['stage']=='search':receipt['sort']=p['sort']
        receipt_path=directory/(h+'-'+task['id']+'-receipt.json');legacy.write_json(receipt_path,receipt)
        native.record(project,task['id'],receipt_path)
        # Use payload returned by record, preserving the evidence association.
        p=project.task(task['id'])['payload']
        # Index immutable observations without overwriting another transport's canonical text.
        project.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',(h,str(path),str(path),'bridge_'+p['stage'],legacy.now()))
        if data.get('body'):
            project.doc(h,p['target'],'bridge_body',data['body'],{'url':data['url'],'title':data.get('title','')},'body',captured_at=legacy.now())
        for index,row in enumerate(data.get('results',[])):
            project.doc(h,p['target'],'bridge_comment',row.get('text',''),row,'comment/'+str(index),captured_at=legacy.now())
        if any(row.get('images') or row.get('renderedInlineImages') for row in data.get('results',[])):
            from xhs_media import inspect_sources
            inspect_sources([path],project.path/'media')
            manifest=directory/(h+'-comment-media-scopes.json')
            legacy.write_json(manifest,{'units':[{'stage':'images','target':p['target'],'sources':[str(path)],'next':'Review observed comment attachments at their actual comment IDs; body carousel is separate.'}]})
            children=native.import_manifest(project,manifest)['task_ids']
            from xhs_rounds import inherit_tasks
            inherit_tasks(project,task['id'],children)
        if p['stage']=='body' and data.get('media'):
            units=[{'stage':stage,'target':p['target'],'sources':[str(path)],'next':'Read observed media positions; acquisition is not visual review.'}
                   for stage in ('images','video') if any((m.get('tag') in {'VIDEO','SOURCE'})==(stage=='video') for m in data['media'])]
            manifest=directory/(h+'-media-scopes.json');legacy.write_json(manifest,{'units':units})
            if units:
                imported=native.import_manifest(project,manifest)['task_ids']
                from xhs_rounds import inherit_tasks
                inherit_tasks(project,task['id'],imported)
    ids=progress.setdefault('observed_note_ids',[])
    for card in data.get('cards',[]):
        m=re.search(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/([a-f0-9]{24})',card.get('url',''))
        if m and m[1] not in ids:ids.append(m[1])
    if 'deepCoverage' in data:
        from xhs_browser import merge_browser_progress
        snap={'coverage':data['deepCoverage'],'data':data.get('results',[])}
        progress['comments']=merge_browser_progress(progress.get('comments',{}),snap,str(path))
    progress.update(last_snapshot=str(path),last_url=data['url'],collected_at=legacy.now())
    position=data.get('position')
    if isinstance(position,dict) and isinstance(position.get('scrollTop'),(int,float)):
        progress['last_observed_position']=dict(position)
        progress['last_observed_position_at']=legacy.now()
        progress['last_observed_position_snapshot']=str(path)
        if position['scrollTop']>=progress.get('cursor',{}).get('scrollTop',0):progress['cursor']=position
    coverage=progress.get('comments',{})
    if coverage:
        apply_comment_collection_state(progress,comment_collection_state(coverage),path,h)
    else:
        progress['collection_end']=progress.get('collection_end',False) or bool(data.get('explicitEnd'))
    # A rendered root-pagination stall is a local gap, never a platform end or
    # a reason to activate/reload the search page. Preserve any earlier fault.
    if reply_marker is not None:
        progress['collection_end']=False
        if reply_marker.get('state') in {'no_current_safe_controls','reply_actions_unconfirmed','known_parent_visit_pass_finished'}:
            progress.setdefault('discovered_reply_gap',{'reason':reply_marker['state'],
                'source_snapshot':str(path),'source_sha256':h,'scopeId':reply_marker['scopeId']})
    if (reply_marker is None and p['stage'] in {'roots','replies'} and data.get('deepCoverage',{}).get('rootPaginationState')=='stalled'
            and data.get('deepCoverage',{}).get('explicitEnd') is not True):
        progress['collection_end']=False
        progress.setdefault('fault',{'code':'COMMENT_PAGINATION_STALLED','hold':True,'at':legacy.now(),
            'recovery':{'exhausted':True},'source_snapshot':str(path),'source_sha256':h})
    if p['stage'] in {'search','author'} and progress['stalled_count']>=3 and not progress.get('collection_end'):
        progress['fault']={'code':'PAGINATION_STALLED','hold':True,'at':legacy.now(), 'recovery':progress.get('fault',{}).get('recovery',{})}
    progress['awaiting_review']=p['stage'] in {'body','images','video'} or progress['collection_end'] or progress['stalled_count']>=3
    p['bridge']=progress
    if p['stage']=='images':
        diagnostic=capture_diagnostic_status(project,dict(task,payload=p))
        if diagnostic['confirmed_hashes'] or diagnostic['invalid']:
            progress['awaiting_review']=bool(set(progress.get('hashes',[]))-
                set(progress.get('reviewed_hashes',[]))-set(diagnostic['confirmed_hashes']))
    project.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',(xp.dumps(p),legacy.now(),task['id']))
    project.db.commit()
    return {'snapshot':str(path),'duplicate':semantic_duplicate,'identical_snapshot':duplicate,'awaiting_review':progress['awaiting_review'],'observed_notes':len(ids),'new_note_ids':len(new_ids)}


def _diagnostic_file(path,sha,within=None):
    """Hash-bound existing files only; this does not infer visual readability."""
    file=Path(path or '')
    if (not file.is_absolute() or file.is_symlink() or not file.is_file()
            or not re.fullmatch(r'[0-9a-f]{64}',sha or '')):
        raise BridgeError('CAPTURE_DIAGNOSTIC_SOURCE_INVALID')
    if within is not None and not file.resolve().is_relative_to(Path(within).resolve()):
        raise BridgeError('CAPTURE_DIAGNOSTIC_SOURCE_OUTSIDE_SCOPE')
    raw=file.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha:raise BridgeError('CAPTURE_DIAGNOSTIC_SOURCE_CHANGED')
    return raw


def _validate_capture_diagnostic(project,task,receipt):
    """Validate a manual diagnosis of ONE superseded blank image capture.

    A successful metadata call is never a diagnosis. Only Codex's explicit
    visual attestation is accepted; code cannot independently prove its truth.
    A distinct, already-read replacement of this exact media key is required.
    """
    def require(condition,code):
        if not condition:raise BridgeError(code)
    p=task['payload'];b=p.get('bridge',{})
    require(isinstance(receipt,dict) and task.get('kind')=='native' and p.get('stage')=='images',
            'CAPTURE_DIAGNOSTIC_IMAGES_ONLY')
    require(receipt.get('kind')=='failed_media_capture_diagnostic_v1'
            and receipt.get('task_id')==task['id'] and receipt.get('target')==p['target'],
            'CAPTURE_DIAGNOSTIC_TARGET_MISMATCH')
    require(receipt.get('reviewer')=='codex' and receipt.get('inspection_method')=='view_image'
            and receipt.get('visual_checked') is True and receipt.get('target_context_verified') is True
            and receipt.get('classification')=='target_image_region_blank'
            and receipt.get('media_content_readable') is False
            and isinstance(receipt.get('visual_observation'),str) and receipt['visual_observation'].strip(),
            'EXPLICIT_VISUAL_CAPTURE_DIAGNOSIS_REQUIRED')
    require(all(receipt.get(k) is False for k in ('content_observed','reviewed','complete','comment_media_scan_complete')),
            'DIAGNOSTIC_MUST_NOT_CLAIM_CONTENT_OR_COMPLETION')
    try:
        when=datetime.fromisoformat(receipt['inspected_at'].replace('Z','+00:00'))
        require(when.tzinfo is not None,'CAPTURE_DIAGNOSTIC_TIME_REQUIRED')
    except (KeyError,ValueError,TypeError,AttributeError):raise BridgeError('CAPTURE_DIAGNOSTIC_TIME_REQUIRED')
    h=receipt.get('snapshot_hash');known=b.get('hashes',[])
    require(isinstance(h,str) and re.fullmatch(r'[0-9a-f]{64}',h)
            and h in known and h not in b.get('reviewed_hashes',[]),'CAPTURE_DIAGNOSTIC_NOT_UNREAD_SNAPSHOT')
    source=project.path/'bridge'/'raw'/(str(h)+'.json')
    raw=json.loads(_diagnostic_file(str(source),h,project.path/'bridge'/'raw'))
    require(raw.get('body_carousel_only') is True and raw.get('comment_media_scan_complete') is False
            and not any(raw.get(k) for k in ('body','results','cards','video_requires_segments'))
            and len(raw.get('media_captures',[]))==1,'CAPTURE_DIAGNOSTIC_NOT_SINGLE_IMAGE_CAPTURE')
    native.validate_url(task,raw.get('url',''))
    cap=raw['media_captures'][0];position=cap.get('position');key='body:'+str(position)
    require(type(position) is int and position>0 and type(cap.get('count')) is int
            and position<=cap['count'] and cap.get('kind')=='image'
            and raw.get('captured_positions')==[position] and raw.get('carousel_count')==cap['count']
            and receipt.get('media_key')==key and receipt.get('position')==position
            and receipt.get('png_file')==cap.get('file') and receipt.get('png_sha256')==cap.get('sha256')
            and cap.get('url')==raw['url'],'CAPTURE_DIAGNOSTIC_POSITION_MISMATCH')
    try:
        captured=datetime.fromisoformat(cap['observed_at'].replace('Z','+00:00'))
        require(captured.tzinfo is not None and when>=captured,'CAPTURE_DIAGNOSTIC_TIME_REQUIRED')
    except (KeyError,ValueError,TypeError,AttributeError):raise BridgeError('CAPTURE_DIAGNOSTIC_TIME_REQUIRED')
    _diagnostic_file(cap['file'],cap['sha256'],project.path/'bridge'/'media'/p['target'])
    sidecar=Path(cap['file']).with_suffix('.json')
    require(receipt.get('sidecar_file')==str(sidecar),'CAPTURE_DIAGNOSTIC_SIDECAR_MISMATCH')
    side=json.loads(_diagnostic_file(str(sidecar),receipt.get('sidecar_sha256'),sidecar.parent))
    require(all(side.get(k)==cap.get(k) for k in ('file','position','count','kind','url','observed_at')),
            'CAPTURE_DIAGNOSTIC_SIDECAR_MISMATCH')
    # Do not reclassify a capture that any prior visual receipt actually read.
    events=list(project.db.execute('SELECT * FROM native_events WHERE task_id=?',(task['id'],)))
    replacement=None
    for row in events:
        value=json.loads(row['receipt'])
        if value.get('media_verified') is True and key in value.get('media_reviewed_keys',[]):
            require(row['sha256']!=cap['sha256'],'CAPTURE_ALREADY_VISUALLY_REVIEWED')
        if row['id']==receipt.get('replacement_event_id'):replacement=(row,value)
    require(replacement is not None,'REVIEWED_REPLACEMENT_REQUIRED')
    event,value=replacement
    require(value.get('target')==p['target'] and value.get('reviewed') is True
            and value.get('media_verified') is True and key in value.get('media_reviewed_keys',[])
            and event['sha256']!=cap['sha256'],'REVIEWED_REPLACEMENT_REQUIRED')
    _diagnostic_file(str(project.path/event['artifact']),event['sha256'],project.path)
    saved=json.loads(_diagnostic_file(str(project.path/value.get('receipt_archive','')),
                                    value.get('receipt_sha256'),project.path))
    require(saved=={k:v for k,v in value.items() if k not in {'receipt_archive','receipt_sha256'}},
            'REPLACEMENT_RECEIPT_CHANGED')
    replacement_hashes=value.get('bridge_snapshot_hashes',[])
    require(replacement_hashes and set(replacement_hashes)<=set(b.get('reviewed_hashes',[])),
            'REPLACEMENT_SNAPSHOT_NOT_REVIEWED')
    found=False
    for rh in replacement_hashes:
        snapshot=json.loads(_diagnostic_file(str(project.path/'bridge'/'raw'/(rh+'.json')),rh,project.path))
        native.validate_url(task,snapshot.get('url',''))
        for rcap in snapshot.get('media_captures',[]):
            if rcap.get('position')==position and rcap.get('sha256')==event['sha256']:
                require(rcap.get('kind')=='image' and rcap.get('count')==cap['count'],
                        'REPLACEMENT_POSITION_MISMATCH')
                _diagnostic_file(rcap.get('file'),event['sha256'],project.path/'bridge'/'media'/p['target'])
                found=True
    require(found,'REPLACEMENT_POSITION_MISMATCH')
    return h


def capture_diagnostic_status(project,task):
    """Read-only, fail-closed diagnostic exclusions for bridge and round packets.

    confirmed_hashes are NOT read-content hashes. Callers must never copy them
    into reviewed_hashes, media_inventory, semantic coverage or completion.
    """
    result={'confirmed_hashes':[],'invalid':[]}
    if not project.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_capture_diagnostics'").fetchone():
        return result
    for row in project.db.execute('SELECT * FROM bridge_capture_diagnostics WHERE task_id=?',(task['id'],)):
        try:
            receipt=json.loads(_diagnostic_file(row['receipt_file'],row['receipt_sha256'],project.path/'bridge'/'capture-diagnostics'))
            h=_validate_capture_diagnostic(project,task,receipt)
            if h!=row['snapshot_hash']:raise BridgeError('CAPTURE_DIAGNOSTIC_LEDGER_MISMATCH')
            result['confirmed_hashes'].append(h)
        except (BridgeError,legacy.ResearchError,ValueError,TypeError,KeyError,AttributeError,IndexError,OSError) as exc:
            result['invalid'].append({'snapshot_hash':row['snapshot_hash'],'reason':str(exc)})
    result['confirmed_hashes']=sorted(set(result['confirmed_hashes']))
    return result


def confirm_capture_diagnostic(project,task_id,receipt_path):
    """Local, append-only diagnostic ledger. Never calls native.record or a driver."""
    task=project.task(task_id)
    if task['status']!='pending':raise BridgeError('CAPTURE_DIAGNOSTIC_PENDING_TASK_REQUIRED')
    file=Path(receipt_path)
    if not file.is_absolute() or not file.is_file() or file.is_symlink():
        raise BridgeError('CAPTURE_DIAGNOSTIC_RECEIPT_REQUIRED')
    raw=file.read_bytes();receipt=json.loads(raw);h=_validate_capture_diagnostic(project,task,receipt)
    sha=hashlib.sha256(raw).hexdigest();directory=project.path/'bridge'/'capture-diagnostics';archive=directory/(sha+'.json')
    if archive.exists() and (archive.is_symlink() or archive.read_bytes()!=raw):
        raise BridgeError('CAPTURE_DIAGNOSTIC_ARCHIVE_CHANGED')
    # Validation precedes every write. An interruption before the DB transaction
    # can leave only an unreferenced receipt file, never a false acknowledgement.
    directory.mkdir(parents=True,exist_ok=True)
    if not archive.exists():archive.write_bytes(raw)
    project.db.execute('SAVEPOINT capture_diagnostic_confirm')
    try:
        # Reserve the SQLite writer before rereading. WHERE 0 changes no rows,
        # but upgrades this savepoint to a write transaction (or fails on a
        # stale concurrent snapshot). Never rewrite the pre-validation payload.
        project.db.execute('UPDATE tasks SET payload=payload WHERE 0')
        task=project.task(task_id)
        if task['status']!='pending':raise BridgeError('CAPTURE_DIAGNOSTIC_PENDING_TASK_REQUIRED')
        if _validate_capture_diagnostic(project,task,receipt)!=h:
            raise BridgeError('CAPTURE_DIAGNOSTIC_TARGET_MISMATCH')
        project.db.execute('CREATE TABLE IF NOT EXISTS bridge_capture_diagnostics('
            'task_id TEXT NOT NULL,snapshot_hash TEXT NOT NULL,receipt_file TEXT NOT NULL,'
            'receipt_sha256 TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(task_id,snapshot_hash))')
        prior=project.db.execute('SELECT * FROM bridge_capture_diagnostics WHERE task_id=? AND snapshot_hash=?',(task_id,h)).fetchone()
        if prior and prior['receipt_sha256']!=sha:raise BridgeError('CAPTURE_DIAGNOSTIC_ALREADY_RECORDED')
        project.db.execute('INSERT OR IGNORE INTO bridge_capture_diagnostics VALUES(?,?,?,?,?)',
                           (task_id,h,str(archive),sha,receipt['inspected_at']))
        p=task['payload'];b=p['bridge'];status=capture_diagnostic_status(project,task)
        b['awaiting_review']=bool(set(b['hashes'])-set(b.get('reviewed_hashes',[]))-set(status['confirmed_hashes']))
        project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(p),task_id))
    except BaseException:
        project.db.execute('ROLLBACK TO capture_diagnostic_confirm')
        project.db.execute('RELEASE capture_diagnostic_confirm')
        raise
    else:project.db.execute('RELEASE capture_diagnostic_confirm')
    return {'task_id':task_id,'snapshot_hash':h,'diagnostic_confirmed':True,'duplicate':bool(prior),
            'awaiting_review':b['awaiting_review'],'content_observed':False,'complete':False,
            'media_read_count_added':0,'source_archive':str(archive)}


def review(project,task_id,receipt_path):
    """Record actual Codex reading, then resume without direct payload editing."""
    task=project.task(task_id);progress=task['payload'].get('bridge')
    # Shared note entities can exist before this task acquires its own snapshot.
    # Refuse before packet indexing/receipt writes so a missing context cannot
    # partially commit semantic review and then fail updating bridge progress.
    if (not isinstance(progress,dict) or not isinstance(progress.get('hashes'),list)
            or not progress['hashes']):
        raise BridgeError('BRIDGE_COLLECTION_CONTEXT_REQUIRED')
    receipt=legacy.read_json(receipt_path,{})
    comment_state=(reviewed_comment_state(project,task)
                   if task['payload']['stage'] in {'roots','replies'} else None)
    if receipt.get('complete') and comment_state is not None:
        if not comment_state['complete']:raise BridgeError('COMMENT_SCOPE_INCOMPLETE')
        if progress.get('fault',{}).get('code')==COMMENT_BODY_GAP_FAULT:
            raise BridgeError('COMMENT_BODY_GAP_REPAIR_REQUIRED')
    from xhs_rounds import commit_packet,review_media
    packet=commit_packet(project,task_id,receipt)
    review_media(project,task,receipt)
    hashes=receipt.get('bridge_snapshot_hashes')
    if receipt.get('reviewed') is not True or not isinstance(hashes,list) or (not hashes and not packet):
        raise BridgeError('EXPLICIT_REVIEW_RECEIPT_REQUIRED')
    known=set(progress.get('hashes',[]))
    diagnosed=set(capture_diagnostic_status(project,task)['confirmed_hashes'])
    if set(hashes)&diagnosed:raise BridgeError('DIAGNOSTIC_SNAPSHOT_IS_NOT_CONTENT')
    if not set(hashes)<=known:raise BridgeError('REVIEW_SNAPSHOT_NOT_IN_TASK')
    for h in hashes:
        source=project.path/'bridge'/'raw'/(h+'.json')
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h:
            raise BridgeError('REVIEW_SOURCE_CHANGED')
    reviewed=set(progress.get('reviewed_hashes',[])) | set(hashes)
    if receipt.get('complete') and (reviewed|diagnosed)!=known:
        raise BridgeError('UNREAD_SNAPSHOTS_REMAIN')
    observed_end=comment_state['complete'] if comment_state is not None else progress.get('collection_end')
    if receipt.get('complete') and task['payload']['stage'] in {'search','roots','replies','author'} and not observed_end:
        raise BridgeError('COLLECTION_END_NOT_OBSERVED')
    result=native.record(project,task_id,receipt_path)
    commit_packet(project,task_id,receipt,apply=True)
    review_media(project,task,receipt,apply=True)
    current=project.task(task_id);p=current['payload'];progress=p['bridge']
    if comment_state is not None:
        source=progress.get('last_snapshot') or str(project.path/'bridge'/'raw'/(progress['hashes'][-1]+'.json'))
        apply_comment_collection_state(progress,comment_state,source,Path(source).stem)
    progress['reviewed_hashes']=sorted(reviewed)
    progress['awaiting_review']=(reviewed|diagnosed)!=known
    # Reading the same evidence does not repair a stuck browser cursor.
    if receipt.get('complete') and progress.get('fault',{}).get('code')=='MEDIA_CONTEXT_REVIEW_REQUIRED':
        progress['resolved_review_handoff']=progress.pop('fault')
    progress['last_review_event']=result['event_id']
    project.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',(xp.dumps(p),legacy.now(),task_id))
    project.db.commit()
    return {'task_id':task_id,'status':current['status'],'awaiting_review':progress['awaiting_review'],
            'event_id':result['event_id'],'reviewed_snapshots':len(reviewed)}


def summary(project):
    result={'bindings':[], 'acquired_scopes':0,'awaiting_reading_scopes':0,'collection_end_scopes':0,
            'review_handoff_scopes':0,'pending_review_hash_count':0,'pending_review_entity_count':0,
            'pending_review_entity_version_count':0,'review_source_gap_scopes':0,
            'note':'Acquisition end is not analyst review or platform-wide completion.'}
    for file in sorted((project.path/'bridge'/'bindings').glob('*.json')):
        if file.is_symlink():continue
        value=legacy.read_json(file,{})
        result['bindings'].append({k:value.get(k) for k in ('profile_label','context_id','state','last_error','last_success')})
    from xhs_rounds import review_backlog
    review_memo={}
    for row in project.db.execute("SELECT id,payload,status FROM tasks WHERE kind='native'"):
        payload=json.loads(row['payload']);progress=payload.get('bridge',{})
        backlog=review_backlog(project,{'id':row['id'],'kind':'native','payload':payload},memo=review_memo)
        if progress.get('hashes'):result['acquired_scopes']+=1
        if (backlog['pending_review_hash_count'] or backlog['pending_review_entity_version_count']) and row['status']!='complete':result['awaiting_reading_scopes']+=1
        result['review_handoff_scopes']+=int(backlog['review_handoff_due'] and row['status']!='complete')
        for key in ('pending_review_hash_count','pending_review_entity_count','pending_review_entity_version_count'):
            result[key]+=backlog[key]
        result['review_source_gap_scopes']+=int(bool(backlog['pending_review_source_gaps'] or backlog['pending_review_diagnostic_gaps']))
        if progress.get('collection_end'):result['collection_end_scopes']+=1
    result['pending_review_count_scope']='task_snapshot_references_and_semantic_entity_versions_not_patients'
    return result


DOM_TEXT_STAGES={'search','body','roots','replies','author'}


def background_text_allowed(binding,stage):
    return binding.get('background_text_collection') is True and stage in DOM_TEXT_STAGES


def prepare_target(project,b,driver,task,*,text_only=False):
    if task.get('payload',{}).get('target') in b.get('manual_excluded_note_ids',[]):
        raise BridgeError('ORIGINAL_NOTE_ACCESS_UNRESOLVED')
    p=task['payload'];stage=p['stage'];role='search' if stage=='search' else 'detail'
    url=observed_url(project.path,task)
    try:meta=verify_tab(driver,b,role)
    except BridgeError as exc:
        if exc.code!='TARGET_MISSING':raise
        meta=recover_owned_role(project.path,b['binding_name'],b,driver,role)
    args={'stage':stage,'expectedPath':urlparse(url).path}
    if stage=='search':args['query']=p['target']
    current=meta['url'];changed=target_path(urlparse(current).path)!=target_path(urlparse(url).path) or (stage=='search' and parse_qs(urlparse(current).query).get('keyword')!=[p['target']])
    if changed:
        # Persist before sending so an uncertain command is never blindly resent.
        if b['tabs'][role].get('navigation_intent'):raise BridgeError('NAVIGATION_UNCONFIRMED')
        b['tabs'][role]['navigation_intent']={'url':url,'at':legacy.now()}
        save_binding(project.path,b['binding_name'],b)
        try:driver.call('navigate',b,role,url=url)
        except BridgeError as exc:
            if exc.code!='COMMAND_OUTCOME_UNKNOWN':raise
            # The submitted command may have succeeded. Observe its exact target;
            # never repeat navigation merely because the response was lost.
        # Bounded readiness, not repeated navigations; identity may load asynchronously.
        for attempt in range(3):
            time.sleep(1)
            try:meta=verify_tab(driver,b,role);break
            except BridgeError as exc:
                if exc.code not in {'IDENTITY_UNCONFIRMED','CONTENT_LOADING'} or attempt==2:raise
    # This opt-in is invocation-specific: media can use a body task, but never
    # opts into text_only. Identity, URL, CDP target and all readiness guards
    # above remain mandatory even when the operating-system window is behind.
    if text_only is True and background_text_allowed(b,stage):
        if not isinstance(meta.get('visible'),bool):raise BridgeError('TARGET_VISIBILITY_UNCONFIRMED')
        args['backgroundText']=True
        return url,role,args
    # setActivePage only routes commands; it does not select the actual Chrome
    # role tab. Use the final document after navigation, never the old page's
    # visibility. This selects the existing tab, not its window or a new lease.
    if meta.get('visible') is False:
        driver.call('activate',b,role)
        meta=verify_tab(driver,b,role)
        authorization=b.get('app_foreground_authorization')
        if (meta.get('visible') is False and isinstance(authorization,str)
                and authorization.strip() and sys.platform=='darwin'):
            # Explicit per-binding opt-in: activate only the app, without
            # inspecting windows or treating OS success as target verification.
            try:
                subprocess.run(['/usr/bin/osascript','-e','tell application "Google Chrome" to activate'],
                               shell=False,check=True,timeout=5,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except (OSError,subprocess.SubprocessError):
                raise BridgeError('TARGET_NOT_VISIBLE')
            time.sleep(1)
            meta=verify_tab(driver,b,role)
        if meta.get('visible') is False:raise BridgeError('TARGET_NOT_VISIBLE')
    if meta.get('visible') is not True:raise BridgeError('TARGET_VISIBILITY_UNCONFIRMED')
    return url,role,args


def collect_one(project,b,driver,task,force_advance=False,*,root_repair_probe=None,discovered_reply_probe=None):
    p=task['payload'];stage=p['stage']
    reply_marker=discovered_reply_probe or active_discovered_reply_scope(project,task)
    if reply_marker is not None:
        validate_discovered_reply_marker(project,task,reply_marker)
        if root_repair_probe is not None:raise BridgeError('COMMENT_PHASE_AUTHORITY_CONFLICT')
    elif stage=='replies' and discovered_reply_context(project,task):
        raise BridgeError('DISCOVERED_REPLY_PROBE_REQUIRED')
    if root_repair_probe is not None:
        fault=p.get('bridge',{}).get('fault',{})
        attempt=p.get('bridge',{}).get('repair_probes',{}).get(root_repair_probe.get('probeId'))
        if (stage not in {'roots','replies'} or fault.get('code')!='COMMENT_PAGINATION_STALLED' or not fault.get('hold') or not attempt
                or attempt.get('scope')!='same_note_rendered_comments' or attempt.get('maximum_acquisitions')!=2
                or attempt.get('status') not in {'started','unverified'}
                or root_repair_probe.get('kind')!='single_bounded_root_repair_v1' or root_repair_probe.get('maxScrolls')!=1
                or root_repair_probe.get('acquisition') not in {1,2}
                or root_repair_probe.get('allowScroll') is not (root_repair_probe.get('acquisition')==1)
                or root_repair_probe.get('noteId')!=p['target']
                or root_repair_probe.get('acquisition')!=len(attempt.get('observations',[]))+1):
            raise BridgeError('ROOT_REPAIR_PROBE_AUTHORITY_REQUIRED')
    url,role,args=prepare_target(project,b,driver,task,text_only=True)
    if stage in {'roots','replies'}:
        shared=project.path/'bridge'/'comment-progress'/(p['target']+'.json')
        prior=legacy.read_json(shared,p.get('bridge',{}).get('comments',{}))
        driver_progress=dict(prior)
        # Authority is invocation-local, never inherited from a stored snapshot.
        driver_progress.pop('rootRepairProbe',None)
        driver_progress.pop('discoveredReplyScope',None)
        if root_repair_probe is not None:
            driver_progress['rootRepairProbe']=dict(root_repair_probe)
        if reply_marker is not None:driver_progress['discoveredReplyScope']=dict(reply_marker)
        data=driver.call('deep',b,role,url=url,progress=driver_progress,
                         backgroundText=args.get('backgroundText') is True)
        if not isinstance(data,dict):raise BridgeError('INVALID_COMMENT_SNAPSHOT')
        data['url']=url
        if reply_marker is not None:
            returned=data.get('deepCoverage',{}).get('discoveredReplyScope')
            validate_discovered_reply_marker(project,task,returned)
            allowed=set(reply_marker['rootIds'])
            if any((row.get('parentCommentId') if row.get('is_reply') else row.get('commentId')) not in allowed
                   for row in data.get('results',[])):raise BridgeError('DISCOVERED_REPLY_SCOPE_VIOLATION')
        # A fresh roots/replies task may reuse the other stage's proven capture.
        # Carry only that exact thread's prior acquisition, never an unknown one.
        for thread in data.get('deepCoverage',{}).get('threads',[]):
            old=prior.get('threads',{}).get(thread.get('rootKey'),{})
            if thread.get('state')=='previously_read' and old.get('state')=='visible_replies_read':
                thread['state']='visible_replies_read'
                thread['prior_capture_source']=old.get('last_snapshot','')
    else:
        data=ready_capture(project,b,driver,role,args)
        if stage=='search':
            label={'general':'综合','latest':'最新','popular':'最多点赞'}[p['sort']]
            if label not in data.get('selected',[]):
                for attempt in range(2):
                    action=driver.call('advance',b,role,sort=p['sort'],**args)
                    time.sleep(1)
                    data=ready_capture(project,b,driver,role,args)
                    if label in data.get('selected',[]) or action.get('action')=='sort_clicked':break
                if label not in data.get('selected',[]):raise BridgeError('SORT_UNCONFIRMED')
            verified_sort={'label':label,'url':data['url'],'at':legacy.now(),
                           'selected_before_close':list(data.get('selected',[])),
                           'verified_before_close':True}
            driver.call('advance',b,role,dismissFilters=True,**args)
            time.sleep(1)
            data=ready_capture(project,b,driver,role,args)
            data['sortVerification']=verified_sort
        if stage in {'search','author'}:
            previous_top=p.get('bridge',{}).get('cursor',{}).get('scrollTop',0)
            # Restore only a recorded position; lazy loading can clamp it. Each
            # attempt captures the actually rendered intermediate page before
            # another bounded scroll, so replay cannot silently skip discoveries.
            for attempt in range(3):
                current_top=(data.get('position') or {}).get('scrollTop',0)
                if current_top>=previous_top or data.get('explicitEnd'):break
                store_capture(project,project.task(task['id']),data,count_stall=False)
                anchor=p.get('bridge',{}).get('cursor',{}).get('lastNoteId')
                restoration=driver.call('advance',b,role,resumeTop=previous_top,anchor=anchor,**args)
                time.sleep(1)
                data=ready_capture(project,b,driver,role,args)
                if stage=='search':data['sortVerification']=verified_sort
                restored=store_capture(project,project.task(task['id']),data,count_stall=False)
                position=data.get('position') or {}
                top=position.get('scrollTop');height=position.get('scrollHeight');viewport=position.get('viewportHeight')
                maximum=max(0,height-viewport) if all(type(v) in (int,float) for v in (height,viewport)) and viewport>0 else None
                reported_top=restoration.get('after') if isinstance(restoration,dict) else None
                at_max=maximum is not None and any(type(v) in (int,float) and abs(v-maximum)<=1 for v in (top,reported_top))
                anchor_restored=isinstance(restoration,dict) and restoration.get('anchorRestored') is True
                clamped=maximum is not None and previous_top>maximum+1 and at_max
                if clamped or anchor_restored:
                    current=project.task(task['id']);pr=current['payload']['bridge']
                    pr.setdefault('cursor_restoration_observations',[]).append({
                        'at':legacy.now(),'requested_top':previous_top,'anchor':anchor,
                        'reason':'current_document_maximum_clamped' if clamped else 'observed_anchor_restored',
                        'current_maximum':maximum,'observed_position':position,
                        'advance_result':restoration,'snapshot':restored['snapshot'],
                        'platform_end_inferred':False})
                    project.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',
                        (xp.dumps(current['payload']),legacy.now(),task['id']));project.db.commit()
                    break
        if stage in {'search','author'} and not data.get('cards') and not data.get('explicitEnd'):
            raise BridgeError('CONTENT_LOADING')
        if stage=='body' and not data.get('body') and not data.get('media'):raise BridgeError('CONTENT_LOADING')
    result=store_capture(project,project.task(task['id']),data,count_stall=stage not in {'search','author'})
    if stage=='body' and data.get('body') and data.get('uid')==b['account_uid']:
        b['web_validation']={'at':legacy.now(),'target':p['target'],'snapshot':result['snapshot'],'context_id':b['context_id'],'uid':data['uid']}
        save_binding(project.path,b['binding_name'],b)
    if stage in {'roots','replies'}:
        from xhs_browser import merge_browser_progress
        legacy.write_json(shared,merge_browser_progress(prior,{'coverage':data.get('deepCoverage',{}),'data':data.get('results',[])},result['snapshot']))
    if stage in {'search','author'} and (force_advance or not result['awaiting_review']):
        # Cards can precede layout. Capture the post-scroll state before another
        # task navigates this shared role, preserving both discoveries and cursor.
        time.sleep(2)
        driver.call('advance',b,role,**args)
        time.sleep(1)
        after=ready_capture(project,b,driver,role,args)
        if stage=='search':after['sortVerification']=verified_sort
        result=store_capture(project,project.task(task['id']),after)
    return result


LOCAL_FAULTS={'CONTENT_LOADING','CONTENT_LOADING_AFTER_SINGLE_RELOAD','SCROLL_CONTAINER_UNAVAILABLE','BACKGROUND_LAYOUT_UNSETTLED',
    'SORT_UNCONFIRMED','SORT_CONTROL_UNAVAILABLE','OBSERVED_SOURCE_URL_REQUIRED','DEEP_READ_FAILED',
    'COMMENT_PAGINATION_STALLED',COMMENT_BODY_GAP_FAULT,'TARGET_NOT_VISIBLE','TARGET_VISIBILITY_UNCONFIRMED',
    'MEDIA_CONTAINER_UNAVAILABLE','MEDIA_INDEX_UNCONFIRMED','MEDIA_CONTROL_UNAVAILABLE','MEDIA_EMPTY_OR_UNLOADED','MEDIA_ACTIVATION_UNCONFIRMED','MEDIA_IDENTITY_UNCONFIRMED','MEDIA_WARMUP_UNCONFIRMED','MEDIA_POSITION_CHANGED','MEDIA_SELECTION_UNCONFIRMED','VIDEO_REQUIRES_REVIEW'}


def hold_task(project,task_id,code):
    t=project.task(task_id);p=t['payload'];b=p.setdefault('bridge',{})
    b['fault']={'code':code,'hold':True,'at':legacy.now(),'recovery':{'exhausted':True}}
    project.db.execute('UPDATE tasks SET payload=?,reason=?,updated=? WHERE id=?',(xp.dumps(p),code,legacy.now(),task_id));project.db.commit()


def sync_shared_comment_stall(project,task):
    """Transfer only a hash-verified same-note raw stall, without acquiring/reviewing."""
    p=task['payload'];pr=p.get('bridge',{})
    if task['status']!='pending' or p.get('stage') not in {'roots','replies'} or pr.get('fault'):return task
    state=reviewed_comment_state(project,task)
    if state and state['terminal_body_gaps']:
        pr=p.setdefault('bridge',{})
        apply_comment_collection_state(pr,state,pr['last_snapshot'],Path(pr['last_snapshot']).stem)
        project.db.execute('UPDATE tasks SET payload=?,reason=?,updated=? WHERE id=?',
            (xp.dumps(p),COMMENT_BODY_GAP_FAULT,legacy.now(),task['id']));project.db.commit()
        return project.task(task['id'])
    if p.get('stage')=='replies':
        context=discovered_reply_context(project,task)
        if not context or active_discovered_reply_scope(project,task,context):return task
        # This is a blocked dependency, not a failed reply acquisition.
        if not pr.get('root_dependency_gap'):
            pr=p.setdefault('bridge',{})
            pr['root_dependency_gap']={'kind':'parent_pagination_dependency','scopeId':context['scopeId'],
                'parent_faults':context['parentFaults'],'root_ids':context['rootIds'],'source_hashes':context['sourceHashes']}
            project.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',(xp.dumps(p),legacy.now(),task['id']));project.db.commit()
        return project.task(task['id'])
    shared=project.path/'bridge'/'comment-progress'/(p['target']+'.json')
    if not shared.is_file() or shared.is_symlink():return task
    previous=legacy.read_json(shared,{})
    if previous.get('rootPaginationState')!='stalled' and previous.get('rootScrollProgress',{}).get('state')!='stalled':return task
    source=Path(previous.get('last_snapshot',''));h=source.stem
    expected=project.path/'bridge'/'raw'/(h+'.json')
    if (not re.fullmatch('[a-f0-9]{64}',h) or source!=expected or source.is_symlink()
            or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h):
        raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
    raw=json.loads(source.read_text());coverage=raw.get('deepCoverage',{})
    if (coverage.get('rootPaginationState')!='stalled' or coverage.get('explicitEnd') is True
            or target_path(urlparse(raw.get('url','')).path) not in {'/explore/'+p['target'],'/search_result/'+p['target']}):return task
    pr=p.setdefault('bridge',{})
    pr['fault']={'code':'COMMENT_PAGINATION_STALLED','hold':True,'at':legacy.now(),
        'recovery':{'exhausted':True},'source_snapshot':str(source),'source_sha256':h,'inherited_shared_stall':True}
    pr['collection_end']=False
    project.db.execute('UPDATE tasks SET payload=?,reason=?,updated=? WHERE id=?',
        (xp.dumps(p),'COMMENT_PAGINATION_STALLED',legacy.now(),task['id']));project.db.commit()
    return project.task(task['id'])


def comment_scope_source(project,h,note):
    if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
    source=project.path/'bridge'/'raw'/(h+'.json')
    if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h:
        raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
    value=json.loads(source.read_text())
    if target_path(urlparse(value.get('url','')).path) not in {'/explore/'+note,'/search_result/'+note}:
        raise BridgeError('COMMENT_SOURCE_SCOPE_MISMATCH')
    return value


def discovered_reply_context(project,task):
    """Derive an existing-parent allowlist from immutable root acquisitions."""
    p=task['payload'];fault=p.get('bridge',{}).get('fault')
    if p.get('stage')!='replies' or task['status']!='pending':return None
    if fault and not (fault.get('code')=='COMMENT_PAGINATION_STALLED' and fault.get('hold')
                      and fault.get('inherited_shared_stall') is True):return None
    roots=[];sources=[];parents=[]
    for row in project.db.execute("SELECT id,status,payload FROM tasks WHERE kind='native' AND json_extract(payload,'$.stage')='roots' AND json_extract(payload,'$.target')=?",(p['target'],)):
        payload=json.loads(row['payload']);bridge=payload.get('bridge',{});parent_fault=bridge.get('fault',{})
        if (row['status']!='pending' or bridge.get('collection_end') or
                parent_fault.get('code')!='COMMENT_PAGINATION_STALLED' or not parent_fault.get('hold')):continue
        h=parent_fault.get('source_sha256')
        if h not in bridge.get('hashes',[]):continue
        raw=comment_scope_source(project,h,p['target'])
        if raw.get('deepCoverage',{}).get('rootPaginationState')!='stalled' or raw.get('deepCoverage',{}).get('explicitEnd') is True:continue
        parents.append({'task_id':row['id'],'fault':json.loads(xp.dumps(parent_fault))})
        for source_hash in bridge.get('hashes',[]):
            raw=comment_scope_source(project,source_hash,p['target'])
            if source_hash not in sources:sources.append(source_hash)
            for item in raw.get('results',[]):
                cid=item.get('commentId')
                if (isinstance(cid,str) and re.fullmatch('[a-fA-F0-9]{16,40}',cid) and
                        not item.get('is_reply') and not item.get('parentCommentId') and cid not in roots):roots.append(cid)
    if not parents or not roots:return None
    if fault:
        inherited=comment_scope_source(project,fault.get('source_sha256'),p['target'])
        if inherited.get('deepCoverage',{}).get('rootPaginationState')!='stalled':return None
    scope_id=xp.digest({'note':p['target'],'root_ids':roots,'parent_faults':parents})
    return {'kind':'discovered_replies_v1','noteId':p['target'],'scopeId':scope_id,'taskId':task['id'],
        'rootIds':roots,'sourceHashes':sources,'parentFaults':parents,'maxRootScrolls':0}


def active_discovered_reply_scope(project,task,context=None):
    scope=task['payload'].get('bridge',{}).get('discovered_reply_scope',{})
    if scope.get('state')!='verified':return None
    context=context or discovered_reply_context(project,task)
    attempt=task['payload'].get('bridge',{}).get('repair_probes',{}).get(scope.get('probe_id'),{})
    recorded=attempt.get('scope_context',{})
    if (not context or recorded.get('scopeId')!=scope.get('scope_id') or attempt.get('scope')!='same_note_discovered_replies'
            or attempt.get('status')!='verified' or not attempt.get('verified')
            or recorded.get('noteId')!=context['noteId'] or recorded.get('taskId')!=task['id']
            or recorded.get('kind')!='discovered_replies_v1' or recorded.get('maxRootScrolls')!=0 or not recorded.get('rootIds')
            or not set(recorded.get('rootIds',[]))<=set(context['rootIds'])
            or not set(recorded.get('sourceHashes',[]))<=set(context['sourceHashes'])):return None
    ref=attempt.get('result_archive',{});path=Path(ref.get('file',''))
    expected=project.path/'bridge'/'local-fix-probes'/task['id']/scope.get('probe_id','')/'result.json'
    if (path!=expected or path.is_symlink() or not path.is_file() or
            hashlib.sha256(path.read_bytes()).hexdigest()!=ref.get('sha256')):return None
    result=json.loads(path.read_text())
    if (result.get('repair_verified') is not True or result.get('parent_repair_verified') is not False or
            result.get('discovered_reply_scope') is not True or not any(result.get(k) for k in
                ('new_reply_ids','changed_reply_ids','restored_known_replies'))):return None
    comment_scope_source(project,Path(result.get('snapshot','')).stem,recorded['noteId'])
    # Later root observations cannot expand this already authorised allowlist.
    return dict(recorded,phase='continuation')


def validate_discovered_reply_marker(project,task,marker):
    context=discovered_reply_context(project,task)
    if not isinstance(marker,dict) or not context:raise BridgeError('DISCOVERED_REPLY_AUTHORITY_REQUIRED')
    if marker.get('phase')=='continuation':
        active=active_discovered_reply_scope(project,task,context)
        if not active or any(marker.get(k)!=v for k,v in active.items()):raise BridgeError('DISCOVERED_REPLY_AUTHORITY_REQUIRED')
    elif marker.get('phase')=='probe':
        if any(marker.get(k)!=v for k,v in context.items()):raise BridgeError('DISCOVERED_REPLY_AUTHORITY_REQUIRED')
        attempt=task['payload'].get('bridge',{}).get('repair_probes',{}).get(marker.get('probeId'),{})
        if (attempt.get('scope')!='same_note_discovered_replies' or attempt.get('scope_context')!=context
                or attempt.get('maximum_acquisitions')!=2 or attempt.get('status') not in {'started','unverified'}
                or marker.get('acquisition')!=len(attempt.get('observations',[]))+1 or marker.get('acquisition') not in {1,2}):
            raise BridgeError('DISCOVERED_REPLY_AUTHORITY_REQUIRED')
    else:raise BridgeError('DISCOVERED_REPLY_AUTHORITY_REQUIRED')
    return marker


def reply_row_identity(row):
    candidates=[row.get('authorId'),row.get('userId')]
    match=re.search(r'/user/profile/([a-fA-F0-9]{16,40})(?:[/?#]|$)',row.get('authorHrefRaw',''))
    if match:candidates.append(match[1])
    known={v for v in candidates if isinstance(v,str) and re.fullmatch('[a-fA-F0-9]{16,40}',v)}
    uid=next(iter(known)) if len(known)==1 else ''
    return row.get('commentId'),row.get('parentCommentId'),uid


def reliable_reply_text(row):
    text=row.get('text');source=row.get('textSource','rendered_text')
    return (isinstance(text,str) and bool(text.strip()) and text!='[图片评论]' and
        not re.fullmatch(r'回复\s*.+?\s*[:：]',text.strip()) and
        not re.fullmatch(r'(?:原|该|此)?评论已(?:被)?删除[。.!！]?',re.sub(r'\s+','',text.strip())) and
        source in {'rendered_text','rendered_emoji_alt','rendered_emoji_accessible_label'})


def reply_scope_baseline(project,note):
    ids=set();bodies={};proofs=set()
    for row in project.db.execute("SELECT payload FROM tasks WHERE kind='native' AND json_extract(payload,'$.target')=? AND json_extract(payload,'$.stage') IN ('roots','replies')",(note,)):
        for h in json.loads(row[0]).get('bridge',{}).get('hashes',[]):
            raw=comment_scope_source(project,h,note)
            for item in raw.get('results',[]):
                cid,parent,uid=reply_row_identity(item)
                if isinstance(cid,str) and re.fullmatch('[a-fA-F0-9]{16,40}',cid):ids.add(cid)
                if item.get('is_reply') and parent and uid and reliable_reply_text(item):
                    bodies.setdefault((cid,parent,uid),set()).add((item['text'],item.get('textSource','rendered_text')))
            for thread in raw.get('deepCoverage',{}).get('threads',[]):
                proof=thread.get('lastReplyExpansion')
                if isinstance(proof,dict):proofs.add(xp.digest(proof))
    return {'ids':ids,'bodies':bodies,'proofs':proofs}


def discovered_reply_progress(context,before,previous,data):
    from xhs_browser import _observed_reply_intent_progress
    roots=set(context['rootIds']);added=[];changed=[];restored=[];rows=data.get('results',[])
    for row in rows:
        cid,parent,uid=reply_row_identity(row)
        if not row.get('is_reply') or parent not in roots or not isinstance(cid,str) or not re.fullmatch('[a-fA-F0-9]{16,40}',cid):continue
        if cid not in before['ids']:added.append(cid)
        old=before['bodies'].get((cid,parent,uid))
        if old and reliable_reply_text(row) and (row['text'],row.get('textSource','rendered_text')) not in old:changed.append(cid)
    for thread in data.get('deepCoverage',{}).get('threads',[]):
        proof=thread.get('lastReplyExpansion',{});root=thread.get('commentId')
        if (root not in roots or proof.get('progressBasis')!='known_reply_render_restored'
                or xp.digest(proof) in before['proofs']):continue
        joined=dict(thread,pendingReplyExpansion=dict(proof,state='click_submitted'))
        prior=previous.get('threads',{}).get('id:'+root,{})
        if _observed_reply_intent_progress(joined,rows,note_id=context['noteId'],previous=prior):
            # The claimed pre-click cache must also exist in immutable history.
            for cid in proof.get('restoredReplyIds',[]):
                body=proof.get('beforeKnownReplyBodyCaptures',{}).get(cid,{})
                if (body.get('text'),body.get('textSource')) in before['bodies'].get((cid,root,body.get('authorId')),set()):
                    restored.append({'root':root,'comment_id':cid,'clickedAt':proof.get('clickedAt'),'observedAt':proof.get('observedAt')})
    return {'new_reply_ids':sorted(set(added)),'changed_reply_ids':sorted(set(changed)),'restored_known_replies':restored}


def verify_discovered_replies(project,b,driver,task,basis,context):
    """Two leased observations; success authorises replies, never parent repair."""
    from xhs_batch import batch_lock
    with batch_lock(project.path/'bridge'/'runner'):
        task=project.task(task['id']);pr=task['payload'].setdefault('bridge',{})
        context=discovered_reply_context(project,task)
        if not context:raise BridgeError('DISCOVERED_REPLY_DEPENDENCY_REQUIRED')
        version=hashlib.sha256(b''.join(Path(__file__).with_name(n).read_bytes() for n in
            ('xhs_bridge.py','xhs_bridge_driver.mjs','deep_comments.mjs'))).hexdigest()
        probe=xp.digest({'collector':version,'scope':context['scopeId']})
        if probe in pr.get('repair_probes',{}):raise BridgeError('REPAIR_PROBE_ALREADY_USED')
        fault=json.loads(xp.dumps(pr.get('fault')));before=reply_scope_baseline(project,context['noteId'])
        directory=project.path/'bridge'/'local-fix-probes'/task['id']/probe
        def archive(name,value):
            directory.mkdir(parents=True,exist_ok=True);path=directory/name
            if path.exists() or path.is_symlink():raise BridgeError('REPAIR_PROBE_ARCHIVE_EXISTS')
            raw=xp.dumps(value).encode()
            with path.open('xb') as output:output.write(raw)
            return {'file':str(path),'sha256':hashlib.sha256(raw).hexdigest()}
        attempt={'at':legacy.now(),'basis':basis,'prior_fault':fault,'scope':'same_note_discovered_replies',
            'scope_context':context,'maximum_acquisitions':2,'interval_seconds':10,'status':'started','observations':[]}
        attempt['archive']=archive('attempt.json',dict(attempt,task_id=task['id'],collector_version=version))
        pr.setdefault('repair_probes',{})[probe]=attempt
        pr.setdefault('root_dependency_gap',{'kind':'parent_pagination_dependency','scopeId':context['scopeId'],
            'parent_faults':context['parentFaults'],'root_ids':context['rootIds'],'source_hashes':context['sourceHashes']})
        project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));project.db.commit()
        def persist():
            current=project.task(task['id']);state=current['payload']['bridge'];state['repair_probes'][probe]=attempt
            if attempt.get('verified'):
                if fault:
                    history=state.setdefault('reply_dependency_fault_history',[])
                    if not any(item.get('probe_id')==probe for item in history):history.append({'fault':fault,'probe_id':probe,
                        'evidence':attempt['observations'][-1]})
                    state.pop('fault',None)
                state['discovered_reply_scope']={'state':'verified','scope_id':context['scopeId'],'probe_id':probe}
                if attempt.get('terminal_gap'):state['discovered_reply_gap']=attempt['terminal_gap']
                else:state.pop('discovered_reply_gap',None)
                state['awaiting_review']=True
            elif fault:state['fault']=fault
            if attempt.get('requires_codex_review'):state['awaiting_review']=True
            state['collection_end']=False
            project.db.execute('UPDATE tasks SET payload=?,reason=?,updated=? WHERE id=?',
                (xp.dumps(current['payload']),'discovered_reply_progress_verified' if attempt.get('verified') else 'root_dependency_reply_probe_unverified',legacy.now(),task['id']));project.db.commit()
        for index in range(2):
            keys=None
            try:
                shared=project.path/'bridge'/'comment-progress'/(context['noteId']+'.json')
                previous=legacy.read_json(shared,{})
                marker=dict(context,phase='probe',probeId=probe,acquisition=index+1)
                with lease(project.path,b) as keys:
                    result=collect_one(project,b,driver,project.task(task['id']),discovered_reply_probe=marker)
                data=json.loads(Path(result['snapshot']).read_text());evidence=discovered_reply_progress(context,before,previous,data)
                verified=bool(any(evidence.values()))
                observation={'at':legacy.now(),'acquisition':index+1,'snapshot':result['snapshot'],
                    'verified':verified,**evidence}
                observation['archive']=archive(f'observation-{index+1}.json',observation)
                attempt['observations'].append(observation);attempt.update(verified=verified,
                    status='verified' if verified else 'unverified',requires_codex_review=verified,acquisitions=index+1)
                terminal=data.get('deepCoverage',{}).get('discoveredReplyScope',{}).get('state')
                if terminal in {'no_current_safe_controls','reply_actions_unconfirmed','known_parent_visit_pass_finished'}:
                    attempt['terminal_gap']={'reason':terminal,'source_snapshot':result['snapshot'],
                        'source_sha256':Path(result['snapshot']).stem,'scopeId':context['scopeId']}
                persist()
                if verified or index==1:
                    final=dict(result,repair_verified=verified,parent_repair_verified=False,
                        collection_attempts=index+1,new_comment_ids=len(evidence['new_reply_ids']),
                        requires_codex_review=verified,discovered_reply_scope=True,**evidence)
                    attempt['result_archive']=archive('result.json',final);persist();save_binding(project.path,b['binding_name'],b)
                    return final
            except Exception as exc:
                attempt.update(status='failed',verified=False,error=getattr(exc,'code',type(exc).__name__))
                attempt['failure_archive']=archive('failure.json',{'at':legacy.now(),'error':attempt['error'],
                    'prior_fault':fault,'parent_dependency':context['parentFaults'],'no_automatic_retry':True})
                persist();save_failure(project.path,b['binding_name'],b,exc,keys);raise
            if index==0:time.sleep(10)


def comment_repair_evidence(project,note):
    """Only immutable rendered evidence advances a failed comment collector."""
    from xhs_rounds import semantic
    hashes=set()
    for row in project.db.execute("SELECT payload FROM tasks WHERE kind='native' AND json_extract(payload,'$.target')=?",(note,)):
        payload=json.loads(row[0])
        if payload.get('stage') in {'roots','replies'}:hashes.update(payload.get('bridge',{}).get('hashes',[]))
    ids=set();rendered=set();checkpoints=set();root_ids=set();root_ends=set()
    for h in hashes:
        if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
        path=project.path/'bridge'/'raw'/(h+'.json')
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=h:
            raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
        raw=json.loads(path.read_text())
        for row in raw.get('results',[]):
            cid=row.get('commentId')
            if not isinstance(cid,str) or not re.fullmatch('[a-fA-F0-9]{16,40}',cid):continue
            ids.add(cid)
            if isinstance(row.get('text'),str) and row['text'].strip():rendered.add(xp.digest({'id':cid,'body':semantic(row)}))
        coverage=raw.get('deepCoverage',{})
        observed_roots=set(coverage.get('observedRootIds',[])) | {t.get('commentId') for t in coverage.get('threads',[])}
        for row in raw.get('results',[]):
            cid=row.get('commentId')
            if cid in ids and cid in observed_roots and not row.get('is_reply') and not row.get('parentCommentId'):
                root_ids.add(cid)
        for thread in coverage.get('threads',[]):
            root=thread.get('rootKey')
            if not root or not thread.get('commentId'):continue
            if (thread.get('state')=='visible_replies_read' and thread.get('contentCaptured') is True
                    and thread.get('replyContentCaptured') is True and thread.get('replyIdentityComplete') is True
                    and not thread.get('pendingReplyExpansion') and not thread.get('scanIncomplete')
                    and not thread.get('identityAmbiguous') and not thread.get('replyBodyGaps')
                    and not thread.get('expanders') and not thread.get('unsafeExpanders')):
                checkpoints.add(xp.digest({'root':root,'completed_rendered_replies':True}))
            expansion=thread.get('lastReplyExpansion',{})
            before=set(expansion.get('beforeReplyIds',[]));after=set(expansion.get('observedReplyIds',[]))
            if (expansion.get('state')=='render_progress_observed' and
                    (after-before or expansion.get('observedVisibleReplies',0)>expansion.get('beforeVisibleReplies',0))):
                checkpoints.add(xp.digest({'root':root,'before_ids':sorted(before),'after_ids':sorted(after),
                    'before_count':expansion.get('beforeVisibleReplies'),'after_count':expansion.get('observedVisibleReplies')}))
        empty=coverage.get('emptyCommentState') or {}
        rendered_empty=(coverage.get('rootEndBasis')=='rendered_comment_empty_state'
            and empty.get('scope')=='rendered_comments_container_empty_state' and empty.get('acceptedAsEnd') is True
            and empty.get('visible') is True and not empty.get('loadingVisible') and bool(empty.get('text','').strip())
            and not (coverage.get('commentCountObservation',{}).get('latest') or {}).get('positiveTotalObserved')
            and not raw.get('results') and not coverage.get('observedRootIds'))
        if coverage.get('rootPaginationState')=='complete' and (coverage.get('explicitEnd') is True or rendered_empty):
            basis='explicit_rendered_root_end' if coverage.get('explicitEnd') is True else 'rendered_empty_comment_scope'
            checkpoints.add(xp.digest({'observed_root_end':basis}))
            root_ends.add(basis)
    if root_ids:
        root_ends.discard('rendered_empty_comment_scope')
        checkpoints.discard(xp.digest({'observed_root_end':'rendered_empty_comment_scope'}))
    return {'ids':ids,'rendered':rendered,'checkpoints':checkpoints,'hashes':hashes,'root_ids':root_ids,'root_ends':root_ends}


def verify_comment_local_fix(project,b,driver,task,basis):
    """One code-version probe, at most two paced acquisitions, never a retry loop."""
    from xhs_batch import batch_lock
    with batch_lock(project.path/'bridge'/'runner'):
        task=project.task(task['id']);progress=task['payload'].get('bridge',{})
        fault=json.loads(xp.dumps(progress.get('fault',{})))
        if (task['status']!='pending' or fault.get('code') not in {'DEEP_READ_FAILED','COMMENT_PAGINATION_STALLED',COMMENT_BODY_GAP_FAULT} or not fault.get('hold')):
            raise BridgeError('LOCAL_COMMENT_FAULT_REQUIRED')
        files=[Path(__file__),Path(__file__).with_name('xhs_bridge_driver.mjs'),Path(__file__).with_name('deep_comments.mjs')]
        version=hashlib.sha256(b''.join(file.read_bytes() for file in files)).hexdigest()
        if version in progress.get('repair_probes',{}):raise BridgeError('REPAIR_PROBE_ALREADY_USED')
        before=comment_repair_evidence(project,task['payload']['target'])
        directory=project.path/'bridge'/'local-fix-probes'/task['id']/version
        def archive(name,value):
            directory.mkdir(parents=True,exist_ok=True)
            path=directory/name;raw=xp.dumps(value).encode()
            if path.is_symlink() or path.exists():raise BridgeError('REPAIR_PROBE_ARCHIVE_EXISTS')
            with path.open('xb') as output:output.write(raw)
            return {'file':str(path),'sha256':hashlib.sha256(raw).hexdigest()}
        attempt={'at':legacy.now(),'basis':basis,'prior_fault':fault,'scope':'same_note_rendered_comments',
            'maximum_acquisitions':2,'interval_seconds':10,'status':'started','observations':[],
            'baseline':{key:{'count':len(value),'digest':xp.digest(sorted(value))} for key,value in before.items()}}
        attempt['archive']=archive('attempt.json',dict(attempt,task_id=task['id'],collector_version=version))
        progress.setdefault('repair_probes',{})[version]=attempt
        project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task['id']));project.db.commit()
        def persist(record,verified=False):
            current=project.task(task['id']);state=current['payload']['bridge']
            state['repair_probes'][version]=record
            if verified:
                state.pop('fault',None);state['awaiting_review']=True
            else:state['fault']=fault
            if record.get('requires_codex_review'):state['awaiting_review']=True
            project.db.execute('UPDATE tasks SET payload=?,reason=? WHERE id=?',
                (xp.dumps(current['payload']),'local_fix_verified' if verified else 'local_fix_unverified',task['id']));project.db.commit()
        for index in range(2):
            keys=None
            try:
                with lease(project.path,b) as keys:
                    marker=({'kind':'single_bounded_root_repair_v1','probeId':version,'noteId':task['payload']['target'],
                        'acquisition':index+1,'maxScrolls':1,'allowScroll':index==0}
                        if fault['code']=='COMMENT_PAGINATION_STALLED' else None)
                    result=collect_one(project,b,driver,project.task(task['id']),root_repair_probe=marker)
                after=comment_repair_evidence(project,task['payload']['target'])
                new_ids=len(after['ids']-before['ids']);new_rendered=len(after['rendered']-before['rendered'])
                new_checkpoints=len(after['checkpoints']-before['checkpoints'])
                verified=bool(new_ids or new_rendered or new_checkpoints)
                needs_review=verified
                new_roots=len(after['root_ids']-before['root_ids'])
                latest=json.loads(Path(result['snapshot']).read_text()).get('deepCoverage',{})
                if fault['code']=='COMMENT_PAGINATION_STALLED':
                    verified=bool(new_roots or after['root_ends']-before['root_ends'])
                if fault['code']==COMMENT_BODY_GAP_FAULT:
                    # New text elsewhere does not fix the held prefix-only floor.
                    verified=bool(verified and reviewed_comment_state(project,project.task(task['id']))['complete'])
                if latest.get('rootPaginationState')=='stalled':verified=False
                observation={'at':legacy.now(),'acquisition':index+1,'snapshot':result.get('snapshot'),
                    'new_comment_ids':new_ids,'new_rendered_versions':new_rendered,'new_verified_checkpoints':new_checkpoints,
                    'new_root_comment_ids':new_roots,
                    'verified':verified,'requires_codex_review':needs_review}
                observation['archive']=archive(f'observation-{index+1}.json',observation)
                attempt['observations'].append(observation)
                attempt.update(verified=verified,status='verified' if verified else 'unverified',acquisitions=index+1,
                    requires_codex_review=needs_review)
                persist(attempt,verified)
                if verified or index==1:
                    final=dict(result,repair_verified=verified,new_note_ids=0,scroll_advanced=False,new_comment_ids=new_ids,
                        new_root_comment_ids=new_roots,
                        new_rendered_versions=new_rendered,checkpoint_advanced=bool(new_checkpoints),
                        collection_attempts=index+1,requires_codex_review=needs_review,
                        awaiting_review=needs_review or result.get('awaiting_review',False),metrics=driver.metrics)
                    attempt['result_archive']=archive('result.json',final);persist(attempt,verified)
                    save_binding(project.path,b['binding_name'],b)
                    return final
            except Exception as exc:
                attempt.update(status='failed',verified=False,error=getattr(exc,'code',type(exc).__name__))
                attempt['failure_archive']=archive('failure.json',{'at':legacy.now(),'error':attempt['error'],'prior_fault':fault,
                    'acquisitions_completed':len(attempt['observations']),'no_automatic_retry':True})
                persist(attempt)
                save_failure(project.path,b['binding_name'],b,exc,keys)
                raise
            if index==0:time.sleep(10)


VISIBILITY_FAULTS={'TARGET_NOT_VISIBLE','TARGET_VISIBILITY_UNCONFIRMED'}


def visibility_probe_version():
    files=[Path(__file__),Path(__file__).with_name('xhs_bridge_driver.mjs'),Path(__file__).with_name('deep_comments.mjs')]
    return hashlib.sha256(b''.join(file.read_bytes() for file in files)).hexdigest()


def background_visibility_candidate(task,binding,version=None):
    """Only an unread-free, exact old visibility hold may receive one probe."""
    p=task['payload'];pr=p.get('bridge',{});fault=pr.get('fault',{})
    if (task['status']!='pending' or not background_text_allowed(binding,p.get('stage'))
            or fault.get('code') not in VISIBILITY_FAULTS or fault.get('hold') is not True
            or pr.get('awaiting_review') or set(pr.get('hashes',[]))-set(pr.get('reviewed_hashes',[]))):
        return False
    return (version or visibility_probe_version()) not in pr.get('visibility_repair_probes',{})


def collect_background_visibility_probe(project,binding,driver,task,basis):
    """Caller holds the ordinary account lease; journal before the sole acquisition."""
    task=project.task(task['id']);version=visibility_probe_version()
    if not background_visibility_candidate(task,binding,version):
        raise BridgeError('BACKGROUND_VISIBILITY_PROBE_NOT_ELIGIBLE')
    p=task['payload'];pr=p['bridge'];fault=json.loads(xp.dumps(pr['fault']))
    attempt={'at':legacy.now(),'basis':basis,'status':'started','maximum_acquisitions':1,
             'prior_fault':fault,'stage':p['stage'],'target':p['target'],
             'context_id':binding['context_id'],'account_uid':binding['account_uid']}
    pr.setdefault('visibility_repair_probes',{})[version]=attempt
    project.db.execute('UPDATE tasks SET payload=?,updated=? WHERE id=?',(xp.dumps(p),legacy.now(),task['id']))
    project.db.commit()

    def persist(record,verified=False):
        current=project.task(task['id']);state=current['payload']['bridge']
        state.setdefault('visibility_repair_probes',{})[version]=record
        if verified and state.get('fault')==fault:
            state.setdefault('resolved_visibility_faults',[]).append(
                {'at':legacy.now(),'collector_version':version,'prior_fault':state.pop('fault'),
                 'snapshot':record['snapshot'],'verification':record['verification']})
        elif not state.get('fault'):
            state['fault']=fault
        # Acquiring/repairing is never semantic reading, even for roots before END.
        state['awaiting_review']=bool(state.get('awaiting_review') or record.get('snapshot')
            or set(state.get('hashes',[]))-set(state.get('reviewed_hashes',[])))
        project.db.execute('UPDATE tasks SET payload=?,reason=?,updated=? WHERE id=?',
            (xp.dumps(current['payload']),'background_visibility_verified' if verified else 'background_visibility_unverified',
             legacy.now(),task['id']));project.db.commit()

    try:
        result=collect_one(project,binding,driver,project.task(task['id']))
        current=project.task(task['id']);state=current['payload']['bridge']
        source=Path(result.get('snapshot',''));h=source.stem
        if (source!=project.path/'bridge'/'raw'/(h+'.json') or h not in state.get('hashes',[])
                or source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h):
            raise BridgeError('IMMUTABLE_SOURCE_CHANGED')
        role='search' if p['stage']=='search' else 'detail'
        meta=verify_tab(driver,binding,role)
        expected=urlparse(observed_url(project.path,task));actual=urlparse(meta['url'])
        if (target_path(actual.path)!=target_path(expected.path)
                or p['stage']=='search' and parse_qs(actual.query).get('keyword')!=[p['target']]):
            raise BridgeError('TARGET_MISMATCH')
        # A preserved old visibility fault must not mask a newly observed deep gap.
        if p['stage'] in {'roots','replies'} and state.get('fault')==fault:
            comment_state=reviewed_comment_state(project,current)
            if comment_state and comment_state['terminal_body_gaps']:
                replacement={};apply_comment_collection_state(replacement,comment_state,source,h)
                state['fault']=replacement['fault']
            elif state.get('comments',{}).get('rootPaginationState')=='stalled':
                state['fault']={'code':'COMMENT_PAGINATION_STALLED','hold':True,'at':legacy.now(),
                    'recovery':{'exhausted':True},'source_snapshot':str(source),'source_sha256':h}
            project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(current['payload']),task['id']))
            project.db.commit()
        verified=state.get('fault')==fault
        attempt.update(status='verified' if verified else 'new_fault_preserved',verified=verified,
            snapshot=str(source),source_sha256=h,verification={'page':meta['page'],'url':meta['url'],
                'account_uid':binding['account_uid'],'visible':meta.get('visible'),'at':legacy.now()},
            requires_codex_review=True)
        persist(attempt,verified)
        return dict(result,repair_verified=verified,visibility_probe=True,awaiting_review=True,
                    collector_version=version,requires_codex_review=True)
    except BaseException as exc:
        attempt.update(status='failed',verified=False,error=getattr(exc,'code',type(exc).__name__),
                       failed_at=legacy.now(),no_automatic_retry=True)
        persist(attempt)
        raise


def verify_local_fix(project,name,task_id,basis):
    """One probe per changed collector; retain the fault until real progress exists."""
    db=xp.Project(project);b=load_binding(project,name);driver=Driver(b['runtime'])
    try:
        task=db.task(task_id);pr=task['payload'].get('bridge',{});fault=pr.get('fault',{})
        if fault.get('code') in VISIBILITY_FAULTS and background_text_allowed(b,task['payload'].get('stage')):
            if not basis:raise BridgeError('REPAIR_BASIS_REQUIRED')
            from xhs_batch import batch_lock
            with batch_lock(db.path/'bridge'/'runner'):
                with lease(project,b) as keys:
                    try:result=collect_background_visibility_probe(db,b,driver,task,basis)
                    except Exception as exc:save_failure(project,name,b,exc,keys);raise
            save_binding(project,name,b)
            return dict(result,metrics=driver.metrics)
        if task['payload'].get('stage') in {'roots','replies'}:
            if not basis:raise BridgeError('REPAIR_BASIS_REQUIRED')
            context=discovered_reply_context(db,task)
            if context:return verify_discovered_replies(db,b,driver,task,basis,context)
            return verify_comment_local_fix(db,b,driver,task,basis)
        if task['payload'].get('stage') not in {'search','author'} or fault.get('code') not in LOCAL_FAULTS|{'PAGINATION_STALLED'}:
            raise BridgeError('LOCAL_PAGINATION_FAULT_REQUIRED')
        if not basis:raise BridgeError('REPAIR_BASIS_REQUIRED')
        version=hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('xhs_bridge_driver.mjs').read_bytes()).hexdigest()
        attempts=pr.setdefault('repair_probes',{})
        if version in attempts:raise BridgeError('REPAIR_PROBE_ALREADY_USED')
        attempts[version]={'at':legacy.now(),'basis':basis,'prior_fault':fault}
        db.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(task['payload']),task_id));db.db.commit()
        oldtop=pr.get('cursor',{}).get('scrollTop',0);oldids=set(pr.get('observed_note_ids',[]))
        with lease(project,b) as keys:
            try:r=collect_one(db,b,driver,task,force_advance=True)
            except Exception as exc:save_failure(project,name,b,exc,keys);raise
        current=db.task(task_id);new=current['payload']['bridge']
        moved=new.get('cursor',{}).get('scrollTop',0)>oldtop
        added=len(set(new.get('observed_note_ids',[]))-oldids)
        verified=moved or added>0 or new.get('collection_end',False)
        new['repair_probes'][version].update(verified=bool(verified),new_note_ids=added,scroll_advanced=moved,snapshot=r['snapshot'])
        if verified:new.pop('fault',None)
        else:new['fault']=fault
        db.db.execute('UPDATE tasks SET payload=?,reason=? WHERE id=?',(xp.dumps(current['payload']),'local_fix_verified' if verified else 'local_fix_unverified',task_id));db.db.commit()
        save_binding(project,name,b)
        return dict(r,repair_verified=bool(verified),new_note_ids=added,scroll_advanced=moved,metrics=driver.metrics)
    finally:db.close();driver.close()


def repair_pagination(project,b,driver,task):
    """One persisted activation and reload per stalled cycle, under the same account lease."""
    p=task['payload'];progress=p['bridge'];fault=progress['fault'];rec=fault.setdefault('recovery',{})
    role='search' if p['stage']=='search' else 'detail'
    url=observed_url(project.path,task);args={'stage':p['stage'],'expectedPath':urlparse(url).path}
    if p['stage']=='search':args['query']=p['target']
    url,role,args=prepare_target(project,b,driver,task,text_only=True)
    if p['stage']=='search':
        diagnostic=driver.call('scroll_diagnose',b,role,**args)
        legacy.write_json(project.path/'bridge'/('diagnostic-'+task['id']+'.json'),diagnostic)
    recovery_actions=('reload',) if args.get('backgroundText') is True else ('activate','reload')
    for action in recovery_actions:
        if rec.get(action):continue
        rec[action]=legacy.now()
        project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(p),task['id']));project.db.commit()
        driver.call(action,b,role);time.sleep(1)
        if p['stage']=='search':
            label={'general':'综合','latest':'最新','popular':'最多点赞'}[p['sort']]
            check=ready_capture(project,b,driver,role,args)
            if label not in check.get('selected',[]):
                for attempt in range(2):
                    action=driver.call('advance',b,role,sort=p['sort'],**args)
                    time.sleep(1)
                    check=ready_capture(project,b,driver,role,args)
                    if label in check.get('selected',[]) or action.get('action')=='sort_clicked':break
                if label not in check.get('selected',[]):raise BridgeError('SORT_UNCONFIRMED')
        driver.call('advance',b,role,dismissFilters=True,**args)
        ready_capture(project,b,driver,role,args)
        driver.call('advance',b,role,resumeTop=progress.get('cursor',{}).get('scrollTop',0),anchor=progress.get('cursor',{}).get('lastNoteId'),**args)
        driver.call('advance',b,role,**args);time.sleep(1)
        snap=ready_capture(project,b,driver,role,args)
        store_capture(project,project.task(task['id']),snap)
        fresh=project.task(task['id'])
        if not fresh['payload']['bridge'].get('fault'):return {'recovered':True,'task_id':task['id']}
    current=project.task(task['id']);current['payload']['bridge']['fault']['recovery']['exhausted']=True
    project.db.execute('UPDATE tasks SET payload=? WHERE id=?',(xp.dumps(current['payload']),task['id']));project.db.commit()
    return {'recovered':False,'task_id':task['id'],'reason':'PAGINATION_STALLED'}


def observed_comment_ids(project,note):
    """Cumulative real comment IDs across this note's root/reply task scopes."""
    ids=set()
    for row in project.db.execute("SELECT payload FROM tasks WHERE kind='native' AND json_extract(payload,'$.target')=? AND json_extract(payload,'$.stage') IN ('roots','replies')",(note,)):
        payload=json.loads(row['payload'])
        ids.update(cid for cid in payload.get('bridge',{}).get('comments',{}).get('observedCommentIds',[]) if isinstance(cid,str) and cid)
    return ids


def work(project,name,units=10,max_seconds=300,interval=10,task_ids=None,emit=None,review_limit=20,search_pages=5,round_id=None,home_restore_authorization=None,**unused):
    from xhs_batch import batch_lock,select_ready,routing_group
    import xhs_rounds as rounds
    wall_started=time.monotonic()
    if not 1<=units<=100 or not 1<=max_seconds<=3600 or interval<3:raise BridgeError('INVALID_BUDGET')
    b=load_binding(project,name);driver=Driver(b['runtime']);started=time.monotonic();done=0
    if home_restore_authorization:
        b['allow_home_restore']={'authorization':home_restore_authorization,'at':legacy.now()};save_binding(project,name,b)
    result={'event':'checkpoint','reason':'batch_budget','processed_units':0};search_units=0
    root=Path(project)/'bridge';pause=root/'PAUSE';state=legacy.read_json(root/'scheduler.json',{})
    if pause.exists():driver.close();return {'event':'paused','reason':'requested','processed_units':0}
    with batch_lock(root/'runner'):
        db=xp.Project(project);rounds.schema(db)
        try:
            allowed=set(rounds.task_ids(db,round_id)) if round_id else None
            while done<units and time.monotonic()-started<max_seconds:
                if round_id:allowed=set(rounds.task_ids(db,round_id))
                if pause.exists():result.update(event='paused',reason='requested');break
                # Project.task normally recomputes all event health. Share one
                # explicit snapshot for this scheduling decision only; fresh
                # browser/account leases remain mandatory for every unit.
                dispatch=native.dispatch_snapshot(db)
                allrows=[db.task(r[0],native_dispatch=dispatch) for r in db.db.execute("SELECT id FROM tasks WHERE kind='native'")]
                if round_id:
                    routes=rounds.scheduler_routes(db,round_id,tasks=allrows,native_dispatch=dispatch)
                    for t in allrows:t['payload']['routing_doctors']=routes.get(t['id'],[])
                selection_ids=list(task_ids) if task_ids is not None else None
                if selection_ids is not None:
                    for t in allrows:
                        if t['id'] in task_ids and t['payload'].get('stage')=='replies':
                            deps=[x['id'] for x in allrows if x['payload'].get('stage')=='roots' and x['payload'].get('target')==t['payload'].get('target') and x['status']=='pending']
                            for dep in deps:
                                if dep not in selection_ids:selection_ids.insert(selection_ids.index(t['id']),dep)
                rows=[]
                for t in allrows:
                    p=t['payload']
                    if t['status']!='pending' or p.get('stage') not in STAGES:continue
                    if allowed is not None and t['id'] not in allowed:continue
                    if selection_ids is not None and t['id'] not in selection_ids:continue
                    if p['stage'] in {'roots','replies'}:t=sync_shared_comment_stall(db,t)
                    p=t['payload'];pr=p.get('bridge',{});fault=pr.get('fault',{})
                    reply_scope=active_discovered_reply_scope(db,t) if p['stage']=='replies' else None
                    diagnostic=capture_diagnostic_status(db,t)
                    if diagnostic['confirmed_hashes'] or diagnostic['invalid']:
                        pr['awaiting_review']=bool(set(pr.get('hashes',[]))-set(pr.get('reviewed_hashes',[]))-
                                                  set(diagnostic['confirmed_hashes']))
                    repair=fault.get('code')=='PAGINATION_STALLED' and not fault.get('recovery',{}).get('exhausted')
                    visibility_repair=background_visibility_candidate(t,b)
                    if fault.get('hold') and not (repair or visibility_repair):continue
                    if pr.get('awaiting_review') and not repair:continue
                    if reply_scope and pr.get('discovered_reply_gap'):continue
                    if p['stage']=='replies':
                        roots=[x for x in allrows if x['payload'].get('target')==p['target'] and x['payload'].get('stage')=='roots']
                        if not reply_scope and roots and roots[0]['status']!='complete' and not root_pagination_ended(roots[0]['payload'].get('bridge',{})):continue
                    rows.append(t)
                if (b.get('web_channel_authorization') or b.get('web_parent_authorization')) and not b.get('web_validation'):rows=[t for t in rows if t['payload']['stage']=='body']
                if not rows:
                    result.update(event='handoff',reason='codex_review_or_remaining_gaps');break
                task=select_ready(rows,state,selection_ids);unit_start=time.monotonic();calls_before=getattr(driver,'metrics',{}).get('calls',0)
                comment_ids_before=observed_comment_ids(db,task['payload']['target']) if task['payload']['stage'] in {'roots','replies'} else set()
                try:
                    if task['payload']['stage'] in {'images','video'}:
                        from xhs_bridge_media import collect_inventory
                        r=collect_inventory(db,name,task,driver)
                        b=load_binding(project,name)
                    else:
                        with lease(project,b) as keys:
                            try:
                                if background_visibility_candidate(task,b):
                                    r=collect_background_visibility_probe(db,b,driver,task,'Explicit background text mode; verify the saved visibility hold once')
                                elif task['payload'].get('bridge',{}).get('fault',{}).get('code')=='PAGINATION_STALLED':r=repair_pagination(db,b,driver,task)
                                else:r=collect_one(db,b,driver,task)
                            except Exception as exc:save_failure(project,name,b,exc,keys);raise
                            b['state']='ready';b['last_success']={'task':task['id'],'at':legacy.now()};save_binding(project,name,b)
                except legacy.CollectionBusy:
                    time.sleep(min(interval,5));continue
                except legacy.ResearchError as exc:
                    reason=getattr(exc,'code',b.get('last_error',{}).get('code','SHARED_STOP'))
                    if reason=='UNEXPECTED_TARGET_CHANGE' and b.get('allow_home_restore'):
                        role='search' if task['payload']['stage']=='search' else 'detail'
                        try:
                            with lease(project,b):restore_owned_home(project,name,b,driver,role)
                            r={'restored_original_target':True}
                        except legacy.ResearchError as restore_exc:
                            result.update(event='blocked',reason=getattr(restore_exc,'code','UNEXPECTED_TARGET_CHANGE'),task_id=task['id']);break
                    elif reason in LOCAL_FAULTS:
                        hold_task(db,task['id'],reason);r={'local_gap':reason}
                    else:result.update(event='blocked',reason=reason,task_id=task['id']);break
                done+=1
                db.db.execute('UPDATE tasks SET turn=turn+1 WHERE id=?',(task['id'],))
                group=routing_group(task)
                state['group_units']=state.get('group_units',0)+1 if state.get('active_group')==group else 1
                state['active_group']=group
                state['active_note']=task['payload'].get('target');legacy.write_json(root/'scheduler.json',state)
                before=task['payload'].get('bridge',{});after=db.task(task['id'])['payload'].get('bridge',{})
                new_notes=len(set(after.get('observed_note_ids',[]))-set(before.get('observed_note_ids',[])))
                new_comments=len(observed_comment_ids(db,task['payload']['target'])-comment_ids_before) if task['payload']['stage'] in {'roots','replies'} else 0
                metric={'seconds':time.monotonic()-unit_start,'browser_calls':getattr(driver,'metrics',{}).get('calls',0)-calls_before,'stage':task['payload']['stage'],'duplicate':r.get('duplicate',False) and not new_notes and not new_comments,'gap':r.get('local_gap'),'new_note_ids':new_notes,'new_unique_comments':new_comments,'metric_scope':'project_note_comment_ids_v2','note_id':task['payload']['target']}
                db.db.execute('INSERT INTO execution_metrics(task_id,body,at) VALUES(?,?,?)',(task['id'],rounds.packed(metric),legacy.now()));db.db.commit()
                if task['payload']['stage']=='search':search_units+=1
                if emit:emit({'event':'unit','task_id':task['id'],**r})
                if state['group_units']>=10 or search_units>=search_pages or done>=review_limit:
                    result.update(event='handoff',reason='codex_reading_checkpoint');break
                if done<units:time.sleep(interval)
            result.update(processed_units=done,actor='codex',requires_user_input=False,metrics=getattr(driver,'metrics',{}))
        except KeyboardInterrupt:
            legacy.write_json(pause,{'at':legacy.now(),'reason':'user_interrupted'});result.update(event='paused',reason='user_interrupted',processed_units=done)
        finally:db.close();driver.close()
    result['wall_seconds']=time.monotonic()-wall_started
    result['driver_metrics_scope']='driver_call_seconds_only'
    legacy.write_json(root/'handoff.json',result)
    if emit:emit(result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--project',required=True,type=Path)
    s=p.add_subparsers(dest='command',required=True)
    for name in ('doctor','profiles'):
        a=s.add_parser(name);a.add_argument('--runtime',required=True,type=Path)
        if name=='doctor':a.add_argument('--start-connector',action='store_true')
    a=s.add_parser('bind');a.add_argument('--runtime',required=True,type=Path)
    for name in ('binding','context-id','profile-label','account-uid','profile-guard-key','authorization'):a.add_argument('--'+name,required=True)
    a.add_argument('--web-authorization',type=Path)
    a=s.add_parser('fork-binding')
    for name in ('parent','binding','intent','authorization'):a.add_argument('--'+name,required=True)
    for name in ('status','recover','release'):
        a=s.add_parser(name);a.add_argument('--binding',required=True)
    a=s.add_parser('window-state');a.add_argument('--binding',required=True)
    a.add_argument('--role',choices=['search','detail'],required=True)
    a=s.add_parser('text-mode');a.add_argument('--binding',required=True)
    a.add_argument('--mode',choices=['background','foreground'],required=True)
    a=s.add_parser('review');a.add_argument('--task',required=True);a.add_argument('--receipt',required=True,type=Path)
    a=s.add_parser('confirm-capture-diagnostic');a.add_argument('--task',required=True);a.add_argument('--receipt',required=True,type=Path)
    a=s.add_parser('verify-local-fix');a.add_argument('--binding',required=True);a.add_argument('--task',required=True);a.add_argument('--basis',required=True)
    a=s.add_parser('attach-source');a.add_argument('--task',required=True);a.add_argument('--file',required=True,type=Path)
    a=s.add_parser('diagnose-error');a.add_argument('--binding',required=True);a.add_argument('--role',choices=['search','detail'],required=True);a.add_argument('--file',required=True,type=Path)
    a=p.parse_args()
    if a.command in {'doctor','profiles'}:result=Driver(legacy.read_json(a.runtime)).call(a.command,startConnector=getattr(a,'start_connector',False))
    elif a.command=='bind':result=bind(a.project,a.binding,legacy.read_json(a.runtime),a.context_id,a.profile_label,a.account_uid,a.profile_guard_key,a.authorization,a.web_authorization)
    elif a.command=='fork-binding':result=fork_binding(a.project,a.parent,a.binding,a.intent,a.authorization)
    elif a.command=='status':
        b=load_binding(a.project,a.binding);result={k:b.get(k) for k in ('state','profile_label','context_id','account_uid','session','tabs','last_error','last_success')}
    elif a.command=='window-state':result=window_state(a.project,a.binding,a.role)
    elif a.command=='text-mode':result=text_mode(a.project,a.binding,a.mode)
    elif a.command=='verify-local-fix':result=verify_local_fix(a.project,a.binding,a.task,a.basis)
    elif a.command=='attach-source':result=attach_source(a.project,a.task,a.file)
    elif a.command=='diagnose-error':result=diagnose_error(a.project,a.binding,a.role,a.file)
    elif a.command=='confirm-capture-diagnostic':
        # Open an existing DB without Project's schema/metadata migration. This
        # command owns only its diagnostic ledger and the derived handoff flag.
        path=a.project.resolve();database=path/'project.sqlite3'
        if not database.is_file() or database.is_symlink():raise BridgeError('EXISTING_PROJECT_REQUIRED')
        db=xp.Project.__new__(xp.Project);db.path=path
        db.db=sqlite3.connect(database.as_uri()+'?mode=rw',uri=True);db.db.row_factory=sqlite3.Row
        try:result=confirm_capture_diagnostic(db,a.task,a.receipt)
        finally:db.close()
    elif a.command=='review':
        db=xp.Project(a.project)
        try:result=review(db,a.task,a.receipt)
        finally:db.close()
    else:result=recover(a.project,a.binding,a.command=='release')
    print(json.dumps({'ok':True,'data':result},ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except legacy.ResearchError as e:
        print(json.dumps({'ok':False,'code':getattr(e,'code','SHARED_STOP' if isinstance(e,legacy.StopRun) else 'BRIDGE_ERROR'),'reason':str(e)}));raise SystemExit(1)
