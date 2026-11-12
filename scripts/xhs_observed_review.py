"""Hash-bound human review of already collected comment documents only.

This module never promotes collection/task completion or invokes a transport.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re

from xhs_research import ResearchError

FORMAT = "xiaohongshu-observed-scope-review-v1"
API_IMAGES_FORMAT = "xiaohongshu-api-comment-images-review-v1"
BROWSER_INLINE_IMAGES_FORMAT = "xiaohongshu-browser-inline-images-review-v1"
BROWSER_COMMENT_IMAGES_FORMAT = "xiaohongshu-browser-comment-images-review-v1"
HEX_ID = r"[a-fA-F0-9]{16,40}"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def capture_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ResearchError("Observed visual review requires an explicit valid capture time with timezone") from None


def author_id(row):
    user = row.get("user_info") or row.get("user") or {}
    values = {value for value in [row.get("userId"), user.get("user_id")] if value}
    href = str(row.get("authorHrefRaw", "")).split("?", 1)[0].split("#", 1)[0]
    match = re.fullmatch(r"(?:https://www\.xiaohongshu\.com)?/user/profile/(" + HEX_ID + r")/?", href)
    if match:
        values.add(match[1])
    if len(values) != 1 or not re.fullmatch(HEX_ID, str(next(iter(values), ""))):
        raise ResearchError("Observed review requires one stable public author UID")
    return next(iter(values))


def local_bytes(project, value, prefix=None):
    project = Path(project).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = project / path
    if not path.is_relative_to(project) or any(part == ".." for part in path.parts):
        raise ResearchError("Observed review artifact must remain inside its project")
    current = path
    while current != project:
        if current.is_symlink():
            raise ResearchError("Observed review artifact must not use symlinks")
        current = current.parent
    if prefix and not path.is_relative_to(project / prefix):
        raise ResearchError("Observed review source is outside immutable snapshots")
    try:
        return path, path.read_bytes()
    except OSError:
        raise ResearchError("Observed review artifact is unavailable") from None


def source_rows(record, note_id, comment_id):
    if record.get("note_id") != note_id or record.get("ok") is False:
        return []
    result = []
    if record.get("operation") == "browser_comments" and record.get("ok") is True and isinstance(record.get("data"), list):
        result = [(row, "reply" if row.get("is_reply") else "root_comment", row.get("text")) for row in record["data"]
                  if (row.get("commentId") or row.get("comment_id")) == comment_id]
    elif record.get("operation") == "comments" and isinstance(record.get("data"), dict):
        for root in record["data"].get("comments") or []:
            if root.get("id") == comment_id:
                result.append((root, "root_comment", root.get("content")))
            result.extend((dict(reply, root_comment_id=root.get("id")), "reply", reply.get("content")) for reply in root.get("sub_comments") or []
                          if reply.get("id") == comment_id)
    return result


def relation(row):
    return (row.get("parentCommentId") or row.get("root_comment_id"),
            row.get("replyToCommentId") or (row.get("target_comment") or {}).get("id"))


def current_scope(db, project, task_id):
    task = db.execute("SELECT kind,entity_key FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task["kind"] not in {"browser", "comments"}:
        raise ResearchError("Observed review supports collected browser/comments documents only")
    note_id, cache, documents, needs_visual = task["entity_key"], {}, [], []
    rows = db.execute("SELECT * FROM documents WHERE note_id=? AND kind IN ('root_comment','reply') ORDER BY id", (note_id,)).fetchall()
    if not rows:
        raise ResearchError("No collected comment documents to review")
    for doc in rows:
        prefix = "comment:" + note_id + ":"
        cid = doc["id"].removeprefix(prefix)
        if (doc["identity_kind"] != "platform_id" or not doc["id"].startswith(prefix) or
                not re.fullmatch(HEX_ID, cid)):
            raise ResearchError("Observed review cannot certify snapshot-local or unknown comment identities")
        metadata = json.loads(doc["metadata"])
        uid = author_id(metadata)
        sources, matches_current, capture_times, times_known = [], False, [], True
        refs = db.execute("SELECT DISTINCT s.id,s.path FROM document_sources d JOIN snapshots s ON s.id=d.snapshot_id WHERE d.document_id=? ORDER BY s.id", (doc["id"],)).fetchall()
        if not refs:
            raise ResearchError("Observed review document has no immutable source")
        for ref in refs:
            if ref["id"] not in cache:
                _, content = local_bytes(project, ref["path"], "snapshots")
                if sha(content) != ref["id"]:
                    raise ResearchError("Observed review immutable snapshot checksum changed")
                cache[ref["id"]] = json.loads(content)
            found = source_rows(cache[ref["id"]], note_id, cid)
            if len(found) != 1 or found[0][1] != doc["kind"] or author_id(found[0][0]) != uid:
                raise ResearchError("Observed review source identity is missing, ambiguous or conflicting")
            matches_current |= found[0][2] == doc["text"] and relation(found[0][0]) == relation(metadata)
            try:
                capture_times.append(capture_time(cache[ref["id"]].get("captured_at")))
            except ResearchError:
                times_known = False
            sources.append({"snapshot_id": ref["id"], "sha256": ref["id"]})
        if not matches_current:
            raise ResearchError("Observed review current text has no matching original source")
        documents.append({"document_id": doc["id"], "user_id": uid,
                          "text_sha256": sha(doc["text"].encode()),
                          "context_sha256": sha(canonical(metadata).encode()), "sources": sources,
                          "latest_source_captured_at": max(capture_times).isoformat() if times_known and capture_times else None})
        if (not doc["text"].strip() or doc["text"] == "[图片评论]" or
                metadata.get("renderedReplyPrefixOnly") and not metadata.get("renderedImageLabels") or
                doc["kind"] == "reply" and re.fullmatch(r"回复\s*.+?\s*[:：]", doc["text"].strip())):
            needs_visual.append(doc["id"])
    return {"format": FORMAT, "task_id": task_id, "note_id": note_id,
            "scope": "all_current_collected_comment_documents_only",
            "reviewer": "", "all_current_documents_read": False,
            "documents": documents, "visual_observations": [], "requires_visual": needs_visual,
            "does_not_certify": ["uncollected_comments_or_replies", "whole_task_completion", "other_media_content", "doctor_attribution"]}


def validate_visual(project, entry, document, note_id):
    if set(entry) != {"document_id", "audit_path", "audit_sha256"}:
        raise ResearchError("Visual review must reference one immutable, scoped audit")
    audit_path, content = local_bytes(project, entry["audit_path"])
    if sha(content) != entry["audit_sha256"]:
        raise ResearchError("Visual review audit checksum changed")
    audit = json.loads(content)
    if audit.get("format") == API_IMAGES_FORMAT:
        result = validate_api_images(project, audit, document, note_id)
        return {"document_id": document["document_id"], "audit_path": str(audit_path),
                "audit_sha256": entry["audit_sha256"], **result}
    if audit.get("format") == BROWSER_INLINE_IMAGES_FORMAT:
        result = validate_browser_inline_images(project, audit, document, note_id)
        return {"document_id": document["document_id"], "audit_path": str(audit_path),
                "audit_sha256": entry["audit_sha256"], **result}
    if audit.get("format") == BROWSER_COMMENT_IMAGES_FORMAT:
        result = validate_browser_comment_images(project, audit, document, note_id)
        return {"document_id": document["document_id"], "audit_path": str(audit_path),
                "audit_sha256": entry["audit_sha256"], **result}
    screenshot, raw = audit.get("screenshot", {}), audit.get("raw_automation_source", {})
    cid = document["document_id"].split(":")[-1]
    source_ids = {source["snapshot_id"] for source in document["sources"]}
    if (not audit.get("reviewer") or screenshot.get("actual_view_image") is not True or
            screenshot.get("note_id") != note_id or screenshot.get("comment_id") != cid or
            raw.get("document_id") != document["document_id"] or raw.get("user_id") != document["user_id"] or
            raw.get("snapshot_id") not in source_ids or not audit.get("visual_observation", {}).get("body")):
        raise ResearchError("Visual review must bind an actual image reading to this document and author")
    image_time = capture_time(screenshot.get("captured_at"))
    if image_time < capture_time(document.get("latest_source_captured_at")):
        raise ResearchError("Visual review screenshot predates the latest captured version of this document")
    image_path, image_bytes = local_bytes(project, screenshot.get("file", ""))
    if sha(image_bytes) != screenshot.get("sha256"):
        raise ResearchError("Visual review screenshot checksum changed")
    binding = audit.get("capture_source_record", {})
    _, binding_bytes = local_bytes(project, binding.get("file", ""))
    if sha(binding_bytes) != binding.get("sha256"):
        raise ResearchError("Visual review capture binding checksum changed")
    recorded = json.loads(binding_bytes)
    shot = recorded.get("screenshot", {})
    candidates = [row for row in recorded.get("structure", []) if row.get("commentId") == cid and row.get("matchCount") == 1]
    if (len(candidates) != 1 or shot.get("noteId") != note_id or shot.get("commentId") != cid or
            Path(shot.get("path", "")) != image_path or capture_time(shot.get("capturedAt")) != image_time):
        raise ResearchError("Visual review screenshot is not uniquely bound to this comment")
    return {"document_id": document["document_id"], "audit_path": str(audit_path),
            "audit_sha256": entry["audit_sha256"], "screenshot_sha256": screenshot["sha256"]}


def acquisition_binding(item):
    """Freeze the downloaded resource, not unrelated mutable manifest reviews."""
    fields = ("media_id", "kind", "note_id", "comment_id", "source_path", "locator",
              "observed_url", "resource_id_hint", "resource_fingerprint", "resource_version",
              "file", "sha256", "acquisition_status", "acquired_at")
    return {key: item.get(key) for key in fields}


def validate_api_images(project, audit, document, note_id):
    """Verify a human reading of every image in one original API-only empty body.

    This alternative does not weaken browser screenshot/capture bindings. It
    cannot resolve text prefixes, missing identities, unreadable media, or video.
    """
    from xhs_media import extract_media, resource_identity
    cid = document["document_id"].split(":")[-1]
    raw = audit.get("raw_api_source", {})
    if not isinstance(raw, dict):
        raise ResearchError("API image review source binding must be an object")
    source_ids = {source["snapshot_id"] for source in document["sources"]}
    sid = raw.get("snapshot_id")
    if (not isinstance(audit.get("reviewer"), str) or not audit["reviewer"].strip() or
            audit.get("note_id") != note_id or audit.get("comment_id") != cid or
            raw.get("document_id") != document["document_id"] or raw.get("user_id") != document["user_id"] or
            sid not in source_ids or raw.get("sha256") != sid or
            raw.get("context_sha256") != document["context_sha256"] or
            not isinstance(audit.get("observation"), str) or not audit["observation"].strip()):
        raise ResearchError("API image review must bind the current comment, UID, context and original snapshot")
    source_path, content = local_bytes(project, "snapshots/" + sid + ".json", "snapshots")
    if sha(content) != sid:
        raise ResearchError("API image review source checksum changed")
    record = json.loads(content)
    matches = source_rows(record, note_id, cid)
    if record.get("operation") != "comments" or len(matches) != 1:
        raise ResearchError("API image review requires one original API comment")
    row, _, text = matches[0]
    if (author_id(row) != document["user_id"] or not isinstance(text, str) or text.strip() or
            sha(text.encode()) != document["text_sha256"] or
            sha(canonical(row).encode()) != document["context_sha256"]):
        raise ResearchError("API image review only supports the exact current empty-text comment")
    pictures = row.get("pictures", row.get("images"))
    if not isinstance(pictures, list) or not pictures:
        raise ResearchError("Empty API comment has no original pictures; gap remains")
    media = [item for item in extract_media(record, str(source_path)) if item.get("comment_id") == cid]
    if (len(media) != len(pictures) or any(item.get("kind") != "image" or not item.get("observed_url") for item in media)):
        raise ResearchError("API image audit cannot certify missing URLs or paired video/live-photo content")
    return _validate_comment_image_slots(project, audit, document, note_id, record, row, media,
                                         "comments", "api_comment_images")


def validate_browser_inline_images(project, audit, document, note_id):
    """Read actual downloaded inline images without inventing a browser screenshot."""
    from xhs_media import extract_media
    cid = document["document_id"].split(":")[-1]
    raw = audit.get("raw_browser_source", {})
    if not isinstance(raw, dict):
        raise ResearchError("Browser image review requires a source object")
    sid = raw.get("snapshot_id")
    source_ids = {source["snapshot_id"] for source in document["sources"]}
    if (not isinstance(audit.get("reviewer"), str) or not audit["reviewer"].strip() or
            audit.get("note_id") != note_id or audit.get("comment_id") != cid or
            raw.get("document_id") != document["document_id"] or raw.get("user_id") != document["user_id"] or
            sid not in source_ids or raw.get("sha256") != sid or raw.get("context_sha256") != document["context_sha256"] or
            not isinstance(audit.get("observation"), str) or not audit["observation"].strip()):
        raise ResearchError("Browser image review must bind current comment, UID, context and original snapshot")
    source_path, content = local_bytes(project, "snapshots/" + sid + ".json", "snapshots")
    if sha(content) != sid:
        raise ResearchError("Browser image review source checksum changed")
    record = json.loads(content)
    matches = source_rows(record, note_id, cid)
    if record.get("operation") != "browser_comments" or record.get("ok") is not True or len(matches) != 1:
        raise ResearchError("Browser image review requires one original successful browser comment")
    row, _, text = matches[0]
    parent = row.get("parentCommentId") or ""
    if (author_id(row) != document["user_id"] or not isinstance(text, str) or
            sha(text.encode()) != document["text_sha256"] or sha(canonical(row).encode()) != document["context_sha256"] or
            type(row.get("is_reply")) is not bool or not isinstance(parent, str) or raw.get("parent_comment_id") != parent or
            row["is_reply"] and not re.fullmatch(HEX_ID, parent) or not row["is_reply"] and parent):
        raise ResearchError("Browser image review current text, parent or author binding differs")
    inline = row.get("renderedInlineImages")
    if (not isinstance(inline, list) or not inline or row.get("images") or
            any(not isinstance(image, dict) or image.get("observedClass") != "note-content-emoji" or
                type(image.get("domOrdinal")) is not int or image["domOrdinal"] < 1 or
                not isinstance(image.get("url"), str) or not image["url"] for image in inline) or
            len({image["domOrdinal"] for image in inline}) != len(inline)):
        raise ResearchError("Browser inline review needs all actual DOM slots and URLs; ordinary pictures require separate scope")
    media = [item for item in extract_media(record, str(source_path)) if item.get("comment_id") == cid]
    if (len(media) != len(inline) or any(item.get("kind") != "image" or item.get("media_role") != "inline_emoji" or
                                     not item.get("observed_url") for item in media)):
        raise ResearchError("Browser inline review cannot certify unsupported or missing media")
    return _validate_comment_image_slots(project, audit, document, note_id, record, row, media,
                                         "browser_comments", "browser_inline_images")


def validate_browser_comment_images(project, audit, document, note_id):
    """Bind ordinary browser comment pictures to actual full image readings.

    This separate format cannot stand in for inline emoji, video, hidden text,
    or screenshots. It verifies current source/slot identity without transport.
    """
    from xhs_media import extract_media
    cid = document["document_id"].split(":")[-1]
    raw = audit.get("raw_browser_source", {})
    if not isinstance(raw, dict):
        raise ResearchError("Browser comment image review requires a source object")
    sid = raw.get("snapshot_id")
    source_ids = {source["snapshot_id"] for source in document["sources"]}
    if (not isinstance(audit.get("reviewer"), str) or not audit["reviewer"].strip() or
            audit.get("note_id") != note_id or audit.get("comment_id") != cid or not re.fullmatch(HEX_ID, cid) or
            raw.get("document_id") != document["document_id"] or raw.get("user_id") != document["user_id"] or
            sid not in source_ids or raw.get("sha256") != sid or raw.get("context_sha256") != document["context_sha256"] or
            not isinstance(audit.get("observation"), str) or not audit["observation"].strip()):
        raise ResearchError("Browser comment image review must bind current CID, UID, context and original snapshot")
    source_path, content = local_bytes(project, "snapshots/" + sid + ".json", "snapshots")
    if sha(content) != sid:
        raise ResearchError("Browser comment image review source checksum changed")
    record = json.loads(content)
    matches = source_rows(record, note_id, cid)
    if record.get("operation") != "browser_comments" or record.get("ok") is not True or len(matches) != 1:
        raise ResearchError("Browser comment image review requires one original successful browser comment")
    row, _, text = matches[0]
    parent = row.get("parentCommentId") or ""
    if (author_id(row) != document["user_id"] or not isinstance(text, str) or
            sha(text.encode()) != document["text_sha256"] or sha(canonical(row).encode()) != document["context_sha256"] or
            type(row.get("is_reply")) is not bool or not isinstance(parent, str) or raw.get("parent_comment_id") != parent or
            row["is_reply"] and not re.fullmatch(HEX_ID, parent) or not row["is_reply"] and parent):
        raise ResearchError("Browser comment image review current text, parent or author binding differs")
    pictures = row.get("images")
    if not isinstance(pictures, list) or not pictures or row.get("renderedInlineImages"):
        raise ResearchError("Browser comment image review needs ordinary pictures only; inline scope remains separate")
    media = [item for item in extract_media(record, str(source_path)) if item.get("comment_id") == cid]
    if (len(media) != len(pictures) or any(item.get("kind") != "image" or item.get("media_role") or
                                         not item.get("observed_url") for item in media)):
        raise ResearchError("Browser comment image review cannot certify missing URLs or paired video/live-photo content")
    return _validate_comment_image_slots(project, audit, document, note_id, record, row, media,
                                         "browser_comments", "browser_comment_images")


def _validate_comment_image_slots(project, audit, document, note_id, record, row, media, source_operation, visual_kind):
    from xhs_media import extract_media, resource_identity
    cid = document["document_id"].split(":")[-1]
    source_ids = {source["snapshot_id"] for source in document["sources"]}
    expected = {item["media_id"]: item for item in media}
    images = audit.get("images")
    if (len(expected) != len(media) or not isinstance(images, list) or
            any(not isinstance(image, dict) for image in images) or
            len(images) != len(expected) or {image.get("media_id") for image in images} != set(expected)):
        raise ResearchError("Comment image review must cover every original picture slot exactly once")
    checked = []
    for image in images:
        original = expected[image["media_id"]]
        if (image.get("locator") != original["locator"] or
                image.get("resource_fingerprint") != resource_identity(original) or
                image.get("actual_view_image") is not True or image.get("review_status") != "reviewed" or
                not isinstance(image.get("observation"), str) or not image["observation"].strip()):
            raise ResearchError("Comment image review needs each current resource and a complete actual image reading")
        manifest_path, manifest_bytes = local_bytes(project, image.get("manifest_path", ""))
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), dict):
            raise ResearchError("Comment image review needs an existing media manifest")
        item = manifest.get("items", {}).get(image["media_id"], {})
        if (not isinstance(item, dict) or manifest.get("format") != "xiaohongshu-media-v2" or
                item.get("acquisition_status") != "downloaded" or item.get("review_status") != "reviewed" or
                any(item.get(key) != original.get(key) for key in ("media_id", "kind", "note_id", "comment_id", "locator")) or
                resource_identity(item) != resource_identity(original) or
                item.get("resource_fingerprint") != resource_identity(item) or
                sha(canonical(acquisition_binding(item)).encode()) != image.get("acquisition_sha256")):
            raise ResearchError("Comment image review download binding changed or is incomplete")
        if visual_kind == "browser_inline_images" and (
                type(image.get("dom_ordinal")) is not int or type(item.get("dom_ordinal")) is not int or
                image.get("dom_ordinal") != original.get("dom_ordinal") or item.get("dom_ordinal") != original.get("dom_ordinal") or
                item.get("media_role") != "inline_emoji" or type(image.get("resource_version")) is not int or
                type(item.get("resource_version")) is not int or image["resource_version"] < 1 or
                image["resource_version"] != item.get("resource_version")):
            raise ResearchError("Browser inline image ordinal or current resource version differs")
        if visual_kind == "browser_comment_images" and (
                type(image.get("image_ordinal")) is not int or
                image["image_ordinal"] != int(original["locator"].rsplit("/", 1)[-1]) or
                item.get("media_role") or image.get("observed_url") != item.get("observed_url") or
                type(image.get("resource_version")) is not int or type(item.get("resource_version")) is not int or
                image["resource_version"] < 1 or image["resource_version"] != item.get("resource_version")):
            raise ResearchError("Browser comment image ordinal, URL or current resource version differs")
        download_source_path, download_source = local_bytes(project, item.get("source_path", ""), "snapshots")
        download_sid = sha(download_source)
        if download_sid not in source_ids or download_source_path.name != download_sid + ".json":
            raise ResearchError("Comment image download source is not this comment's immutable evidence")
        download_record = json.loads(download_source)
        download_rows = source_rows(download_record, note_id, cid)
        if (download_record.get("operation") != source_operation or len(download_rows) != 1 or
                author_id(download_rows[0][0]) != document["user_id"]):
            raise ResearchError("Comment image download source has a conflicting comment/author")
        if visual_kind in {"browser_inline_images", "browser_comment_images"} and relation(download_rows[0][0]) != relation(row):
            raise ResearchError("Browser image download source has a conflicting parent or reply target")
        download_media = [m for m in extract_media(download_record, str(download_source_path)) if m["media_id"] == image["media_id"]]
        if (len(download_media) != 1 or resource_identity(download_media[0]) != resource_identity(original) or
                item.get("observed_url") not in [download_media[0]["observed_url"], *download_media[0].get("alternatives", [])]):
            raise ResearchError("Comment image download URL/resource was not observed in the linked source")
        file_path, file_bytes = local_bytes(project, image.get("file", ""))
        manifest_file, _ = local_bytes(project, item.get("file", ""))
        if (file_path != manifest_file or not file_bytes or sha(file_bytes) != image.get("sha256") or
                image.get("sha256") != item.get("sha256")):
            raise ResearchError("Comment image review file checksum or acquisition file changed")
        viewed_at = capture_time(image.get("viewed_at"))
        if viewed_at < capture_time(item.get("acquired_at")) or viewed_at < capture_time(record.get("captured_at")):
            raise ResearchError("Comment image reading predates its source or downloaded file")
        checked.append({"media_id": image["media_id"], "locator": image["locator"],
                        "resource_fingerprint": image["resource_fingerprint"], "sha256": image["sha256"],
                        **({"dom_ordinal": image["dom_ordinal"], "resource_version": image["resource_version"]}
                            if visual_kind == "browser_inline_images" else {}),
                         **({"image_ordinal": image["image_ordinal"], "resource_version": image["resource_version"]}
                            if visual_kind == "browser_comment_images" else {})})
    return {"visual_kind": visual_kind, "images": checked, "whole_task_reviewed": False}


def validate_receipt(db, project, task_id, receipt):
    current = current_scope(db, project, task_id)
    if (receipt.get("format") != FORMAT or receipt.get("task_id") != task_id or
            receipt.get("note_id") != current["note_id"] or receipt.get("scope") != current["scope"] or
            not isinstance(receipt.get("reviewer"), str) or not receipt["reviewer"].strip() or
            receipt.get("all_current_documents_read") is not True):
        raise ResearchError("Observed review requires an explicit reader assertion for the exact current scope")
    supplied = receipt.get("documents")
    if not isinstance(supplied, list) or canonical(supplied) != canonical(current["documents"]):
        raise ResearchError("Observed review document set, UID, text/context hash or sources changed")
    visual = receipt.get("visual_observations", [])
    if not isinstance(visual, list):
        raise ResearchError("Observed visual reviews must be a list")
    documents = {row["document_id"]: row for row in current["documents"]}
    seen, checked = set(), []
    for entry in visual:
        did = entry.get("document_id") if isinstance(entry, dict) else None
        if did not in documents or did in seen:
            raise ResearchError("Observed visual review document is unknown or duplicated")
        seen.add(did)
        checked.append(validate_visual(project, entry, documents[did], current["note_id"]))
    if set(current["requires_visual"]) - seen:
        raise ResearchError("Observed review still has unread empty/prefix/media-only bodies")
    return {"state": "current", "scope": current["scope"], "documents": len(documents),
            "visual_documents": len(checked), "reviewer": receipt["reviewer"],
            "whole_task_reviewed": False, "acquisition_status_unchanged": True,
            "does_not_certify": current["does_not_certify"]}


def observed_review_status(db, project):
    latest = {}
    for row in db.execute("SELECT * FROM reviews WHERE decision='observed_scope' ORDER BY id"):
        latest[row["task_id"]] = row
    result = []
    for task_id, row in latest.items():
        try:
            receipt = json.loads(row["annotations"])["observed_scope_review"]
            state = validate_receipt(db, project, task_id, receipt)
        except (ResearchError, ValueError, TypeError, KeyError) as error:
            state = {"state": "stale", "reason": str(error), "whole_task_reviewed": False,
                     "acquisition_status_unchanged": True}
        result.append({"task_id": task_id, "review_id": row["id"], "at": row["at"],
                       "summary": row["summary"], **state})
    return result
