#!/usr/bin/env python3
"""External person vocabulary -> existing recall/search tasks and read-only coverage.

Discovery targets are scheduling metadata, never evidence of patient attribution.
This module neither collects pages nor changes queues, reviews, or access state.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from xhs_paths import external_path
import xhs_research_desk as desk

FORMAT = 'xhs-people-v1'
SORTS = ('general', 'latest', 'popular')


def key(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def strings(value, field):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(field + ' must be an explicit string list')
    return list(dict.fromkeys(x.strip() for x in value))


def regular_external(path):
    lexical = Path(path).expanduser().absolute()
    if any(x.is_symlink() for x in (lexical, *lexical.parents)):
        raise ValueError('Use regular external paths, not symlink aliases')
    return external_path(lexical)


def validate(cfg):
    if cfg.get('format') != FORMAT:
        raise ValueError('Expected ' + FORMAT)
    entities = cfg.get('entities')
    if not isinstance(entities, list) or not entities:
        raise ValueError('Explicit nonempty entities required')
    ids = set()
    for e in entities:
        if not isinstance(e, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', str(e.get('id', ''))):
            raise ValueError('Each entity needs a stable ID')
        if e['id'] in ids or not isinstance(e.get('name'), str) or not e['name'].strip():
            raise ValueError('Unique IDs and nonempty names required')
        ids.add(e['id'])
        if 'pinyin' in e:
            syllables = strings(e['pinyin'], 'pinyin')
            if not syllables or any(not re.fullmatch('[a-z]+', s) for s in syllables):
                raise ValueError('Pinyin must be explicitly checked lowercase Latin syllables')
        for f in ('surname', 'surname_pinyin'):
            if f in e and (not isinstance(e[f], str) or not e[f].strip()):
                raise ValueError('Nonempty ' + f + ' required when supplied')
        if 'surname_pinyin' in e and (not e.get('surname') or not re.fullmatch('[a-z]+', e['surname_pinyin'])):
            raise ValueError('Surname pinyin needs explicit surname and lowercase spelling')
        aliases = e.get('aliases', [])
        if not isinstance(aliases, list):
            raise ValueError('Aliases must be a list')
        for a in aliases:
            if not isinstance(a, dict) or not isinstance(a.get('text'), str) or not a['text'].strip():
                raise ValueError('Alias text required')
            if a.get('mode', 'literal') not in {'literal', 'initials', 'latin_token'}:
                raise ValueError('Unsupported alias matching mode')
            if a.get('basis') not in {'hypothesis', 'observed'}:
                raise ValueError('Alias must distinguish hypothesis from observed wording')
            strings(a.get('ambiguities', []), 'ambiguities')
            if a['basis'] == 'observed':
                if not isinstance(a.get('sources'), list) or not a['sources']:
                    raise ValueError('Observed aliases need exact source references')
                for ref in a['sources']:
                    if not isinstance(ref, dict) or not ref.get('locator') or not ref.get('file'):
                        raise ValueError('Observed alias source needs file/hash/locator')
                    path = regular_external(ref['file'])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref.get('sha256'):
                        raise ValueError('Observed alias source missing or changed')
                    try:
                        value = json.loads(path.read_text())
                        for part in str(ref['locator']).strip('/').split('/'):
                            value = value[int(part)] if isinstance(value, list) else value[part]
                        text = value.get('text') if isinstance(value, dict) else value
                    except (ValueError, KeyError, IndexError, TypeError) as error:
                        raise ValueError('Observed alias needs a valid JSON text locator') from error
                    from xhs_recall import compile_terms, matches
                    if not isinstance(text, str) or not matches(text, compile_terms([dict(a, id='source-check')])):
                        raise ValueError('Observed alias does not occur at the cited text locator')
    contexts = cfg.get('contexts')
    if not isinstance(contexts, dict):
        raise ValueError('Explicit institution/profession contexts required')
    for f in ('institutions', 'professions'):
        strings(contexts.get(f), f)
    for f in ('titles', 'experiences'):
        strings(cfg.get(f), f)
    sorts = strings(cfg.get('sorts', list(SORTS)), 'sorts')
    if not sorts or any(s not in SORTS for s in sorts):
        raise ValueError('Unsupported or empty sorts')
    projects = cfg.get('projects')
    if not isinstance(projects, list) or (not projects and not cfg.get('references') and not cfg.get('media_reviews')):
        raise ValueError('Select explicit recall projects or external references')
    for p in projects:
        if not isinstance(p, dict) or not isinstance(p.get('path'), str):
            raise ValueError('Project path required')
        regular_external(p['path'])
        if (p.get('all_tasks') is True) == ('task_ids' in p):
            raise ValueError('Choose all_tasks or explicit task_ids per project')
        if 'task_ids' in p and not strings(p['task_ids'], 'task_ids'):
            raise ValueError('Nonempty task_ids required')
    return cfg


def load_config(path):
    path = regular_external(path)
    return validate(json.loads(path.read_text())), path


def plan(cfg, config_path, project=None):
    validate(cfg)
    source = regular_external(config_path)
    if not source.is_file() or json.loads(source.read_text()) != cfg:
        raise ValueError('Plan source must be the exact external configuration')
    contexts = cfg['contexts']; context_words = contexts['institutions'] + contexts['professions']
    labels = {e['id']: e['name'] for e in cfg['entities']}
    surname_words = {e['id']: {key(word) for word in (e.get('surname'), e.get('surname_pinyin')) if word}
                     for e in cfg['entities']}
    queries, terms = {}, {}

    def query(text, family, eid):
        normalized = key(text)
        if normalized in surname_words[eid]: return
        q = queries.setdefault(normalized, {'query_id': digest(normalized)[:20], 'query': text.strip(),
                                           'entity_ids': [], 'families': [], 'entity_families': {}})
        if eid not in q['entity_ids']: q['entity_ids'].append(eid)
        if family not in q['families']: q['families'].append(family)
        if family not in q['entity_families'].setdefault(eid, []): q['entity_families'][eid].append(family)

    def term(text, mode, family, eid, basis='hypothesis', ambiguities=None, sources=None):
        ident = digest([key(text), mode])[:20]
        t = terms.setdefault(ident, {'id': ident, 'text': text, 'mode': mode,
                                    'entity_ids': [], 'families': [], 'ambiguities': [], 'origins': []})
        if eid not in t['entity_ids']: t['entity_ids'].append(eid)
        if family not in t['families']: t['families'].append(family)
        for ambiguity in ambiguities or []:
            if ambiguity not in t['ambiguities']: t['ambiguities'].append(ambiguity)
        origin = {'entity_id': eid, 'basis': basis, 'sources': sources or []}
        if origin not in t['origins']: t['origins'].append(origin)

    for e in cfg['entities']:
        eid, name = e['id'], e['name']
        term(name, 'literal', 'full_name', eid, 'provided_name')
        query(name, 'full_name', eid)
        for context in context_words: query(name + ' ' + context, 'full_name', eid)
        if e.get('pinyin'):
            forms = list(dict.fromkeys([''.join(e['pinyin']), ' '.join(e['pinyin'])]))
            for form in forms:
                term(form, 'latin_token', 'full_pinyin', eid)
                query(form, 'full_pinyin', eid)
            initials = ''.join(s[0] for s in e['pinyin'])
            if 2 <= len(initials) <= 12:
                term(initials, 'initials', 'initials', eid, ambiguities=['Initials may identify another person, institution, or ordinary term.'])
                query(initials, 'initials', eid)
                for context in context_words: query(context + ' ' + initials, 'initials', eid)
        for word, mode, family in [(e.get('surname'), 'literal', 'surname'),
                                   (e.get('surname_pinyin'), 'latin_token', 'surname_pinyin')]:
            if not word: continue
            term(word, mode, family, eid, ambiguities=['Surname-only matches are weak discovery clues, not identity confirmation.'])
            for context in context_words:
                query(context + ' ' + word, family, eid)
                for title in cfg['titles']:
                    query(context + ' ' + word + title, family, eid)
            for title in cfg['titles']: term(word + title, 'literal', family, eid)
        for a in e.get('aliases', []):
            mode = a.get('mode', 'literal'); text = a['text'].strip()
            term(text, mode, 'alias', eid, a['basis'], a.get('ambiguities'), a.get('sources'))
            # Explicit short aliases still need context; no bare surname escape hatch.
            weak_forms = {key(word + title) for word in (e.get('surname'), e.get('surname_pinyin')) if word
                          for title in ['', *cfg['titles']]}
            weak = key(text) in weak_forms or len(text) == 1
            if not weak: query(text, 'alias', eid)
            for context in context_words: query(context + ' ' + text, 'alias', eid)
        for experience in cfg['experiences']:
            query(name + ' ' + experience, 'experience', eid)
            for institution in contexts['institutions']:
                query(institution + ' ' + experience, 'institution_experience', eid)
    recall = {'format': 'xhs-recall-v1', 'projects': cfg['projects'], 'terms': list(terms.values())}
    for f in ('references', 'media_reviews'):
        if f in cfg: recall[f] = cfg[f]
    # Use the existing matcher to validate aliases rather than creating a second grammar.
    from xhs_recall import compile_terms
    compile_terms(recall['terms'])
    units = []; reused = []
    existing = defaultdict(list)
    if project is not None:
        for t in read_tasks(project):
            d = t['payload']
            if t['kind'] == 'native' and d.get('stage') == 'search':
                existing[(key(d['target']), d.get('sort'))].append(t)
    for q in queries.values():
        for sort in cfg.get('sorts', SORTS):
            target = q['query']; matches = existing[(key(target), sort)]
            if matches:
                prior = next((t for t in matches if t['payload']['target'] == target), matches[0])
                target = prior['payload']['target']
                reused.append({'query_id': q['query_id'], 'sort': sort, 'task_id': prior['id'], 'target': target})
            units.append({'stage': 'search', 'target': target, 'sort': sort,
                          'sources': [str(source)], 'next': 'Read actual results, then included bodies, threads and related media; preserve gaps.',
                          'discovery_entity_ids': q['entity_ids'],
                          'discovery_entity_labels': {eid: labels[eid] for eid in q['entity_ids']},
                          'discovery_families': q['families'], 'discovery_entity_families': q['entity_families']})
    return {'format': 'xhs-people-plan-v1', 'config_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'entities': [{'id': eid, 'name': name} for eid, name in labels.items()],
            'queries': list(queries.values()), 'recall_config': recall, 'manifest': {'units': units},
            'existing_query_tasks_reused': reused,
            'discovery_is_not_attribution': True, 'read_only': True, 'network_permission': False}


def read_tasks(project):
    project = regular_external(project); path = project / 'project.sqlite3'
    if path.is_symlink() or not path.is_file(): raise ValueError('Existing project database required')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True); db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        rows = []
        for row in db.execute('SELECT * FROM tasks ORDER BY id'):
            task = dict(row); task['payload'] = json.loads(task['payload']); rows.append(task)
        return rows
    finally: db.close()


def coverage(cfg, config_path, project):
    from xhs_rounds import discovery_entities
    bundle = plan(cfg, config_path); tasks = read_tasks(project)
    entities = {e['id']: e['name'] for e in cfg['entities']}
    planned = {(key(u['target']), u['sort']): u for u in bundle['manifest']['units']}
    routes, byhash, peers = {}, defaultdict(set), defaultdict(set)
    for t in tasks:
        d = t['payload']; target = d.get('target', ''); ids = set()
        explicit = discovery_entities(d)
        if explicit is not None:
            ids.update(eid for eid, label in explicit.items()
                       if eid in entities and label == entities[eid])
        if d.get('stage') == 'search':
            q = planned.get((key(target), d.get('sort')))
            if q: ids.update(q['discovery_entity_ids'])
            if explicit is None:
                ids.update(eid for eid, name in entities.items() if name in target)
        routes[t['id']] = ids
        # A search string equal to an ID must not make unrelated body/author peers.
        peers[(d.get('stage') == 'search', target)].add(t['id'])
        for h in d.get('bridge', {}).get('hashes', []): byhash[h].add(t['id'])
    changed = True
    while changed:
        changed = False
        for t in tasks:
            d = t['payload']; before = len(routes[t['id']])
            for peer in peers[(d.get('stage') == 'search', d.get('target', ''))]: routes[t['id']].update(routes[peer])
            for ref in d.get('provenance', []):
                for parent in byhash.get(ref.get('sha256'), []): routes[t['id']].update(routes[parent])
            changed |= before != len(routes[t['id']])
    selected = [t['id'] for t in tasks if routes[t['id']]]
    inventory = desk.scan(project, selected) if selected else {'tasks': [], 'gaps': [], 'media': [], 'sources': {}, 'project_stop_recorded': None}
    byid = {t['task_id']: t for t in inventory['tasks']}
    valid_hashes = set(inventory['sources'])
    task_states = {}
    for t in tasks:
        if t['id'] not in byid: continue
        r = byid[t['id']]; d = t['payload']; b = d.get('bridge', {})
        observed = len(set(b.get('hashes', [])) & valid_hashes)
        if r['source_integrity_gaps'] or r['fault'].get('hold') or t['status'] == 'blocked': state = 'blocked'
        elif observed and r['collection_end_recorded']: state = 'ended'
        elif observed: state = 'observed'
        else: state = 'pending'
        task_states[t['id']] = dict(r, coverage_state=state, verified_source_count=observed,
                                    discovery_entity_ids=sorted(routes[t['id']]),
                                    end_basis='recorded_collection_end_not_revalidated' if state == 'ended' else None)
    search = defaultdict(list)
    for t in tasks:
        if t['id'] in task_states and t['payload'].get('stage') == 'search':
            search[(key(t['payload']['target']), t['payload'].get('sort'))].append(task_states[t['id']])
    result = []
    for eid, name in entities.items():
        queries = []
        for qkey, unit in planned.items():
            if eid not in unit['discovery_entity_ids']: continue
            observations = search[qkey]
            states = {x['coverage_state'] for x in observations}
            # A completed historical method does not hide another method's live gap.
            state = 'missing' if not states else next(s for s in ('blocked', 'pending', 'observed', 'ended') if s in states)
            queries.append({'query': unit['target'], 'sort': unit['sort'],
                            'families': unit['discovery_entity_families'][eid], 'state': state,
                            'tasks': observations})
        downstream = [r for r in task_states.values() if eid in r['discovery_entity_ids'] and r['stage'] != 'search']
        extra_search = [r for r in task_states.values() if eid in r['discovery_entity_ids'] and r['stage'] == 'search'
                        and (key(r['target']), r['sort']) not in planned]
        counts = Counter(q['state'] for q in queries)
        result.append({'entity_id': eid, 'name': name, 'query_states': dict(counts), 'queries': queries,
                       'additional_discovered_searches': extra_search, 'downstream': downstream,
                       'attributable_evaluation_count': None, 'evaluation_count_requires_evidence_review': True})
    return {'format': 'xhs-people-coverage-v1', 'entities': result, 'source_gaps': inventory['gaps'],
            'media': inventory['media'], 'project_stop_recorded': inventory['project_stop_recorded'],
            'planned_query_sort_scopes': len(planned), 'routed_existing_task_count': len(selected),
            'discovery_is_not_attribution': True, 'completion_not_revalidated': True,
            'media_absence_not_inferred': True, 'read_only': True, 'network_permission': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'coverage'):
        p = sub.add_parser(name); p.add_argument('--config', required=True)
        p.add_argument('--project', required=name == 'coverage')
    args = parser.parse_args(); cfg, source = load_config(args.config)
    value = plan(cfg, source, args.project) if args.command == 'plan' else coverage(cfg, source, args.project)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
