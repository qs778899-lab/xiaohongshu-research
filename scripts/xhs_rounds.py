"""Additive fixed-scope rounds, delta review, evidence export. No network or auto attribution."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def now(): return datetime.now(timezone.utc).isoformat()
def packed(v): return json.dumps(v, ensure_ascii=False, sort_keys=True)
def digest(v): return hashlib.sha256(packed(v).encode()).hexdigest()


def schema(p):
    p.db.executescript('''
    CREATE TABLE IF NOT EXISTS research_rounds(id TEXT PRIMARY KEY, config TEXT NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS round_tasks(round_id TEXT NOT NULL,task_id TEXT NOT NULL,disposition TEXT NOT NULL DEFAULT 'included',basis TEXT NOT NULL DEFAULT '{}',PRIMARY KEY(round_id,task_id));
    CREATE TABLE IF NOT EXISTS entity_versions(note_id TEXT NOT NULL,entity_id TEXT NOT NULL,version TEXT NOT NULL,body TEXT NOT NULL,source TEXT NOT NULL,locator TEXT NOT NULL,reviewed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(note_id,entity_id,version));
    CREATE TABLE IF NOT EXISTS review_packets(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,body TEXT NOT NULL,created TEXT NOT NULL,committed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS media_inventory(note_id TEXT NOT NULL,media_key TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'uninspected',PRIMARY KEY(note_id,media_key));
    CREATE TABLE IF NOT EXISTS evidence_versions(id TEXT NOT NULL,version TEXT NOT NULL,body TEXT NOT NULL,imported TEXT NOT NULL,current INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(id,version));
    CREATE TABLE IF NOT EXISTS execution_metrics(id INTEGER PRIMARY KEY,task_id TEXT,body TEXT NOT NULL,at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS round_entities(round_id TEXT NOT NULL,note_id TEXT NOT NULL,entity_id TEXT NOT NULL,version TEXT NOT NULL,disposition TEXT NOT NULL,basis TEXT NOT NULL,PRIMARY KEY(round_id,note_id,entity_id,version));
    CREATE TABLE IF NOT EXISTS queue_audit_reviews(task_id TEXT NOT NULL,source_hash TEXT NOT NULL,body TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(task_id,source_hash));
    CREATE TABLE IF NOT EXISTS round_card_reads(round_id TEXT NOT NULL,note_id TEXT NOT NULL,words_hash TEXT NOT NULL,words TEXT NOT NULL,task_id TEXT NOT NULL,packet_id TEXT NOT NULL,event_id TEXT NOT NULL,source_hash TEXT NOT NULL,locator TEXT NOT NULL,proof TEXT NOT NULL,PRIMARY KEY(round_id,packet_id,event_id,source_hash,locator));
    CREATE INDEX IF NOT EXISTS round_card_reads_key ON round_card_reads(round_id,note_id,words_hash);
    CREATE TABLE IF NOT EXISTS round_card_indexed_packets(round_id TEXT NOT NULL,packet_id TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(round_id,packet_id,event_id));
    ''')
    p.db.commit()


def create(p, rid, cfg):
    schema(p)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', rid): raise ValueError('Invalid round ID')
    if p.db.execute('SELECT 1 FROM research_rounds WHERE id=?',(rid,)).fetchone(): raise ValueError('Round already exists')
    if not all(k in cfg for k in ('cutoff','doctors','employment_rule')):raise ValueError('Missing round scope fields')
    cutoff=datetime.fromisoformat(cfg['cutoff'].replace('Z','+00:00'))
    if not cutoff.tzinfo or not cfg.get('doctors') or not cfg.get('employment_rule'):
        raise ValueError('cutoff with timezone, doctors and employment_rule required')
    if p.db.execute('SELECT 1 FROM research_rounds WHERE id=?',(rid,)).fetchone(): raise ValueError('Round already exists')
    tasks=[p.task(r[0]) for r in p.db.execute('SELECT id FROM tasks')]
    ids=cfg.get('task_ids',[t['id'] for t in tasks])
    byid={t['id']:t for t in tasks}
    if any(t not in byid for t in ids): raise ValueError('Unknown task ID')
    frozen=dict(cfg,query_matrix=[{'task_id':i,'query':byid[i]['payload'].get('target'), 'sort':byid[i]['payload'].get('sort')} for i in ids if byid[i]['payload'].get('stage')=='search'])
    p.db.execute('INSERT INTO research_rounds VALUES(?,?,?,?)',(rid,packed(frozen),now(),'active'))
    p.db.executemany('INSERT INTO round_tasks(round_id,task_id) VALUES(?,?)',[(rid,i) for i in ids]);p.db.commit()
    return status(p,rid)


def round_config(p,rid):
    schema(p);r=p.db.execute('SELECT config FROM research_rounds WHERE id=?',(rid,)).fetchone()
    if not r: raise ValueError('Unknown round')
    return json.loads(r[0])


def enroll(p,rid,task_id,published_at=None,basis=None):
    cfg=round_config(p,rid);t=p.task(task_id);stage=t['payload'].get('stage')
    if not t: raise ValueError('Unknown task')
    state=p.db.execute('SELECT state FROM research_rounds WHERE id=?',(rid,)).fetchone()[0]
    if state!='active': raise ValueError('Round is closed')
    disposition='included'
    if stage=='search' and task_id not in {x['task_id'] for x in cfg['query_matrix']}:
        disposition='next_round'
    elif published_at:
        dt=datetime.fromisoformat(published_at.replace('Z','+00:00'))
        if not dt.tzinfo: raise ValueError('Publication time needs timezone')
        if dt>datetime.fromisoformat(cfg['cutoff'].replace('Z','+00:00')): disposition='next_round'
    b={'published_at':published_at,'date_unknown':not bool(published_at),'source':basis}
    old=p.db.execute('SELECT disposition,basis FROM round_tasks WHERE round_id=? AND task_id=?',(rid,task_id)).fetchone()
    if old and not published_at:
        return {'task_id':task_id,'disposition':old['disposition'],'date_unknown':json.loads(old['basis']).get('date_unknown',True)}
    p.db.execute('INSERT INTO round_tasks VALUES(?,?,?,?) ON CONFLICT(round_id,task_id) DO UPDATE SET disposition=excluded.disposition,basis=excluded.basis',(rid,task_id,disposition,packed(b)));p.db.commit()
    return {'task_id':task_id,'disposition':disposition,'date_unknown':not bool(published_at)}


def task_ids(p,rid):
    round_config(p,rid)
    return [r[0] for r in p.db.execute("SELECT task_id FROM round_tasks WHERE round_id=? AND disposition='included'",(rid,))]


def inherit_tasks(p,parent_id,children):
    schema(p)
    for row in p.db.execute("SELECT rt.round_id FROM round_tasks rt JOIN research_rounds r ON r.id=rt.round_id WHERE rt.task_id=? AND rt.disposition='included' AND r.state='active'",(parent_id,)).fetchall():
        for child in children:enroll(p,row[0],child,basis={'parent_task':parent_id,'date_unknown':True})


def inherit_from_sources(p,children):
    if not p.db.execute("SELECT 1 FROM sqlite_master WHERE name='round_tasks'").fetchone():return
    # This is only a persisted-source index, not a dispatch/health decision.
    # Keep all kinds and stable ID order; enrollment below remains authoritative.
    payloads={r['id']:json.loads(r['payload']) for r in p.db.execute('SELECT id,payload FROM tasks ORDER BY id')}
    byhash={}
    for tid,payload in payloads.items():
        for h in payload.get('bridge',{}).get('hashes',[]):byhash.setdefault(h,[]).append(tid)
    for child in children:
        payload=payloads[child] if child in payloads else p.task(child)['payload']
        for source in payload.get('provenance',[]):
            for parent in byhash.get(source.get('sha256'),[]):
                if parent!=child:inherit_tasks(p,parent,[child])


def _report_tasks(p,rid=None):
    """One report's advisory health and task rows; never a collection authority."""
    schema(p)
    ids=task_ids(p,rid) if rid else [r[0] for r in p.db.execute('SELECT id FROM tasks')]
    import xhs_native as native
    dispatch=native.dispatch_snapshot(p)
    return [p.task(tid,native_dispatch=dispatch) for tid in ids]


