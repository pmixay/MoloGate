import logging
import os

import requests
from open_webui.retrieval.web.main import SearchResult

log = logging.getLogger(__name__)

# Mologate: overridable for a proxy or a test double.
EXA_API_BASE = os.getenv('EXA_API_BASE_URL', 'https://api.exa.ai').rstrip('/')


def search_exa(
    api_key: str,
    query: str,
    count: int,
    filter_list: list[str] | None = None,
    max_content_length: int | None = None,
) -> list[SearchResult]:
    """Search using Exa Search API and return the results as a list of SearchResult objects.

    Args:
        api_key (str): A Exa Search API key
        query (str): The query to search for
        count (int): Number of results to return
        filter_list (list[str] | None): List of domains to filter results by
        max_content_length (int | None): Maximum characters per result; None leaves text unlimited.
    """
    log.info('Searching with Exa for query: %s', query)

    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}

    payload = {
        'query': query,
        'numResults': count or 5,
        'includeDomains': filter_list,
        'contents': {'text': {'maxCharacters': max_content_length} if max_content_length is not None else True},
        'type': 'auto',  # Use the auto search type (keyword or neural)
    }

    try:
        response = requests.post(f'{EXA_API_BASE}/search', headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        results = data['results']
        log.info('Found %s results', len(results))
        return [
            SearchResult(
                link=result['url'],
                title=result['title'],
                snippet=(result.get('text') or '')[:max_content_length],
            )
            for result in results
        ]
    except Exception as e:
        log.error(f'Error searching Exa: {e}')
        return []


def get_exa_contents(api_key: str, urls: list[str], timeout: float = 60) -> list[dict]:
    """Mologate: read web pages through Exa's Contents API, from Exa's cache or a live crawl.

    Returns {'url', 'title', 'text'} for each page Exa could read; pages it couldn't are logged.
    """
    pages = []
    for start in range(0, len(urls), 100):  # The API takes up to 100 URLs per request.
        batch = urls[start : start + 100]
        response = requests.post(
            f'{EXA_API_BASE}/contents',
            headers={'x-api-key': api_key, 'Content-Type': 'application/json'},
            json={'urls': batch, 'text': True},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        for status in data.get('statuses') or []:
            if status.get('status') != 'success':
                log.warning('Exa could not read %s: %s', status.get('id'), status.get('error'))
        pages.extend(
            {'url': result.get('url'), 'title': result.get('title') or '', 'text': result['text']}
            for result in data.get('results') or []
            if result.get('text')
        )
    log.info('Read %s of %s pages through Exa', len(pages), len(urls))
    return pages
