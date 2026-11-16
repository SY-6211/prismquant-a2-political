import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from defusedxml.common import DefusedXmlException

from app.preview_ecb import ECB_FEED_URL, MAX_BYTES, fetch_preview, main, parse_preview, published_utc


def feed(items=''):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>' + items + '</channel></rss>').encode('utf-8')


class EcbPreviewTests(unittest.TestCase):
    def test_latest_five_and_utc_without_attribution(self):
        content = feed(''.join(
            f'<item><title>公开信息 {day}</title><link>https://www.ecb.europa.eu/item/{day}</link>'
            f'<pubDate>2026-10-{day:02d}T08:00:00+08:00</pubDate></item>'
            for day in range(1, 8)
        ))
        items = parse_preview(content)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]['title'], '公开信息 7')
        self.assertEqual(items[0]['published_at'], '2026-10-07T00:00:00Z')
        self.assertTrue(all('person_id' not in item for item in items))

    def test_missing_invalid_or_ambiguous_date_is_not_invented(self):
        for value in (None, '', 'not-a-date', '2026-10-01T12:00:00'):
            with self.subTest(value=value):
                self.assertIsNone(published_utc(value))
        self.assertEqual(published_utc('Fri, 16 Oct 2026 12:00:00 +0200'), '2026-10-16T10:00:00Z')
        item = parse_preview(feed('<item><title>Undated</title></item>'))[0]
        self.assertIsNone(item['published_at'])
        self.assertIsNone(item['url'])
        self.assertEqual(parse_preview(feed()), [])

    def test_non_http_item_link_is_not_returned(self):
        items = parse_preview(feed('<item><title>Test</title><link>javascript:bad()</link></item>'))
        self.assertIsNone(items[0]['url'])

    def test_entities_and_html_are_rejected(self):
        with self.assertRaises(DefusedXmlException):
            parse_preview(b'<!DOCTYPE rss [<!ENTITY test "expanded">]><rss><channel><item><title>&test;</title></item></channel></rss>')
        with self.assertRaises(ValueError):
            parse_preview(b'<html><body>Not RSS</body></html>')

    def test_size_limit(self):
        with self.assertRaises(ValueError):
            parse_preview(b'x' * (MAX_BYTES + 1))
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'x' * (MAX_BYTES + 1)))
        with httpx.Client(transport=transport) as client:
            with self.assertRaises(ValueError):
                fetch_preview(client)

    def test_one_get_without_item_fetches(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=feed('<item><title>Test</title><link>https://example.invalid/details</link></item>'))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(len(fetch_preview(client)), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'GET')
        self.assertEqual(str(requests[0].url), ECB_FEED_URL)

    def test_http_failure_and_redirect_do_not_parse_as_empty_success(self):
        for status in (302, 403, 500):
            with self.subTest(status=status):
                transport = httpx.MockTransport(lambda request: httpx.Response(status, headers={'Location': 'https://example.invalid/'}))
                with httpx.Client(transport=transport, follow_redirects=False) as client:
                    with self.assertRaises(httpx.HTTPStatusError):
                        fetch_preview(client)

    @patch('app.preview_ecb.fetch_preview', side_effect=httpx.ReadTimeout('internal details'))
    @patch('app.preview_ecb.load_source', return_value={'name': 'ECB', 'enabled': False})
    def test_timeout_has_nonzero_exit_without_success(self, load, fetch):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertIn('超时', output.getvalue())
        self.assertNotIn('internal details', output.getvalue())
