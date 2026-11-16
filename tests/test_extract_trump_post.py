import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx

from app.extract_trump_post import TARGET_URL, find_target, main, prepare_extraction
from app.preview_trump import TRUMP_FEED_URL, parse_feed_items, parse_preview
from app.preview_trump_post import MAX_FULL_PARTS, parse_post
from test_preview_trump import feed, source_sample
from test_preview_trump_post import URL, candidate, note, page, status


def extract(html, entry=None):
    entry = entry or candidate()
    return prepare_extraction(parse_post(html, entry, include_full=True), entry)


class FullTrumpExtractionTests(unittest.TestCase):
    def test_long_body_keeps_paragraphs_breaks_inline_words_and_full_hash(self):
        first = 'Synthetic long paragraph. ' * 150
        html = page(status(f'<p>{first.strip()}</p><p>Second <a>inline</a> paragraph.<br>Next line.</p>'))
        result = extract(html)
        expected = first.strip() + '\n\nSecond inline paragraph.\nNext line.'
        self.assertTrue(result['extraction_complete'])
        self.assertTrue(result['ingestion_eligible'])
        self.assertEqual(result['record']['body_text'], expected)
        self.assertEqual(result['record']['content_hash'], hashlib.sha256(expected.encode('utf-8')).hexdigest())
        preview = parse_post(html, candidate())
        self.assertEqual(len(preview['post']['post_text']['text_preview']), 1200)
        self.assertTrue(preview['post']['post_text']['text_preview_truncated'])
        self.assertNotIn('text', preview['post']['post_text'])

    def test_long_media_notes_are_full_separate_and_unresolved(self):
        description = '合成图片说明。' * 300
        result = extract(page(notes=note(text=f'<p>{description}</p><p>Second paragraph.</p>')))
        item = result['archive_post']['archive_media_notes'][0]
        self.assertEqual(item['text'], description + '\n\nSecond paragraph.')
        self.assertGreater(len(item['text']), 1200)
        self.assertEqual(item['kind'], 'archive_image_description')
        self.assertIsNone(item['owner_archive_url'])
        self.assertIsNone(item['attachment_ref'])
        self.assertEqual(item['association_status'], 'unresolved')
        self.assertEqual(result['record']['body_text'], 'Synthetic own words.')
        self.assertNotIn('text_preview', item)

    def test_more_than_five_quotes_and_media_notes_are_all_retained(self):
        nested = ''.join(status('Long quoted text. ' * 100, identity=i, author=f'Quoted author {i}') for i in range(2, 10))
        html = page(status('Own comment.', nested=nested), note() * 8)
        entry = candidate(archive_post_type='quote')
        detail = parse_post(html, entry, include_full=True)
        self.assertEqual(len(detail['quoted_posts']), 8)
        self.assertEqual(len(detail['archive_media_notes']), 8)
        self.assertEqual(detail['quoted_posts_omitted'], 0)
        self.assertEqual(detail['archive_media_notes_omitted'], 0)
        result = prepare_extraction(detail, entry)
        self.assertTrue(result['extraction_complete'])
        self.assertFalse(result['ingestion_eligible'])
        self.assertEqual(result['archive_post']['quoted_posts'][0]['archive_author'], 'Quoted author 2')
        self.assertEqual(result['archive_post']['quoted_posts'][0]['parent_archive_url'], URL)
        self.assertGreater(len(result['archive_post']['quoted_posts'][0]['text']), 1200)

    def test_inline_quotes_keep_full_text_order_and_nested_parent(self):
        quote = 'Synthetic quotation. ' * 100
        html = page(status('Own words.' + ''.join(f'<blockquote>{quote.strip()}</blockquote>' for _ in range(8))))
        result = extract(html)
        self.assertEqual(result['record']['body_text'], 'Own words.')
        self.assertEqual(len(result['archive_post']['inline_quotes']), 8)
        self.assertEqual(result['archive_post']['inline_quotes'][7]['position'], 7)
        self.assertEqual(result['archive_post']['inline_quotes'][0]['text'], quote.strip())
        self.assertIsNone(result['archive_post']['inline_quotes'][0]['attribution'])
        self.assertFalse(result['ingestion_eligible'])
        grandchild = status('Grandchild.', identity=3)
        child = status('Child.', identity=2, nested=grandchild)
        result = extract(page(status('Own words.', nested=child)), candidate(archive_post_type='quote'))
        self.assertEqual(result['archive_post']['quoted_posts'][1]['parent_archive_url'], 'https://www.trumpstruth.org/statuses/2')

    def test_exceeding_full_limits_raises_instead_of_returning_partial_content(self):
        for html in (
            page(status(nested=status(identity=2) * (MAX_FULL_PARTS + 1))),
            page(notes=note() * (MAX_FULL_PARTS + 1)),
            page(status('<blockquote>Quote.</blockquote>' * (MAX_FULL_PARTS + 1))),
        ):
            with self.assertRaises(ValueError):
                parse_post(html, candidate(archive_post_type='quote'), include_full=True)

    def test_bounded_or_omitted_results_cannot_be_prepared_as_full(self):
        with self.assertRaises(ValueError):
            prepare_extraction(parse_post(page(), candidate()), candidate())
        detail = parse_post(page(), candidate(), include_full=True)
        detail['archive_media_notes_omitted'] = 1
        with self.assertRaises(ValueError):
            prepare_extraction(detail, candidate())

    def test_unmatched_page_is_not_complete_and_has_no_prepared_record(self):
        result = extract(page(status(author='Other author')))
        self.assertFalse(result['extraction_complete'])
        self.assertFalse(result['ingestion_eligible'])
        self.assertIsNone(result['record'])
        self.assertIsNone(result['archive_post'])

    def test_media_only_and_unavailable_text_are_not_fabricated(self):
        html = page(status('', media='<div class="status-attachment--video"><video></video></div>'),
                    note('No File Information Available:', 'Processing notice'))
        result = extract(html)
        self.assertTrue(result['extraction_complete'])
        self.assertFalse(result['ingestion_eligible'])
        self.assertIsNone(result['record']['body_text'])
        self.assertIsNone(result['record']['content_hash'])
        self.assertIsNone(result['archive_post']['archive_media_notes'][0]['text'])

    def test_unknown_body_or_media_structure_requires_review(self):
        for html in (
            page(status().replace('class="status__content"', 'class="unexpected-text"')),
            page(notes=note('Unknown media section:', 'Unknown meaning.')),
            page(notes=note().replace('status-details-attachment__text', 'unknown-text')),
        ):
            result = extract(html)
            self.assertFalse(result['extraction_complete'])
            self.assertFalse(result['ingestion_eligible'])

    def test_comments_scripts_navigation_and_alt_are_not_text(self):
        html = page(status('<p>First <strong>bold</strong> paragraph.</p><!-- Not text -->'
                           '<script>Do not extract.</script><style>Do not extract.</style>'
                           '<p>Second paragraph.</p>', media='<div class="status-attachment--image"><img alt="Not main text"></div>'))
        self.assertEqual(extract(html)['record']['body_text'], 'First bold paragraph.\n\nSecond paragraph.')

    def test_fixed_target_is_found_beyond_five_without_switching_targets(self):
        items = ''.join(f'<item><title>Synthetic {i}</title><link>https://www.trumpstruth.org/statuses/{i}</link>'
                        f'<pubDate>2026-10-{i:02d}T00:00:00Z</pubDate></item>' for i in range(10, 17))
        items += f'<item><title>Target</title><link>{TARGET_URL}#post</link><pubDate>2026-10-01T00:00:00Z</pubDate></item>'
        self.assertIsNone(find_target(parse_preview(feed(items))))
        self.assertEqual(find_target(parse_feed_items(feed(items)))['url'], TARGET_URL)
        self.assertIsNone(find_target([candidate()]))

    @patch('app.extract_trump_post.load_source', return_value=source_sample())
    def test_command_only_gets_feed_and_pinned_detail_and_never_saves(self, load):
        requests = []
        xml = feed(f'<item><title>Synthetic pinned post</title><link>{TARGET_URL}</link>'
                   '<pubDate>2026-10-17T04:00:00Z</pubDate><dc:creator>Donald J. Trump</dc:creator>'
                   '<truth:originalUrl>https://truthsocial.com/@realDonaldTrump/123</truth:originalUrl>'
                   '<archive:postType>original</archive:postType></item>')
        html = page(status(identity=42396), notes=note(text='Synthetic description. ' * 100))
        def handler(request):
            requests.append(str(request.url))
            if str(request.url) == TRUMP_FEED_URL:
                return httpx.Response(200, content=xml)
            if str(request.url) == TARGET_URL:
                return httpx.Response(200, content=html)
            raise AssertionError('Unexpected original/media/quote request')
        output = io.StringIO()
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addCleanup(client.close)
        with patch('app.extract_trump_post.httpx.Client', return_value=client), redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(requests, [TRUMP_FEED_URL, TARGET_URL])
        self.assertEqual(result['mode'], 'manual_full_archive_extraction')
        self.assertFalse(result['saved'])
        self.assertFalse(result['source_enabled'])
        self.assertGreater(len(result['archive_post']['archive_media_notes'][0]['text']), 1200)

    @patch('app.extract_trump_post.load_source', return_value=source_sample())
    @patch('app.extract_trump_post.fetch_post')
    def test_absent_target_and_network_failure_do_not_get_or_return_detail(self, detail, load):
        for side_effect, entries, code in ((None, [candidate()], 2), (httpx.ReadTimeout('DO_NOT_EXPOSE'), None, 1)):
            output = io.StringIO()
            with patch('app.extract_trump_post.fetch_feed_items', return_value=entries, side_effect=side_effect), redirect_stdout(output):
                self.assertEqual(main(), code)
            self.assertNotIn('DO_NOT_EXPOSE', output.getvalue())
            self.assertNotIn('"status": "ok"', output.getvalue())
        detail.assert_not_called()