def _audit_tasks(p,tasks):
    result=[];review_memo={}
    for t in tasks:
        tid=t['id'];d=t['payload'];b=d.get('bridge',{});reason=t.get('reason','')
        backlog=review_backlog(p,t,memo=review_memo)
        category='completed_record' if t['status']=='complete' else 'unprocessed'
        if b.get('fault',{}).get('hold'): category='execution_gap'
        elif b.get('awaiting_review') or backlog['pending_review_hash_count'] or backlog['pending_review_entity_version_count']: category='acquired_needs_review'
        elif t['status']!='complete' and b.get('collection_end'): category='acquisition_end_needs_receipt'
        elif t['status']!='complete' and d.get('stage')=='legacy_audit':category='legacy_claim_needs_source_review'
        reviewed=p.db.execute('SELECT body FROM queue_audit_reviews WHERE task_id=? ORDER BY at DESC LIMIT 1',(tid,)).fetchone()
        result.append({'task_id':tid,'stage':d.get('stage',t['kind']),'target':d.get('target',t['entity_key']), 'status':t['status'],'category':category,'reason':reason,'next':d.get('next'), 'sources':d.get('provenance',[]),'fault':b.get('fault'), 'completion_not_revalidated':t['status']=='complete','independent_audit':json.loads(reviewed[0]) if reviewed else None,**backlog})
    return {'items':result,'count':len(result),'dry_run':True,'semantic_decisions_required':True}


def audit(p,rid=None):
    """Classifies operational evidence, never invents a semantic exclusion or completion."""
    return _audit_tasks(p,_report_tasks(p,rid))


def import_audit(p,path):
    schema(p);path=Path(path);raw=path.read_bytes();report=json.loads(raw);items=report['items']
    for item in items:
        if not p.task(item['task_id']) or item.get('automatic_completion_allowed') is not False:raise ValueError('Audit must not authorize automatic completion')
        for support in item.get('reviewed_support',[]):
            expected=support.get('sha256_at_audit')
            if expected and hashlib.sha256(Path(support['path']).read_bytes()).hexdigest()!=expected:raise ValueError('Audit source changed')
        if not item.get('decision_basis') or not item.get('next_action'):raise ValueError('Audit reasoning required')
    h=hashlib.sha256(raw).hexdigest();archive=p.path/'audit-sources'/h;archive.parent.mkdir(exist_ok=True)
    if not archive.exists():archive.write_bytes(raw)
    if archive.read_bytes()!=raw:raise ValueError('Audit archive changed')
    for item in items:p.db.execute('INSERT OR IGNORE INTO queue_audit_reviews VALUES(?,?,?,?)',(item['task_id'],h,packed(dict(item,audit_source=str(archive))),now()))
    p.db.commit();return {'audit_items':len(items),'completion_status_changed':False,'archive':str(archive)}


def _status_tasks(p,rid,tasks,rows):
    cfg=round_config(p,rid);pending=[r for r in rows if r['status']!='complete']
    # Existing native validity remains the authority; invalid receipts must not close a round.
    import xhs_native as native
    invalid=[]
    for task in tasks:
        if task['status']=='complete' and task['kind']=='native':
            if not native.valid_completion(p,task):invalid.append(task['id'])
    stages={}
    for row,t in zip(rows,tasks):
        b=t['payload'].get('bridge',{});v=stages.setdefault(row['stage'],{'known':0,'complete':0,'acquired':0,'reviewed':0,'awaiting_review':0,'execution_gaps':0})
        v['known']+=1;v['complete']+=int(row['status']=='complete')
        v['acquired']+=int(bool(b.get('hashes')) or row['status']=='complete')
        v['reviewed']+=int(bool(b.get('reviewed_hashes')) or row['status']=='complete')
        v['awaiting_review']+=int(bool(b.get('awaiting_review')))
        v['execution_gaps']+=int(bool(b.get('fault',{}).get('hold')))
        for key in ('pending_review_hash_count','pending_review_entity_count','pending_review_entity_version_count'):
            v[key]=v.get(key,0)+row[key]
        v['pending_review_scopes']=v.get('pending_review_scopes',0)+int(bool(row['pending_review_hash_count'] or row['pending_review_entity_version_count']))
        v['review_source_gap_scopes']=v.get('review_source_gap_scopes',0)+int(bool(row['pending_review_source_gaps'] or row['pending_review_diagnostic_gaps']))
    ids={row['task_id'] for row in rows}
    metrics=[json.loads(r['body']) for r in p.db.execute('SELECT task_id,body FROM execution_metrics') if r['task_id'] in ids]
    efficiency={'measured_units':len(metrics),'seconds':sum(m.get('seconds',0) for m in metrics),'browser_calls':sum(m.get('browser_calls',0) for m in metrics),'duplicate_units':sum(bool(m.get('duplicate')) for m in metrics),'gap_units':sum(bool(m.get('gap')) for m in metrics)}
    efficiency['duplicate_unit_ratio']=efficiency['duplicate_units']/len(metrics) if metrics else None
    return {'round':rid,'state':p.db.execute('SELECT state FROM research_rounds WHERE id=?',(rid,)).fetchone()[0], 'cutoff':cfg['cutoff'],'known':len(rows),'marked_complete':len(rows)-len(pending),'pending':len(pending),'invalid_completion_tasks':invalid,'outcome':'remaining_gaps' if pending or invalid else 'requires_completion_validation','remaining':pending,'stages':stages,'efficiency':efficiency,'no_platform_recall_estimate':True,'pending_review_count_scope':'task_snapshot_references_and_semantic_entity_versions_not_patients'}


def status(p,rid):
    tasks=_report_tasks(p,rid)
    return _status_tasks(p,rid,tasks,_audit_tasks(p,tasks)['items'])


def close(p,rid):
    s=status(p,rid)
    if s['pending'] or s['invalid_completion_tasks']:return dict(s,closed=False)
    members=set(task_ids(p,rid));targets={p.task(i)['payload'].get('target') for i in members}
    deferred={r[0] for r in p.db.execute("SELECT task_id FROM round_tasks WHERE round_id=? AND disposition='next_round'",(rid,))}
    outside=[r[0] for r in p.db.execute("SELECT id FROM tasks WHERE status!='complete'") if r[0] not in members|deferred and p.task(r[0])['payload'].get('target') in targets and p.task(r[0])['payload'].get('stage')!='search']
    if outside:return dict(s,closed=False,reason='unenrolled_related_scopes',tasks=outside)
    import xhs_native as native
    coverage=native.coverage(p)
    if coverage.get('invalid_review_bindings'):return dict(s,closed=False,reason='invalid_review_bindings')
    p.db.execute("UPDATE research_rounds SET state='scope_processed' WHERE id=?",(rid,));p.db.commit()
    return dict(s,closed=True,outcome='defined_scope_processed',state='scope_processed')


