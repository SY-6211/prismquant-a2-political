"""Bounded discovery from the publishers' public historical indexes, not RSS."""
import re
import base64
import json
import time
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.preview_ecb import published_utc
from app.preview_ecb_speech import speech_url
from app.preview_trump_post import own_content

ECB_INDEX = 'https://www.ecb.europa.eu/foedb/dbs/foedb/publications.en/'
TRUMP_INDEX = 'https://www.trumpstruth.org/'
MAX_INDEX_BYTES = 8 * 1024 * 1024


def read_bytes(client, url, pause=time.sleep):
    pause(1)
    with client.stream('GET', url) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('History endpoint must return HTTP 200 without redirects')
        content = bytearray()
        for chunk in response.iter_bytes(65536):
            content.extend(chunk)
            if len(content) > MAX_INDEX_BYTES:
                raise ValueError('History index exceeds size limit')
    return bytes(content)


def in_range(stamp, since, until):
    value = published_utc(stamp)
    return bool(value and since <= datetime.fromisoformat(value).date() <= until)


def parse_ecb_chunk(content, header):
    import json
    values = json.loads(content)
    required = {'pub_timestamp', 'boardmember', 'documentTypes', 'publicationProperties'}
    if not isinstance(header, list) or not required.issubset(header) or not isinstance(values, list) or len(values) % len(header):
        raise ValueError('ECB history schema changed')
    entries, stamps = [], []
    for offset in range(0, len(values), len(header)):
        row = dict(zip(header, values[offset:offset + len(header)]))
        stamp = datetime.fromtimestamp(row['pub_timestamp'], timezone.utc).isoformat().replace('+00:00', 'Z')
        stamps.append(stamp)
        if row['boardmember'] != 'Christine Lagarde':
            continue
        properties = row['publicationProperties']
        title = properties.get('Title', '').strip() if isinstance(properties, dict) else ''
        for path in row['documentTypes'] or []:
            try:
                url = speech_url(urljoin('https://www.ecb.europa.eu', path))
            except ValueError:
                continue
            if title:
                entries.append(dict(url=url, title='Christine Lagarde: ' + title,
                                    published_at=stamp, published_raw=stamp, metadata_origin='ecb_history_index'))
    return entries, stamps


def discover_ecb(client, since, until, max_pages, *, pause=time.sleep):
    import json
    versions = json.loads(read_bytes(client, ECB_INDEX + 'versions.json', pause))
    current = versions[0]
    if not re.fullmatch(r'\d+', current['version']) or not re.fullmatch(r'[A-Za-z0-9_-]+', current['hash']):
        raise ValueError('Invalid ECB index version')
    base = ECB_INDEX + current['version'] + '/' + current['hash'] + '/'
    meta = json.loads(read_bytes(client, base + 'metadata.json', pause))
    chunk_size, group_size, total = meta['chunk_size'], meta['chunk_group_size'], meta['total_records']
    if any(type(v) is not int or v <= 0 for v in (chunk_size, group_size, total)) or chunk_size > 1000:
        raise ValueError('Invalid ECB index chunk sizes')
    if meta.get('sort', [None])[0] != '!pub_timestamp':
        raise ValueError('ECB history is not ordered by descending publication time')
    candidates, previous = [], None
    for page in range(max_pages):
        first = page * chunk_size
        content = read_bytes(client, base + f'data/{first // group_size}/chunk_{page}.json', pause)
        entries, stamps = parse_ecb_chunk(content, meta['header'])
        for stamp in stamps:
            if previous is not None and stamp > previous:
                raise ValueError('ECB history ordering changed; coverage not confirmed')
            previous = stamp
        candidates.extend(item for item in entries if in_range(item['published_at'], since, until))
        if first + len(stamps) >= total or (stamps and date.fromisoformat(stamps[-1][:10]) < since):
            return dict(candidates=candidates, coverage_complete=True, pages=page + 1, next_url=None, index_version=current['version'])
        if not stamps:
            raise ValueError('ECB history returned an unexpected empty chunk')
    return dict(candidates=candidates, coverage_complete=False, pages=max_pages, next_url=base + f'data/{max_pages * chunk_size // group_size}/chunk_{max_pages}.json', index_version=current['version'])


