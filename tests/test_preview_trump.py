import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from defusedxml.common import DefusedXmlException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import create_read_engine
from app.preview_trump import (
    MAX_BYTES, PERSON_ID, SOURCE_ID, TRUMP_FEED_URL, fetch_preview,
    load_source, main, parse_preview, preview_link, validate_source,
)


def source_sample():
    return {
        'source_id': SOURCE_ID, 'person_id': PERSON_ID, 'name': 'Test archive',
        'publisher_name': 'Test archive publisher', 'feed_url': TRUMP_FEED_URL,
        'provenance_type': 'third_party_archive', 'fetch_method': 'rss',
        'enabled': False, 'monitoring_enabled': False, 'link_enabled': False,
    }


def feed(items=''):
    return ('''<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:truth="https://truthsocial.com/ns" xmlns:archive="https://www.trumpstruth.org/rss-extensions">
        <channel>''' + items + '</channel></rss>').encode('utf-8')


class TrumpPreviewTests(unittest.TestCase):
    def test_latest_five_utc_archive_metadata_without_verified_person_claim(self):
        items = ''.join(
            f'<item><title>Synthetic archive item {day}</title><link>https://www.trumpstruth.org/statuses/{day}</link>'
            f'<pubDate>2026-10-{day:02d}T08:00:00+08:00</pubDate>'
            '<dc:creator>Donald J. Trump</dc:creator><truth:originalUrl>https://truthsocial.com/@realDonaldTrump/posts/123</truth:originalUrl>'
            '<archive:postType>retruth</archive:postType><archive:removed>false</archive:removed></item>'
            for day in range(1, 8)
        )
        result = parse_preview(feed(items))
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]['title'], 'Synthetic archive item 7')
        self.assertEqual(result[0]['published_at'], '2026-10-07T00:00:00Z')
        self.assertEqual(result[0]['archive_author'], 'Donald J. Trump')
        self.assertEqual(result[0]['archive_post_type'], 'retruth')
        self.assertEqual(result[0]['archive_removed_raw'], 'false')
        self.assertEqual(result[0]['original_url'], 'https://truthsocial.com/@realDonaldTrump/posts/123')
        self.assertTrue(all(row['verification_status'] == 'archive_only' for row in result))
        self.assertTrue(all('person_id' not in row for row in result))

    def test_unknown_fields_dates_and_empty_feed_do_not_invent_values(self):
        content = feed('<item><title>Unknown fields</title><pubDate>2026-10-01T12:00:00</pubDate></item>'
                       '<item><title>Invalid date</title><pubDate>unknown</pubDate></item>'
                       '<item><title>Known date</title><dc:date>2026-10-02T00:00:00Z</dc:date></item>')
        result = parse_preview(content)
        self.assertEqual(result[0]['title'], 'Known date')
        for row in result[1:]:
            self.assertIsNone(row['published_at'])
            self.assertIsNone(row['url'])
            self.assertIsNone(row['original_url'])
            self.assertIsNone(row['archive_author'])
            self.assertIsNone(row['archive_post_type'])
            self.assertIsNone(row['archive_removed_raw'])
        self.assertEqual(parse_preview(feed()), [])

    def test_display_links_reject_foreign_hosts_credentials_and_non_https(self):
        for value in ('javascript:bad()', 'http://www.trumpstruth.org/statuses/1',
                      'https://www.trumpstruth.org.evil.invalid/statuses/1',
                      'https://user@www.trumpstruth.org/statuses/1',
                      'https://www.trumpstruth.org/statuses/1?redirect=other',
                      'https://www.trumpstruth.org/about', 'https://[bad'):
            self.assertIsNone(preview_link(value))
        self.assertIsNone(preview_link('https://example.invalid/post', original=True))
        self.assertIsNone(preview_link('https://user@truthsocial.com/post', original=True))
        self.assertEqual(preview_link('https://www.trumpstruth.org/statuses/1#post'), 'https://www.trumpstruth.org/statuses/1')

    def test_xml_entities_html_and_size_limit_are_rejected(self):
        with self.assertRaises(DefusedXmlException):
            parse_preview(b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><item><title>&x;</title></item></channel></rss>')
        with self.assertRaises(ValueError):
            parse_preview(b'<html><body>Not RSS</body></html>')
        with self.assertRaises(ValueError):
            parse_preview(b'x' * (MAX_BYTES + 1))
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * (MAX_BYTES + 1)))) as client:
            with self.assertRaises(ValueError):
                fetch_preview(client)

    def test_one_get_does_not_follow_item_links_or_redirects(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=feed('<item><title>Test</title><link>https://www.trumpstruth.org/statuses/1</link></item>'))
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            self.assertEqual(len(fetch_preview(client)), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'GET')
        self.assertEqual(str(requests[0].url), TRUMP_FEED_URL)
        for status in (302, 403, 500):
            with httpx.Client(transport=httpx.MockTransport(
                lambda r: httpx.Response(status, headers={'Location': 'https://example.invalid/'})
            ), follow_redirects=False) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    fetch_preview(client)

    def test_config_must_keep_archive_identity_fixed_entry_and_person(self):
        self.assertEqual(validate_source(source_sample()), source_sample())
        for row in (None, {**source_sample(), 'provenance_type': 'official'},
                    {**source_sample(), 'feed_url': 'https://example.invalid/feed'},
                    {**source_sample(), 'person_id': SOURCE_ID}, {**source_sample(), 'fetch_method': 'html'}):
            with self.assertRaises(ValueError):
                validate_source(row)

    @patch('app.preview_trump.load_source', return_value=source_sample())
    @patch('app.preview_trump.fetch_preview', return_value=[])
    def test_output_marks_third_party_archive_and_preserves_disabled_switches(self, fetch, load):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['mode'], 'manual_archive_preview')
        self.assertEqual(result['provenance_type'], 'third_party_archive')
        self.assertFalse(result['source_enabled'])
        self.assertFalse(result['monitoring_enabled'])
        self.assertFalse(result['link_enabled'])
        self.assertIn('未访问原帖', result['attribution'])
        self.assertEqual(result['items'], [])

    def test_failures_return_nonzero_without_success_or_private_messages(self):
        for failure in (httpx.ReadTimeout('DO_NOT_EXPOSE'), httpx.ConnectError('DO_NOT_EXPOSE'),
                        ValueError('DO_NOT_EXPOSE'), OperationalError('private SQL', {}, Exception('DO_NOT_EXPOSE'))):
            output = io.StringIO()
            with patch('app.preview_trump.load_source', side_effect=failure), redirect_stdout(output):
                self.assertEqual(main(), 1)
            self.assertNotIn('DO_NOT_EXPOSE', output.getvalue())
            self.assertNotIn('"status": "ok"', output.getvalue())


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class TrumpPreviewDatabaseTests(unittest.TestCase):
    def test_real_configuration_read_is_readonly_and_keeps_all_values(self):
        engine = create_read_engine(Settings())
        try:
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar_one(), 'on')
                before = [dict(row) for row in connection.execute(text('SELECT * FROM sources ORDER BY source_id')).mappings()]
            source = load_source(Settings())
            self.assertEqual(source['source_id'], SOURCE_ID)
            self.assertEqual(source['person_id'], PERSON_ID)
            self.assertFalse(source['enabled'])
            self.assertFalse(source['monitoring_enabled'])
            self.assertFalse(source['link_enabled'])
            with engine.connect() as connection:
                after = [dict(row) for row in connection.execute(text('SELECT * FROM sources ORDER BY source_id')).mappings()]
            self.assertEqual(before, after)
        finally:
            engine.dispose()