def semantic(row):
    # Relative display time, likes and signed URLs do not change the words/attribution.
    v={k:row.get(k) for k in ('text','parentCommentId','replyToCommentId','replyToIdentity','reply_to','author','authorId','identityKind','is_reply')}
    uid=re.search(r'/user/profile/([a-f0-9]{24})',row.get('authorHrefRaw') or '')
    v['observed_author_uid']=uid[1] if uid else row.get('authorId')
    return v


def review_backlog(p,task,*,memo=None):
    """Read-only task-local backlog; independent of the batch handoff flag.

    Entity counts are semantic versions/IDs, not patients. Aggregate task counts
    are references and may overlap across roots/replies of the same note.
    """
    memo={} if memo is None else memo
    d=task['payload'];b=d.get('bridge',{});note=d.get('target')
    known=list(dict.fromkeys(b.get('hashes',[])))
    reviewed=set(b.get('reviewed_hashes',[]));diagnostics={'confirmed_hashes':[],'invalid':[]}
    if d.get('stage')=='images' and known:
        from xhs_bridge import capture_diagnostic_status
        diagnostics=capture_diagnostic_status(p,task)
    excluded=set(diagnostics['confirmed_hashes'])
    pending=set(known)-reviewed-excluded
    cache=memo.setdefault('review_sources',{});reading=memo.setdefault('reviewed_entities',{})
    note_key=(str(p.path),note)
    if note_key not in reading:
        exists=p.db.execute("SELECT 1 FROM sqlite_master WHERE name='entity_versions'").fetchone()
        reading[note_key]={(r[0],r[1]) for r in p.db.execute(
            'SELECT entity_id,version FROM entity_versions WHERE note_id=? AND reviewed=1',(note,))} if exists else set()
    read_versions=set(reading[note_key]);candidates=set();gaps=[]
    for h in known:
        if h in excluded:continue  # Same validated diagnostic exclusion as review-next.
        key=(str(p.path),h)
        if key not in cache:
            source=p.path/'bridge'/'raw'/(str(h)+'.json')
            try:
                if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):raise ValueError('invalid_snapshot_hash')
                if source.is_symlink() or not source.is_file():raise ValueError('source_missing_or_unsafe')
                raw=source.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=h:raise ValueError('source_hash_mismatch')
                data=json.loads(raw)
                if not isinstance(data,dict) or not isinstance(data.get('results',[]),list):raise ValueError('invalid_snapshot_shape')
                versions=set()
                for i,row in enumerate(data.get('results',[])):
                    if not isinstance(row,dict):raise ValueError('invalid_entity_shape')
                    entity=row.get('commentId') or f'uncertain:{h}:{i}'
                    versions.add((entity,digest(semantic(row))))
                cache[key]=(versions,None)
            except (OSError,ValueError,TypeError):
                cache[key]=(set(),'source_missing_changed_or_invalid')
        versions,error=cache[key]
        if error:gaps.append({'hash':h,'reason':error})
        if h in reviewed:read_versions.update(versions)
        if h in pending:candidates.update(versions)
    remaining=candidates-read_versions
    return {'pending_review_hash_count':len(pending),
        'pending_review_entity_count':len({entity for entity,_ in remaining}),
        'pending_review_entity_version_count':len(remaining),
        'pending_review_entity_counts_complete':not gaps,
        'pending_review_source_gaps':gaps,'pending_review_diagnostic_gaps':diagnostics['invalid'],
        'review_handoff_due':bool(b.get('awaiting_review')),
        'pending_review_count_scope':'task_snapshot_references_and_semantic_entity_versions_not_patients'}


def entity_disposition(body, cutoff):
    """Use only an observed absolute timestamp; never date relative display text by guess."""
    value=body.get('published_at') or body.get('publishedAt')
    if not isinstance(value,str):return 'date_unknown'
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        end=datetime.fromisoformat(cutoff.replace('Z','+00:00'))
        if not dt.tzinfo:return 'date_unknown'
        return 'next_round' if dt>end else 'included'
    except ValueError:return 'date_unknown'


def scheduler_routes(p,rid,*,tasks=None,native_dispatch=None):
    """Discovery cohorts only, never a doctor's attribution to a patient comment."""
    cfg=round_config(p,rid);included=task_ids(p,rid)
    supplied={t['id']:t for t in tasks} if tasks is not None else {}
    # Reuse exactly this decision's task rows. Missing non-native round members
    # still load normally, preserving existing routing/provenance semantics.
    tasks=[supplied[tid] if tid in supplied else
           p.task(tid,native_dispatch=native_dispatch) if native_dispatch is not None else p.task(tid)
           for tid in included]
    names=[str(x) for x in cfg['doctors']];routes={};byhash={};peers={}
    for t in tasks:
        d=t['payload'];peers.setdefault(d.get('target'),[]).append(t['id']);label=d.get('target','') if d.get('stage')=='search' else ''
        routes[t['id']]={name for name in names if name in label}
        for h in d.get('bridge',{}).get('hashes',[]):byhash.setdefault(h,set()).add(t['id'])
    # Propagate discovery provenance across query -> note -> author/media; finite closure.
    changed=True
    while changed:
        changed=False
        for t in tasks:
            before=set(routes[t['id']]);d=t['payload']
            for peer_id in peers.get(d.get('target'),[]):routes[t['id']].update(routes[peer_id])
            for source in d.get('provenance',[]):
                for parent in byhash.get(source.get('sha256'),[]):routes[t['id']].update(routes[parent])
            if before!=routes[t['id']]:changed=True
    bytarget={}
    for t in tasks:bytarget.setdefault(t['payload'].get('target'),set()).update(routes[t['id']])
    return {t['id']:sorted(bytarget.get(t['payload'].get('target'),set())) for t in tasks}


def index_task(p,task):
    schema(p);d=task['payload'];b=d.get('bridge',{});note=d['target'];rows=[]
    for h in b.get('hashes',[]):
        source=p.path/'bridge'/'raw'/(h+'.json')
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h:raise ValueError('Source hash mismatch')
        snap=json.loads(source.read_text());reviewed=h in b.get('reviewed_hashes',[])
        for i,r in enumerate(snap.get('results',[])):
            entity=r.get('commentId') or f'uncertain:{h}:{i}';v=digest(semantic(r));body=dict(r,note_id=note,source_hash=h)
            p.db.execute('INSERT OR IGNORE INTO entity_versions VALUES(?,?,?,?,?,?,?)',(note,entity,v,packed(body),str(source),f'results/{i}',int(reviewed)))
            # A later observation can supply an absolute date without changing
            # semantic text. Preserve its location while avoiding needless reread.
            if isinstance(body.get('published_at') or body.get('publishedAt'),str):
                current=p.db.execute('SELECT body FROM entity_versions WHERE note_id=? AND entity_id=? AND version=?',(note,entity,v)).fetchone()
                if current and not (json.loads(current[0]).get('published_at') or json.loads(current[0]).get('publishedAt')):
                    p.db.execute('UPDATE entity_versions SET body=?,source=?,locator=? WHERE note_id=? AND entity_id=? AND version=?',(packed(body),str(source),f'results/{i}',note,entity,v))
            if reviewed:p.db.execute('UPDATE entity_versions SET reviewed=1 WHERE note_id=? AND entity_id=? AND version=?',(note,entity,v))
        for r in snap.get('results',[]):
            for i,m in enumerate(r.get('images',[])):
                key=f"comment:{r.get('commentId') or h}:{i}"
                p.db.execute('INSERT OR IGNORE INTO media_inventory VALUES(?,?,?,?,?)',(note,key,'comment_image',packed({'source':str(source),'commentId':r.get('commentId'),'media':m}),'uninspected'))
    p.db.commit()


