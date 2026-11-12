#!/usr/bin/env python3
"""Offline behavior checks; no network, credentials or real Chrome."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError

import xhs_media as media

URL = "https://sns-webpic-qc.xhscdn.com/observed/image"
VIDEO = "https://sns-video-v2.xhscdn.com/observed/video.mp4?sign=example"


def note():
    return {"operation": "note", "note_id": "a" * 24, "url": "https://www.xiaohongshu.com/explore/" + "a" * 24,
            "captured_at": "2026-10-30T00:00:00Z", "data": {"items": [{"note_card": {
                "note_id": "a" * 24, "desc": "Treatment video", "image_list": [{"url_default": URL}],
                "video": {"media": {"stream": {"EF5": [{"width": 720, "height": 1280, "master_url": VIDEO}]}}}}}]}}


class Response(io.BytesIO):
    def __init__(self, content, url=URL, kind="image/png"):
        super().__init__(content)
        self.headers = {"Content-Type": kind}
        self.url = url
    def geturl(self):
        return self.url


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps(note()))
        self.directory = self.root / "media"
        self.manifest = media.inspect_sources([self.source], self.directory)
        self.items = list(self.manifest["items"].values())
        self.image = next(x for x in self.items if x["kind"] == "image")
        self.video = next(x for x in self.items if x["kind"] == "video")
    def tearDown(self):
        self.tmp.cleanup()

    def test_source_locator_and_stable_id_survive_url_expiry(self):
        record = note()
        record["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "?new=1"
        again = media.extract_media(record, "new-snapshot.json")
        self.assertEqual(self.image["media_id"], again[0]["media_id"])
        self.assertEqual(again[0]["locator"], "note/image/1")
        self.assertEqual(again[0]["review_status"], "pending")

    def test_comment_identity_conservative(self):
        raw = {"operation": "comments", "note_id": "a" * 24,
               "data": {"comments": [{"id": "c1", "pictures": [{"url_default": URL}],
                                     "sub_comments": [{"pictures": [{"url_default": URL}]}]}]}}
        first, second = media.extract_media(raw, "one.json"), media.extract_media(raw, "two.json")
        self.assertEqual(first[0]["media_id"], second[0]["media_id"])
        self.assertNotEqual(first[1]["media_id"], second[1]["media_id"])
        self.assertEqual(first[1]["comment_id"], "")

    def test_all_images_not_only_cover(self):
        raw = note()
        raw["data"]["items"][0]["note_card"]["image_list"] *= 6
        result = media.extract_media(raw)
        self.assertEqual(len([x for x in result if x["kind"] == "image"]), 6)
        self.assertEqual(len({x["media_id"] for x in result}), 7)

    def test_public_download_no_credentials_and_hash(self):
        seen = []
        def opener(req, timeout):
            seen.append(req)
            return Response(b"image bytes")
        result = media.download_media(self.directory, self.image["media_id"], opener)
        self.assertEqual(result["acquisition_status"], "downloaded")
        self.assertEqual(result["review_status"], "pending")
        self.assertEqual(Path(result["file"]).read_bytes(), b"image bytes")
        self.assertNotIn("Cookie", seen[0].headers)
        self.assertEqual(len(result["sha256"]), 64)

    def test_real_download_requires_shared_run_guard(self):
        with self.assertRaises(media.MediaError):
            media.download_media(self.directory, self.image["media_id"])

    def test_restriction_stops_future_media_requests(self):
        def limited(req, timeout):
            raise HTTPError(req.full_url, 429, "restricted", {}, None)
        result = media.download_media(self.directory, self.image["media_id"], limited)
        self.assertEqual(result["acquisition_status"], "blocked")
        self.assertTrue((self.directory / "STOP.json").exists())
        with self.assertRaises(media.MediaError):
            media.download_media(self.directory, self.video["media_id"], lambda *a, **k: self.fail("must not request"))

    def test_html_response_is_not_media_success(self):
        result = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"login", kind="text/html"))
        self.assertEqual(result["acquisition_status"], "blocked")
        self.assertEqual(result["review_status"], "pending")

    def test_unsafe_redirect_rejected(self):
        result = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"x", url="http://127.0.0.1/secret"))
        self.assertEqual(result["acquisition_status"], "unavailable")
        self.assertFalse(list((self.directory / "files").glob("*.part")))

    def test_missing_media_remains_unresolved(self):
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.image["media_id"], "Looks readable", "reviewed")
        self.assertIn(self.image["media_id"], media.status(self.directory)["unresolved"])

    def test_caption_priority_and_negative_word_preserved(self):
        raw = note()
        raw["data"]["items"][0]["note_card"]["video"]["subtitles"] = [
            {"start": 0, "end": 2, "text": "不是王医生做的"}]
        self.source.write_text(json.dumps(raw))
        manifest = media.inspect_sources([self.source], self.directory)
        caption = next(x for x in manifest["items"].values() if x["kind"] == "subtitle")
        media.download_media(self.directory, caption["media_id"])
        result = media.transcribe_media(self.directory, self.video["media_id"], runner=lambda *a, **k: self.fail("ASR not needed"))
        transcript = json.loads(Path(result["transcript"]).read_text())
        self.assertEqual(transcript["segments"][0]["text"], "不是王医生做的")
        self.assertFalse(transcript["medical_attribution_verified"])
        self.assertEqual(result["review_status"], "pending")
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.video["media_id"], "Transcript exists", "reviewed")

    def test_local_transcriber_preserves_timestamps_unverified(self):
        media.download_media(self.directory, self.video["media_id"], lambda *a, **k: Response(b"video", VIDEO, "video/mp4"))
        model = self.root / "ggml-medium.bin"
        model.write_bytes(b"fake model")
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            if args[0] == "ffmpeg":
                Path(args[-1]).write_bytes(b"wav")
            else:
                prefix = Path(args[-1])
                prefix.with_suffix(".whisper.json").write_text(json.dumps({"transcription": [
                    {"offsets": {"from": 1200, "to": 3400}, "text": "王医生，不是我的医生"}]}))
                prefix.with_suffix(".whisper.srt").write_text("1\n00:00:01,200 --> 00:00:03,400\ntext\n")
            return subprocess.CompletedProcess(args, 0)
        result = media.transcribe_media(self.directory, self.video["media_id"], "ffmpeg", "whisper-cli", model, runner)
        data = json.loads(Path(result["transcript"]).read_text())
        self.assertEqual(data["segments"][0]["start"], 1.2)
        self.assertEqual(data["segments"][0]["end"], 3.4)
        self.assertEqual(result["review_status"], "pending")
        self.assertIn("zh", calls[1])
        final = media.review_media(self.directory, self.video["media_id"], "Checked original negative wording", "reviewed",
                                   [{"start": 1.2, "end": 3.4, "observation": "Original says not my doctor"},
                                    {"full_video": True, "observation": "Watched entire video; wording above checked twice"}])
        self.assertEqual(final["review_status"], "reviewed")

    def test_excerpt_review_stays_partial(self):
        media.download_media(self.directory, self.video["media_id"], lambda *a, **k: Response(b"video", VIDEO, "video/mp4"))
        ranges = [{"start": 1, "end": 2, "observation": "One phrase checked"}]
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.video["media_id"], "Only excerpt watched", "reviewed", ranges)
        result = media.review_media(self.directory, self.video["media_id"], "Remaining duration unread", "partial", ranges)
        self.assertEqual(result["review_status"], "partial")
        self.assertIn(self.video["media_id"], media.status(self.directory)["unresolved"])

    def test_failed_asr_not_completed(self):
        result = media.transcribe_media(self.directory, self.video["media_id"])
        self.assertEqual(result["transcription_status"], "blocked")
        self.assertEqual(result["review_status"], "pending")

    def test_explicit_cpu_keeps_model_and_creates_output_directory(self):
        local = self.root / "already-local.video"
        local.write_bytes(b"local video")
        model = self.root / "ggml-medium.bin"
        model.write_bytes(b"same medium model")
        directory, manifest, item = media.get_item(self.directory, self.video["media_id"])
        item.update(file=str(local), acquisition_status="downloaded")
        media.save_item(directory, manifest, item)
        self.assertFalse((self.directory / "files").exists())
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            self.assertTrue((self.directory / "files").is_dir())
            if args[0] == "ffmpeg":
                Path(args[-1]).write_bytes(b"wav")
            else:
                prefix = Path(args[-1])
                prefix.with_suffix(".whisper.json").write_text(json.dumps({"transcription": [
                    {"offsets": {"from": 0, "to": 1000}, "text": "不是这位医生"}]}))
                prefix.with_suffix(".whisper.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n")
            return subprocess.CompletedProcess(args, 0)
        result = media.transcribe_media(self.directory, self.video["media_id"], "ffmpeg", "whisper-cli", model, runner, cpu=True)
        self.assertEqual(result["acquisition_status"], "transcribed")
        self.assertEqual(result["transcription_device"], "cpu")
        self.assertIn("-ng", calls[1])
        self.assertEqual(calls[1][calls[1].index("-m") + 1], str(model))

    def test_import_does_not_overwrite_review(self):
        media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"photo"))
        media.review_media(self.directory, self.image["media_id"], "Visible name, unresolved role", "reviewed")
        newer = self.root / "newer.json"
        newer.write_text(json.dumps(note()))
        manifest = media.inspect_sources([newer], self.directory)
        result = manifest["items"][self.image["media_id"]]
        self.assertEqual(result["review_status"], "reviewed")
        self.assertEqual(len(result["sources"]), 2)

    def test_replaced_image_reopens_review_preserves_prior_file(self):
        first = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"old image"))
        first_file = Path(first["file"])
        media.review_media(self.directory, self.image["media_id"], "Old picture reviewed", "reviewed")
        raw = note()
        raw["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "-replacement"
        newer = self.root / "changed.json"
        newer.write_text(json.dumps(raw))
        manifest = media.inspect_sources([newer], self.directory)
        changed = manifest["items"][self.image["media_id"]]
        self.assertEqual(changed["review_status"], "pending")
        self.assertEqual(changed["acquisition_status"], "pending")
        self.assertNotIn("file", changed)
        self.assertEqual(changed["versions"][0]["review_status"], "reviewed")
        self.assertNotEqual(changed["resource_fingerprint"], first["resource_fingerprint"])
        second = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"new image", URL + "-replacement"))
        self.assertNotEqual(first_file, Path(second["file"]))
        self.assertEqual(first_file.read_bytes(), b"old image")
        self.assertEqual(Path(second["file"]).read_bytes(), b"new image")

    def test_signature_query_change_does_not_reset_or_replace_url(self):
        media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"image"))
        media.review_media(self.directory, self.image["media_id"], "Image reviewed", "reviewed")
        raw = note()
        raw["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "?new_signature=1"
        newer = self.root / "signed.json"
        newer.write_text(json.dumps(raw))
        changed = media.inspect_sources([newer], self.directory)["items"][self.image["media_id"]]
        self.assertEqual(changed["review_status"], "reviewed")
        self.assertEqual(changed["observed_url"], URL)
        self.assertNotIn("versions", changed)

    def test_explicit_resource_id_survives_cdn_path_change(self):
        raw = note()
        raw["data"]["items"][0]["note_card"]["image_list"][0]["file_id"] = "observed-file-id"
        first = media.extract_media(raw)[0]
        raw["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "/new-cdn-prefix"
        second = media.extract_media(raw)[0]
        self.assertEqual(media.resource_identity(first), media.resource_identity(second))
        raw["data"]["items"][0]["note_card"]["image_list"][0]["file_id"] = "replacement-file-id"
        third = media.extract_media(raw)[0]
        self.assertNotEqual(media.resource_identity(first), media.resource_identity(third))

    def signed_snapshot(self, name, captured_at, suffix, explicit_id=True):
        raw = note()
        raw["captured_at"] = captured_at
        card = raw["data"]["items"][0]["note_card"]
        card.pop("video")
        picture = card["image_list"][0]
        picture["url_default"] = URL + "?signature=" + suffix
        picture["url_pre"] = URL + "?preview=" + suffix
        if explicit_id:
            picture["file_id"] = "stable-observed-image-id"
        path = self.root / name
        path.write_text(json.dumps(raw))
        return path

    def test_later_explicit_id_refreshes_only_unattempted_url_with_history(self):
        older = self.signed_snapshot("older.json", "2026-10-30T01:00:00Z", "old")
        newer = self.signed_snapshot("newer.json", "2026-10-30T02:00:00Z", "new")
        raw = json.loads(newer.read_text())
        raw["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "/later-signed-path"
        newer.write_text(json.dumps(raw))
        directory = self.root / "refresh"
        initial = next(iter(media.inspect_sources([older], directory)["items"].values()))
        result = next(iter(media.inspect_sources([newer], directory)["items"].values()))
        self.assertEqual(result["observed_url"], URL + "/later-signed-path")
        self.assertEqual(result["alternatives"], [URL + "?preview=new"])
        self.assertEqual(result["source_path"], str(newer.resolve()))
        self.assertEqual(result["captured_at"], "2026-10-30T02:00:00Z")
        self.assertEqual(result["resource_version"], initial["resource_version"])
        self.assertEqual(result["resource_fingerprint"], initial["resource_fingerprint"])
        self.assertEqual(result["acquisition_status"], "pending")
        self.assertEqual(result["review_status"], "pending")
        self.assertEqual(result["historical_observations"][0]["observed_url"], initial["observed_url"])
        self.assertEqual(result["historical_observations"][0]["source_path"], str(older.resolve()))
        # Reordering or reimporting snapshots cannot roll back the URL or inflate history.
        again = next(iter(media.inspect_sources([older, newer], directory)["items"].values()))
        self.assertEqual(again["observed_url"], result["observed_url"])
        self.assertEqual(len(again["historical_observations"]), 1)

    def test_pending_manifest_from_prior_inspector_can_select_already_seen_later_url(self):
        older = self.signed_snapshot("prior.json", "2026-10-30T01:00:00Z", "old")
        newer = self.signed_snapshot("seen.json", "2026-10-30T02:00:00Z", "new")
        directory = self.root / "prior-inspector"
        manifest = media.inspect_sources([older], directory)
        item = next(iter(manifest["items"].values()))
        # Previous inspect_sources advanced latest time but left the selected URL old.
        item["latest_observed_at"] = "2026-10-30T02:00:00Z"
        media.write_json(directory / "manifest.json", manifest)
        result = next(iter(media.inspect_sources([newer], directory)["items"].values()))
        self.assertEqual(result["observed_url"], URL + "?signature=new")

    def test_refresh_requires_proven_chronology_and_explicit_identity(self):
        cases = [("2026-10-30T01:00:00Z", "2026-10-30T00:00:00Z", True),
                 ("2026-10-30T01:00:00Z", "2026-10-30T01:00:00Z", True),
                 (None, "2026-10-30T02:00:00Z", True),
                 ("2026-10-30T01:00:00Z", None, True),
                 ("not-a-date", "2026-10-30T02:00:00Z", True),
                 ("2026-10-30T01:00:00Z", "2026-10-30T02:00:00Z", False)]
        for i, (old_time, new_time, hint) in enumerate(cases):
            with self.subTest(case=i):
                old = self.signed_snapshot(f"old-{i}.json", old_time, "old", hint)
                new = self.signed_snapshot(f"new-{i}.json", new_time, "new", hint)
                directory = self.root / f"chronology-{i}"
                media.inspect_sources([old], directory)
                result = next(iter(media.inspect_sources([new], directory)["items"].values()))
                self.assertEqual(result["observed_url"], URL + "?signature=old")
                self.assertNotIn("observed_url_refreshed_at", result)

    def test_attempt_failure_review_and_artifact_markers_prevent_refresh(self):
        older = self.signed_snapshot("guard-old.json", "2026-10-30T01:00:00Z", "old")
        newer = self.signed_snapshot("guard-new.json", "2026-10-30T02:00:00Z", "new")
        cases = [{"acquisition_status": "blocked", "reason": "HTTP_403"},
                 {"acquisition_status": "unavailable", "reason": "HTTP_404"},
                 {"acquisition_status": "downloaded", "file": "prior.image", "sha256": "retained"},
                 {"review_status": "reviewed", "review": {"observation": "retained"}},
                 {"review_status": "unreadable"}, {"download_attempted_at": "2026-10-30T01:01:00Z"},
                 {"file": "prior.image"}, {"reason": "previous_failure"},
                 {"updated_at": "2026-10-30T01:01:00Z"}, {"chronology_uncertain": True}]
        for i, fields in enumerate(cases):
            with self.subTest(fields=fields):
                directory = self.root / f"guard-{i}"
                manifest = media.inspect_sources([older], directory)
                item = next(iter(manifest["items"].values()))
                item.update(fields)
                media.write_json(directory / "manifest.json", manifest)
                result = next(iter(media.inspect_sources([newer], directory)["items"].values()))
                self.assertEqual(result["observed_url"], URL + "?signature=old")
                for key, value in fields.items():
                    self.assertEqual(result[key], value)

    def test_stop_and_orphan_partial_file_prevent_refresh(self):
        older = self.signed_snapshot("stop-old.json", "2026-10-30T01:00:00Z", "old")
        newer = self.signed_snapshot("stop-new.json", "2026-10-30T02:00:00Z", "new")
        for state in ("stop", "partial"):
            with self.subTest(state=state):
                directory = self.root / state
                item = next(iter(media.inspect_sources([older], directory)["items"].values()))
                if state == "stop":
                    media.write_json(directory / "STOP.json", {"reason": "HTTP_429"})
                else:
                    (directory / "files").mkdir()
                    (directory / "files" / (media.artifact_stem(item) + ".image.part")).write_bytes(b"unfinished")
                result = next(iter(media.inspect_sources([newer], directory)["items"].values()))
                self.assertEqual(result["observed_url"], URL + "?signature=old")
                if state == "stop":
                    self.assertEqual(json.loads((directory / "STOP.json").read_text()), {"reason": "HTTP_429"})

    def test_interrupted_request_persists_attempt_before_http_and_prevents_refresh(self):
        older = self.signed_snapshot("interrupted-old.json", "2026-10-30T01:00:00Z", "old")
        newer = self.signed_snapshot("interrupted-new.json", "2026-10-30T02:00:00Z", "new")
        directory = self.root / "interrupted"
        item = next(iter(media.inspect_sources([older], directory)["items"].values()))
        def interrupted(req, timeout):
            saved = media.load_manifest(directory)[1]["items"][item["media_id"]]
            self.assertTrue(saved["download_attempted_at"])
            raise KeyboardInterrupt("synthetic interruption")
        with self.assertRaises(KeyboardInterrupt):
            media.download_media(directory, item["media_id"], interrupted)
        result = next(iter(media.inspect_sources([newer], directory)["items"].values()))
        self.assertEqual(result["acquisition_status"], "pending")
        self.assertEqual(result["observed_url"], URL + "?signature=old")

    def test_deleted_or_changed_artifact_cannot_be_reviewed(self):
        item = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"original"))
        path = Path(item["file"])
        path.write_bytes(b"replaced")
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.image["media_id"], "Changed bytes", "reviewed")
        path.unlink()
        with self.assertRaises(media.MediaError):
            media.review_media(self.directory, self.image["media_id"], "Missing file", "reviewed")

    def test_changed_caption_invalidates_dependent_video_version(self):
        raw = note()
        raw["data"]["items"][0]["note_card"]["video"]["subtitles"] = [{"start": 0, "end": 2, "text": "不是这位医生"}]
        first = media.extract_media(raw)
        raw["data"]["items"][0]["note_card"]["video"]["subtitles"][0]["text"] = "就是这位医生"
        second = media.extract_media(raw)
        a = next(x for x in first if x["kind"] == "video")
        b = next(x for x in second if x["kind"] == "video")
        self.assertEqual(a["media_id"], b["media_id"])
        self.assertNotEqual(media.resource_identity(a), media.resource_identity(b))

    def test_late_old_snapshot_cannot_rollback_current_media(self):
        old = note()
        old["captured_at"] = "2026-10-30T01:00:00Z"
        self.source.write_text(json.dumps(old))
        media.inspect_sources([self.source], self.directory)
        new = note()
        new["captured_at"] = "2026-10-30T02:00:00Z"
        new["data"]["items"][0]["note_card"]["image_list"][0]["url_default"] = URL + "-replacement"
        newer = self.root / "new.json"
        newer.write_text(json.dumps(new))
        media.inspect_sources([newer], self.directory)
        current = media.download_media(self.directory, self.image["media_id"], lambda *a, **k: Response(b"new", URL + "-replacement"))
        current_file = current["file"]
        media.review_media(self.directory, self.image["media_id"], "New image reviewed", "reviewed")
        result = media.inspect_sources([self.source], self.directory)["items"][self.image["media_id"]]
        self.assertEqual(result["observed_url"], URL + "-replacement")
        self.assertEqual(result["file"], current_file)
        self.assertEqual(result["review_status"], "reviewed")
        self.assertEqual(len(result["historical_observations"]), 1)
        self.assertTrue(result["historical_observations"][0]["historical_only"])


class PairedMediaTests(unittest.TestCase):
    def paired(self):
        raw = note()
        raw['data']['items'][0]['note_card'].pop('video')
        image = raw['data']['items'][0]['note_card']['image_list'][0]
        image.update(live_photo=True, stream={
            'EF4': [{'master_url': VIDEO, 'width': 720, 'height': 1280}],
            'EF5': [{'master_url': VIDEO + '&codec=5', 'width': 720, 'height': 1280}]})
        return raw

    def test_encodings_one_video_static_image_independent(self):
        raw = self.paired()
        items = media.extract_media(raw, 'source.json')
        self.assertEqual(len(items), 2)
        image, video = items
        self.assertNotEqual(image['media_id'], video['media_id'])
        self.assertEqual(video['locator'], 'note/image/1/live_photo')
        self.assertEqual(video['parent_image_locator'], image['locator'])
        self.assertEqual(video['source_path'], 'source.json')
        self.assertEqual({video['observed_url'], *video['alternatives']}, {VIDEO, VIDEO + '&codec=5'})
        pic = raw['data']['items'][0]['note_card']['image_list'][0]
        pic['stream'] = dict(reversed(list(pic['stream'].items())))
        again = media.extract_media(raw, 'new.json')[1]
        self.assertEqual(video['observed_url'], again['observed_url'])
        self.assertEqual(video['resource_fingerprint'], again['resource_fingerprint'])

    def test_plain_photo_no_phantom_video(self):
        raw = self.paired()
        image = raw['data']['items'][0]['note_card']['image_list'][0]
        image.update(live_photo=False, stream={'EF4': [], 'EF5': []})
        self.assertEqual([x['kind'] for x in media.extract_media(raw)], ['image'])
        image.pop('stream')
        self.assertEqual(len(media.extract_media(raw)), 1)

    def test_live_photo_no_stream_or_usable_url_remains_gap(self):
        for value in [None, {}, {'EF4': []}, {'EF4': [{'master_url': 'https://invalid.example/video'}]}]:
            raw = self.paired()
            raw['data']['items'][0]['note_card']['image_list'][0]['stream'] = value
            video = media.extract_media(raw)[1]
            self.assertEqual(video['acquisition_status'], 'unsupported')
            self.assertEqual(video['review_status'], 'pending')
            self.assertEqual(video['observed_url'], '')

    def test_repeat_import_and_smaller_snapshot_preserve_both_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'a.json'; target=root/'media'
            source.write_text(json.dumps(self.paired()))
            items=media.inspect_sources([source],target)['items']
            for item in items.values():
                media.download_media(target,item['media_id'],lambda *a,**k: Response(b'fake fixture',VIDEO,'video/mp4'))
                checks=[{'full_video':True,'observation':'synthetic fixture review'}] if item['kind']=='video' else None
                media.review_media(target,item['media_id'],'synthetic fixture review','reviewed',checks)
            expected=media.load_manifest(target)[1]['items']
            media.inspect_sources([source],target)
            smaller=self.paired(); smaller['data']['items'][0]['note_card']['image_list']=[]
            source2=root/'b.json';source2.write_text(json.dumps(smaller))
            actual=media.inspect_sources([source2],target)['items']
            self.assertEqual(len(actual),2)
            for key in expected:
                self.assertEqual(actual[key]['file'],expected[key]['file'])
                self.assertEqual(actual[key]['review_status'],'reviewed')

    def test_comment_stream_identity_with_and_without_real_id(self):
        pic=self.paired()['data']['items'][0]['note_card']['image_list'][0]
        raw={'operation':'comments','note_id':'a'*24,'data':{'comments':[{'id':'c1','pictures':[pic]},{'pictures':[pic]}]}}
        first=media.extract_media(raw,'one.json');second=media.extract_media(raw,'two.json')
        self.assertEqual(first[1]['media_id'],second[1]['media_id'])
        self.assertNotEqual(first[3]['media_id'],second[3]['media_id'])

    def test_live_photo_cannot_borrow_main_video_subtitles(self):
        raw=self.paired();raw['data']['items'][0]['note_card']['subtitles']=[{'text':'不是同一段视频','start':0,'end':2}]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.json';source.write_text(json.dumps(raw))
            items=list(media.inspect_sources([source],root/'media')['items'].values())
            video=next(x for x in items if x['kind']=='video');caption=next(x for x in items if x['kind']=='subtitle')
            self.assertNotIn('caption_resource_fingerprints',video)
            media.download_media(root/'media',caption['media_id'])
            result=media.transcribe_media(root/'media',video['media_id'])
            self.assertEqual(result['transcription_status'],'blocked')
            self.assertNotIn('transcript',result)

    def multiplex(self):
        raw=note();video=raw['data']['items'][0]['note_card']['video']
        silent={'master_url':VIDEO+'&silent=1','width':1080,'height':1920,'audio_channels':0}
        audible={'master_url':VIDEO+'&audio=1','width':720,'height':1280,'audio_channels':2,'format':'mp4'}
        video['media']={'video_id':'real-id','stream':{'EF5':[silent],'EF4':[audible]}}
        video['media_v2']=json.dumps({'video_id':'real-id','video':{'subtitles':{
            'en-US':[{'url':'https://sns-subtitle-s1.xhscdn.com/en.srt','language':'en-US'}],
            'source':[{'url':'https://sns-subtitle-s1.xhscdn.com/source.srt','language':'zh-CN'}],
            'zh-CN':[{'url':'https://sns-subtitle-s1.xhscdn.com/zh.srt','language':'zh-CN'}]}}})
        return raw

    def test_audible_preferred_and_nested_language_captions_are_urls(self):
        items=media.extract_media(self.multiplex())
        video=next(x for x in items if x['kind']=='video')
        captions=[x for x in items if x['kind']=='subtitle']
        self.assertEqual(video['observed_url'],VIDEO+'&audio=1')
        self.assertEqual(video['selected_stream']['audio_channels'],2)
        self.assertEqual(len(captions),3)
        self.assertTrue(all(x['acquisition_status']=='pending' and 'inline_caption' not in x for x in captions))
        self.assertIn('/source/',captions[0]['locator'])

    def test_explicit_audio_variant_keeps_existing_downloaded_item_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.json';target=root/'media';raw=self.multiplex()
            old=note();old['data']['items'][0]['note_card']['video']={'media':{'video_id':'real-id','stream':{'EF5':[{'master_url':VIDEO+'&silent=1','audio_channels':0}]}}}
            source.write_text(json.dumps(old));items=media.inspect_sources([source],target)['items']
            video=next(x for x in items.values() if x['kind']=='video')
            media.download_media(target,video['media_id'],lambda *a,**k: Response(b'silent old bytes',VIDEO+'&silent=1','video/mp4'))
            before=media.get_item(target,video['media_id'])[2]
            original_bytes=Path(before['file']).read_bytes()
            source2=root/'full.json';source2.write_text(json.dumps(raw))
            actual=media.inspect_sources([source2],target,audio_variant=True)['items']
            self.assertEqual(actual[video['media_id']],before)
            self.assertEqual(Path(before['file']).read_bytes(),original_bytes)
            mux=next(x for x in actual.values() if x.get('media_role')=='audio_mux')
            self.assertEqual(mux['observed_url'],VIDEO+'&audio=1')
            self.assertEqual(mux['review_status'],'pending')
            self.assertNotIn('file',mux)
            self.assertEqual(len(media.inspect_sources([source2],target,audio_variant=True)['items']),len(actual))

    def test_audio_variant_requires_observed_audio(self):
        with self.assertRaises(media.MediaError):media.extract_media(self.paired(),audio_variant=True)
        raw=note();raw['data']['items'][0]['note_card']['video']['media_v2']='not JSON'
        self.assertEqual(len(media.extract_media(raw)),2)


if __name__ == "__main__":
    unittest.main()
