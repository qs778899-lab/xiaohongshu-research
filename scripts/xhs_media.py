#!/usr/bin/env python3
"""Observed public media, local transcription, and explicit human/agent review.

No login extraction, guessed media URLs, cloud upload, or automatic attribution.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

FORMAT = "xiaohongshu-media-v2"
LIMITS = {"image": 25 * 1024**2, "video": 250 * 1024**2, "subtitle": 5 * 1024**2}


class MediaError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def resource_identity(item):
    """Resource version, separate from the logical note/comment media slot.

    Explicit source file/video IDs take priority. Without one, conservatively
    compare the entire observed host/path; only query/fragment are ignored.
    A new signed query is not permission to refresh or retry a blocked URL.
    """
    kind = item.get("kind", "")
    if item.get("resource_id_hint"):
        value = "id:" + str(item["resource_id_hint"])
    elif "inline_caption" in item:
        value = "inline:" + json.dumps(item["inline_caption"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        parsed = urlparse(item.get("observed_url", ""))
        value = "url:" + (parsed.hostname or "").lower() + parsed.path
    captions = item.get("caption_resource_fingerprints", []) if kind == "video" else []
    return digest(kind + "|" + value + ("|captions:" + "|".join(sorted(captions)) if captions else ""))


def artifact_stem(item):
    return item["media_id"] + "-v" + str(item.get("resource_version", 1)) + "-" + resource_identity(item)[:12]


def captured_time(item):
    value = item.get("latest_observed_at") or item.get("captured_at")
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
    except ValueError:
        return None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with open(temporary, "x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def allowed_url(value):
    if not isinstance(value, str):
        return False
    u = urlparse(value)
    host = u.hostname or ""
    try:
        port_ok = u.port in (None, 80, 443)
    except ValueError:
        return False
    return (u.scheme in {"http", "https"} and not u.username and not u.password and port_ok
            and any(host == d or host.endswith("." + d) for d in ("xhscdn.com", "xiaohongshu.com")))


def observed_urls(image):
    if isinstance(image, str):
        return [image] if allowed_url(image) else []
    if not isinstance(image, dict):
        return []
    urls = [image.get("url_default"), image.get("url"), image.get("url_pre")]
    infos = sorted(image.get("info_list", []), key=lambda x: x.get("image_scene") != "WB_DFT")
    urls = [x.get("url") for x in infos] + urls
    return list(dict.fromkeys(x for x in urls if allowed_url(x)))


def extract_media(record, source_path="", audio_variant=False):
    """Extract media only from observed note/comment fields; never synthesize URLs."""
    source_path = str(source_path)
    if not record.get('operation') and isinstance(record.get('results'),list):
        # Rendered bridge snapshots use the same observed-comment media parser.
        # Keep the actual snapshot as provenance; no new URL or network request.
        import re
        match=re.search(r'/(?:explore|search_result|user/profile/[a-f0-9]{24})/([a-f0-9]{24})',record.get('url',''))
        if match:record=dict(record,operation='browser_comments',data=record['results'],note_id=match[1])
    nid = record.get("note_id", "")
    found = []

    def add(kind, locator, urls=(), cid="", inline=None, context="", identity=None, resource_hint=None):
        identity = identity or (nid + "|" + cid + "|" + locator)
        item = {"media_id": digest(identity)[:32], "kind": kind, "note_id": nid,
                "comment_id": cid, "source_path": source_path, "source_url": record.get("url", ""),
                "locator": locator, "context": context, "observed_url": urls[0] if urls else "",
                "alternatives": list(urls[1:]), "acquisition_status": "pending" if urls or inline is not None else "unsupported",
                "review_status": "pending", "captured_at": record.get("captured_at"), "created_at": now()}
        if inline is not None:
            item["inline_caption"] = inline
        if resource_hint:
            item["resource_id_hint"] = str(resource_hint)
        item["resource_fingerprint"] = resource_identity(item)
        item["resource_version"] = 1
        if not urls and inline is None:
            item["reason"] = "no_supported_observed_url"
        found.append(item)

    def pictures(items, prefix, cid="", uncertain=""):
        for index, pic in enumerate(items if isinstance(items, list) else [], 1):
            loc = prefix + "/image/" + str(index)
            # Unknown comment identity stays source-specific; no false cross-snapshot dedup.
            key = (nid + "|" + cid + "|image|" + str(index)) if cid else nid + "|" + uncertain + "|" + loc
            add("image", loc, observed_urls(pic), cid, identity=key,
                resource_hint=pic.get("file_id") if isinstance(pic, dict) else None)
            # A Live Photo contains an independently reviewable video. Encoding
            # variants are alternatives of that one slot, never extra videos.
            stream = pic.get("stream") if isinstance(pic, dict) else None
            streams = [s for group in stream.values() if isinstance(group, list)
                       for s in group if isinstance(s, dict)] if isinstance(stream, dict) else []
            if streams or (isinstance(pic, dict) and pic.get("live_photo") is True):
                def number(value):
                    return value if isinstance(value, (int, float)) else 0
                # Resolve equal-quality variants deterministically; reordered
                # encoding groups must not create a different resource version.
                streams.sort(key=lambda s: (-number(s.get("width")) * number(s.get("height")),
                                            -number(s.get("weight")), str(s.get("master_url", ""))))
                urls = [s.get("master_url") for s in streams]
                urls += [u for s in streams if isinstance(s.get("backup_urls"), list)
                         for u in s["backup_urls"] if isinstance(u, str)]
                urls = list(dict.fromkeys(u for u in urls if allowed_url(u)))
                add("video", loc + "/live_photo", urls, cid,
                    context="Live Photo paired with " + loc,
                    identity=key + "|live_photo",
                    resource_hint=pic.get("video_id"))
                found[-1].update(media_role="live_photo", parent_image_locator=loc)

    def captions(obj, prefix):
        for key in ("subtitle", "subtitles", "subtitle_list", "caption", "captions"):
            value = obj.get(key)
            if not value:
                continue
            # Observed language dictionaries are tracks, not inline caption text.
            if isinstance(value, dict) and not any(k in value for k in ("url", "subtitle_url", "text", "content")):
                for language in sorted(value, key=lambda x: (x != "source", not x.startswith("zh"), x)):
                    captions({key: value[language]}, prefix + "/" + language)
                continue
            values = value if isinstance(value, list) else [value]
            # A sequence of timed text segments is one caption track.
            if values and all(isinstance(x, dict) and ("text" in x or "content" in x) for x in values):
                add("subtitle", prefix + "/" + key, inline=values)
                continue
            for i, entry in enumerate(values, 1):
                u = entry if isinstance(entry, str) else entry.get("url", entry.get("subtitle_url", "")) if isinstance(entry, dict) else ""
                loc = prefix + "/" + key + "/" + str(i)
                if allowed_url(u):
                    if not any(x["kind"] == "subtitle" and x["observed_url"] == u and x["note_id"] == nid for x in found):
                        add("subtitle", loc, [u])
                        if isinstance(entry, dict) and entry.get("language"):
                            found[-1]["language"] = entry["language"]
                elif isinstance(entry, str) and urlparse(entry).scheme:
                    add("subtitle", loc)
                elif isinstance(entry, (str, dict)):
                    add("subtitle", loc, inline=entry)

    op, data = record.get("operation"), record.get("data", {})
    if op == "note" and isinstance(data, dict):
        for item in data.get("items", []):
            note = item.get("note_card", {})
            nid = note.get("note_id", nid or item.get("id", ""))
            pictures(note.get("image_list", []), "note")
            video = note.get("video")
            if isinstance(video, dict):
                containers = []
                for media_key in ("media", "media_v2"):
                    container = video.get(media_key)
                    if isinstance(container, str):
                        try:
                            container = json.loads(container)
                        except (ValueError, TypeError):
                            container = None
                    if isinstance(container, dict):
                        containers.append((media_key, container))
                streams = []
                for media_key, container in containers:
                    stream = container.get("stream", {})
                    if isinstance(stream, dict):
                        streams.extend(s for group in stream.values() if isinstance(group, list)
                                       for s in group if isinstance(s, dict))
                def number(value):
                    return value if isinstance(value, (int, float)) else 0
                streams.sort(key=lambda s: (not (number(s.get("audio_channels")) > 0),
                    -number(s.get("width")) * number(s.get("height")),
                    -number(s.get("weight")), str(s.get("master_url", ""))))
                urls = [s.get("master_url") for s in streams]
                urls += [u for s in streams if isinstance(s.get("backup_urls"), list) for u in s["backup_urls"]]
                urls += [video.get("url"), video.get("play_url")]
                urls = list(dict.fromkeys(u for u in urls if allowed_url(u)))
                add("video", "note/video", urls, context=note.get("desc", ""),
                    resource_hint=next((c.get("video_id") for _, c in containers if c.get("video_id")), None))
                selected = next((s for s in streams if s.get("master_url") == found[-1]["observed_url"]), {})
                found[-1]["selected_stream"] = {k: selected[k] for k in
                    ("audio_channels", "audio_codec", "video_codec", "format", "width", "height", "stream_type") if k in selected}
                captions(video, "note/video")
                for media_key, container in containers:
                    captions(container, "note/video/" + media_key)
                    if isinstance(container.get("video"), dict):
                        captions(container["video"], "note/video/" + media_key + "/video")
            elif note.get("type") == "video":
                add("video", "note/video")
            captions(note, "note")
    elif op == "comments" and isinstance(data, dict):
        def comment(c, ordinal, parent=""):
            cid = c.get("id", "")
            loc = "comment/" + (cid or str(ordinal))
            pictures(c.get("pictures", c.get("images", [])), loc, cid,
                     source_path + "|" + parent + "|" + str(ordinal))
            for i, child in enumerate(c.get("sub_comments", []), 1):
                comment(child, i, loc)
        rows = data.get("comments", [])
        if rows is None and data.get("has_more") is False:
            rows = []
        if not isinstance(rows, list):
            raise MediaError("Unexpected comments collection schema")
        for i, c in enumerate(rows, 1):
            comment(c, i)
    elif op == "browser_comments" and isinstance(data, list):
        for i, c in enumerate(data, 1):
            cid = c.get("comment_id", c.get("commentId", "")) or ""
            pictures(c.get("images", []), "comment/" + (cid or str(i)), cid, source_path + "|" + str(i))
            # Inline emoji are separate from ordinary comment pictures. Preserve
            # repeated occurrences even when their observed resource URL matches.
            for index, pic in enumerate(c.get("renderedInlineImages", []), 1):
                ordinal = pic.get("domOrdinal", index) if isinstance(pic, dict) else index
                loc = "comment/" + (cid or str(i)) + "/inline_emoji/" + str(ordinal)
                key = (nid + "|" + cid + "|inline_emoji|" + str(ordinal)) if cid else nid + "|" + source_path + "|" + str(i) + "|" + loc
                add("image", loc, observed_urls(pic), cid, identity=key)
                found[-1].update(media_role="inline_emoji", dom_ordinal=ordinal)
    # Caption revisions invalidate dependent video transcription/review as well.
    for item in found:
        if item["kind"] == "video" and item.get("media_role") != "live_photo":
            caption_ids = sorted(x["resource_fingerprint"] for x in found
                                 if x["kind"] == "subtitle" and x["note_id"] == item["note_id"])
            if caption_ids:
                item["caption_resource_fingerprints"] = caption_ids
                item["resource_fingerprint"] = resource_identity(item)
    if audio_variant:
        # Explicit local repair for an already preserved silent rendition. This
        # is a distinct task, never a replacement URL or an automatic retry.
        audible = [x for x in found if x["kind"] == "video" and x["locator"] == "note/video"
                   and isinstance(x.get("selected_stream", {}).get("audio_channels"), (int, float))
                   and x["selected_stream"]["audio_channels"] > 0]
        if not audible:
            raise MediaError("No explicitly observed audible note video variant")
        for item in audible:
            item.update(locator="note/video/audio_mux", media_role="audio_mux",
                        media_id=digest(item["note_id"] + "||note/video/audio_mux")[:32])
        return audible + [x for x in found if x["kind"] == "subtitle"]
    return found


def load_manifest(directory):
    directory = Path(directory).expanduser().resolve()
    p = directory / "manifest.json"
    if p.exists():
        manifest = json.loads(p.read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT:
            raise MediaError("Unsupported media manifest")
        return directory, manifest
    return directory, {"format": FORMAT, "created_at": now(), "items": {}}


def refresh_unattempted_url(directory, old, item):
    """Use a later observed URL only before this resource has ever been tried.

    Host/path equality is insufficient for changing a URL: both raw observations
    must supply the same explicit file/video ID. Never turn failure into a retry.
    """
    selected_time = captured_time({"captured_at": old.get("captured_at")})
    latest_time, incoming_time = captured_time(old), captured_time(item)
    if (not old.get("resource_id_hint") or old.get("resource_id_hint") != item.get("resource_id_hint")
            or resource_identity(old) != resource_identity(item)
            or selected_time is None or latest_time is None or incoming_time is None
            or incoming_time <= selected_time or incoming_time < latest_time
            or old.get("acquisition_status") != "pending" or old.get("review_status") != "pending"
            or old.get("chronology_uncertain") or (directory / "STOP.json").exists()
            or old.get("observed_url") == item.get("observed_url") or not allowed_url(item.get("observed_url"))):
        return False
    # updated_at also excludes legacy pending records that have already passed
    # through a mutating acquisition/review operation without an attempt marker.
    activity = {"download_attempted_at", "file", "sha256", "bytes", "acquired_at", "reason", "error",
                "updated_at", "transcript", "transcription_status", "reviewed_at", "review"}
    if activity.intersection(old) or any((directory / "files").glob(old["media_id"] + "*")):
        return False
    history = old.setdefault("historical_observations", [])
    archived = {k: v for k, v in old.items() if k not in {"versions", "historical_observations", "sources"}}
    archived.update(historical_only=True, observation_reason="unattempted_url_superseded")
    history.append(archived)
    for key in ("observed_url", "alternatives", "source_path", "source_url", "captured_at"):
        old[key] = item[key]
    old["observed_url_refreshed_at"] = now()
    return True


def inspect_sources(sources, media_dir, audio_variant=False):
    directory, manifest = load_manifest(media_dir)
    for source in sources:
        source = Path(source).resolve()
        record = json.loads(source.read_text(encoding="utf-8"))
        for item in extract_media(record, source, audio_variant=audio_variant):
            old = manifest["items"].get(item["media_id"])
            if old:
                if resource_identity(old) != resource_identity(item):
                    old_time, incoming_time = captured_time(old), captured_time(item)
                    if old_time is not None and incoming_time is not None and incoming_time < old_time:
                        history = old.setdefault("historical_observations", [])
                        key = (item["resource_fingerprint"], item.get("captured_at"), item["source_path"])
                        if not any((x.get("resource_fingerprint"), x.get("captured_at"), x.get("source_path")) == key for x in history):
                            item["historical_only"] = True
                            history.append(item)
                        continue
                    archived = {k: v for k, v in old.items() if k not in {"versions", "historical_observations"}}
                    archived.setdefault("resource_fingerprint", resource_identity(old))
                    item["versions"] = old.get("versions", []) + [archived]
                    if old.get("historical_observations"):
                        item["historical_observations"] = old["historical_observations"]
                    item["resource_version"] = old.get("resource_version", 1) + 1
                    item["change_reason"] = "observed_resource_changed_reacquire_and_review"
                    if old_time is None or incoming_time is None or incoming_time == old_time:
                        item["chronology_uncertain"] = True
                    # Fresh item carries no old file/transcript/review. Versioned filenames
                    # preserve prior downloaded evidence when the replacement is acquired.
                    manifest["items"][item["media_id"]] = item
                else:
                    refs = old.setdefault("sources", [old["source_path"]])
                    if str(source) not in refs:
                        refs.append(str(source))
                    old_time, incoming_time = captured_time(old), captured_time(item)
                    refresh_unattempted_url(directory, old, item)
                    if incoming_time is not None and (old_time is None or incoming_time > old_time):
                        old["latest_observed_at"] = item.get("latest_observed_at") or item.get("captured_at")
                    # Existing acquisition/review outcomes and attempted URLs stay intact.
            else:
                manifest["items"][item["media_id"]] = item
    write_json(directory / "manifest.json", manifest)
    return manifest


def get_item(directory, media_id):
    directory, manifest = load_manifest(directory)
    if media_id not in manifest["items"]:
        raise MediaError("Unknown media ID; inspect the observed source first")
    return directory, manifest, manifest["items"][media_id]


def save_item(directory, manifest, item):
    item["updated_at"] = now()
    write_json(directory / "manifest.json", manifest)
    return item


class ObservedRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise MediaError("Media redirect outside the permitted public CDN domains")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_media(media_dir, media_id, opener=None, run=None):
    """All real HTTP shares the authorized session's account lock and STOP state."""
    directory, manifest, item = get_item(media_dir, media_id)
    local_only = "inline_caption" in item or item["acquisition_status"] in {"downloaded", "caption_available", "transcribed"}
    if local_only or opener is not None:
        return _download_media(media_dir, media_id, opener)
    if run is None:
        raise MediaError("Public media download requires the active authorized --run for shared access-stop control")
    from xhs_research import account_lock, ensure_active, stop, load_run
    run = load_run(run)
    project = directory.parent
    if (project / 'project.sqlite3').is_file():
        import time
        from xhs_queue import collection_turn
        # Project media joins the same FIFO as search/comments. Legacy standalone
        # media keeps its existing account lock and does not create a project.
        turn = collection_turn(project, run, time.time() + 600,
                               project / 'batch' / 'PAUSE')
    else:
        turn = account_lock(run)
    with turn:
        ensure_active(run)
        result = _download_media(media_dir, media_id)
        if result.get("acquisition_status") == "blocked":
            stop(run, "Public media access restriction: " + result.get("reason", "unknown"))
        return result


