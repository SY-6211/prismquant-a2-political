import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.event_model import EventSnapshot, event_id_for, native_identity, snapshot_from_record
from app.main import create_app


def record(**changes):
    return {'title': 'Synthetic own words', 'content_type': 'public_statement',
            'original_url': 'https://truthsocial.com/@realDonaldTrump/123456',
            'retrieved_url': 'https://www.trumpstruth.org/statuses/900',
            'published_at': '2026-11-07T08:00:00Z', 'published_raw': '2026-11-07T08:00:00Z',
            'page_date': None, 'language': None, 'person_id': uuid4(), 'speaker_evidence': 'Archive attribution',
            'provenance_type': 'third_party_archive', 'verification_status': 'archive_only',
            'body_text': 'Only the main author words.', **changes}


def snapshot(**changes):
    return snapshot_from_record(record(**changes), {'archive_post_type': 'original', 'parser_version': 'test_v1'})


def event_detail():
    snap = snapshot()
    return {'event_id': uuid4(), 'platform': 'truth_social', 'native_id': '123456',
            'version_id': uuid4(), 'version_number': 1, 'channel_id': uuid4(), 'source_id': uuid4(),
            'transport': 'rss', 'source_name': 'Test archive', 'publisher_name': 'Archive publisher',
            'person_id': snap.person_id, 'person_name_original': 'Test person', 'person_name_zh': None,
            'provenance_type': 'third_party_archive', 'verification_status': 'archive_only', 'review_status': 'accepted',
            'title': snap.title, 'post_type': 'original', 'published_at': snap.published_at,
            'last_observed_at': datetime.now(timezone.utc), 'payload': snap.model_dump(mode='json'),
            'snapshot_hash': snap.digest(), 'linked_record_ids': [uuid4()]}


