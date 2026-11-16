import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import create_read_engine, database_url
from app.import_trump_post import save_post
from app.main import create_app
from app.repositories.public_records import select_public_record
from test_import_trump_post import SEEN, add_fixture, extraction
from test_public_record_detail_api import detail_sample


def archive_sample():
    return {
        'record_id': UUID(int=1), 'archive_author': 'Donald J. Trump', 'archive_handle': '@realDonaldTrump',
        'archive_post_type': 'original', 'archive_removed_raw': 'false', 'title_origin': 'archive_rss',
        'page_post_published_at': datetime(2026, 10, 17, 12, tzinfo=timezone(timedelta(hours=8))),
        'page_post_published_raw': '2026-10-17T04:00:00+00:00',
        'archive_captured_at': datetime(2026, 10, 17, 21, tzinfo=timezone(timedelta(hours=8))),
        'archive_capture_raw': '2026-10-17T13:00:00+00:00', 'image_count': 1, 'video_count': 0,
        'inline_quotes': [], 'quoted_posts': [],
        'archive_media_notes': [{
            'position': 0, 'kind': 'archive_image_description', 'label': 'Image Description:',
            'text': '独立图片说明。' * 300, 'attachment_ref': 'attachment_1',
            'owner_archive_url': None, 'association_status': 'unresolved',
        }],
        'parser_version': 'trump_archive_v1', 'snapshot_hash': 'a' * 64,
        'created_at': datetime(2026, 10, 17, 23, tzinfo=timezone(timedelta(hours=8))),
    }


def quoted_sample():
    return {
        'position': 0, 'archive_url': 'https://www.trumpstruth.org/statuses/2',
        'parent_archive_url': 'https://www.trumpstruth.org/statuses/1',
        'original_url': 'https://truthsocial.com/@other/456',
        'archive_author': 'Another archive-reported author', 'archive_handle': '@other',
        'published_at': '2026-10-17T12:00:00+08:00', 'published_raw': '2026-10-17T12:00:00+08:00',
        'text': 'Separate quoted content. ' * 100, 'image_count': 0, 'video_count': 1,
        'inline_quotes': [{'position': 0, 'text': 'Nested inline quote.', 'attribution': None}],
    }


class ArchiveDetailApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)
        self.url = '/api/v1/public-records/' + str(UUID(int=1))

    @patch('app.api.public_records.read_public_record')
    def test_archive_metadata_complete_notes_utc_and_raw_values_are_preserved(self, read):
        archive = archive_sample()
        record = detail_sample()
        read.return_value = {**record, 'provenance_type': 'third_party_archive',
                             'verification_status': 'archive_only', 'archive_post': archive}
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result['body_text'], record['body_text'])
        self.assertEqual(result['verification_status'], 'archive_only')
        result = result['archive_post']
        self.assertEqual(result['record_id'], str(UUID(int=1)))
        self.assertEqual(result['archive_removed_raw'], 'false')
        self.assertEqual(result['page_post_published_at'], '2026-10-17T04:00:00Z')
        self.assertEqual(result['archive_captured_at'], '2026-10-17T13:00:00Z')
        self.assertEqual(result['created_at'], '2026-10-17T15:00:00Z')
        self.assertEqual(result['archive_capture_raw'], archive['archive_capture_raw'])
        self.assertEqual(result['archive_media_notes'], archive['archive_media_notes'])
        self.assertGreater(len(result['archive_media_notes'][0]['text']), 1200)
        self.assertEqual(result['snapshot_hash'], archive['snapshot_hash'])

    @patch('app.api.public_records.read_public_record')
    def test_quotes_keep_their_authors_times_and_text_and_nulls_remain_null(self, read):
        archive = archive_sample()
        archive.update(archive_post_type='quote', quoted_posts=[quoted_sample()],
                       page_post_published_at=None, page_post_published_raw=None,
                       archive_captured_at=None, archive_capture_raw=None, archive_removed_raw=None)
        archive['inline_quotes'] = [{'position': 0, 'text': 'Separate inline words.', 'attribution': None}]
        archive['archive_media_notes'].append({
            'position': 1, 'kind': 'unavailable', 'label': 'No File Information Available:', 'text': None,
            'attachment_ref': None, 'owner_archive_url': None, 'association_status': 'unresolved',
        })
        read.return_value = {**detail_sample(), 'archive_post': archive}
        result = self.client.get(self.url).json()['archive_post']
        self.assertEqual(result['quoted_posts'][0]['archive_author'], quoted_sample()['archive_author'])
        self.assertEqual(result['quoted_posts'][0]['text'], quoted_sample()['text'])
        self.assertEqual(result['quoted_posts'][0]['published_at'], '2026-10-17T04:00:00Z')
        self.assertEqual(result['quoted_posts'][0]['published_raw'], quoted_sample()['published_raw'])
        self.assertEqual(result['inline_quotes'], archive['inline_quotes'])
        self.assertIsNone(result['archive_media_notes'][1]['text'])
        for key in ('page_post_published_at', 'page_post_published_raw', 'archive_captured_at',
                    'archive_capture_raw', 'archive_removed_raw'):
            self.assertIsNone(result[key])

    @patch('app.api.public_records.read_public_record')
    def test_malformed_stored_json_returns_redacted_503_instead_of_partial_data(self, read):
        archive = archive_sample()
        archive['archive_media_notes'] = [{'text': 'DO_NOT_EXPOSE'}]
        read.return_value = {**detail_sample(), 'archive_post': archive}
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'database_unavailable')
        self.assertNotIn('DO_NOT_EXPOSE', response.text)

    def test_openapi_describes_nullable_archive_and_separate_nested_models(self):
        schemas = self.client.get('/openapi.json').json()['components']['schemas']
        detail = schemas['PublicRecordDetail']['properties']['archive_post']
        self.assertTrue(any(variant.get('type') == 'null' for variant in detail['anyOf']))
        self.assertIn('archive_media_notes', schemas['ArchivePostResponse']['properties'])
        self.assertIn('quoted_posts', schemas['ArchivePostResponse']['properties'])
        self.assertNotIn('archive_post', schemas['PublicRecordSummary']['properties'])


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class ArchiveDetailDatabaseTests(unittest.TestCase):
    def test_actual_saved_post_and_official_speech_do_not_change_when_read(self):
        engine = create_read_engine(Settings())
        def snapshot():
            with engine.connect() as connection:
                return ([dict(row) for row in connection.execute(text('SELECT * FROM public_records ORDER BY record_id')).mappings()],
                        [dict(row) for row in connection.execute(text('SELECT * FROM archive_post_details ORDER BY record_id')).mappings()])
        try:
            before = snapshot()
            with TestClient(create_app()) as client:
                items = client.get('/api/v1/public-records').json()['items']
                self.assertTrue(all('archive_post' not in item for item in items))
                trump = next(item for item in items if item['person_name_original'] == 'Donald J. Trump'
                             and item['title'] == 'Best in History!')
                result = client.get('/api/v1/public-records/' + trump['record_id'])
                self.assertEqual(result.status_code, 200)
                detail = result.json()
                self.assertEqual(detail['body_text'], 'Best in History!')
                archive = detail['archive_post']
                self.assertEqual(archive['record_id'], trump['record_id'])
                self.assertEqual(archive['archive_author'], 'Donald J. Trump')
                self.assertEqual(archive['image_count'], 1)
                stored = next(row for row in before[1] if str(row['record_id']) == trump['record_id'])
                self.assertEqual(archive['archive_media_notes'], stored['archive_media_notes'])
                self.assertEqual(archive['snapshot_hash'], stored['snapshot_hash'])
                self.assertEqual(len(archive['archive_media_notes'][0]['text']), 425)
                lagarde = next(item for item in items if item['person_name_original'] == 'Christine Lagarde'
                               and item['title'] == 'IMFC Statement')
                official = client.get('/api/v1/public-records/' + lagarde['record_id']).json()
                self.assertIsNone(official['archive_post'])
                self.assertEqual(len(official['body_text']), 9655)
            self.assertEqual(snapshot(), before)
        finally:
            engine.dispose()

    def test_jsonb_nested_arrays_and_absent_extension_use_left_join(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    source, person = uuid4(), uuid4()
                    add_fixture(connection, source, person)
                    imported = save_post(connection, extraction(source, person), SEEN, source_id=source, person_id=person)
                    identity = imported['record_id']
                    quotes = [quoted_sample()]
                    inline = [{'position': 0, 'text': 'Separate inline words.', 'attribution': None}]
                    connection.execute(text(
                        "UPDATE archive_post_details SET archive_post_type='quote', quoted_posts=CAST(:quotes AS jsonb), "
                        'inline_quotes=CAST(:inline AS jsonb), archive_captured_at=NULL, archive_capture_raw=NULL WHERE record_id=:id'
                    ), {'id': identity, 'quotes': json.dumps(quotes), 'inline': json.dumps(inline)})
                    with patch('app.api.public_records.read_public_record',
                               side_effect=lambda settings, record_id: select_public_record(connection, record_id)):
                        with TestClient(create_app()) as client:
                            response = client.get('/api/v1/public-records/' + identity)
                            self.assertEqual(response.status_code, 200)
                            detail = response.json()
                            self.assertEqual(detail['body_text'], 'Short own text.')
                            archive = detail['archive_post']
                            self.assertEqual(archive['inline_quotes'], inline)
                            self.assertEqual(archive['quoted_posts'][0]['text'], quotes[0]['text'])
                            self.assertEqual(archive['quoted_posts'][0]['published_at'], '2026-10-17T04:00:00Z')
                            self.assertIsNone(archive['archive_captured_at'])
                            connection.execute(text('DELETE FROM archive_post_details WHERE record_id=:id'), {'id': identity})
                            no_extension = client.get('/api/v1/public-records/' + identity)
                            self.assertEqual(no_extension.status_code, 200)
                            self.assertIsNone(no_extension.json()['archive_post'])
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
