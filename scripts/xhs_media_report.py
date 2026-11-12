"""Render only hash-bound, actually read media observations; no case templates."""
import hashlib
import json
from pathlib import Path
import re


def checked(path, sha):
    p = Path(path)
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('Expected media/source SHA256')
    if not p.is_file() or p.is_symlink():
        raise ValueError('Expected regular evidence file: ' + str(p))
    raw = p.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError('Media/source changed: ' + str(p))
    return raw


def observation(entry):
    """A review is evidence, not instructions; reject absent identities and text."""
    review = json.loads(checked(entry['file'], entry['sha256']))
    if review.get('comment_id') or review.get('commentId') or review.get('kind') in {'video', 'comment_image'}:
        raise ValueError('Body media renderer cannot render comments or video')
    note = review.get('note_id')
    position = review.get('position')
    if not isinstance(note, str) or not re.fullmatch('[0-9a-f]{24}', note):
        raise ValueError('Missing exact media note ID')
    if type(position) is not int or position < 1:
        raise ValueError('Missing positive media position')
    if not (review.get('actual_visual_review') is True or review.get('actual_visual_read') is True):
        raise ValueError('Media has no actual reading attestation')
    text = review.get('observation') or review.get('summary')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Missing actual media observation; never use another case as fallback')
    checked(review['file'], review['file_sha256'])
    # The sidecar binds the photograph to the original target and carousel slot.
    if review.get('capture_metadata'):
        meta = json.loads(checked(review['capture_metadata'], review['capture_metadata_sha256']))
        if meta.get('position') != position:
            raise ValueError('Media position disagrees with capture metadata')
        if meta.get('kind') != 'image':
            raise ValueError('Expected body image capture, not a video frame')
        if 'sha256' in meta and meta['sha256'] != review['file_sha256']:
            raise ValueError('Media bytes disagree with capture metadata')
        if 'sha256' not in meta:
            # Legacy sidecars precede the hash field; compare their actual file bytes.
            checked(meta['file'], review['file_sha256'])
        from urllib.parse import urlparse
        url = urlparse(meta.get('url', ''))
        if url.scheme != 'https' or url.hostname not in {'www.xiaohongshu.com', 'xiaohongshu.com'}:
            raise ValueError('Unconfirmed original note URL')
        path = url.path.rstrip('/')
        target = meta.get('activation', {}).get('target_note_id')
        if not re.fullmatch(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/' + note, path):
            raise ValueError('Media note disagrees with capture metadata')
        if target and target != note:
            raise ValueError('Conflicting capture target')
    return {'note_id': note, 'position': position, 'observation': text.strip(),
            'image_file': review['file'], 'image_sha256': review['file_sha256'],
            'review_file': entry['file'], 'review_sha256': entry['sha256'],
            'whole_media_scope_complete': False}


def render_media_reviews(entries, titles=None):
    """Return the complete original observation, once per evidence version."""
    titles = titles or {}
    rows = []
    seen = set()
    for entry in entries:
        row = observation(entry)
        key = (row['note_id'], row['position'], row['review_sha256'])
        if key not in seen:
            seen.add(key)
            rows.append(row)
    paragraphs = []
    for row in rows:
        title = titles.get(row['note_id'], row['note_id'])
        title = str(title).replace('\n', ' ').replace('[', '［').replace(']', '］')
        paragraphs.append(f"{title} · 正文图{row['position']}（笔记 {row['note_id']}）："
                          f"[原图](<{row['image_file']}>)、[视觉回执](<{row['review_file']}>)。"
                          f"{row['observation']}\n\n"
                          "仅记上述位置的实际审读；其他图片、评论附件和视频的完成状态另计。\n\n")
    return ''.join(paragraphs), rows
