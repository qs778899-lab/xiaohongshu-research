import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import xhs_public_reply_check as check


class ReplyCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'capture.json'
        self.intent = dict(note_id='a'*24, root_comment_id='b'*24, recipient_comment_id='b'*24,
                           recipient_uid='c'*24, recipient_name='Example recipient', account_uid='d'*24,
                           message='A synthetic question?', submitted_at='2026-01-01T01:00:00+00:00')
        self.target = dict(commentId='b'*24, authorHrefRaw='/user/profile/'+'c'*24,
                           parentCommentId='', text='Synthetic invitation')
        self.own = dict(commentId='e'*24, authorHrefRaw='/user/profile/'+'d'*24,
                        parentCommentId='b'*24, reply_to='Example recipient',
                        text='回复 Example recipient : A synthetic question?')
        self.data = dict(url='https://www.xiaohongshu.com/explore/'+'a'*24, results=[self.target],
                         deepCoverage=dict(rootPaginationState='complete', explicitEnd=True, remainingThreads=[],
                           threads=[dict(commentId='b'*24, contentCaptured=True, replyContentCaptured=True,
                                         replyIdentityComplete=True, expanders=[], replyBodyGaps=[], currentReplyBodyGaps=[])]))
        self.review = dict(reader='codex', reviewed=True, account_verified=True, account_uid='d'*24,
                           note_id='a'*24, root_comment_id='b'*24, summary='Read synthetic sources.',
                           reviewed_at='2026-01-01T03:00:00+00:00', fresh_page_loaded=True, target_thread_fully_read=True)

    def run_check(self):
        raw=json.dumps(self.data).encode(); self.path.write_bytes(raw)
        self.review['observations']=[dict(file=str(self.path), sha256=hashlib.sha256(raw).hexdigest(),
                                           observed_at='2026-01-01T02:00:00+00:00')]
        return check.assess(self.intent,self.review)

    def test_exact_reply_and_prefix(self):
        self.data['results'].append(self.own)
        result=self.run_check()
        self.assertEqual(result['outcome'],'visible_confirmed')
        self.assertEqual(result['matching_comment_ids'],['e'*24])
        self.assertFalse(result['resend_permitted'])

    def test_complete_absence_is_not_delivery_failure(self):
        result=self.run_check()
        self.assertEqual(result['outcome'],'not_visible_after_complete_check')
        self.assertFalse(result['publication_failure_confirmed'])
        self.assertFalse(result['resend_permitted'])

    def test_wrong_uid_cannot_confirm(self):
        self.own['authorHrefRaw']='/user/profile/'+'f'*24
        self.data['results'].append(self.own)
        self.assertNotEqual(self.run_check()['outcome'],'visible_confirmed')

    def test_other_thread_cannot_confirm(self):
        self.own['parentCommentId']='f'*24
        self.data['results'].append(self.own)
        self.assertNotEqual(self.run_check()['outcome'],'visible_confirmed')

    def test_conflicting_reply_id_overrides_display_name(self):
        self.own['replyToCommentId']='f'*24
        self.data['results'].append(self.own)
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')

    def test_same_display_name_and_missing_author_need_review(self):
        other=dict(self.target, commentId='f'*24, author='Example recipient', authorHrefRaw='/user/profile/'+'f'*24)
        self.data['results'] += [other,self.own]
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')
        self.data['results']=[self.target,dict(self.own,authorHrefRaw='')]
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')

    def test_wrong_verified_account_is_rejected(self):
        self.review['account_uid']='f'*24
        with self.assertRaisesRegex(ValueError,'Same account'):self.run_check()

    def test_unmatched_own_reply_requires_review(self):
        self.own['text']='Different text'
        self.data['results'].append(self.own)
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')

    def test_incomplete_thread_or_stale_view_cannot_prove_absence(self):
        for field,value in [('remainingThreads',[{}]),('rootPaginationState','stalled'),('explicitEnd',False)]:
            with self.subTest(field=field):
                original=self.data['deepCoverage'][field]
                self.data['deepCoverage'][field]=value
                self.assertEqual(self.run_check()['outcome'],'verification_incomplete')
                self.data['deepCoverage'][field]=original
        self.review['fresh_page_loaded']=False
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')

    def test_missing_reply_body_cannot_prove_absence(self):
        self.data['deepCoverage']['threads'][0]['replyBodyGaps']=[{'reason':'not_rendered'}]
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')

    def test_source_hash_and_time_are_required(self):
        self.run_check()
        self.path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash changed'):check.assess(self.intent,self.review)
        self.run_check()
        self.review['observations'][0]['observed_at']='2026-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(ValueError,'Pre-submission'):check.assess(self.intent,self.review)

    def test_access_warning_and_other_note(self):
        self.data['securityBlock']=True
        self.assertEqual(self.run_check()['outcome'],'verification_incomplete')
        self.data['url']='https://www.xiaohongshu.com/explore/'+'f'*24
        with self.assertRaisesRegex(ValueError,'note mismatch'):self.run_check()


if __name__=='__main__':unittest.main()