class EventModelTests(unittest.TestCase):
    def test_native_identity_uses_original_provider_id_across_retrieval_channels(self):
        first = snapshot()
        other = first.model_copy(update={'retrieved_url': 'https://example.invalid/licensed-feed',
                                         'original_url': 'https://www.truthsocial.com/@someone/posts/123456'})
        self.assertEqual(native_identity(first), ('truth_social', '123456'))
        self.assertEqual(native_identity(first), native_identity(other))
        self.assertEqual(event_id_for(*native_identity(first)), event_id_for(*native_identity(other)))

    def test_archive_id_and_title_never_claim_original_native_identity(self):
        value = snapshot(original_url=None)
        with self.assertRaises(ValueError):
            native_identity(value)
        fallback = uuid4()
        self.assertEqual(native_identity(value, fallback), ('source_record', str(fallback)))

    def test_tracking_queries_credentials_and_other_hosts_cannot_claim_provider_identity(self):
        for url in ('https://truthsocial.com.evil.invalid/@u/123456',
                    'https://truthsocial.com/@u/123456?track=1', 'https://truthsocial.com:444/@u/123456'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                native_identity(snapshot(original_url=url))
        for url in ('https://name:secret@truthsocial.com/@u/123456', 'javascript:alert(1)', 'https://example.invalid/a\nb'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                snapshot(original_url=url)

    def test_ecb_path_normalization_and_dates_without_invented_times(self):
        url = 'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261107~abc.en.html'
        value = snapshot(original_url=url, provenance_type='official', verification_status='verified_page_attribution',
                         published_at=None, published_raw=None, page_date='2026-11-07')
        self.assertEqual(native_identity(value)[0], 'ecb')
        self.assertIsNone(value.published_at)
        self.assertEqual(str(value.page_date), '2026-11-07')

    def test_equivalent_timezone_has_identical_hash_and_media_change_has_new_hash(self):
        value = snapshot()
        data = value.model_dump()
        data['published_at'] = '2026-11-07T16:00:00+08:00'
        self.assertEqual(value.digest(), EventSnapshot(**data).digest())
        data['main_text'] += ' Changed.'
        self.assertNotEqual(value.digest(), EventSnapshot(**data).digest())

    def test_quoted_and_media_content_is_separate_and_unknown_attribution_survives(self):
        row = record()
        archive = {'archive_post_type': 'quote', 'parser_version': 'test_v1', 'inline_quotes': [
            {'position': 0, 'text': 'Inline other words', 'attribution': None}], 'quoted_posts': [
            {'position': 0, 'text': 'Other author words', 'archive_author': None, 'original_url': None}],
            'archive_media_notes': [{'position': 0, 'kind': 'archive_transcript', 'text': 'Transcript only',
                                     'attachment_ref': 'a1', 'association_status': 'unresolved'}]}
        value = snapshot_from_record(row, archive)
        self.assertEqual(value.main_text, row['body_text'])
        self.assertEqual(len(value.quoted_content), 2)
        self.assertIsNone(value.quoted_content[1].reported_author)
        self.assertEqual(value.media_notes[0].attribution, 'archive_supplied_not_main_text')
        self.assertNotIn('Transcript', value.main_text)

    def test_media_only_event_has_no_fabricated_main_text(self):
        data = snapshot().model_dump()
        data.update(main_text=None, image_count=1, post_type='original')
        value = EventSnapshot(**data)
        self.assertIsNone(value.main_text)
        self.assertEqual(value.image_count, 1)

    def test_empty_event_repost_main_text_and_false_official_attribution_are_rejected(self):
        base = snapshot().model_dump()
        for changes in ({'main_text': None}, {'post_type': 'retruth'}, {'title': '  '},
                        {'verification_status': 'verified_page_attribution'},
                        {'provenance_type': 'official', 'verification_status': 'verified_page_attribution', 'person_id': None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                EventSnapshot(**{**base, **changes})

    def test_archive_capture_time_does_not_generate_content_version(self):
        row = record()
        archive = {'archive_post_type': 'original', 'parser_version': 'test_v1', 'archive_captured_at': '2026-11-01T00:00:00Z'}
        first = snapshot_from_record(row, archive)
        archive['archive_captured_at'] = '2026-11-07T00:00:00Z'
        self.assertEqual(first.digest(), snapshot_from_record(row, archive).digest())


class EventApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)

    @patch('app.api.events.read_query', return_value=[])
    def test_empty_list_and_invalid_filters(self, query):
        self.assertEqual(self.client.get('/api/v1/events').json(), {'items': [], 'next_cursor': None})
        query.reset_mock()
        for suffix in ('?limit=0', '?limit=101', '?cursor=bad', '?person_id=bad'):
            self.assertEqual(self.client.get('/api/v1/events' + suffix).status_code, 422)
        query.assert_not_called()

    @patch('app.api.events.read_query')
    def test_list_cursor_does_not_expose_payload_and_detail_keeps_independent_content(self, query):
        row = event_detail()
        query.return_value = [row, event_detail()]
        result = self.client.get('/api/v1/events?limit=1').json()
        self.assertEqual(result['next_cursor'], str(row['event_id']))
        self.assertNotIn('payload', result['items'][0])
        query.return_value = row
        result = self.client.get(f"/api/v1/events/{row['event_id']}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['payload']['main_text'], row['payload']['main_text'])
        self.assertEqual(result.json()['payload']['verification_status'], 'archive_only')

    @patch('app.api.events.read_query', return_value=None)
    def test_missing_event_version_and_record_link_return_404(self, query):
        eid, vid = uuid4(), uuid4()
        for path in (f'/events/{eid}', f'/events/{eid}/versions/{vid}', f'/events/{eid}/versions',
                     f'/events/{eid}/observations', f'/public-records/{eid}/event'):
            self.assertEqual(self.client.get('/api/v1' + path).status_code, 404)

    @patch('app.api.events.read_query', side_effect=OperationalError('private', {}, Exception('SECRET')))
    def test_database_errors_do_not_leak_credentials(self, query):
        response = self.client.get('/api/v1/events')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET', response.text)

    @patch('app.api.events.read_query')
    def test_version_and_observation_pagination_are_bounded(self, query):
        row = event_detail()
        version = {key: row[key] for key in ('version_id', 'version_number', 'channel_id', 'source_id', 'transport',
                  'person_id', 'provenance_type', 'verification_status', 'review_status', 'snapshot_hash')}
        version['created_at'] = datetime.now(timezone.utc)
        query.side_effect = [row, [version, {**version, 'version_number': 2}]]
        result = self.client.get(f"/api/v1/events/{row['event_id']}/versions?limit=1").json()
        self.assertEqual(result['next_before'], 1)
        query.side_effect = None
        query.reset_mock()
        self.assertEqual(self.client.get(f"/api/v1/events/{row['event_id']}/observations?before=0").status_code, 422)
        query.assert_not_called()
