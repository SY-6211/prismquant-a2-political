import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx

from app.preview_ecb_speech import PREVIEW_CHARACTERS, fetch_speech, main, parse_speech, select_candidate, speech_url

URL = 'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261017~7015ea3099.en.html'


def page(subtitle='Statement by Christine Lagarde, President of the ECB', category='SPEECH', body=None):
    if body is None:
        body = 'This is a synthetic test speech paragraph about monetary policy. ' * 5
    return (f'''<!doctype html><html><head><meta name="author" content="European Central Bank">
    <meta property="article:published_time" content="2026-10-17"></head><body>
    <nav>Navigation should not be extracted</nav><main>
    <div class="title"><ul><li>{category}</li></ul><h1>Test statement</h1></div>
    <div class="section"><h2 class="ecb-pressContentSubtitle">{subtitle}</h2>
    <p class="ecb-publicationDate">17 October 2026</p><h2>Introduction</h2><p>{body}</p>
    <script>Never execute or extract this</script></div></main>
    <footer>Cookie notice should not be extracted</footer></body></html>''').encode('utf-8')


class EcbSpeechPreviewTests(unittest.TestCase):
    def test_welcome_address_requires_explicit_lagarde_author_and_speech_category(self):
        subtitle = 'Welcome address by Christine Lagarde, President of the ECB'
        result = parse_speech(page(subtitle=subtitle), URL, include_body=True)
        self.assertEqual(result['verification_status'], 'verified_page_attribution')
        self.assertGreater(len(result['body_text']), 80)
        for evidence, category in (
            ('Welcome address by Another Speaker, discussing Christine Lagarde', 'SPEECH'),
            ('Welcome address mentioning Christine Lagarde', 'SPEECH'),
            (subtitle, 'PRESS RELEASE'),
        ):
            with self.subTest(evidence=evidence, category=category):
                self.assertEqual(parse_speech(page(subtitle=evidence, category=category), URL)['verification_status'], 'needs_review')

    def test_explicit_page_attribution_and_isolated_body(self):
        result = parse_speech(page(), URL)
        self.assertEqual(result['verification_status'], 'verified_page_attribution')
        self.assertEqual(result['speaker'], 'Christine Lagarde')
        self.assertEqual(result['page_date'], '2026-10-17')
        self.assertEqual(result['person_id'], '6d46c161-c1d3-58e8-8a09-bbe9118d9b9e')
        self.assertIn('Introduction', result['body_preview'])
        for text in ('Navigation', 'Cookie notice', 'Never execute', 'Statement by'):
            self.assertNotIn(text, result['body_preview'])

    def test_other_speaker_mentioning_lagarde_is_not_attributed(self):
        result = parse_speech(page(subtitle='Speech by Frank Elderson, with remarks about Christine Lagarde'), URL)
        self.assertEqual(result['verification_status'], 'needs_review')
        self.assertIsNone(result['speaker'])
        self.assertIsNone(result['person_id'])
        self.assertIsNone(result['body_preview'])

    def test_meta_author_or_title_alone_is_insufficient(self):
        content = page().replace(b'ecb-pressContentSubtitle', b'unknown-subtitle').replace(b'European Central Bank', b'Christine Lagarde')
        self.assertEqual(parse_speech(content, URL)['verification_status'], 'needs_review')
        self.assertEqual(parse_speech(page(category='PRESS RELEASE'), URL)['verification_status'], 'needs_review')

    def test_short_body_and_missing_structure_need_review(self):
        self.assertEqual(parse_speech(page(body='Short'), URL)['verification_status'], 'needs_review')
        self.assertEqual(parse_speech(b'<html><main><h1>Cookie screen</h1></main></html>', URL)['verification_status'], 'needs_review')

    def test_preview_is_bounded_and_missing_date_is_not_invented(self):
        content = page(body='Test text ' * 1000).replace(b'2026-10-17', b'unknown')
        result = parse_speech(content, URL)
        self.assertEqual(len(result['body_preview']), PREVIEW_CHARACTERS)
        self.assertTrue(result['body_preview_truncated'])
        self.assertIsNone(result['page_date'])

    def test_candidate_scans_beyond_five_and_checks_host(self):
        items = [{'title': 'Other speaker', 'url': URL}] * 6
        items += [{'title': 'Christine Lagarde: Test', 'url': 'https://example.invalid/speech'}]
        items += [{'title': 'Christine Lagarde: Test', 'url': URL.replace('/press/', '//press/')}]
        self.assertEqual(select_candidate(items)['url'], URL)
        self.assertIsNone(select_candidate(items[:6]))
        for value in (URL.replace('https:', 'http:'), URL.replace('www.ecb.europa.eu', 'www.ecb.europa.eu.evil.invalid'),
                      URL + '?next=other', URL.replace('www.ecb.europa.eu', 'user@www.ecb.europa.eu'),
                      'https://www.ecb.europa.eu/press/accounts/2026/test.html'):
            with self.subTest(url=value):
                with self.assertRaises(ValueError):
                    speech_url(value)

    def test_detail_fetch_is_one_get_without_following_links(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=page())
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            result = fetch_speech(client, URL)
        self.assertEqual(result['speaker'], 'Christine Lagarde')
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), URL)

    @patch('app.preview_ecb_speech.fetch_speech')
    @patch('app.preview_ecb_speech.fetch_feed_items', return_value=[])
    @patch('app.preview_ecb_speech.load_source', return_value={'enabled': False})
    def test_no_candidate_does_not_fetch_detail(self, load, feed, detail):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 2)
        detail.assert_not_called()
        self.assertIn('未找到', output.getvalue())
