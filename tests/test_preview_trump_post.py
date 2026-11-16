import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx

from app.preview_trump_post import (
    MAX_BYTES, MAX_MEDIA_NOTES, MAX_QUOTED_POSTS, PREVIEW_CHARACTERS,
    archive_url, fetch_post, main, parse_post, select_candidate,
)
from test_preview_trump import source_sample

URL = 'https://www.trumpstruth.org/statuses/1'
ORIGINAL = 'https://truthsocial.com/@realDonaldTrump/123'


def candidate(**changes):
    return {
        'url': URL, 'title': 'Synthetic archive title', 'archive_author': 'Donald J. Trump',
        'archive_post_type': 'original', 'original_url': ORIGINAL,
        'published_at': '2026-10-17T04:00:00Z', 'archive_removed_raw': 'false', **changes,
    }


def status(text='Synthetic own words.', nested='', media='', identity=1,
           author='Donald J. Trump', handle='@realDonaldTrump', original=ORIGINAL):
    return f'''<div class="status"><div class="status__header">
        <a class="status-info__account-name">{author}</a><div class="status-info__meta">
        <a>{handle}</a><a href="https://www.trumpstruth.org/statuses/{identity}">
        <time datetime="2026-10-17T04:00:00+00:00">Archive displayed date</time></a></div>
        <a class="status__external-link" href="{original}">Original Post</a></div>
        <div class="status__body"><div class="status__content">{text}</div>
        <div class="status__attachments">{media}</div>{nested}</div></div>'''


def note(label='Image Description:', text='Synthetic image explanation.'):
    return f'''<div class="status-details-attachment__body"><h5>{label}</h5>
        <div class="status-details-attachment__text">{text}</div></div>'''


def page(post=None, notes=''):
    if post is None:
        post = status()
    return f'''<html><body><nav>Do not extract navigation</nav><div class="main-body__content">
        {post}<div class="status-details"><table class="status-details-table">
        <tr><td class="status-details-table__key">Capture Date</td><td>
        <time datetime="2026-10-17T13:00:00+00:00">Capture date</time></td></tr></table>{notes}</div>
        </div><footer>Do not extract footer</footer></body></html>'''.encode('utf-8')