def validate_trump_index(value):
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.scheme != 'https' or parsed.netloc != 'www.trumpstruth.org' or parsed.path not in ('', '/') or parsed.fragment:
        raise ValueError('History pagination left the allowed archive index')
    if set(query) - {'cursor', 'sort', 'per_page'} or query.get('sort', ['desc']) != ['desc']:
        raise ValueError('Unexpected archive pagination query')
    if any(len(v) != 1 for v in query.values()) or len(value) > 4096:
        raise ValueError('Invalid archive pagination cursor')
    if query.get('per_page', ['100'])[0] not in ('10', '25', '50', '100'):
        raise ValueError('Unexpected archive page size')
    return value


def cursor_boundary(value):
    cursor = parse_qs(urlsplit(validate_trump_index(value)).query).get('cursor', [None])[0]
    if not cursor:
        raise ValueError('Next page has no cursor')
    payload = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), validate=True))
    stamp = payload.get('status_created_at', '')
    if payload.get('_pointsToNextItems') is not True or not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', stamp):
        raise ValueError('Archive cursor schema changed')
    return datetime.fromisoformat(stamp)


def parse_trump_index(content):
    soup = BeautifulSoup(content, 'html.parser')
    statuses = [node for node in soup.select('.status[data-status-url]') if not node.find_parent(class_='status')]
    if not statuses:
        raise ValueError('Archive history structure changed or no posts found')
    entries, stamps = [], []
    for status in statuses:
        post = own_content(status, include_full=True)
        stamp = published_utc(post.get('page_post_published_raw'))
        if not stamp or not post.get('archive_url'):
            raise ValueError('Archive history has an undated or unidentified post')
        stamps.append(stamp)
        if post.get('archive_author') != 'Donald J. Trump' or post.get('archive_handle') != '@realDonaldTrump':
            continue
        nested = status.select_one('.status__body .status') is not None
        body = post['post_text'].get('text') or ''
        entries.append(dict(url=post['archive_url'], original_url=post['original_url'],
                            title=body[:240].strip() or f'Post from {stamp[:10]}',
                            published_at=stamp, published_raw=post['page_post_published_raw'],
                            archive_author=post['archive_author'], archive_post_type='quote' if nested else 'original',
                            archive_removed_raw=None, metadata_origin='archive_listing'))
    next_links = [a for a in soup.select('a[href]') if a.get_text(' ', strip=True) == 'Next Page']
    if len(next_links) > 1:
        raise ValueError('Ambiguous archive next page')
    next_url = validate_trump_index(urljoin(TRUMP_INDEX, next_links[0]['href'])) if next_links else None
    return entries, stamps, next_url


def discover_trump(client, since, until, max_pages, *, start_url=None, pause=time.sleep):
    url = validate_trump_index(start_url or TRUMP_INDEX + '?sort=desc&per_page=100')
    seen, candidates = set(), {}
    previous = cursor_boundary(url) if 'cursor=' in url else None
    for page in range(max_pages):
        if url in seen:
            raise ValueError('Archive pagination repeated a page')
        seen.add(url)
        entries, stamps, next_url = parse_trump_index(read_bytes(client, url, pause))
        for item in entries:
            if in_range(item['published_at'], since, until):
                old = candidates.get(item['url'])
                if old and old != item:
                    raise ValueError('Conflicting archive history metadata')
                candidates[item['url']] = item
        # Reposted originals can carry much older header dates. Coverage follows
        # the index's own pagination boundary, never the oldest visible post.
        boundary = cursor_boundary(next_url) if next_url else None
        if boundary and previous and boundary >= previous:
            raise ValueError('Archive pagination did not advance backwards')
        if not next_url or boundary.date() < since:
            return dict(candidates=list(candidates.values()), coverage_complete=True, pages=page + 1, next_url=None)
        previous = boundary
        url = next_url
    return dict(candidates=list(candidates.values()), coverage_complete=False, pages=max_pages, next_url=url)
