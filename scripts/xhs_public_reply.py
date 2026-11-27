#!/usr/bin/env python3
"""Explicit opt-in, tab-scoped, single-shot public comment/reply helper.

Default skill tools (xhs_bridge.py, xhs_gemini_pipeline.py) remain read-only.
This module requires an immutable saved intent JSON, cross-account recipient UID
deduplication against an external ledger, a pre-send journal lock, and explicit
--execute before performing a single bounded DOM click on the bound detail tab.
It never retries an uncertain send outcome, never touches direct messages or
follows, and immediately triggers shared account STOP on any captcha/access warning.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

import xhs_account_identity as identity
import xhs_bridge as bridge
import xhs_project as xp
import xhs_research as legacy

HEX_24_RE = re.compile(r'^[a-f0-9]{24}$')
ALLOWED_DESTINATIONS = {'reply_to_comment', 'top_level_comment'}
BLOCKING_LEDGER_STATUSES = {
    'sent_confirmed',
    'send_result_unknown',
    'unknown_after_send',
    'unknown_after_send_scan_verification_no_retry',
    'reserved_action_not_confirmed',
    'not_visible_after_complete_check',
    'sent_awaiting_verification',
    'send_dispatched_outcome_pending',
    'send_outcome_unknown_no_retry',
    'access_stop_after_send_attempt',
}
DEFAULT_COOLDOWN_SECONDS = 90.0


class PublicReplyError(legacy.ResearchError):
    def __init__(self, code: str, detail: str = ''):
        self.code = code
        super().__init__(f'{code}: {detail}' if detail else code)


def _require(cond: bool, code: str, detail: str = '') -> None:
    if not cond:
        raise PublicReplyError(code, detail)


def _replies_dir(project: Path) -> Path:
    root = Path(project).resolve() / 'bridge' / 'public-replies'
    if root.is_symlink() or root.parent.is_symlink():
        raise PublicReplyError('UNSAFE_REPLY_JOURNAL_PATH')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PublicReplyError('UNSAFE_REPLY_JOURNAL_PATH')
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    _require(dt.tzinfo is not None, 'TIMEZONE_REQUIRED', str(value))
    return dt


def load_and_validate_intent(intent_path: Path) -> tuple[dict, str]:
    path = Path(intent_path)
    _require(path.is_file() and not path.is_symlink(), 'INTENT_FILE_REQUIRED', str(path))
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        intent = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise PublicReplyError('INTENT_JSON_INVALID', str(exc)) from None
    _require(isinstance(intent, dict), 'INTENT_JSON_INVALID')

    destination = intent.get('destination') or 'reply_to_comment'
    _require(destination in ALLOWED_DESTINATIONS, 'UNSUPPORTED_DESTINATION', str(destination))
    _require(HEX_24_RE.fullmatch(str(intent.get('note_id') or '')) is not None, 'INVALID_NOTE_ID')
    _require(HEX_24_RE.fullmatch(str(intent.get('recipient_uid') or '')) is not None, 'INVALID_RECIPIENT_UID')
    _require(HEX_24_RE.fullmatch(str(intent.get('account_uid') or '')) is not None, 'INVALID_ACCOUNT_UID')
    _require(intent['recipient_uid'] != intent['account_uid'], 'SELF_REPLY_NOT_ALLOWED')

    url = str(intent.get('url') or '').strip()
    bridge.safe_url(url)
    parsed = urlparse(url)
    expected_path = parsed.path.rstrip('/')
    note_id = intent['note_id']
    _require(expected_path in (f'/explore/{note_id}', f'/search_result/{note_id}'),
             'INTENT_URL_NOTE_MISMATCH', expected_path)

    message = str(intent.get('message') or '').strip()
    _require(bool(message) and len(message) <= 500 and '\x00' not in message, 'INVALID_MESSAGE')
    _require(intent.get('maximum_send_clicks') == 1, 'SINGLE_SEND_CLICK_REQUIRED')
    _require(intent.get('private_messages') is False, 'PRIVATE_MESSAGES_FORBIDDEN')
    _require(intent.get('follow') is False, 'FOLLOW_FORBIDDEN')
    _require(intent.get('join') is False, 'AUTO_JOIN_FORBIDDEN')
    _require(isinstance(intent.get('authorization'), str) and bool(intent['authorization'].strip()),
             'AUTHORIZATION_BASIS_REQUIRED')

    if destination == 'reply_to_comment':
        _require(HEX_24_RE.fullmatch(str(intent.get('root_comment_id') or '')) is not None,
                 'INVALID_ROOT_COMMENT_ID')
        _require(HEX_24_RE.fullmatch(str(intent.get('recipient_comment_id') or '')) is not None,
                 'INVALID_RECIPIENT_COMMENT_ID')
        _require(isinstance(intent.get('recipient_name'), str) and bool(intent['recipient_name'].strip()),
                 'RECIPIENT_NAME_REQUIRED')
    else:
        _require(not intent.get('root_comment_id') and not intent.get('recipient_comment_id'),
                 'TOP_LEVEL_MUST_NOT_SET_REPLY_IDS')

    normalized = dict(intent)
    normalized['destination'] = destination
    normalized['expected_path'] = expected_path
    normalized['message'] = message
    return normalized, digest


def check_ledger_dedupe(ledger_path: Path | None, recipient_uid: str) -> dict:
    """Enforce cross-account recipient UID deduplication against an external ledger."""
    if ledger_path is None:
        return {'checked': False, 'blocked': False, 'reason': None}
    path = Path(ledger_path)
    _require(path.is_file() and not path.is_symlink(), 'LEDGER_FILE_REQUIRED', str(path))
    data = json.loads(path.read_text(encoding='utf-8'))
    _require(isinstance(data, dict), 'LEDGER_JSON_INVALID')

    for entry in data.get('excluded_accounts') or []:
        if isinstance(entry, dict) and str(entry.get('uid') or '').lower() == recipient_uid.lower():
            return {'checked': True, 'blocked': True, 'code': 'RECIPIENT_EXCLUDED_IN_LEDGER',
                    'reason': str(entry.get('reason') or 'excluded_in_ledger')}

    for entry in data.get('attempts') or []:
        if not isinstance(entry, dict) or str(entry.get('uid') or '').lower() != recipient_uid.lower():
            continue
        status = str(entry.get('status') or '')
        clicks = entry.get('send_action_count')
        if status in BLOCKING_LEDGER_STATUSES or (isinstance(clicks, int) and clicks > 0):
            return {'checked': True, 'blocked': True, 'code': 'RECIPIENT_ALREADY_IN_LEDGER',
                    'reason': f'status={status}, send_action_count={clicks}'}
    return {'checked': True, 'blocked': False, 'reason': None}


def check_local_journal_and_cooldown(project: Path, recipient_uid: str, intent_digest: str, *,
                                     cooldown_seconds: float, now_ts: float | None = None) -> dict:
    root = _replies_dir(project)
    by_recipient = root / 'by-recipient' / f'{recipient_uid}.json'
    if by_recipient.exists():
        rec = legacy.read_json(by_recipient, {})
        if rec.get('status') != 'aborted_before_click':
            raise PublicReplyError('RECIPIENT_ALREADY_ATTEMPTED', str(by_recipient))

    by_intent = root / 'by-intent' / f'{intent_digest}.json'
    if by_intent.exists():
        rec = legacy.read_json(by_intent, {})
        if rec.get('status') != 'aborted_before_click':
            raise PublicReplyError('INTENT_ALREADY_ATTEMPTED', str(by_intent))

    journal = root / 'journal.jsonl'
    latest_click_ts = None
    if journal.is_file():
        for line in journal.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get('send_clicked') is True and ev.get('clicked_at'):
                ts = _parse_iso(ev['clicked_at']).timestamp()
                if latest_click_ts is None or ts > latest_click_ts:
                    latest_click_ts = ts

    now_val = time.time() if now_ts is None else now_ts
    elapsed = None if latest_click_ts is None else max(0.0, now_val - latest_click_ts)
    if elapsed is not None and elapsed < cooldown_seconds:
        remaining = round(cooldown_seconds - elapsed, 1)
        raise PublicReplyError('COOLDOWN_ACTIVE', f'Wait {remaining}s before next public reply')
    return {'elapsed_since_last_send': elapsed, 'cooldown_seconds': cooldown_seconds}


def _ensure_detail_note_loaded(project: Path, binding_name: str, b: dict, driver: bridge.Driver,
                               url: str, expected_path: str, *, force_reload: bool = False,
                               sleep=time.sleep) -> dict:
    meta = bridge.verify_tab(driver, b, 'detail')
    current_path = bridge.target_path(urlparse(meta['url']).path)
    want_path = bridge.target_path(expected_path)
    if current_path != want_path:
        b['tabs']['detail']['navigation_intent'] = {'url': url, 'at': legacy.now()}
        bridge.save_binding(project, binding_name, b)
        driver.call('navigate', b, 'detail', url=url)
        sleep(1.5)
        for attempt in range(4):
            try:
                meta = bridge.verify_tab(driver, b, 'detail')
                break
            except bridge.BridgeError as exc:
                if exc.code not in {'CONTENT_LOADING', 'IDENTITY_UNCONFIRMED'} or attempt == 3:
                    raise
                sleep(1.0)
        bridge.save_binding(project, binding_name, b)
    elif force_reload:
        driver.call('reload', b, 'detail')
        sleep(1.5)
        for attempt in range(4):
            try:
                meta = bridge.verify_tab(driver, b, 'detail')
                break
            except bridge.BridgeError as exc:
                if exc.code not in {'CONTENT_LOADING', 'IDENTITY_UNCONFIRMED'} or attempt == 3:
                    raise
                sleep(1.0)
        bridge.save_binding(project, binding_name, b)
    return meta


def _reply_driver_fields(intent: dict, *, execute: bool = False, intent_digest: str = '') -> dict:
    fields = {
        'expectedPath': intent['expected_path'],
        'destination': intent['destination'],
        'recipientUid': intent['recipient_uid'],
        'recipientName': intent.get('recipient_name') or '',
        'recipientCommentId': intent.get('recipient_comment_id') or '',
        'rootCommentId': intent.get('root_comment_id') or '',
        'message': intent['message'],
    }
    if execute:
        fields['execute'] = True
        fields['intentDigest'] = intent_digest
    return fields


def prepare_public_reply(project: Path, binding_name: str, intent_path: Path, *,
                         ledger_path: Path | None = None, locate_comment: bool = False,
                         driver=None, sleep=time.sleep) -> dict:
    project = Path(project).resolve()
    intent, digest = load_and_validate_intent(intent_path)
    dedupe = check_ledger_dedupe(ledger_path, intent['recipient_uid'])
    with xp.project_lock(project):
        b = bridge.load_binding(project, binding_name)
        _require(b.get('account_uid') == intent['account_uid'], 'BINDING_ACCOUNT_MISMATCH')
        owned_driver = driver is None
        drv = driver or bridge.Driver(b['runtime'])
        try:
            with bridge.lease(project, b) as keys:
                try:
                    _ensure_detail_note_loaded(project, binding_name, b, drv, intent['url'],
                                               intent['expected_path'], sleep=sleep)
                    res = drv.call('public_reply_prepare', b, 'detail', **_reply_driver_fields(intent))
                    if (locate_comment and intent['destination'] == 'reply_to_comment'
                            and res.get('gap') == 'RECIPIENT_COMMENT_NOT_RENDERED'):
                        drv.call('deep', b, 'detail', url=intent['url'],
                                 deepRoots=40, deepReplyClicks=4, deepBudgetMs=10000)
                        sleep(0.5)
                        res = drv.call('public_reply_prepare', b, 'detail', **_reply_driver_fields(intent))
                except Exception as exc:
                    bridge.save_failure(project, binding_name, b, exc, keys)
                    raise
        finally:
            if owned_driver:
                drv.close()
    ready = bool(res.get('readyForSubmit')) and not dedupe.get('blocked')
    return {
        'ok': True,
        'action': 'prepare',
        'binding': binding_name,
        'intent_file': str(Path(intent_path).resolve()),
        'intent_sha256': digest,
        'destination': intent['destination'],
        'note_id': intent['note_id'],
        'recipient_uid': intent['recipient_uid'],
        'account_uid': intent['account_uid'],
        'ledger_dedupe': dedupe,
        'preflight': res,
        'ready_to_execute': ready,
        'send_clicked': False,
        'checked_at': legacy.now(),
    }


def send_public_reply(project: Path, binding_name: str, intent_path: Path, ledger_path: Path, *,
                      execute: bool = False, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                      locate_comment: bool = False, driver=None, sleep=time.sleep,
                      now_ts: float | None = None) -> dict:
    project = Path(project).resolve()
    intent, digest = load_and_validate_intent(intent_path)
    dedupe = check_ledger_dedupe(ledger_path, intent['recipient_uid'])
    if dedupe.get('blocked'):
        raise PublicReplyError(dedupe['code'], dedupe.get('reason') or '')

    if not execute:
        prep = prepare_public_reply(project, binding_name, intent_path, ledger_path=ledger_path,
                                    locate_comment=locate_comment, driver=driver, sleep=sleep)
        return dict(prep, action='send_dry_run', dry_run=True,
                    note='Pass --execute to perform exactly one guarded DOM send click.')

    _require(cooldown_seconds >= 0, 'INVALID_COOLDOWN')
    with xp.project_lock(project):
        b = bridge.load_binding(project, binding_name)
        _require(b.get('account_uid') == intent['account_uid'], 'BINDING_ACCOUNT_MISMATCH')
        cooldown_info = check_local_journal_and_cooldown(
            project, intent['recipient_uid'], digest, cooldown_seconds=cooldown_seconds, now_ts=now_ts
        )

        root = _replies_dir(project)
        (root / 'by-recipient').mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / 'by-intent').mkdir(parents=True, exist_ok=True, mode=0o700)
        rec_path = root / 'by-recipient' / f'{intent["recipient_uid"]}.json'
        int_path = root / 'by-intent' / f'{digest}.json'

        owned_driver = driver is None
        drv = driver or bridge.Driver(b['runtime'])
        try:
            with bridge.lease(project, b) as keys:
                try:
                    _ensure_detail_note_loaded(project, binding_name, b, drv, intent['url'],
                                               intent['expected_path'], sleep=sleep)
                    prep = drv.call('public_reply_prepare', b, 'detail', **_reply_driver_fields(intent))
                    if (locate_comment and intent['destination'] == 'reply_to_comment'
                            and prep.get('gap') == 'RECIPIENT_COMMENT_NOT_RENDERED'):
                        drv.call('deep', b, 'detail', url=intent['url'],
                                 deepRoots=40, deepReplyClicks=4, deepBudgetMs=10000)
                        sleep(0.5)
                        prep = drv.call('public_reply_prepare', b, 'detail', **_reply_driver_fields(intent))
                    if not prep.get('readyForSubmit'):
                        raise PublicReplyError('PREFLIGHT_NOT_READY',
                                               json.dumps({'gap': prep.get('gap'), 'gaps': prep.get('gaps'),
                                                           'own_in_scope': prep.get('ownCommentsInTargetScopeCount')},
                                                          ensure_ascii=False))

                    # Pre-send immutable journal BEFORE the single click.
                    dispatch_record = {
                        'status': 'send_dispatched_outcome_pending',
                        'dispatched_at': legacy.now(),
                        'binding': binding_name,
                        'account_uid': intent['account_uid'],
                        'recipient_uid': intent['recipient_uid'],
                        'note_id': intent['note_id'],
                        'destination': intent['destination'],
                        'intent_file': str(Path(intent_path).resolve()),
                        'intent_sha256': digest,
                        'maximum_send_clicks': 1,
                        'never_resend': True,
                    }
                    legacy.write_json(rec_path, dispatch_record)
                    legacy.write_json(int_path, dispatch_record)
                except Exception as exc:
                    bridge.save_failure(project, binding_name, b, exc, keys)
                    raise

                # Dispatch exactly once; never retry on unknown outcome.
                try:
                    res = drv.call('public_reply_submit', b, 'detail',
                                   **_reply_driver_fields(intent, execute=True, intent_digest=digest))
                except Exception as exc:
                    is_stop = isinstance(exc, bridge.BridgeError) and exc.code == 'ACCESS_STOP'
                    fail_rec = dict(dispatch_record,
                                    status='access_stop_after_send_attempt' if is_stop else 'send_outcome_unknown_no_retry',
                                    error=str(exc), failed_at=legacy.now(), send_clicked=None, never_resend=True)
                    legacy.write_json(rec_path, fail_rec)
                    legacy.write_json(int_path, fail_rec)
                    _append_jsonl(root / 'journal.jsonl', fail_rec)
                    bridge.save_failure(project, binding_name, b, exc, keys)
                    raise

                if res.get('sendClicked') is False:
                    abort_rec = dict(dispatch_record, status='aborted_before_click',
                                     aborted_at=legacy.now(), send_clicked=False,
                                     never_resend=False, driver_result=res)
                    legacy.write_json(rec_path, abort_rec)
                    legacy.write_json(int_path, abort_rec)
                    _append_jsonl(root / 'journal.jsonl', abort_rec)
                    return {
                        'ok': False,
                        'action': 'send',
                        'status': 'aborted_before_click',
                        'send_clicked': False,
                        'gap': res.get('gap') or res.get('error'),
                        'driver_result': res,
                        'intent_sha256': digest,
                    }

                if res.get('captchaObserved') is True:
                    stop_rec = dict(dispatch_record, status='access_stop_after_send_attempt',
                                    clicked_at=res.get('clickedAt') or legacy.now(),
                                    send_clicked=True, never_resend=True,
                                    captcha_observed=True,
                                    access_warning_text=res.get('accessWarningText', ''),
                                    driver_result=res)
                    legacy.write_json(rec_path, stop_rec)
                    legacy.write_json(int_path, stop_rec)
                    _append_jsonl(root / 'journal.jsonl', stop_rec)
                    identity.stop_all(keys, 'Observed captcha/verification or access restriction during public reply')

                final_rec = dict(dispatch_record,
                                 status='sent_awaiting_verification',
                                 clicked_at=res.get('clickedAt') or legacy.now(),
                                 send_clicked=True,
                                 clicks_submitted=1,
                                 never_resend=True,
                                 cooldown=cooldown_info,
                                 driver_result=res)
                legacy.write_json(rec_path, final_rec)
                legacy.write_json(int_path, final_rec)
                _append_jsonl(root / 'journal.jsonl', final_rec)
                return {
                    'ok': True,
                    'action': 'send',
                    'status': 'sent_awaiting_verification',
                    'binding': binding_name,
                    'account_uid': intent['account_uid'],
                    'recipient_uid': intent['recipient_uid'],
                    'note_id': intent['note_id'],
                    'destination': intent['destination'],
                    'intent_sha256': digest,
                    'send_clicked': True,
                    'clicks_submitted': 1,
                    'clicked_at': final_rec['clicked_at'],
                    'never_resend': True,
                    'verification_required': True,
                    'driver_result': res,
                }
        finally:
            if owned_driver:
                drv.close()


def capture_verification_snapshot(project: Path, binding_name: str, intent_path: Path,
                                  output_capture: Path, *, reload_page: bool = True,
                                  max_passes: int = 3, driver=None, sleep=time.sleep) -> dict:
    """Capture a fresh read-only post-send comment snapshot for xhs_public_reply_check.py."""
    project = Path(project).resolve()
    out = Path(output_capture).resolve()
    legacy.research_path(out)
    _require(not out.exists(), 'OUTPUT_ALREADY_EXISTS', str(out))
    intent, digest = load_and_validate_intent(intent_path)
    _require(1 <= max_passes <= 8, 'INVALID_MAX_PASSES')

    with xp.project_lock(project):
        b = bridge.load_binding(project, binding_name)
        _require(b.get('account_uid') == intent['account_uid'], 'BINDING_ACCOUNT_MISMATCH')
        owned_driver = driver is None
        drv = driver or bridge.Driver(b['runtime'])
        try:
            with bridge.lease(project, b) as keys:
                try:
                    _ensure_detail_note_loaded(project, binding_name, b, drv, intent['url'],
                                               intent['expected_path'], force_reload=reload_page, sleep=sleep)
                    progress = {}
                    latest_data = None
                    for idx in range(max_passes):
                        data = drv.call('deep', b, 'detail', url=intent['url'], progress=progress,
                                        deepRoots=50, deepReplyClicks=5, deepBudgetMs=14000)
                        latest_data = data
                        cov = data.get('deepCoverage') or {}
                        # Stop early if our own matching comment is already visible OR full pagination ended.
                        own_visible = any(
                            intent['account_uid'] in str(r.get('authorHrefRaw') or '').lower()
                            for r in (data.get('results') or [])
                        )
                        if own_visible or (cov.get('rootPaginationState') == 'complete'
                                           and cov.get('explicitEnd') is True
                                           and not cov.get('remainingThreads')):
                            break
                        if idx < max_passes - 1:
                            progress = {'observedRootIds': cov.get('observedRootIds') or []}
                            sleep(1.5)
                except Exception as exc:
                    bridge.save_failure(project, binding_name, b, exc, keys)
                    raise
        finally:
            if owned_driver:
                drv.close()

    _require(isinstance(latest_data, dict), 'VERIFICATION_CAPTURE_EMPTY')
    payload = dict(latest_data)
    payload.setdefault('url', intent['url'])
    observed_at = legacy.now()
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    with out.open('xb') as stream:
        stream.write(raw)
    sha = hashlib.sha256(raw).hexdigest()
    cov = payload.get('deepCoverage') or {}
    return {
        'ok': True,
        'action': 'verify_capture',
        'file': str(out),
        'sha256': sha,
        'observed_at': observed_at,
        'account_uid': intent['account_uid'],
        'note_id': intent['note_id'],
        'intent_sha256': digest,
        'fresh_page_loaded': bool(reload_page),
        'results_count': len(payload.get('results') or []),
        'root_pagination_state': cov.get('rootPaginationState'),
        'explicit_end': cov.get('explicitEnd'),
        'remaining_threads': len(cov.get('remainingThreads') or []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)

    p_prep = sub.add_parser('prepare', help='Read-only target, composer, and ledger dedupe preflight.')
    p_prep.add_argument('--binding', required=True)
    p_prep.add_argument('--intent', required=True, type=Path)
    p_prep.add_argument('--ledger', type=Path)
    p_prep.add_argument('--locate-comment', action='store_true')
    p_prep.add_argument('--output', type=Path)

    p_send = sub.add_parser('send', help='Single-shot guarded DOM public comment/reply submission.')
    p_send.add_argument('--binding', required=True)
    p_send.add_argument('--intent', required=True, type=Path)
    p_send.add_argument('--ledger', required=True, type=Path)
    p_send.add_argument('--output', required=True, type=Path)
    p_send.add_argument('--execute', action='store_true',
                        help='Actually submit once. Without this flag, runs read-only dry-run preflight.')
    p_send.add_argument('--cooldown-seconds', type=float, default=DEFAULT_COOLDOWN_SECONDS)
    p_send.add_argument('--locate-comment', action='store_true')

    p_ver = sub.add_parser('verify-capture', help='Capture fresh post-send read-only comments for verification.')
    p_ver.add_argument('--binding', required=True)
    p_ver.add_argument('--intent', required=True, type=Path)
    p_ver.add_argument('--output-capture', required=True, type=Path)
    p_ver.add_argument('--reload', action=argparse.BooleanOptionalAction, default=True)
    p_ver.add_argument('--max-passes', type=int, default=3)

    args = parser.parse_args()
    if args.command == 'prepare':
        res = prepare_public_reply(args.project, args.binding, args.intent,
                                   ledger_path=args.ledger, locate_comment=args.locate_comment)
        if args.output:
            legacy.research_path(args.output)
            legacy.write_json(args.output, res)
    elif args.command == 'send':
        legacy.research_path(args.output)
        res = send_public_reply(args.project, args.binding, args.intent, args.ledger,
                                execute=args.execute, cooldown_seconds=args.cooldown_seconds,
                                locate_comment=args.locate_comment)
        legacy.write_json(args.output, res)
    else:
        res = capture_verification_snapshot(args.project, args.binding, args.intent,
                                            args.output_capture, reload_page=args.reload,
                                            max_passes=args.max_passes)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (legacy.ResearchError, PublicReplyError, ValueError, OSError) as exc:
        print(json.dumps({'ok': False, 'code': getattr(exc, 'code', type(exc).__name__),
                          'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
