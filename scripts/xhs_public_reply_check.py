"""Offline, source-bound verification of an already-authorised public reply.

This module never opens a browser, sends a message or changes an outreach ledger.
"""
import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'Timezone required')
    return result


def profile_uid(value):
    parsed = urlparse(value or '')
    if parsed.netloc and parsed.netloc != 'www.xiaohongshu.com':
        return ''
    match = re.fullmatch(r'/user/profile/([a-fA-F0-9]{24})/?', parsed.path)
    return match.group(1).lower() if match else ''


def normal(value):
    return ''.join(unicodedata.normalize('NFKC', value or '').split())


def reply_text(row, recipient_name):
    value = unicodedata.normalize('NFKC', row.get('text', ''))
    name = unicodedata.normalize('NFKC', recipient_name)
    return re.sub(r'^\s*回复\s*' + re.escape(name) + r'\s*:\s*', '', value, count=1)


ALLOWED_READERS = ('codex', 'gemini')
ALLOWED_DESTINATIONS = ('reply_to_comment', 'top_level_comment')


def assess(intent, review):
    destination = intent.get('destination') or 'reply_to_comment'
    require(destination in ALLOWED_DESTINATIONS, 'Unsupported reply destination')
    if destination == 'reply_to_comment':
        required = ('note_id', 'root_comment_id', 'recipient_comment_id', 'recipient_uid',
                    'recipient_name', 'account_uid', 'message', 'submitted_at')
        id_fields = ('note_id', 'root_comment_id', 'recipient_comment_id', 'recipient_uid', 'account_uid')
    else:
        required = ('note_id', 'recipient_uid', 'account_uid', 'message', 'submitted_at')
        id_fields = ('note_id', 'recipient_uid', 'account_uid')
        require(not intent.get('root_comment_id') and not intent.get('recipient_comment_id'),
                'Top-level comment intent must not set reply comment IDs')
    require(all(intent.get(k) for k in required), 'Incomplete reply intent')
    for key in id_fields:
        require(re.fullmatch('[a-f0-9]{24}', intent[key]) is not None, 'Invalid identity')
    require(review.get('reader') in ALLOWED_READERS and review.get('reviewed') is True,
            'Actual source review required')
    require(review.get('account_verified') is True and review.get('account_uid') == intent['account_uid'],
            'Same account verification required')
    if destination == 'reply_to_comment':
        require(review.get('note_id') == intent['note_id'] and review.get('root_comment_id') == intent['root_comment_id'],
                'Review scope mismatch')
    else:
        require(review.get('note_id') == intent['note_id'], 'Review scope mismatch')
    require(bool(review.get('summary')), 'Actual reading summary required')
    submitted = stamp(intent['submitted_at'])
    observations = review.get('observations', [])
    require(bool(observations), 'Observed sources required')
    loaded = []
    refs = []
    for ref in observations:
        path = Path(ref['file'])
        require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'Exact source file required')
        raw = path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == ref['sha256'], 'Source hash changed')
        at = stamp(ref['observed_at'])
        require(at > submitted, 'Pre-submission source cannot verify publication')
        data = json.loads(raw)
        parsed = urlparse(data.get('url') or data.get('pageUrl', ''))
        require(parsed.scheme == 'https' and parsed.netloc == 'www.xiaohongshu.com'
                and parsed.path.rstrip('/') in ('/explore/' + intent['note_id'], '/search_result/' + intent['note_id']),
                'Source note mismatch')
        loaded.append((at, data))
        refs.append({k: ref[k] for k in ('file', 'sha256', 'observed_at')})
    at, latest = max(loaded, key=lambda x: x[0])
    require(stamp(review['reviewed_at']) >= at, 'Review predates capture')
    result = {'outcome': 'verification_incomplete', 'destination': destination,
              'scope': 'current_same_account_rendered_comments',
              'checked_at': at.isoformat(), 'matching_comment_ids': [], 'sources': refs,
              'resend_permitted': False, 'publication_failure_confirmed': False,
              'reason': 'Target thread or publication evidence incomplete'}
    if any(latest.get(k) for k in ('loginWall', 'securityBlock', 'notFound')):
        result['reason'] = 'Latest source has an access or availability warning'
        return result
    rows = latest.get('results', [])
    if destination == 'top_level_comment':
        own_top_rows = [r for r in rows if profile_uid(r.get('authorHrefRaw')) == intent['account_uid']
                        and not r.get('parentCommentId') and not r.get('replyToCommentId')]
        for row in own_top_rows:
            if row.get('commentId') and normal(row.get('text', '')) == normal(intent['message']):
                result['matching_comment_ids'].append(row['commentId'])
        if result['matching_comment_ids']:
            result.update(outcome='visible_confirmed',
                          reason='Own UID, exact top-level message and note match')
            return result
        if own_top_rows:
            result['reason'] = 'Own top-level comments in target note need manual text reconciliation'
            return result
        coverage = latest.get('deepCoverage', {})
        root_rows = [r for r in rows if not r.get('parentCommentId')]
        identities_readable = all(profile_uid(r.get('authorHrefRaw')) and r.get('text') for r in root_rows)
        threads = coverage.get('threads', [])
        roots_captured = all(t.get('contentCaptured') is True for t in threads)
        fully_read = bool(review.get('target_thread_fully_read') is True
                          or review.get('root_comments_fully_read') is True)
        fully_visible = (coverage.get('rootPaginationState') == 'complete'
                         and coverage.get('explicitEnd') is True
                         and identities_readable
                         and roots_captured
                         and not coverage.get('missingExpectedRootIds'))
        if fully_visible and review.get('fresh_page_loaded') is True and fully_read:
            result.update(outcome='not_visible_after_complete_check',
                          reason='Fresh original page and full root comments read; expected own top-level comment absent. This does not prove rejection or failed delivery.')
        return result
    target_rows = [r for r in rows if r.get('commentId') == intent['recipient_comment_id']]
    if len(target_rows) != 1 or profile_uid(target_rows[0].get('authorHrefRaw')) != intent['recipient_uid']:
        result['reason'] = 'Exact recipient comment and UID not visible'
        return result
    target = target_rows[0]
    if (target.get('parentCommentId') or target.get('commentId')) != intent['root_comment_id']:
        result['reason'] = 'Recipient parent thread differs'
        return result
    own_rows = [r for r in rows if profile_uid(r.get('authorHrefRaw')) == intent['account_uid']
                and r.get('parentCommentId') == intent['root_comment_id']]
    namesakes = {profile_uid(r.get('authorHrefRaw')) for r in rows
                 if r.get('author') == intent['recipient_name']}
    for row in own_rows:
        direct = row.get('replyToCommentId')
        recipient_matches = (direct == intent['recipient_comment_id'] if direct else
                             row.get('reply_to') == intent['recipient_name']
                             and namesakes <= {intent['recipient_uid']})
        if row.get('commentId') and recipient_matches and normal(reply_text(row, intent['recipient_name'])) == normal(intent['message']):
            result['matching_comment_ids'].append(row['commentId'])
    if result['matching_comment_ids']:
        result.update(outcome='visible_confirmed', reason='Own UID, exact message, recipient and parent thread match')
        return result
    if own_rows:
        result['reason'] = 'Own replies in target thread need manual text or recipient reconciliation'
        return result
    coverage = latest.get('deepCoverage', {})
    threads = [t for t in coverage.get('threads', []) if t.get('commentId') == intent['root_comment_id']]
    thread = threads[0] if len(threads) == 1 else {}
    thread_rows = [r for r in rows if r.get('commentId') == intent['root_comment_id']
                   or r.get('parentCommentId') == intent['root_comment_id']]
    identities_readable = all(profile_uid(r.get('authorHrefRaw')) and r.get('text') for r in thread_rows)
    fully_visible = (coverage.get('rootPaginationState') == 'complete'
                     and coverage.get('explicitEnd') is True
                     and coverage.get('remainingThreads') == []
                     and thread.get('contentCaptured') is True
                     and thread.get('replyContentCaptured') is True
                     and thread.get('replyIdentityComplete') is True
                     and identities_readable
                     and not thread.get('replyBodyVerificationPending')
                     and not any(thread.get(k) for k in ('expanders', 'replyBodyGaps', 'currentReplyBodyGaps')))
    if fully_visible and review.get('fresh_page_loaded') is True and review.get('target_thread_fully_read') is True:
        result.update(outcome='not_visible_after_complete_check',
                      reason='Fresh original page and full thread read; expected own reply absent. This does not prove rejection or failed delivery.')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--intent', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import xhs_research as legacy
    legacy.research_path(args.output)
    result = assess(json.loads(args.intent.read_text()), json.loads(args.review.read_text()))
    require(not args.output.exists(), 'Keep previous verification results; choose a new output')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'outcome': result['outcome'], 'output': str(args.output),
                      'matching_comment_ids': result['matching_comment_ids'], 'resend_permitted': False}))


if __name__ == '__main__':
    main()