class TrumpPostPreviewTests(unittest.TestCase):
    def test_short_own_text_is_separate_from_image_description_and_capture_time(self):
        result = parse_post(page(notes=note()), candidate())
        self.assertEqual(result['verification_status'], 'archive_only')
        self.assertEqual(result['post']['post_text']['text_preview'], 'Synthetic own words.')
        self.assertEqual(result['post']['original_url'], ORIGINAL)
        self.assertEqual(result['archive_media_notes'][0]['kind'], 'archive_image_description')
        self.assertNotIn('explanation', result['post']['post_text']['text_preview'])
        self.assertEqual(result['post']['page_post_published_at'], '2026-10-17T04:00:00Z')
        self.assertEqual(result['page_capture_at'], '2026-10-17T13:00:00Z')

    def test_nested_quotes_keep_authors_and_text_separate(self):
        inner = status('Quoted words from another account.', identity=2, author='Other author',
                       handle='@other', original='https://truthsocial.com/@other/456')
        result = parse_post(page(status('Own comment.', nested=inner)), candidate(archive_post_type='quote'))
        self.assertEqual(result['verification_status'], 'archive_only')
        self.assertEqual(result['post']['archive_author'], 'Donald J. Trump')
        self.assertEqual(result['post']['post_text']['text_preview'], 'Own comment.')
        quoted = result['quoted_posts'][0]
        self.assertEqual(quoted['archive_author'], 'Other author')
        self.assertEqual(quoted['post_text']['text_preview'], 'Quoted words from another account.')
        self.assertEqual(quoted['parent_archive_url'], URL)
        self.assertNotIn('person_id', quoted)

    def test_image_only_post_keeps_empty_text_and_does_not_use_title_or_alt(self):
        post = status('', media='<div class="status-attachment--image"><img alt="Do not turn alt into own words"></div>')
        result = parse_post(page(post, note()), candidate(title='Synthetic generated image title'))
        self.assertEqual(result['verification_status'], 'archive_only')
        self.assertIsNone(result['post']['post_text']['text_preview'])
        self.assertEqual(result['post']['post_text']['text_characters'], 0)
        self.assertEqual(result['post']['image_count'], 1)

    def test_video_transcript_and_unavailable_notice_are_not_own_text(self):
        post = status('', media='<div class="status-attachment--video"><video src="https://example.invalid/video"></video></div>')
        notes = note('Video Transcript:', 'Synthetic unverified transcript.') + note('No File Information Available:', 'Processing notice')
        result = parse_post(page(post, notes), candidate())
        self.assertEqual(result['verification_status'], 'archive_only')
        self.assertIsNone(result['post']['post_text']['text_preview'])
        self.assertEqual(result['post']['video_count'], 1)
        self.assertEqual(result['archive_media_notes'][0]['kind'], 'archive_transcript')
        self.assertEqual(result['archive_media_notes'][1]['kind'], 'unavailable')
        self.assertIsNone(result['archive_media_notes'][1]['text_preview'])

    def test_inline_blockquotes_are_not_merged_with_own_words(self):
        result = parse_post(page(status('Own words.<blockquote>Unattributed quotation.</blockquote>')), candidate())
        self.assertEqual(result['post']['post_text']['text_preview'], 'Own words.')
        self.assertEqual(result['post']['inline_quotes'][0]['text_preview'], 'Unattributed quotation.')

    def test_structure_author_url_type_and_time_mismatches_need_review(self):
        for html, entry in (
            (b'<html><h1>Unknown page</h1></html>', candidate()),
            (page(status() + status(identity=2)), candidate()),
            (page(status('')), candidate()),
            (page(status(author='Other Person')), candidate()),
            (page(status(handle='@other')), candidate()),
            (page(), candidate(archive_author='Other Person')),
            (page(), candidate(original_url='https://truthsocial.com/@other/999')),
            (page(status(identity=3)), candidate()),
            (page(), candidate(archive_post_type='quote')),
            (page(), candidate(archive_post_type='unknown')),
            (page(status(nested=status(identity=2))), candidate()),
            (page(), candidate(published_at='2026-10-17T05:00:00Z')),
        ):
            with self.subTest(entry=entry, html_length=len(html)):
                self.assertEqual(parse_post(html, entry)['verification_status'], 'needs_review')

    def test_unknown_post_time_does_not_use_capture_time(self):
        html = page().replace(b'2026-10-17T04:00:00+00:00', b'unknown')
        result = parse_post(html, candidate(published_at=None))
        self.assertIsNone(result['post']['page_post_published_at'])
        self.assertEqual(result['page_capture_at'], '2026-10-17T13:00:00Z')

    def test_text_and_nested_output_limits_are_explicit(self):
        long_text = 'Synthetic long text. ' * 200
        nested = ''.join(status(long_text, identity=i) for i in range(2, 10))
        result = parse_post(page(status(long_text, nested=nested), note(text=long_text) * 8), candidate(archive_post_type='quote'))
        self.assertEqual(len(result['post']['post_text']['text_preview']), PREVIEW_CHARACTERS)
        self.assertTrue(result['post']['post_text']['text_preview_truncated'])
        self.assertEqual(len(result['quoted_posts']), MAX_QUOTED_POSTS)
        self.assertEqual(result['quoted_posts_omitted'], 3)
        self.assertEqual(len(result['archive_media_notes']), MAX_MEDIA_NOTES)
        self.assertEqual(result['archive_media_notes_omitted'], 3)

    def test_candidate_and_fetch_allow_only_archive_status_urls(self):
        for url in ('https://example.invalid/statuses/1', URL + '?next=other',
                    'https://user@www.trumpstruth.org/statuses/1', 'http://www.trumpstruth.org/statuses/1'):
            with self.assertRaises(ValueError):
                archive_url(url)
        self.assertIsNone(select_candidate([{'url': 'https://example.invalid/'}]))
        self.assertEqual(select_candidate([{'url': None}, candidate()])['url'], URL)

    def test_one_detail_get_does_not_follow_media_or_quotes_and_rejects_failures(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=page(status(nested=status(identity=2)), note()))
        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            fetch_post(client, candidate(archive_post_type='quote'))
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), URL)
        for status_code in (302, 403, 500):
            with httpx.Client(transport=httpx.MockTransport(
                lambda r: httpx.Response(status_code, headers={'Location': 'https://example.invalid/'})
            ), follow_redirects=False) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    fetch_post(client, candidate())
        with self.assertRaises(ValueError):
            parse_post(b'x' * (MAX_BYTES + 1), candidate())
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * (MAX_BYTES + 1)))) as client:
            with self.assertRaises(ValueError):
                fetch_post(client, candidate())

    @patch('app.preview_trump_post.load_source', return_value=source_sample())
    @patch('app.preview_trump_post.fetch_rss_preview', return_value=[])
    @patch('app.preview_trump_post.fetch_post')
    def test_no_candidate_does_not_fetch_detail(self, detail, feed, load):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        detail.assert_not_called()

    @patch('app.preview_trump_post.load_source', return_value=source_sample())
    @patch('app.preview_trump_post.fetch_rss_preview', return_value=[candidate()])
    def test_command_marks_archive_only_review_and_network_failure(self, feed, load):
        for verification, expected in (('archive_only', 0), ('needs_review', 2)):
            output = io.StringIO()
            with patch('app.preview_trump_post.fetch_post', return_value={'verification_status': verification}), redirect_stdout(output):
                self.assertEqual(main(), expected)
            result = json.loads(output.getvalue())
            self.assertEqual(result['mode'], 'manual_archive_post_preview')
            self.assertEqual(result['provenance_type'], 'third_party_archive')
            self.assertFalse(result['source_enabled'])
        output = io.StringIO()
        with patch('app.preview_trump_post.fetch_post', side_effect=httpx.ReadTimeout('DO_NOT_EXPOSE')), redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertNotIn('DO_NOT_EXPOSE', output.getvalue())