def _download_media(media_dir, media_id, opener=None):
    directory, manifest, item = get_item(media_dir, media_id)
    if (directory / "STOP.json").exists():
        raise MediaError("Media acquisition stopped after an access restriction; do not automatically retry")
    if item["acquisition_status"] in {"downloaded", "caption_available", "transcribed"}:
        return item
    if "inline_caption" in item:
        p = directory / "files" / (artifact_stem(item) + ".caption.json")
        write_json(p, item["inline_caption"])
        item.update(acquisition_status="caption_available", file=str(p), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        return save_item(directory, manifest, item)
    u = item.get("observed_url", "")
    if not allowed_url(u):
        item.update(acquisition_status="unsupported", reason="no_supported_observed_url")
        return save_item(directory, manifest, item)
    open_url = opener or build_opener(ObservedRedirects()).open
    target = directory / "files" / (artifact_stem(item) + {"image": ".image", "video": ".video", "subtitle": ".caption"}[item["kind"]])
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_suffix(target.suffix + ".part")
    # Persist before HTTP, including attempts interrupted before a response or file.
    item["download_attempted_at"] = now()
    save_item(directory, manifest, item)
    try:
        request = Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, method="GET")
        with open_url(request, timeout=30) as response:
            final = response.geturl() if hasattr(response, "geturl") else u
            if not allowed_url(final):
                raise MediaError("Unexpected media redirect")
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                item.update(acquisition_status="blocked", reason="unexpected_html_access_state_unconfirmed")
                write_json(directory / "STOP.json", {"at": now(), "reason": item["reason"], "media_id": media_id})
                return save_item(directory, manifest, item)
            maximum = LIMITS[item["kind"]]
            size = int(response.headers.get("Content-Length", 0))
            if size > maximum:
                raise MediaError("Media exceeds bounded download size")
            total, hasher = 0, hashlib.sha256()
            with open(partial, "wb") as output:
                os.chmod(partial, 0o600)
                while True:
                    chunk = response.read(min(1024 * 1024, maximum - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > maximum:
                        raise MediaError("Media exceeds bounded download size")
                    hasher.update(chunk)
                    output.write(chunk)
            if not total:
                raise MediaError("Empty media response")
        os.replace(partial, target)
        item.update(acquisition_status="downloaded", file=str(target), sha256=hasher.hexdigest(), bytes=total,
                    content_type=content_type, acquired_at=now())
        item.pop("reason", None)
    except HTTPError as exc:
        restricted = exc.code in {401, 403, 429, 461, 471}
        item.update(acquisition_status="blocked" if restricted else "unavailable", reason="HTTP_" + str(exc.code))
        if restricted:
            write_json(directory / "STOP.json", {"at": now(), "reason": item["reason"], "media_id": media_id})
    except (URLError, OSError, ValueError, MediaError) as exc:
        item.update(acquisition_status="unavailable", reason=str(exc) if isinstance(exc, MediaError) else type(exc).__name__)
    finally:
        partial.unlink(missing_ok=True)
    return save_item(directory, manifest, item)


def caption_segments(value):
    """Keep raw captions; normalize supported timestamps without inventing timing."""
    if isinstance(value, str):
        entries = []
        for match in re.finditer(r"(?m)(\d{1,2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n(.+?)(?=\n\s*\n|\Z)", value, re.S):
            def seconds(t):
                h, m, s = t.replace(",", ".").split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            entries.append({"start": seconds(match[1]), "end": seconds(match[2]), "text": match[3].strip()})
        return entries or [{"start": None, "end": None, "text": value}]
    if isinstance(value, dict):
        value = value.get("segments", value.get("subtitles", [value]))
    if not isinstance(value, list):
        return []
    return [{"start": x.get("start"), "end": x.get("end"), "text": x.get("text", x.get("content", ""))}
            for x in value if isinstance(x, dict) and ("text" in x or "content" in x)]


def normalize_whisper(value):
    segments = []
    for row in value.get("transcription", []):
        offsets = row.get("offsets", {})
        if "from" not in offsets or "to" not in offsets:
            raise MediaError("Whisper output lacks millisecond offsets")
        if not all(isinstance(offsets[k], (int, float)) for k in ("from", "to")) or not 0 <= offsets["from"] <= offsets["to"]:
            raise MediaError("Whisper output has invalid timestamps")
        segments.append({"start": offsets["from"] / 1000, "end": offsets["to"] / 1000, "text": row.get("text", "")})
    if not segments:
        raise MediaError("Whisper produced no timed speech; inspect the original audio")
    return segments


def transcribe_media(media_dir, media_id, ffmpeg=None, whisper=None, model=None, runner=subprocess.run, cpu=False):
    directory, manifest, item = get_item(media_dir, media_id)
    if item["kind"] not in {"video", "subtitle"}:
        raise MediaError("Images need visual review, not an automatic transcript")
    if item.get("transcript"):
        return item
    (directory / "files").mkdir(parents=True, exist_ok=True, mode=0o700)
    # A note-level caption is not evidence for a paired Live Photo's audio.
    available_captions = sorted((x for x in manifest["items"].values()
                if item.get("media_role") != "live_photo" and x["kind"] == "subtitle"
                and x["note_id"] == item["note_id"] and x.get("file")),
                key=lambda x: ("/source/" not in x.get("locator", ""),
                               not str(x.get("language", "")).startswith("zh"), x.get("locator", "")))
    caption = item if item["kind"] == "subtitle" else next(iter(available_captions), None)
    try:
        if caption:
            if not caption.get("file"):
                raise MediaError("Download or materialize the observed caption before transcription")
            content = Path(caption["file"]).read_text(encoding="utf-8-sig")
            try:
                content = json.loads(content)
            except ValueError:
                pass
            segments = caption_segments(content)
            method = "observed_caption"
            source = caption["media_id"]
        else:
            if not item.get("file"):
                raise MediaError("Download the observed video before transcription")
            if not (ffmpeg and whisper and model and Path(model).is_file()):
                raise MediaError("Local ffmpeg, whisper.cpp and multilingual medium model are required")
            wav = directory / "files" / (artifact_stem(item) + ".wav")
            prefix = directory / "files" / (artifact_stem(item) + ".whisper")
            runner([str(ffmpeg), "-nostdin", "-y", "-i", item["file"], "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   check=True, capture_output=True, timeout=600)
            whisper_args = [str(whisper), "-m", str(model), "-l", "zh", "-f", str(wav), "-oj", "-osrt"]
            if cpu:
                whisper_args.append("-ng")
            whisper_args.extend(["-of", str(prefix)])
            runner(whisper_args,
                   check=True, capture_output=True, timeout=7200)
            segments = normalize_whisper(json.loads(prefix.with_suffix(".whisper.json").read_text(encoding="utf-8")))
            method, source = "local_whisper_cpp", item["file"]
            item.update(audio_file=str(wav), srt=str(prefix.with_suffix(".whisper.srt")), model_path=str(Path(model).resolve()),
                        transcription_device="cpu" if cpu else "default")
        if not segments or not any(x.get("text", "").strip() for x in segments):
            raise MediaError("Caption contains no usable text")
        transcript = directory / "files" / (artifact_stem(item) + ".transcript.json")
        write_json(transcript, {"media_id": media_id, "method": method, "source": source, "segments": segments,
                               "review_status": "pending", "medical_attribution_verified": False,
                               "critical_words_require_original_check": True, "created_at": now()})
        item.update(acquisition_status="transcribed", transcription_status="ready_unreviewed", transcript=str(transcript), transcript_method=method,
                    review_status="pending", transcript_sha256=hashlib.sha256(transcript.read_bytes()).hexdigest())
        item.pop("reason", None)
    except (MediaError, OSError, ValueError, subprocess.SubprocessError) as exc:
        item.update(transcription_status="blocked", reason=str(exc) if isinstance(exc, MediaError) else type(exc).__name__)
    return save_item(directory, manifest, item)


def caption_review_binding(item):
    """Bind a caption reading to the current original file, not a video transcript."""
    if item.get("kind") != "subtitle":
        raise MediaError("Full-caption reading applies only to subtitle media")
    path, expected = item.get("file"), item.get("sha256")
    version = item.get("resource_version", 1)
    if not path or not expected or type(version) is not int or version < 1:
        raise MediaError("Caption reading requires the original file hash and resource version")
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        raise MediaError("Caption original file is unavailable") from None
    if actual != expected:
        raise MediaError("Caption original file changed since capture")
    fingerprint = resource_identity(item)
    if item.get("resource_fingerprint", fingerprint) != fingerprint:
        raise MediaError("Caption resource identity changed")
    return {"media_id": item["media_id"], "kind": "subtitle", "file": str(Path(path).resolve()),
            "file_sha256": actual, "resource_fingerprint": fingerprint, "resource_version": version}


def caption_declaration_valid(segment, binding):
    return (isinstance(segment, dict) and segment.get("full_caption_text") is True and
            segment.get("actual_text_read") is True and isinstance(segment.get("reviewer"), str) and
            bool(segment["reviewer"].strip()) and segment.get("original_audio_checked") is False and
            segment.get("full_video") is not True and isinstance(segment.get("observation"), str) and bool(segment["observation"].strip()) and
            type(segment.get("resource_version")) is int and
            all(segment.get(key) == binding[key] for key in ("file_sha256", "resource_fingerprint", "resource_version")))


def caption_text_review_valid(item):
    """Revalidate an explicit text-only reading; it never certifies original audio."""
    if item.get("kind") != "subtitle" or item.get("review_scope") != "full_caption_text":
        return False
    try:
        binding = caption_review_binding(item)
    except (MediaError, ValueError, TypeError):
        return False
    segments = item.get("checked_segments")
    return (isinstance(segments, list) and item.get("review_binding") == binding and
            any(caption_declaration_valid(segment, binding) for segment in segments
                if isinstance(segment, dict)))


def video_full_review_valid(item):
    if item.get("kind") != "video" or not item.get("file") or not item.get("sha256"):
        return False
    segments = item.get("checked_segments")
    if not isinstance(segments, list) or not any(isinstance(s, dict) and s.get("full_video") is True and
            s.get("full_caption_text") is not True and isinstance(s.get("observation"), str) and s["observation"].strip()
            for s in segments):
        return False
    try:
        hasher = hashlib.sha256()
        with Path(item["file"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == item["sha256"]
    except (OSError, TypeError, ValueError):
        return False


def image_review_valid(item, *, file_cache=None):
    """Check retained image bytes; cache only within one reporting invocation."""
    if item.get("kind") != "image" or not item.get("file") or not item.get("sha256"):
        return False
    try:
        path = Path(item["file"])
        stat = path.stat()
        key = (str(path.resolve()), item["sha256"], stat.st_dev, stat.st_ino,
               stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if file_cache is not None and key in file_cache:
            return file_cache[key]
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        valid = stat.st_size > 0 and hasher.hexdigest() == item["sha256"]
        if file_cache is not None:
            file_cache[key] = valid
        return valid
    except (OSError, TypeError, ValueError):
        return False


def review_binding_gaps(items, *, image_file_cache=None):
    """Read-only effective validity; never erase a prior reading or change tasks."""
    result = []
    for item in items:
        if item.get("review_status") != "reviewed" or item.get("kind") not in {"image", "subtitle", "video"}:
            continue
        valid = (image_review_valid(item, file_cache=image_file_cache) if item["kind"] == "image" else
                 caption_text_review_valid(item) if item["kind"] == "subtitle" else video_full_review_valid(item))
        if not valid:
            result.append({"media_id": item.get("media_id", ""), "kind": item["kind"], "reason": "current_media_review_binding_invalid"})
    return result


def review_scope_counts(items, invalid_ids=None):
    if invalid_ids is None:
        invalid_ids = {gap["media_id"] for gap in review_binding_gaps(items)}
    counts = {"caption_text_reviewed": 0, "caption_text_not_reviewed": 0,
              "caption_reading_does_not_verify_original_audio": 0,
              "video_fully_reviewed": 0, "video_not_fully_reviewed": 0,
              "caption_excluded": 0, "video_excluded": 0}
    for item in items:
        if item.get("kind") in {"subtitle", "video"} and item.get("review_status") in {"irrelevant", "excluded"}:
            counts["caption_excluded" if item["kind"] == "subtitle" else "video_excluded"] += 1
            continue
        if item.get("kind") == "subtitle":
            read = item.get("review_status") == "reviewed" and item.get("media_id", "") not in invalid_ids
            counts["caption_text_reviewed" if read else "caption_text_not_reviewed"] += 1
            if read:
                counts["caption_reading_does_not_verify_original_audio"] += 1
        elif item.get("kind") == "video":
            read = item.get("review_status") == "reviewed" and item.get("media_id", "") not in invalid_ids
            counts["video_fully_reviewed" if read else "video_not_fully_reviewed"] += 1
    return counts


def review_media(media_dir, media_id, observation, verdict, checked_segments=None):
    directory, manifest, item = get_item(media_dir, media_id)
    if verdict not in {"reviewed", "partial", "unreadable", "irrelevant"} or not observation.strip():
        raise MediaError("Review requires a supported verdict and explicit observation")
    if verdict in {"reviewed", "partial"}:
        artifacts = [(item.get("file"), item.get("sha256")), (item.get("transcript"), item.get("transcript_sha256"))]
        existing = [(Path(p), expected) for p, expected in artifacts if p and Path(p).is_file()]
        if not existing:
            raise MediaError("Cannot mark missing/unavailable media reviewed")
        for path, expected in existing:
            if expected:
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                if hasher.hexdigest() != expected:
                    raise MediaError("Media artifact changed since capture; preserve and inspect the new version")
    if verdict in {"reviewed", "partial"} and item["kind"] in {"video", "subtitle"} and not checked_segments:
        raise MediaError("Video/caption review requires checked ranges or an explicit full reading of this media kind")
    caption_binding = None
    if checked_segments:
        if not isinstance(checked_segments, list):
            raise MediaError("Checked segments must be a list")
        for segment in checked_segments:
            timed = isinstance(segment, dict) and all(isinstance(segment.get(k), (int, float)) for k in ("start", "end")) and 0 <= segment["start"] <= segment["end"]
            entire = isinstance(segment, dict) and segment.get("full_video") is True
            caption = isinstance(segment, dict) and segment.get("full_caption_text") is True
            if caption:
                caption_binding = caption_review_binding(item)
                if not caption_declaration_valid(segment, caption_binding):
                    raise MediaError("Caption reading needs actual reader, current file/version hashes and explicit original_audio_checked=false")
            if entire and item["kind"] == "subtitle":
                raise MediaError("Full-video viewing cannot certify subtitle text reading; record it on the video task")
            if not (timed or entire or caption) or not str(segment.get("observation", "")).strip():
                raise MediaError("Each checked segment needs valid timing or an explicit full reading, plus an observation")
    if verdict == "reviewed" and item["kind"] == "video" and not any(x.get("full_video") is True for x in checked_segments):
        raise MediaError("Checked excerpts alone are partial; use verdict=partial or record a full-video observation")
    if verdict == "reviewed" and item["kind"] == "video" and not (item.get("file") and Path(item["file"]).is_file()):
        raise MediaError("Full-video review requires the original video file; a caption/transcript alone is insufficient")
    if verdict == "reviewed" and item["kind"] == "subtitle" and caption_binding is None:
        raise MediaError("Subtitle completion requires an explicit full_caption_text reading of the current file")
    for key in ("review_scope", "review_binding", "reading_limits"):
        item.pop(key, None)
    if verdict == "reviewed" and item["kind"] == "subtitle":
        item.update(review_scope="full_caption_text", review_binding=caption_binding,
                    reading_limits=["original_audio_not_verified_by_caption_reading", "full_video_not_verified_by_caption_reading"])
    item.update(review_status=verdict, observation=observation, checked_segments=checked_segments or [], reviewed_at=now())
    return save_item(directory, manifest, item)


def status(media_dir):
    directory, manifest = load_manifest(media_dir)
    items = list(manifest["items"].values())
    invalid = review_binding_gaps(items)
    invalid_ids = {gap["media_id"] for gap in invalid}
    counts = {}
    for item in items:
        key = item["kind"] + ":" + ("pending" if item["media_id"] in invalid_ids else item["review_status"])
        counts[key] = counts.get(key, 0) + 1
    return {"manifest": str(directory / "manifest.json"), "total": len(items), "counts": counts,
            "review_scopes": review_scope_counts(items, invalid_ids), "invalid_review_bindings": invalid,
            "unresolved": [x["media_id"] for x in items if x["review_status"] not in {"reviewed", "irrelevant"} or x["media_id"] in invalid_ids],
            "stop": json.loads((directory / "STOP.json").read_text()) if (directory / "STOP.json").exists() else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect"); p.add_argument("sources", nargs="+", type=Path)
    p.add_argument("--audio-variant", action="store_true", help="Explicitly register an observed audible rendition separately; no download or replacement")
    p = sub.add_parser("download"); p.add_argument("media_id"); p.add_argument("--run", type=Path)
    p = sub.add_parser("transcribe"); p.add_argument("media_id")
    p.add_argument("--ffmpeg"); p.add_argument("--whisper"); p.add_argument("--model")
    p.add_argument("--cpu", action="store_true", help="Explicit whisper.cpp -ng; retain the same model")
    p = sub.add_parser("review"); p.add_argument("media_id"); p.add_argument("--observation", required=True)
    p.add_argument("--verdict", choices=["reviewed", "partial", "unreadable", "irrelevant"], required=True)
    p.add_argument("--checked-segments", type=Path, help="JSON list of checked original time ranges")
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            inspect_sources(args.sources, args.media_dir, audio_variant=args.audio_variant); result = status(args.media_dir)
        elif args.command == "download": result = download_media(args.media_dir, args.media_id, run=args.run)
        elif args.command == "transcribe": result = transcribe_media(args.media_dir, args.media_id, args.ffmpeg, args.whisper, args.model, cpu=args.cpu)
        elif args.command == "review":
            ranges = json.loads(args.checked_segments.read_text()) if args.checked_segments else None
            result = review_media(args.media_dir, args.media_id, args.observation, args.verdict, ranges)
        else: result = status(args.media_dir)
        # URLs remain in the evidence manifest; do not emit signed query strings in routine logs.
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k not in {"observed_url", "alternatives", "inline_caption", "context"}}
        print(json.dumps(result, ensure_ascii=False))
    except (MediaError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, MediaError) else type(exc).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