CARD_TEXT_FIELDS=('title','text','body','description','desc','content','excerpt')
CARD_WORD_FIELDS=CARD_TEXT_FIELDS+('author','authorId','author_id','authorHrefRaw','authorUrl','author_url')


def card_review_key(card):
    from urllib.parse import urlparse
    if not isinstance(card,dict):return None
    try:
        url=urlparse(card.get('url') or '')
        match=re.fullmatch(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/([a-f0-9]{24})/?',url.path)
        if (url.scheme!='https' or url.hostname not in {'www.xiaohongshu.com','xiaohongshu.com'} or
                url.port or url.username or url.password or not match):return None
    except (ValueError,TypeError):return None
    return match[1],packed({k:card[k] for k in CARD_WORD_FIELDS if k in card})


def _card_has_words(card):
    return isinstance(card,dict) and any(isinstance(card.get(k),str) and card[k].strip() for k in CARD_TEXT_FIELDS)


def _checked_json(path,sha=None):
    if path.is_symlink() or not path.is_file():return None
    try:
        raw=path.read_bytes()
        if sha and hashlib.sha256(raw).hexdigest()!=sha:return None
        value=json.loads(raw)
        return (value,hashlib.sha256(raw).hexdigest()) if isinstance(value,dict) else None
    except (OSError,ValueError):return None


def _card_read_source(p,rid,tid,pid,eid,h,memo):
    """A cache never replaces current source, committed packet and receipt proof."""
    cache_key=(rid,tid,pid,eid,h)
    if cache_key in memo:return memo[cache_key]
    memo[cache_key]=None
    if not all(isinstance(v,str) and re.fullmatch('[a-f0-9]{64}',v) for v in (pid,eid,h)):return None
    if not p.db.execute("SELECT 1 FROM round_tasks WHERE round_id=? AND task_id=? AND disposition='included'",(rid,tid)).fetchone():return None
    task=p.db.execute('SELECT kind,payload FROM tasks WHERE id=?',(tid,)).fetchone()
    if not task or task['kind']!='native':return None
    payload=json.loads(task['payload'])
    if payload.get('stage')!='search' or h not in payload.get('bridge',{}).get('reviewed_hashes',[]):return None
    row=p.db.execute('SELECT body,committed FROM review_packets WHERE id=? AND task_id=?',(pid,tid)).fetchone()
    if not row or row['committed']!=1:return None
    packet=json.loads(row['body'])
    if packet.get('round')!=rid or packet.get('stage')!='search' or packet.get('id')!=pid or digest({k:v for k,v in packet.items() if k!='id'})!=pid:return None
    if ((p.path/'bridge').is_symlink() or (p.path/'native-sources').is_symlink() or
            any((p.path/'bridge'/part).is_symlink() for part in ('raw','review-packets'))):return None
    packet_file=p.path/'bridge'/'review-packets'/(pid+'.json');packet_json=_checked_json(packet_file)
    source=p.path/'bridge'/'raw'/(h+'.json');source_json=_checked_json(source,h)
    if not packet_json or packet_json[0]!=packet or not source_json:return None
    snapshot=next((s for s in packet.get('snapshots',[]) if s.get('hash')==h and s.get('file')==str(source)),None)
    if not snapshot or not isinstance(snapshot.get('reading',{}).get('cards'),list):return None
    event=p.db.execute('SELECT * FROM native_events WHERE id=? AND task_id=?',(eid,tid)).fetchone()
    if not event:return None
    receipt=json.loads(event['receipt'])
    if (receipt.get('review_packet_id')!=pid or receipt.get('reviewed') is not True or not receipt.get('summary') or
            receipt.get('content_observed') is False or receipt.get('outcome') not in {'ready','offline'} or
            not isinstance(receipt.get('bridge_snapshot_hashes'),list) or h not in receipt['bridge_snapshot_hashes']):return None
    if (not re.fullmatch(r'native-sources/[a-f0-9]{64}',event['artifact']) or
            not re.fullmatch(r'native-sources/[a-f0-9]{64}',receipt.get('receipt_archive',''))):return None
    import xhs_native as native
    if not native.valid_event_source(p,event):return None
    archived=_checked_json(p.path/receipt['receipt_archive'],receipt.get('receipt_sha256'))
    if (not archived or {k:v for k,v in receipt.items() if k not in {'receipt_archive','receipt_sha256'}}!=archived[0] or
            digest([tid,receipt.get('receipt_sha256')])!=eid):return None
    if not any(e['id']==eid for e,_ in native.effective_events(p,tid)[0]):return None
    raw=source_json[0]
    try:native.validate_url({'payload':payload},raw.get('url',''))
    except Exception:return None
    proof={'round':rid,'task_id':tid,'query':payload['target'],'sort':payload.get('sort'),
        'packet_id':pid,'packet_file':str(packet_file),'packet_sha256':packet_json[1],
        'event_id':eid,'receipt_file':str(p.path/receipt['receipt_archive']),'receipt_sha256':receipt['receipt_sha256'],
        'source_hash':h,'source_file':str(source)}
    result={'proof':proof,'raw':raw,'delivered':{key for card in snapshot['reading']['cards'] if (key:=card_review_key(card)) is not None}}
    memo[cache_key]=result
    return result


def _sync_round_card_reads(p,rid,memo):
    if not p.db.execute("SELECT 1 FROM sqlite_master WHERE name='native_events'").fetchone():return
    rows=p.db.execute('''SELECT rp.id,rp.task_id,rp.body,e.id event_id FROM review_packets rp
      JOIN tasks t ON t.id=rp.task_id JOIN round_tasks rt ON rt.task_id=t.id AND rt.round_id=? AND rt.disposition='included'
      JOIN native_events e ON e.task_id=t.id AND json_extract(e.receipt,'$.review_packet_id')=rp.id
      WHERE rp.committed=1 AND json_extract(rp.body,'$.round')=? AND json_extract(t.payload,'$.stage')='search'
      AND json_extract(e.receipt,'$.reviewed')=1 AND NOT EXISTS(
        SELECT 1 FROM round_card_indexed_packets i WHERE i.round_id=? AND i.packet_id=rp.id AND i.event_id=e.id)''',(rid,rid,rid)).fetchall()
    for row in rows:
        packet=json.loads(row['body']);all_valid=True
        event=p.db.execute('SELECT receipt FROM native_events WHERE id=?',(row['event_id'],)).fetchone()
        attested=json.loads(event[0]).get('bridge_snapshot_hashes',[])
        if not isinstance(attested,list):continue
        for snapshot in packet.get('snapshots',[]):
            h=snapshot.get('hash')
            if h not in attested:continue
            context=_card_read_source(p,rid,row['task_id'],row['id'],row['event_id'],h,memo)
            if not context:all_valid=False;continue
            for index,card in enumerate(context['raw'].get('cards',[])):
                key=card_review_key(card)
                if key is None or key not in context['delivered'] or not _card_has_words(card):continue
                p.db.execute('INSERT OR IGNORE INTO round_card_reads VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (rid,key[0],hashlib.sha256(key[1].encode()).hexdigest(),key[1],row['task_id'],row['id'],row['event_id'],h,'cards/'+str(index),packed(context['proof'])))
        if all_valid:p.db.execute('INSERT OR IGNORE INTO round_card_indexed_packets VALUES(?,?,?)',(rid,row['id'],row['event_id']))
    p.db.commit()


def _card_at(raw,locator):
    match=re.fullmatch(r'cards/(0|[1-9][0-9]*)',locator or '')
    cards=raw.get('cards',[])
    return cards[int(match[1])] if match and isinstance(cards,list) and int(match[1])<len(cards) else None


def _shared_card_read(p,rid,key,memo,lookup):
    if key in lookup:return lookup[key]
    lookup[key]=None
    for row in p.db.execute('SELECT * FROM round_card_reads WHERE round_id=? AND note_id=? AND words_hash=? AND words=? ORDER BY rowid DESC',
            (rid,key[0],hashlib.sha256(key[1].encode()).hexdigest(),key[1])):
        context=_card_read_source(p,rid,row['task_id'],row['packet_id'],row['event_id'],row['source_hash'],memo)
        if (context and context['proof']==json.loads(row['proof']) and key in context['delivered'] and
                card_review_key(_card_at(context['raw'],row['locator']))==key):
            lookup[key]=(context['proof'],row['locator']);break
    return lookup[key]


def _validate_shared_card_reads(p,packet):
    shared=packet.get('shared_card_reviews')
    if not shared:return
    rid=packet.get('round');memo={}
    if (not rid or shared.get('round')!=rid or packet.get('stage')!='search' or packet.get('id')!=digest({k:v for k,v in packet.items() if k!='id'})):
        raise ValueError('Invalid shared card review binding')
    if packet['task_id'] not in task_ids(p,rid):raise ValueError('Shared card task outside round')
    contexts=[]
    for proof in shared.get('sources',[]):
        context=_card_read_source(p,rid,proof.get('task_id'),proof.get('packet_id'),proof.get('event_id'),proof.get('source_hash'),memo)
        if not context or context['proof']!=proof:raise ValueError('Shared card review evidence no longer valid; read current card text again')
        contexts.append(context)
    snapshots={s['hash']:s for s in packet['snapshots']};current={}
    for match in shared.get('matches',[]):
        index=match.get('source_index');h=match.get('snapshot_hash')
        if type(index) is not int or not 0<=index<len(contexts) or h not in snapshots:raise ValueError('Invalid shared card source locator')
        if h not in current:
            loaded=_checked_json(p.path/'bridge'/'raw'/(h+'.json'),h)
            if not loaded:raise ValueError('Changed current card source')
            current[h]=loaded[0]
        key=card_review_key(_card_at(current[h],match.get('locator')));context=contexts[index]
        if (key is None or key[0]!=match.get('note_id') or hashlib.sha256(key[1].encode()).hexdigest()!=match.get('words_hash') or
                key not in context['delivered'] or card_review_key(_card_at(context['raw'],match.get('prior_locator')))!=key):
            raise ValueError('Shared card words or identity changed')


def review_next(p,task_id,limit=40,round_id=None):
    if not 1<=limit<=200:raise ValueError('Review limit out of range')
    task=p.task(task_id);index_task(p,task);d=task['payload'];b=d.get('bridge',{});note=d['target']
    rows=p.db.execute('SELECT * FROM entity_versions WHERE note_id=? AND reviewed=0 ORDER BY rowid LIMIT ?',(note,limit)).fetchall()
    items=[]
    for r in rows:
        body=json.loads(r['body']);parent=body.get('parentCommentId');context=None
        def contexts_for(entity):
            if not entity or entity==r['entity_id']:return []
            records=p.db.execute('SELECT body,source,locator,version FROM entity_versions WHERE note_id=? AND entity_id=? ORDER BY rowid',(note,entity)).fetchall()
            return [dict(q,body=json.loads(q['body'])) for q in records]
        parents=contexts_for(parent)
        if parents:context=parents[-1]  # Keep the legacy latest-parent field.
        reply_id=body.get('replyToCommentId')
        replies=contexts_for(reply_id) if reply_id and reply_id!=parent else []
        missing=[]
        if parent and not parents:missing.append({'relation':'parent','entity_id':parent})
        if reply_id and reply_id!=parent and not replies:missing.append({'relation':'reply_to','entity_id':reply_id})
        text=body.get('text','');items.append({'note_id':note,'entity_id':r['entity_id'],'version':r['version'],'body':body,'source':r['source'],'locator':r['locator'],'parent':context,
            'parent_versions':parents,'reply_target_versions':replies,'context_gaps':missing,
            'classification_hint':'deleted_placeholder' if text.strip()=='原评论已删除' else 'unclassified','not_a_patient_count':True})
    if round_id:
        cfg=round_config(p,round_id)
        if task_id not in task_ids(p,round_id):raise ValueError('Task outside selected round')
        for item in items:
            disposition=entity_disposition(item['body'],cfg['cutoff']);item['round_disposition']=disposition
            p.db.execute('INSERT OR REPLACE INTO round_entities VALUES(?,?,?,?,?,?)',(round_id,note,item['entity_id'],item['version'],disposition,packed({'cutoff':cfg['cutoff'],'source':item['source'],'locator':item['locator']})))
    # Non-comment stages retain exact snapshot binding and are never auto-reviewed.
    diagnostics={'confirmed_hashes':[],'invalid':[]}
    if d['stage']=='images':
        from xhs_bridge import capture_diagnostic_status
        diagnostics=capture_diagnostic_status(p,task)
    excluded_diagnostics=set(diagnostics['confirmed_hashes'])
    snapshots=[{'hash':h,'file':str(p.path/'bridge'/'raw'/(h+'.json'))} for h in b.get('hashes',[])
        if h not in b.get('reviewed_hashes',[]) and h not in excluded_diagnostics]
    share_round_cards=bool(round_id and d['stage']=='search')
    shared_sources=[];shared_matches=[];source_indexes={};proof_memo={};lookup={}
    if share_round_cards:_sync_round_card_reads(p,round_id,proof_memo)
    if d['stage'] not in {'roots','replies'}:
        card_key=card_review_key
        already_read=set()
        # Old commands without --round retain their task-local behavior. An
        # explicit round only reuses valid committed reading from that round.
        for h in ([] if share_round_cards else b.get('reviewed_hashes',[])):
            if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):continue
            source=p.path/'bridge'/'raw'/(h+'.json')
            try:
                prior=source.read_bytes()
                if hashlib.sha256(prior).hexdigest()!=h:continue
                prior=json.loads(prior)
            except (OSError,ValueError):continue
            if not isinstance(prior,dict):continue
            for card in prior.get('cards',[]):
                key=card_key(card)
                if key is not None:already_read.add(key)
        seen=set()
        for snapshot in snapshots:
            raw=json.loads(Path(snapshot['file']).read_text());view={k:raw[k] for k in ('title','body','authors','media','media_captures','explicitEnd','endSignals') if k in raw}
            cards=[];read_count=duplicate_count=cross_count=0
            for ordinal,card in enumerate(raw.get('cards',[])):
                key=card_key(card)
                shared=_shared_card_read(p,round_id,key,proof_memo,lookup) if share_round_cards and key and _card_has_words(card) else None
                if key is not None and key in already_read:read_count+=1
                elif shared:
                    proof,prior_locator=shared;identity=packed(proof)
                    if identity not in source_indexes:
                        source_indexes[identity]=len(shared_sources);shared_sources.append(proof)
                    same_task=proof['task_id']==task_id
                    read_count+=int(same_task);cross_count+=int(not same_task)
                    shared_matches.append({'snapshot_hash':snapshot['hash'],'locator':'cards/'+str(ordinal),
                        'note_id':key[0],'words_hash':hashlib.sha256(key[1].encode()).hexdigest(),
                        'source_index':source_indexes[identity],'prior_locator':prior_locator,
                        'kind':'same_task' if same_task else 'cross_task','query_assignment_requires_review':True})
                elif key is not None and key in seen:duplicate_count+=1
                else:
                    cards.append(card)
                    if key is not None:seen.add(key)
            if 'cards' in raw:
                view['cards']=cards
                view['card_delta']={'observed':len(raw['cards']),'returned':len(cards),'already_reviewed':read_count,'duplicate_in_packet':duplicate_count}
                if share_round_cards:view['card_delta']['cross_task_reviewed']=cross_count
            if share_round_cards:view['search_scope']={'query':d['target'],'sort':d.get('sort'),
                'query_assignment_requires_review':True,'card_review_is_not_body_or_comment_review':True}
            snapshot['reading']=view
    remaining=p.db.execute('SELECT count(*) FROM entity_versions WHERE note_id=? AND reviewed=0',(note,)).fetchone()[0]
    packet={'task_id':task_id,'stage':d['stage'],'target':note,'round':round_id,'items':items,'snapshots':snapshots,'more_entities':remaining>len(items),'requires_codex_reading':True}
    if diagnostics['confirmed_hashes'] or diagnostics['invalid']:packet['capture_diagnostics']=diagnostics
    if share_round_cards:packet['query_assignment_requires_review']=True
    if shared_matches:packet['shared_card_reviews']={'round':round_id,'text_only':True,'no_attribution_or_collection_completion':True,
        'sources':shared_sources,'matches':shared_matches}
    pid=digest(packet);packet['id']=pid
    p.db.execute('INSERT OR IGNORE INTO review_packets VALUES(?,?,?,?,0)',(pid,task_id,packed(packet),now()));p.db.commit()
    path=p.path/'bridge'/'review-packets'/(pid+'.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(packed(packet))
    return dict(packet,file=str(path))


def commit_packet(p,task_id,receipt,apply=False):
    pid=receipt.get('review_packet_id')
    if not pid:return
    schema(p);r=p.db.execute('SELECT * FROM review_packets WHERE id=? AND task_id=?',(pid,task_id)).fetchone()
    if not r:raise ValueError('Unknown review packet')
    packet=json.loads(r['body']);versions={(i['note_id'],i['entity_id'],i['version']) for i in packet['items']}
    if receipt.get('reviewed') is not True or not receipt.get('summary'):raise ValueError('Explicit reading receipt required')
    _validate_shared_card_reads(p,packet)
    for item in packet['items']:
        contexts=item.get('parent_versions',[])+item.get('reply_target_versions',[])
        if item.get('parent'):contexts=contexts+[item['parent']]
        for record in [item]+contexts:
            source=Path(record['source'])
            h=source.stem
            if not re.fullmatch('[0-9a-f]{64}',h) or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=h:
                raise ValueError('Changed review entity/context source')
    index_task(p,p.task(task_id))
    for s in packet['snapshots']:
        if hashlib.sha256(Path(s['file']).read_bytes()).hexdigest()!=s['hash']:raise ValueError('Changed review source')
    if receipt.get('complete') and packet['more_entities']:raise ValueError('Unread delta entities remain')
    if packet['more_entities'] and receipt.get('bridge_snapshot_hashes'):raise ValueError('Partial packet cannot attest whole snapshots')
    pending={(r[0],r[1],r[2]) for r in p.db.execute('SELECT note_id,entity_id,version FROM entity_versions WHERE note_id=? AND reviewed=0',(packet['target'],))}
    if receipt.get('bridge_snapshot_hashes') and pending-versions:raise ValueError('Unreviewed entity versions outside packet')
    if apply:
        p.db.executemany('UPDATE entity_versions SET reviewed=1 WHERE note_id=? AND entity_id=? AND version=?',list(versions));p.db.execute('UPDATE review_packets SET committed=1 WHERE id=?',(pid,));p.db.commit()
    return packet


def review_media(p,task,receipt,apply=False):
    """A partial carousel or a video frame cannot attest an entire media scope."""
    stage=task['payload'].get('stage')
    if stage not in {'images','video'}:return
    schema(p);note=task['payload']['target']
    rows={r['media_key']:dict(r) for r in p.db.execute('SELECT * FROM media_inventory WHERE note_id=?',(note,))}
    keys=receipt.get('media_reviewed_keys',[])
    if not isinstance(keys,list) or any(k not in rows for k in keys):raise ValueError('Unknown media review position')
    if stage=='images' and any(rows[k]['kind']=='video' for k in keys):
        raise ValueError('Images receipt cannot review video positions')
    if stage=='video' and any(rows[k]['kind']!='video' for k in keys):
        raise ValueError('Video receipt cannot review non-video positions')
    if receipt.get('media_verified') and not keys and not receipt.get('complete'):
        raise ValueError('Partial media receipt requires exact positions')
    check_keys=set(keys)
    if receipt.get('complete'):
        check_keys.update(k for k,r in rows.items() if r['state']=='reviewed')
    for key in check_keys:
        record=json.loads(rows[key]['body']);file=Path(record.get('file',''))
        if not file.is_file() or not record.get('sha256') or hashlib.sha256(file.read_bytes()).hexdigest()!=record['sha256']:
            raise ValueError('Media file missing or changed')
    if receipt.get('complete'):
        if receipt.get('end')=='explicit_zero':
            present=any((r['kind']=='video')==(stage=='video') for r in rows.values())
            scan=receipt.get('absence_scan',{});file=Path(scan.get('source_file',''))
            comments=receipt.get('comment_media_scan',{});comment_file=Path(comments.get('source_file',''))
            if present or scan.get('kind')!=stage or scan.get('count')!=0 or not scan.get('complete') or not scan.get('reviewer'):
                raise ValueError('Explicit zero needs actual absence inspection')
            for proof,f in [(scan,file),(comments,comment_file)]:
                if not proof.get('complete') or not f.is_file() or not proof.get('sha256') or hashlib.sha256(f.read_bytes()).hexdigest()!=proof['sha256']:
                    raise ValueError('Absence inspection needs hash-bound source')
            return
        body={k:json.loads(r['body']) for k,r in rows.items() if k.startswith('body:')}
        counts={v.get('count') for v in body.values()}
        if len(counts)!=1 or not counts or None in counts:raise ValueError('Carousel extent unknown')
        count=next(iter(counts))
        if set(body)!={f'body:{i}' for i in range(1,count+1)}:raise ValueError('Uncaptured media positions remain')
        checked={k for k,r in rows.items() if r['state']=='reviewed'}|set(keys)
        if stage=='images' and any(k not in checked for k,r in rows.items() if r['kind']!='video'):
            raise ValueError('Unread image positions remain')
        scan=receipt.get('comment_media_scan',{})
        source=Path(scan.get('source_file',''))
        if not scan.get('complete') or not source.is_file() or not scan.get('sha256') or hashlib.sha256(source.read_bytes()).hexdigest()!=scan['sha256']:
            raise ValueError('Comment attachment scan needs hash-bound evidence')
        videos=[k for k,r in rows.items() if r['kind']=='video']
        if stage=='video':
            if videos:
                manifest=receipt.get('video_review',{})
                if set(manifest)!=set(videos):raise ValueError('Video review incomplete')
                for key in videos:
                    v=manifest[key];end=0
                    for seg in sorted(v.get('segments',[]),key=lambda x:x['start']):
                        f=Path(seg.get('source_file',''))
                        if seg['start']>end or seg['end']<=seg['start'] or not seg.get('verified') or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest()!=seg.get('sha256'):
                            raise ValueError('Video segment gap or invalid source')
                        end=max(end,seg['end'])
                    if not v.get('duration') or end<v['duration']:raise ValueError('Video timeline unfinished')
            elif receipt.get('end')!='explicit_zero':raise ValueError('No video requires explicit_zero')
    if apply:
        # Exact frame/segment observations may be kept as partial native events.
        # Only the explicit full-video path above can review an entire video row.
        reviewed_keys=keys if stage!='video' or receipt.get('complete') is True else []
        p.db.executemany("UPDATE media_inventory SET state='reviewed' WHERE note_id=? AND media_key=?",[(note,k) for k in reviewed_keys]);p.db.commit()


def import_evidence(p,path,reviewed=False,round_id=None):
    schema(p);root=Path(path).resolve();d=json.loads(root.read_text());entries=d['evaluation_entries'];retained=0
    for e in entries:
        if not e.get('id') or not e.get('source') or not e.get('url'):raise ValueError('Evidence source fields required')
        source=(root.parent/e['source']).resolve()
        if not source.is_file():raise ValueError('Evidence source missing')
        if reviewed and not e.get('review_basis'):raise ValueError('Reviewed evidence needs explicit review_basis')
        h=hashlib.sha256(source.read_bytes()).hexdigest();archive=p.path/'evidence-sources'/h;archive.parent.mkdir(exist_ok=True)
        if not archive.exists():archive.write_bytes(source.read_bytes())
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=h:raise ValueError('Evidence archive changed')
        body=dict(e,source_file=str(archive),original_source_file=str(source),source_sha256=h,semantic_verified=reviewed,legacy_claim=not reviewed)
        v=digest(body)
        known=p.db.execute('SELECT current FROM evidence_versions WHERE id=? AND version=?',(e['id'],v)).fetchone()
        if known and not known[0]:retained+=1;continue
        p.db.execute('UPDATE evidence_versions SET current=0 WHERE id=?',(e['id'],));p.db.execute('INSERT OR REPLACE INTO evidence_versions VALUES(?,?,?,?,1)',(e['id'],v,packed(body),now()))
    p.db.commit();added=[]
    if round_id:
        round_config(p,round_id)
        targets={p.task(t)['payload'].get('target') for t in task_ids(p,round_id)}
        missing={e['note_id'] for e in entries if re.fullmatch('[a-f0-9]{24}',e.get('note_id','')) and e['note_id'] not in targets}
        if missing:
            import xhs_native as native
            units=[{'stage':'legacy_audit','target':note,'next':'Imported baseline evidence has no round task; reconcile original reading receipts and remaining scopes before completion.','sources':[str(root)]} for note in sorted(missing)]
            manifest=p.path/'evidence-sources'/('round-import-'+digest({'round':round_id,'units':units})+'.json')
            if not manifest.exists():manifest.write_text(packed({'units':units}))
            added=native.import_manifest(p,manifest)['task_ids']
            for tid in added:
                note=p.task(tid)['payload']['target'];matching=[e for e in entries if e.get('note_id')==note]
                # Defer only when all linked evidence has an observed future date.
                cutoff=round_config(p,round_id)['cutoff']
                dated=matching and all(entity_disposition(e,cutoff)=='next_round' for e in matching)
                published=matching[0].get('published_at',matching[0].get('publishedAt')) if dated else None
                enroll(p,round_id,tid,published,basis={'imported_baseline':str(root),'not_a_new_query':True})
    return {'imported':len(entries),'older_known_versions_not_reactivated':retained,'legacy_claims_preserved_not_promoted':True,'missing_ledger_scopes_added':len(added)}


def readable_locator(e):
    """Append only IDs already recorded in this evidence, without changing it."""
    locator=str(e.get('locator','待补'))
    for key,label in [('comment_id','评论CID'),('parent_comment_id','父CID')]:
        cid=e.get(key)
        if isinstance(cid,str) and cid.strip() and cid not in locator:
            locator+=('；' if locator else '')+f'{label}：{cid}'
    return locator


def export(p,rid):
    tasks=_report_tasks(p,rid);queue_audit=_audit_tasks(p,tasks)
    s=_status_tasks(p,rid,tasks,queue_audit['items']);out=p.path/'exports'/rid;out.mkdir(parents=True,exist_ok=True)
    targets={t['payload'].get('target') for t in tasks}
    cutoff=round_config(p,rid)['cutoff']
    versions=[json.loads(r[0]) for r in p.db.execute('SELECT body FROM evidence_versions ORDER BY imported,rowid')]
    selected={};later={}
    for e in versions:
        if e.get('note_id') not in targets:continue
        if entity_disposition(e,cutoff)=='next_round':later[e['id']]=e
        else:selected[e['id']]=e
    evidence=[selected[k] for k in sorted(selected)]
    deferred=[later[k] for k in sorted(later)]
    invalid=[]
    for e in evidence:
        source=Path(e['source_file'])
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=e['source_sha256']:
            invalid.append(e['id']);e['source_invalid']=True
    text=f'# 医生评价与原帖索引\n\n研究轮次：{rid}；已登记{s["known"]}项，待处理{s["pending"]}项。不是全站召回率。\n\n'
    adult='# 成人正畸评价核查\n\n仅显式标记为成人的当前证据进入确定区；旧导入条目未自动通过成人/任职审核。\n\n当前轮次的任职口径为“近期官方临床证据”支持，资料发布日期与本次核对日分别列示；核对日不代表当日实时在岗或实际排班。任职背景不自动确认患者年龄、评价归属或治疗发生地点。\n\n'
    for e in evidence:
        provenance='已导入旧研究结论，保留原审核边界；本轮尚未重新审读。' if e.get('legacy_claim') else '本轮已提交对应来源的审读依据。'
        section=f'## {e["id"]} · {e.get("doctor", "未具名")}\n\n{e.get("summary", "")}\n\n[打开原帖]({e["url"]})\n\n定位：{readable_locator(e)}\n\n边界：{e.get("boundary", "待核")}\n\n审核状态：{provenance}\n\n[原始依据](<{e["source_file"]}>)\n\n'
        if e.get('employment_status')=='recent_official_clinical_affiliation_supported':
            from urllib.parse import urlparse
            source_url=e.get('employment_source_url') or ''
            parsed=urlparse(source_url)
            official=f'[官方／院方供稿来源](<{source_url}>)' if parsed.scheme in {'http','https'} and parsed.netloc else '待核'
            section+=(f'任职背景（近期官方临床证据）：{e.get("employment_doctor") or "待核"}。\n\n'
                      f'资料发布日期：{e.get("employment_source_published_at") or "待核"}；本次核对日：{e.get("employment_checked_on") or "待核"}。\n\n'
                      f'官方资料：{official}；来源位置：{e.get("employment_source_locator") or "待核"}。\n\n'
                      f'核验范围：{e.get("employment_verification_scope") or "待核"}。核对日不代表当日实时在岗或实际排班。\n\n')
            if e.get('employment_boundary'):section+=f'任职依据边界：{e["employment_boundary"]}\n\n'
        if e.get('source_invalid'):section='**来源归档失效，以下结论待复核。**\n\n'+section
        text+=section
        adult_known=e.get('adult_verified') is True or e.get('adult_status') in {'adult_start_confirmed','explicit_adult_consultation','adult_consultation_doctor_unknown','adult_treatment_doctor_unknown'}
        if e.get('adult_status')=='minor_at_start_explicit':
            adult+=f'- {e["id"]}：已确认未成年起治，不纳入成人起治核心。依据：{e.get("adult_basis") or "见总文档的起治年龄依据"} [打开原帖]({e["url"]})\n'
        elif e.get('adult_status')=='explicit_child_consultation_start_not_confirmed' and e.get('adult_verified') is False:
            adult+=f'- {e["id"]}：已明确儿童咨询，不纳入成人范围；尚未确认起治。依据：{e.get("adult_basis") or "见总文档的儿童咨询依据"} [打开原帖]({e["url"]})\n'
        elif e.get('adult_status')=='not_applicable_professional_content_without_patient_evaluation' and e.get('core_evaluation_eligible') is False:
            adult+=f'- {e["id"]}：专业账号内容，未取得独立患者评价，不纳入成人患者核心。[打开原帖]({e["url"]})\n'
        elif adult_known and e.get('employment_verified') is True:adult+=section
        elif adult_known:adult+='### 成人范围有依据，医生归属及现任条件另核\n\n'+section
        else:adult+=f'- {e["id"]}：成人或现任条件未结构化核验，见总文档，不能自动排除或视为已确认。\n'
    for name,content in [('总文档',text),('成人专项',adult)]:
        for suffix in ['md','txt']:(out/f'{name}.{suffix}').write_text(content)
    for name,obj in [('原帖索引',evidence),('覆盖与缺口',s),('队列审计',queue_audit),('下轮增量',deferred)]:(out/f'{name}.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2))
    return {'directory':str(out),'evidence_entries':len(evidence),'invalid_evidence_sources':invalid,'pending':s['pending'],'legacy_documents_untouched':True}


def add_cli(sub):
    q=sub.add_parser('round');q.add_argument('action',choices=['create','status','close','enroll']);q.add_argument('--id',required=True);q.add_argument('--config',type=Path);q.add_argument('--task');q.add_argument('--published-at');q.add_argument('--basis')
    q=sub.add_parser('audit');q.add_argument('--dry-run',action='store_true',required=True);q.add_argument('--round')
    q=sub.add_parser('review-next');q.add_argument('--task',required=True);q.add_argument('--limit',type=int,default=40);q.add_argument('--round')
    q=sub.add_parser('import-evidence');q.add_argument('--file',required=True,type=Path);q.add_argument('--reviewed',action='store_true');q.add_argument('--round')
    q=sub.add_parser('export');q.add_argument('--round',required=True)
    q=sub.add_parser('import-audit');q.add_argument('--file',required=True,type=Path)


def compact_status(result):
    if 'remaining' not in result:return result
    return {k:v for k,v in result.items() if k!='remaining'} | {'remaining_by_stage':{stage:sum(r['stage']==stage for r in result['remaining']) for stage in sorted({r['stage'] for r in result['remaining']})}}


def compact_review(packet):
    """Return complete text once; keep hashes and signed URLs in the bound file."""
    def content(body):
        uid=re.search(r'/user/profile/([a-f0-9]{24})',body.get('authorHrefRaw') or '')
        return {'author':body.get('author'),'author_uid':uid[1] if uid else body.get('authorId'),'text':body.get('text'),'time':body.get('time'),'images':body.get('images',[]),'parent_id':body.get('parentCommentId'),'reply_to':body.get('reply_to'),'reply_to_id':body.get('replyToCommentId'),'reply_identity':body.get('replyToIdentity')}
    def source_key(source):
        sources.setdefault(source,'S'+str(len(sources)+1))
        return sources[source]
    sources={};parents={};reply_targets={};items=[]
    for item in packet['items']:
        row=dict(content(item['body']),id=item['entity_id'],source=source_key(item['source']),locator=item['locator'],round_disposition=item.get('round_disposition'))
        items.append(row)
        if item.get('context_gaps'):row['context_gaps']=item['context_gaps']
        parent_versions=item.get('parent_versions') or ([item['parent']] if item.get('parent') else [])
        for group,key,versions in ((parents,row['parent_id'],parent_versions),
                                   (reply_targets,row['reply_to_id'],item.get('reply_target_versions',[]))):
            for record in versions:
                body=content(record['body']);contexts=group.setdefault(key,[])
                if not any(existing['content']==body for existing in contexts):
                    contexts.append({'content':body,'source':source_key(record['source']),'locator':record['locator']})
    snapshots=[]
    for snapshot in packet['snapshots']:
        reading=dict(snapshot.get('reading',{}))
        if 'cards' in reading:reading['cards']=[{'note_id':key[0] if (key:=card_review_key(c)) else None,
            **{k:c[k] for k in CARD_WORD_FIELDS if k in c},**({} if key else {'identity':'unconfirmed'})} for c in reading['cards']]
        snapshots.append({'file':snapshot['file'],'reading':reading})
    visible={}
    for item in packet['items']:visible.setdefault(item['entity_id'],[]).append(content(item['body']))
    parents={key:[context for context in contexts if context['content'] not in visible.get(key,[])] for key,contexts in parents.items()}
    parents={key:contexts for key,contexts in parents.items() if contexts}
    reply_targets={key:[c for c in values if c['content'] not in visible.get(key,[])] for key,values in reply_targets.items()}
    reply_targets={key:values for key,values in reply_targets.items() if values}
    result={k:packet[k] for k in ('id','file','task_id','stage','target','round','more_entities')} | {'items':items,'parent_context':parents,'sources':{v:k for k,v in sources.items()},'snapshots':snapshots,'full_receipt_binding_in_file':True}
    if reply_targets:result['reply_target_context']=reply_targets
    if packet.get('query_assignment_requires_review'):result['query_assignment_requires_review']=True
    shared=packet.get('shared_card_reviews')
    if shared:result['card_review_reuse']={'same_task_reviewed':sum(m['kind']=='same_task' for m in shared['matches']),
        'cross_task_reviewed':sum(m['kind']=='cross_task' for m in shared['matches']),'source_groups':len(shared['sources']),
        'bindings_in_full_packet_file':True,'text_only':True,'query_assignment_requires_review':True}
    if packet.get('capture_diagnostics'):result['capture_diagnostics']={'excluded_failed_captures':len(packet['capture_diagnostics']['confirmed_hashes']),
        'invalid_diagnostics':len(packet['capture_diagnostics']['invalid']),'not_a_content_review':True,'bindings_in_full_packet_file':True}
    return result


def dispatch(p,a):
    if a.command=='round':
        if a.action=='create':return compact_status(create(p,a.id,json.loads(a.config.read_text())))
        if a.action=='status':return compact_status(status(p,a.id))
        if a.action=='close':return compact_status(close(p,a.id))
        return enroll(p,a.id,a.task,a.published_at,a.basis)
    if a.command=='audit':
        result=audit(p,a.round);path=p.path/'audit-dry-run.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        from collections import Counter
        return {'file':str(path),'count':result['count'],'categories':dict(Counter(r['category'] for r in result['items'])),'dry_run':True}
    if a.command=='review-next':return compact_review(review_next(p,a.task,a.limit,a.round))
    if a.command=='import-evidence':return import_evidence(p,a.file,a.reviewed,a.round)
    if a.command=='import-audit':return import_audit(p,a.file)
    return export(p,a.round)
