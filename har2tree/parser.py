#!/usr/bin/env python
# -*- coding: utf-8 -*-

from pathlib import Path
import json
import copy
from datetime import datetime, timedelta
import uuid
from urllib.parse import urlparse, unquote_plus
from base64 import b64decode
from collections import defaultdict
import re
import os
from io import BytesIO
import hashlib
from operator import itemgetter
from typing import List, Dict, Optional, Union, Tuple
import ipaddress
import sys

import publicsuffix2  # type: ignore
from ete3 import TreeNode  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
import logging
# import html

logger = logging.getLogger(__name__)

# Initialize Public Suffix List
psl = publicsuffix2.PublicSuffixList()


class Har2TreeLogAdapter(logging.LoggerAdapter):
    """
    Prepend log entry with the UUID of the capture
    """
    def process(self, msg, kwargs):
        return '[%s] %s' % (self.extra['uuid'], msg), kwargs


class Har2TreeError(Exception):
    def __init__(self, message: str):
        super(Har2TreeError, self).__init__(message)
        self.message = message


def rebuild_url(base_url: str, partial: str, known_urls: List[str]) -> str:
    splitted_base_url = urlparse(base_url)
    # Remove all possible quotes
    partial = partial.strip()
    partial = unquote_plus(partial)
    if not partial:
        return ''
    if re.match('^https?://', partial):
        # we have a proper URL... hopefully
        # DO NOT REMOVE THIS CLAUSE, required to make the difference with a path
        final_url = partial
    elif partial.startswith('//'):
        # URL without scheme => takes the scheme from the caller
        final_url = f'{splitted_base_url.scheme}:{partial}'
        if final_url not in known_urls:
            logger.debug(f'URL without scheme: {base_url} - {partial} - {final_url}')
    elif partial.startswith('/') or partial[0] not in [';', '?', '#']:
        # We have a path
        if partial[0] != '/':
            # Yeah, that happens, and the browser appends the path in redirect_url to the current path
            if base_url[-1] == '/':
                # Example: http://foo.bar/some/path/ and some/redirect.html becomes http://foo.bar/some/path/some/redirect.html
                final_url = f'{base_url}{partial}'
            else:
                # Need to strip the last part of the URL down to the first / (included), and attach the redirect
                # Example: http://foo.bar/some/path/blah and some/redirect.html becomes http://foo.bar/some/path/some/redirect.html
                last_slash = base_url.rfind('/') + 1
                final_url = f'{base_url[:last_slash]}{partial}'
        else:
            final_url = f'{splitted_base_url.scheme}://{splitted_base_url.netloc}{partial}'
        if final_url not in known_urls:
            # There is something weird, to investigate
            logger.debug(f'URL without netloc: {base_url} - {partial} - {final_url}')
    elif partial.startswith(';'):
        # URL starts at the parameters
        final_url = '{}{}'.format(base_url.split(';')[0], partial)
        if final_url not in known_urls:
            logger.debug(f'URL with only parameter: {base_url} - {partial} - {final_url}')
    elif partial.startswith('?'):
        # URL starts at the query
        final_url = '{}{}'.format(base_url.split('?')[0], partial)
        if final_url not in known_urls:
            logger.debug(f'URL with only query: {base_url} - {partial} - {final_url}')
    elif partial.startswith('#'):
        # URL starts at the fragment
        final_url = '{}{}'.format(base_url.split('#')[0], partial)
        if final_url not in known_urls:
            logger.debug(f'URL with only fragment: {base_url} - {partial} - {final_url}')

    if final_url not in known_urls:
        # sometimes, the port is in the redirect, and striped later on...
        if final_url.startswith('https://') and ':443' in final_url:
            final_url = final_url.replace(':443', '')
        if final_url.startswith('http://') and ':80' in final_url:
            final_url = final_url.replace(':80', '')

    if final_url not in known_urls:
        # strip the single-dot crap: https://foo.bar/path/./blah.js => https://foo.bar/path/blah.js
        try:
            parsed = urlparse(final_url)
            if parsed.path:
                # NOTE: Path('').resolve() => PosixPath('/path/to/current/directory') <= if you run that from your home dir, it is the path to your home dir
                # FIXME: Path('sdsfs/../dsfsdfs/../..').resolve() => PosixPath('/path/to/current')
                resolved_path = str(Path(parsed.path).resolve())
                final_url = parsed._replace(path=resolved_path).geturl()
                if final_url not in known_urls and resolved_path[-1] != '/':
                    # NOTE: the last '/' at the end of the path is stripped by resolve, we try to re-add it
                    resolved_path += '/'
                    final_url = parsed._replace(path=resolved_path).geturl()
            else:
                final_url = parsed._replace(path='/').geturl()
        except Exception:
            logger.debug(f'Not a URL: {base_url} - {partial}')

    if final_url not in known_urls and splitted_base_url.fragment:
        # On a redirect, if the initial has a fragment, it is appended to the dest URL
        try:
            parsed = urlparse(final_url)
            final_url = parsed._replace(fragment=splitted_base_url.fragment).geturl()
        except Exception:
            logger.debug(f'Not a URL: {base_url} - {partial}')

    return final_url


# Standalone methods to extract and cleanup content from an HTML blob.
def url_cleanup(dict_to_clean: dict, base_url: str, all_requests: List[str]) -> Dict[str, List[str]]:
    to_return: Dict[str, List[str]] = {}
    for key, urls in dict_to_clean.items():
        to_return[key] = []
        for url in urls:
            if url.startswith('data'):
                # print(html_doc.getvalue())
                continue
            to_attach = url.strip()
            if to_attach.startswith("\\'") or to_attach.startswith('\\"'):
                to_attach = to_attach[2:-2]
            if to_attach.startswith("'") or to_attach.startswith('"'):
                to_attach = to_attach[1:-1]
            if to_attach.endswith("'") or to_attach.endswith('"'):
                # A quote at the end of the URL can be selected by the fulltext regex
                to_attach = to_attach[:-1]
            to_attach = rebuild_url(base_url, to_attach, all_requests)
            if to_attach.startswith('http'):
                to_return[key].append(to_attach)
            else:
                logger.debug('{key} - not a URL - {to_attach}')
    return to_return


def find_external_ressources(html_doc: BytesIO, base_url: str, all_requests: List[str], full_text_search: bool=True) -> Dict[str, List[str]]:
    # Source: https://stackoverflow.com/questions/31666584/beutifulsoup-to-extract-all-external-resources-from-html
    # Because this is awful.
    # img: https://www.w3schools.com/TAGs/tag_img.asp -> longdesc src srcset
    # script: https://www.w3schools.com/TAGs/tag_script.asp -> src
    # video: https://www.w3schools.com/TAGs/tag_video.asp -> poster src
    # audio: https://www.w3schools.com/TAGs/tag_audio.asp -> src
    # iframe: https://www.w3schools.com/TAGs/tag_iframe.asp -> src
    # embed: https://www.w3schools.com/TAGs/tag_embed.asp -> src
    # source: https://www.w3schools.com/TAGs/tag_source.asp -> src srcset
    # link: https://www.w3schools.com/TAGs/tag_link.asp -> href
    # object: https://www.w3schools.com/TAGs/tag_object.asp -> data
    to_return: Dict[str, List[str]] = {'img': [], 'script': [], 'video': [], 'audio': [],
                                       'iframe': [], 'embed': [], 'source': [],
                                       'link': [],
                                       'object': [],
                                       'css': [],
                                       'full_regex': [],
                                       'javascript': [],
                                       'meta_refresh': []}
    soup = BeautifulSoup(html_doc, 'lxml')
    for link in soup.find_all(['img', 'script', 'video', 'audio', 'iframe', 'embed', 'source', 'link', 'object']):
        if link.get('src'):  # img script video audio iframe embed source
            to_return[link.name].append(unquote_plus(link.get('src')))
        if link.get('srcset'):  # img source
            to_return[link.name].append(unquote_plus(link.get('srcset')))
        if link.get('longdesc'):  # img
            to_return[link.name].append(unquote_plus(link.get('longdesc')))
        if link.get('poster'):  # video
            to_return[link.name].append(unquote_plus(link.get('poster')))
        if link.get('href'):  # link
            to_return[link.name].append(unquote_plus(link.get('href')))
        if link.get('data'):  # object
            to_return[link.name].append(unquote_plus(link.get('data')))

    # Search for meta refresh redirect madness
    # NOTE: we may want to move that somewhere else, but that's currently the only place BeautifulSoup is used.
    meta_refresh = soup.find('meta', attrs={'http-equiv': 'refresh'})
    if meta_refresh:
        to_return['meta_refresh'].append(meta_refresh['content'].partition('=')[2])

    # external stuff loaded from css content, because reasons.
    to_return['css'] = [url.decode() for url in re.findall(rb'url\((.*?)\)', html_doc.getvalue())]

    # Javascript changing the current page
    # I never found a website where it matched anything useful
    to_return['javascript'] = [url.decode() for url in re.findall(b'(?:window|self|top).location(?:.*)\"(.*?)\"', html_doc.getvalue())]

    if full_text_search:
        # Just regex in the whole blob, because we can
        to_return['full_regex'] = [url.decode() for url in re.findall(rb'(?:http[s]?:)?//(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', html_doc.getvalue())]
        # print("################ REGEXES ", to_return['full_regex'])
    # NOTE: unescaping a potential URL as HTML content can make it unusable (example: (...)&ltime=(...>) => (...)<ime=(...))
    # So the next line is disabled and will be reenabled if it turns out to be required at a later time.
    # to_attach = html.unescape(to_attach)
    return url_cleanup(to_return, base_url, all_requests)

# ##################################################################


class HarTreeNode(TreeNode):

    def __init__(self, **kwargs):
        super(HarTreeNode, self).__init__(**kwargs)
        self.logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.add_feature('uuid', str(uuid.uuid4()))
        self.features_to_skip = set(['dist', 'support'])

    def to_dict(self) -> dict:
        to_return = {'uuid': self.uuid, 'children': []}
        for feature in self.features:
            if feature in self.features_to_skip:
                continue
            to_return[feature] = getattr(self, feature)

        for child in self.children:
            to_return['children'].append(child)

        return to_return

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=harnode_json_default)


class URLNode(HarTreeNode):

    def __init__(self, **kwargs):
        super(URLNode, self).__init__(**kwargs)
        # Do not add the body in the json dump
        self.features_to_skip.add('body')
        self.features_to_skip.add('url_split')
        self.features_to_skip.add('start_time')
        self.features_to_skip.add('time')
        self.features_to_skip.add('time_content_received')
        self.features_to_skip.add('ip_address')

    def load_har_entry(self, har_entry: dict, all_requests: List[str]):
        if not self.name:
            # We're in the actual root node
            # NOTE: by the HAR specs: "Absolute URL of the request (fragments are not included)."
            self.add_feature('name', unquote_plus(har_entry['request']['url']))

        self.add_feature('url_split', urlparse(self.name))

        # If the URL contains a fragment (i.e. something after a #), it is stripped in the referer.
        # So we need an alternative URL to do a lookup against
        self.add_feature('alternative_url_for_referer', self.name.split('#')[0])

        # Instant the request is made
        if sys.version_info < (3, 7) and har_entry['startedDateTime'][-1] == 'Z':
            # Python 3.7:
            #   * fromisoformat does not like Z at the end of the string, and wants +XX:XX (not +XXXX)
            #   * strptime %z is cool with Z (=>tzinfo=datetime.timezone.utc) or +XXXX or +XX:XX
            # Python 3.6:
            #   * No fromisoformat
            #   * strptime %z does not like Z at the end of the string, and doesn't like +XX:XX either (wants +XXXX)
            har_entry['startedDateTime'] = har_entry['startedDateTime'].replace('Z', '+0000')
        self.add_feature('start_time', datetime.strptime(har_entry['startedDateTime'], '%Y-%m-%dT%H:%M:%S.%f%z'))

        self.add_feature('pageref', har_entry['pageref'])

        self.add_feature('time', timedelta(milliseconds=har_entry['time']))
        self.add_feature('time_content_received', self.start_time + self.time)  # Instant the response is fully received (and the processing of the content by the browser can start)
        self.add_feature('hostname', urlparse(self.name).hostname)

        if not self.hostname:
            self.logger.warning(f'Something is broken in that node: {har_entry}')

        tld = psl.get_tld(self.hostname)
        if tld:
            if tld in psl.tlds:
                self.add_feature('known_tld', tld)
            else:
                if tld.isdigit():
                    # IPV4
                    pass
                elif ':' in tld:
                    # IPV6
                    pass
                else:
                    self.logger.warning(f'###### TLD WAT {self.name} {tld}')
                    self.add_feature('unknown_tld', tld)
        else:
            self.logger.warning(f'###### No TLD/domain broken {self.name}')

        self.add_feature('request', har_entry['request'])
        # Try to get a referer from the headers
        for h in self.request['headers']:
            if h['name'].lower() == 'referer':
                self.add_feature('referer', unquote_plus(h['value']))
            if h['name'].lower() == 'user-agent':
                self.add_feature('user_agent', h['value'])

        self.add_feature('response', har_entry['response'])

        self.add_feature('response_cookie', har_entry['response']['cookies'])
        if self.response_cookie:
            self.add_feature('set_third_party_cookies', False)
            # https://developer.mozilla.org/en-US/docs/Web/HTTP/headers/Set-Cookie
            # Cookie name must not contain "=", so we can use it safely
            self.add_feature('cookies_received', [])
            for cookie in self.response_cookie:
                is_3rd_party = False
                # If the domain is set, the cookie will be sent in any request to that domain, and any related subdomains
                # Otherwise, it will only be sent to requests to the exact hostname
                # There are other limitations, like secure and path, but in our case, we won't care about it for now as we mainly want to track where the cookies are sent
                if 'domain' in cookie and cookie['domain']:
                    cookie_domain = cookie['domain']
                    if cookie_domain[0] == '.':
                        cookie_domain = cookie_domain[1:]
                else:
                    cookie_domain = self.hostname
                if not self.hostname.endswith(cookie_domain):
                    self.add_feature('set_third_party_cookies', True)
                    is_3rd_party = True
                self.cookies_received.append((cookie_domain, f'{cookie["name"]}={cookie["value"]}', is_3rd_party))

        self.add_feature('request_cookie', har_entry['request']['cookies'])
        if self.request_cookie:
            # https://developer.mozilla.org/en-US/docs/Web/HTTP/headers/Set-Cookie
            # Cookie name must not contain "=", so we can use it safely
            self.add_feature('cookies_sent', {})
            for cookie in self.request_cookie:
                self.cookies_sent[f'{cookie["name"]}={cookie["value"]}'] = []

        if not har_entry['response']['content'].get('text') or har_entry['response']['content']['text'] == '':
            self.add_feature('empty_response', True)
        else:
            if har_entry['response']['content'].get('encoding') == 'base64':
                self.add_feature('body', BytesIO(b64decode(har_entry['response']['content']['text'])))
            else:
                self.add_feature('body', BytesIO(har_entry['response']['content']['text'].encode()))
            self.add_feature('body_hash', hashlib.sha512(self.body.getvalue()).hexdigest())
            self.add_feature('mimetype', har_entry['response']['content']['mimeType'])
            self.add_feature('external_ressources', find_external_ressources(self.body, self.name, all_requests))
            parsed_response_url = urlparse(self.name)
            filename = os.path.basename(parsed_response_url.path)
            if filename:
                self.add_feature('filename', filename)
            else:
                self.add_feature('filename', 'file.bin')

        if ('javascript' in har_entry['response']['content']['mimeType']
                or 'ecmascript' in har_entry['response']['content']['mimeType']):
            self.add_feature('js', True)
        elif har_entry['response']['content']['mimeType'].startswith('image'):
            self.add_feature('image', True)
        elif har_entry['response']['content']['mimeType'].startswith('text/css'):
            self.add_feature('css', True)
        elif 'json' in har_entry['response']['content']['mimeType']:
            self.add_feature('json', True)
        elif har_entry['response']['content']['mimeType'].startswith('text/html'):
            self.add_feature('html', True)
        elif 'font' in har_entry['response']['content']['mimeType']:
            self.add_feature('font', True)
        elif 'octet-stream' in har_entry['response']['content']['mimeType']:
            self.add_feature('octet_stream', True)
        elif ('text/plain' in har_entry['response']['content']['mimeType']
                or 'xml' in har_entry['response']['content']['mimeType']):
            self.add_feature('text', True)
        elif 'video' in har_entry['response']['content']['mimeType']:
            self.add_feature('video', True)
        elif 'mpegurl' in har_entry['response']['content']['mimeType'].lower():
            self.add_feature('livestream', True)
        elif not har_entry['response']['content']['mimeType']:
            self.add_feature('unset_mimetype', True)
        else:
            self.add_feature('unknown_mimetype', True)
            self.logger.warning('Unknown mimetype: {}'.format(har_entry['response']['content']['mimeType']))

        # NOTE: Chrome/Chromium only features
        if har_entry.get('serverIPAddress'):
            self.add_feature('ip_address', ipaddress.ip_address(har_entry['serverIPAddress']))
        if '_initiator' in har_entry:
            if har_entry['_initiator']['type'] == 'other':
                pass
            elif har_entry['_initiator']['type'] == 'parser' and har_entry['_initiator']['url']:
                self.add_feature('initiator_url', unquote_plus(har_entry['_initiator']['url']))
            elif har_entry['_initiator']['type'] == 'script':
                url = self._find_initiator_in_stack(har_entry['_initiator']['stack'])
                if url:
                    self.add_feature('initiator_url', url)
            elif har_entry['_initiator']['type'] == 'redirect':
                # FIXME: Need usecase
                raise Exception(f'Got a redirect! - {har_entry}')
            else:
                # FIXME: Need usecase
                raise Exception(har_entry)

        if har_entry['response']['redirectURL']:
            self.add_feature('redirect', True)
            redirect_url = har_entry['response']['redirectURL']
            # Rebuild the redirect URL so it matches the entry that sould be in all_requests
            redirect_url = rebuild_url(self.name, redirect_url, all_requests)
            # At this point, we should have a URL available in all_requests...
            if redirect_url in all_requests:
                self.add_feature('redirect_url', redirect_url)
            else:
                # ..... Or not. Unable to find a URL for this redirect
                self.add_feature('redirect_to_nothing', True)
                self.add_feature('redirect_url', har_entry['response']['redirectURL'])
                self.logger.warning('Unable to find that URL: {original_url} - {original_redirect} - {modified_redirect}'.format(
                    original_url=self.name, original_redirect=har_entry['response']['redirectURL'], modified_redirect=redirect_url))

    def _find_initiator_in_stack(self, stack: dict):
        # Because everything is terrible, and the call stack can have parents
        if stack['callFrames']:
            return unquote_plus(stack['callFrames'][0]['url'])
        if stack['parent']:
            return self._find_initiator_in_stack(stack['parent'])
        return None


class HostNode(HarTreeNode):

    def __init__(self, **kwargs):
        super(HostNode, self).__init__(**kwargs)
        # Do not add the URLs in the json dump
        self.features_to_skip.add('urls')

        self.add_feature('urls', [])
        self.add_feature('request_cookie', 0)
        self.add_feature('response_cookie', 0)
        self.add_feature('js', 0)
        self.add_feature('redirect', 0)
        self.add_feature('redirect_to_nothing', 0)
        self.add_feature('image', 0)
        self.add_feature('css', 0)
        self.add_feature('json', 0)
        self.add_feature('html', 0)
        self.add_feature('font', 0)
        self.add_feature('octet_stream', 0)
        self.add_feature('text', 0)
        self.add_feature('video', 0)
        self.add_feature('livestream', 0)
        self.add_feature('unset_mimetype', 0)
        self.add_feature('unknown_mimetype', 0)
        self.add_feature('iframe', 0)
        self.add_feature('http_content', False)
        self.add_feature('https_content', False)
        self.add_feature('mixed_content', False)

    def to_dict(self) -> dict:
        to_return = super(HostNode, self).to_dict()
        to_return['urls_count'] = len(self.urls)
        if self.http_content and self.https_content:
            self.mixed_content = True
        return to_return

    def add_url(self, url: URLNode):
        if not self.name:
            # Only used when initializing the root node
            self.add_feature('name', url.hostname)
        self.urls.append(url)
        if hasattr(url, 'request_cookie'):
            self.request_cookie += len(url.request_cookie)
        if hasattr(url, 'response_cookie'):
            self.response_cookie += len(url.response_cookie)
        if hasattr(url, 'js'):
            self.js += 1
        if hasattr(url, 'redirect'):
            self.redirect += 1
        if hasattr(url, 'redirect_to_nothing'):
            self.redirect_to_nothing += 1
        if hasattr(url, 'image'):
            self.image += 1
        if hasattr(url, 'css'):
            self.css += 1
        if hasattr(url, 'json'):
            self.json += 1
        if hasattr(url, 'html'):
            self.html += 1
        if hasattr(url, 'font'):
            self.font += 1
        if hasattr(url, 'octet_stream'):
            self.octet_stream += 1
        if hasattr(url, 'text'):
            self.text += 1
        if hasattr(url, 'video') or hasattr(url, 'livestream'):
            self.video += 1
        if hasattr(url, 'unknown_mimetype') or hasattr(url, 'unset_mimetype'):
            self.unknown_mimetype += 1
        if hasattr(url, 'iframe'):
            self.iframe += 1

        if url.name.startswith('http://'):
            self.http_content = True
        elif url.name.startswith('https://'):
            self.https_content = True


class HarFile():

    def __init__(self, harfile: Union[str, Path], capture_uuid: Optional[str]=None):
        logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.logger = Har2TreeLogAdapter(logger, {'uuid': capture_uuid})
        if isinstance(harfile, str):
            self.path: Path = Path(harfile)
        else:
            self.path = harfile

        with self.path.open() as f:
            self.har: dict = json.load(f)

        # I mean it, that's the last URL the splash browser was on
        last_redirect_file = self.path.parent / f'{self.path.stem}.last_redirect.txt'
        if last_redirect_file.is_file():
            with last_redirect_file.open('r') as _lr:
                self.final_redirect: str = unquote_plus(_lr.read())
            self._search_final_redirect()
        else:
            self.final_redirect = ''

        cookiefile = self.path.parent / f'{self.path.stem}.cookies.json'
        if cookiefile.is_file():
            with cookiefile.open() as c:
                self.cookies: List[dict] = json.load(c)
        else:
            self.cookies = []

        htmlfile = self.path.parent / f'{self.path.stem}.html'
        if htmlfile.is_file():
            with htmlfile.open('rb') as _h:
                self.html_content: BytesIO = BytesIO(_h.read())
        else:
            self.html_content = BytesIO()

        # Sorting the entries by start time (it isn't the case by default)
        # Reason: A specific URL cannot be loaded by something that hasn't been already started
        self.entries.sort(key=itemgetter('startedDateTime'))

        # Used to find the root entry of a page in the capture
        self.pages_start_times = {page['startedDateTime']: page for page in self.har['log']['pages']}
        # The first entry has a different start time as the one from the list, add that
        if self.entries:
            self.pages_start_times[self.initial_start_time] = self.har['log']['pages'][0]

        # Set to false if initial_redirects fails to find the chain.
        self.need_tree_redirects = False

    def _search_final_redirect(self):
        for e in self.entries:
            unquoted_url = unquote_plus(e['request']['url'])
            if unquoted_url == self.final_redirect:
                break
            elif unquoted_url.startswith(f'{self.final_redirect}?'):
                # WARNING: the URL in that file may not be present in the HAR: the query part is stripped by splash
                self.final_redirect = unquoted_url
                break
        else:
            # Update 2020-04-01: .. but the fragment is not striped so self.final_redirect may not be found
            # Unless we find the entry in the har, we need to search again without the fragment
            if '#' in self.final_redirect:
                self.final_redirect = self.final_redirect.split('#', 1)[0]
                self._search_final_redirect()
            elif '?' in self.final_redirect:
                # At this point, we're trying things. The final URL returned by splash may have been changed
                # in JavaScript and never appear in the HAR. Let's try to find the closest one with the same path
                self.final_redirect = self.final_redirect.split('?', 1)[0]
                self._search_final_redirect()
            else:
                self.logger.warning(f'Unable to find the final redirect: {self.final_redirect}')

    @property
    def initial_title(self) -> str:
        if self.har['log']['pages'][0]['title']:
            return self.har['log']['pages'][0]['title']
        else:
            return '!! No title found !!'

    @property
    def initial_start_time(self) -> str:
        if self.entries:
            return self.entries[0]['startedDateTime']
        return '-'

    @property
    def first_url(self) -> str:
        if self.entries:
            return self.entries[0]['request']['url']
        return '-'

    @property
    def entries(self) -> List[dict]:
        return self.har['log']['entries']

    @property
    def root_url(self) -> str:
        return self.entries[0]['request']['url']

    def __find_referer(self, har_entry: Dict) -> Optional[str]:
        for header_entry in har_entry['request']['headers']:
            if header_entry['name'] == 'Referer':
                return header_entry['value']
        return None

    @property
    def has_initial_redirects(self) -> bool:
        if self.final_redirect:
            return self.entries[0]['request']['url'] != self.final_redirect
        return False

    @property
    def initial_redirects(self) -> List[str]:
        '''All the initial redirects from the URL given by the user'''
        to_return = []
        if self.has_initial_redirects:
            # First request different of self.final_redirect, there is at least one redirect
            previous_entry = self.entries[0]
            for e in self.entries[1:]:
                # Lightweight way to hopefully skip the other URLs loaded in parallel with the redirect
                if (previous_entry['response']['redirectURL']):
                    # <insert flip a table GIF>, yes, rebuilding a redirectURL is *fun*
                    full_redirect = rebuild_url(previous_entry['response']['url'],
                                                previous_entry['response']['redirectURL'], [e['request']['url']])
                    if full_redirect == e['request']['url']:
                        to_return.append(e['request']['url'])
                        previous_entry = e
                    else:
                        continue
                elif (self.__find_referer(e) and (self.__find_referer(e) == previous_entry['response']['url'])):
                    to_return.append(e['request']['url'])
                    previous_entry = e
                else:
                    continue

                if e['request']['url'] == self.final_redirect:
                    break
            else:
                # Unable to find redirects chain, needs the whole tree
                to_return = []
                to_return.append(self.final_redirect)
                self.need_tree_redirects = True

        return to_return

    @property
    def root_referrer(self) -> Optional[str]:
        '''Useful when there are multiple tree to attach together'''
        first_entry = self.entries[0]
        for h in first_entry['request']['headers']:
            if h['name'] == 'Referer':
                return h['value']
        return None


class Har2Tree(object):

    def __init__(self, har_path: Union[str, Path], capture_uuid: Optional[str]=None):
        """Build the ETE Toolkit tree based on the HAR file, cookies, and HTML content
        :param har: harfile of a capture
        """
        logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.logger = Har2TreeLogAdapter(logger, {'uuid': capture_uuid})
        self.har = HarFile(har_path, capture_uuid)
        self.hostname_tree = HostNode()
        if not self.har.entries:
            self.has_entries = False
            return
        else:
            self.has_entries = True

        self.nodes_list: List[URLNode] = []
        self.all_url_requests: Dict[str, List[URLNode]] = {unquote_plus(url_entry['request']['url']): [] for url_entry in self.har.entries}

        # Format: pageref: node UUID
        self.pages_root: Dict[str, str] = {}

        self.all_redirects: List[str] = []
        self.all_referer: Dict[str, List[str]] = defaultdict(list)
        self.all_initiator_url: Dict[str, List[str]] = defaultdict(list)
        self._load_url_entries()

        # Generate cookies lookup tables
        # All the initial cookies sent with the initial request given to splash
        self.initial_cookies: Dict[str, dict] = {}
        if hasattr(self.nodes_list[0], 'cookies_sent'):
            self.initial_cookies = {key: cookie for key, cookie in self.nodes_list[0].cookies_sent.items()}

        # Dictionary of all cookies received during the capture
        self.cookies_received: Dict[str, List[Tuple[str, URLNode, bool]]] = defaultdict(list)
        for n in self.nodes_list:
            if hasattr(n, 'cookies_received'):
                for domain, c_received, is_3rd_party in n.cookies_received:
                    self.cookies_received[c_received].append((domain, n, is_3rd_party))

        # NOTE: locally_created contains all cookies not present in a response, and not passed at the begining of the capture to splash
        self.locally_created: Dict[str, dict] = {}
        for c in self.har.cookies:
            c_identifier = f'{c["name"]}={c["value"]}'
            if (c_identifier not in self.cookies_received
                    and c_identifier not in self.initial_cookies):
                self.locally_created[f'{c["name"]}={c["value"]}'] = c

        # if self.locally_created:
        #    for l in self.locally_created.values():
        #        print(json.dumps(l, indent=2))

        # NOTE: locally_created_not_sent only contains cookies that are created locally, and never sent during the capture
        self.locally_created_not_sent: Dict[str, dict] = self.locally_created.copy()
        # Cross reference the source of the cookie
        for n in self.nodes_list:
            if hasattr(n, 'cookies_sent'):
                for c_sent in n.cookies_sent:
                    # Remove cookie from list if sent during the capture.
                    self.locally_created_not_sent.pop(c_sent, None)
                    for domain, setter_node, is_3rd_party in self.cookies_received[c_sent]:
                        if n.hostname.endswith(domain):
                            # This cookie could have been set by this URL
                            # FIXME: append a lightweight URL node as dict
                            n.cookies_sent[c_sent].append({'hostname': setter_node.hostname,
                                                           'uuid': setter_node.uuid,
                                                           'name': setter_node.name,
                                                           '3rd_party': is_3rd_party})
        if self.locally_created_not_sent:
            self.logger.info(f'Cookies locally created & never sent {json.dumps(self.locally_created_not_sent, indent=2)}')

        # if self.locally_created_not_sent:
        #    for c in locally_created.values():
        #        print('##', json.dumps(c, indent=2))

        # Add context if urls are found in external_ressources
        for n in self.nodes_list:
            if hasattr(n, 'external_ressources'):
                for type_ressource, urls in n.external_ressources.items():
                    for url in urls:
                        if url not in self.all_url_requests:
                            continue
                        for node in self.all_url_requests[url]:
                            if type_ressource == 'img':
                                node.add_feature('image', True)

                            if type_ressource == 'script':
                                node.add_feature('js', True)

                            if type_ressource == 'video':
                                node.add_feature('video', True)

                            if type_ressource == 'audio':
                                node.add_feature('audio', True)

                            if type_ressource == 'iframe':
                                node.add_feature('iframe', True)

                            if type_ressource == 'embed':  # FIXME other icon?
                                node.add_feature('octet_stream', True)

                            if type_ressource == 'source':  # FIXME: Can be audio, video, or picture
                                node.add_feature('octet_stream', True)

                            if type_ressource == 'link':  # FIXME: Probably a css?
                                node.add_feature('css', True)

                            if type_ressource == 'object':  # FIXME: Same as embed, but more things
                                node.add_feature('octet_stream', True)

        self.url_tree = self.nodes_list.pop(0)

    @property
    def root_referer(self) -> Optional[str]:
        return self.har.root_referrer

    @property
    def user_agent(self) -> str:
        return self.url_tree.user_agent

    @property
    def start_time(self) -> datetime:
        return self.url_tree.start_time

    def _load_url_entries(self):
        '''Initialize the list of nodes to attach to the tree (as URLNode),
        and create a list of note for each URL we have in the HAR document'''

        for url_entry in self.har.entries:
            n = URLNode(name=unquote_plus(url_entry['request']['url']))
            n.load_har_entry(url_entry, list(self.all_url_requests.keys()))
            if hasattr(n, 'redirect_url'):
                self.all_redirects.append(n.redirect_url)

            if hasattr(n, 'initiator_url'):
                # The HAR file was created by chrome/chromium and we got the _initiator key
                self.all_initiator_url[n.initiator_url].append(n.name)

            if (url_entry['startedDateTime'] in self.har.pages_start_times
                    and self.har.pages_start_times[url_entry['startedDateTime']]['id'] == n.pageref):
                # This node is the root entry of a page. Can be used as a fallback when we build the tree
                self.pages_root[n.pageref] = n.uuid

            if hasattr(n, 'referer'):
                if n.referer == n.name:
                    # Skip to avoid loops:
                    #   * referer to itself
                    self.logger.warning(f'Referer to itself {n.name}')
                    # continue
                else:
                    self.all_referer[n.referer].append(n.name)

            self.nodes_list.append(n)
            self.all_url_requests[n.name].append(n)

    def get_host_node_by_uuid(self, uuid: str) -> HostNode:
        return self.hostname_tree.search_nodes(uuid=uuid)[0]

    def get_url_node_by_uuid(self, uuid: str) -> URLNode:
        return self.url_tree.search_nodes(uuid=uuid)[0]

    @property
    def root_after_redirect(self) -> Optional[str]:
        '''Iterate through the list of entries until there are no redirectURL in
        the response anymore: it is the first URL loading content.
        '''
        if self.har.has_initial_redirects:
            return self.har.final_redirect
        return None

    def to_json(self):
        return self.hostname_tree.to_json()

    def make_hostname_tree(self, root_nodes_url: Union[URLNode, List[URLNode]], root_node_hostname: HostNode):
        """ Groups all the URLs by domain in the hostname tree.
        `root_node_url` can be a list of nodes called by the same `root_node_hostname`
        """
        if not isinstance(root_nodes_url, list):
            root_nodes_url = [root_nodes_url]
        for root_node_url in root_nodes_url:
            children_hostnames: Dict[str, HostNode] = {}
            sub_roots: Dict[HostNode, List[URLNode]] = defaultdict(list)
            for child_node_url in root_node_url.get_children():
                if child_node_url.hostname is None:
                    self.logger.warning(f'Fucked up URL: {child_node_url}')
                    continue
                if child_node_url.hostname in children_hostnames:
                    child_node_hostname = children_hostnames[child_node_url.hostname]
                else:
                    child_node_hostname = root_node_hostname.add_child(HostNode(name=child_node_url.hostname))
                    children_hostnames[child_node_url.hostname] = child_node_hostname
                child_node_hostname.add_url(child_node_url)

                if not child_node_url.is_leaf():
                    sub_roots[child_node_hostname].append(child_node_url)
            for child_node_hostname, child_nodes_url in sub_roots.items():
                self.make_hostname_tree(child_nodes_url, child_node_hostname)

    def make_tree(self) -> URLNode:
        self._make_subtree(self.url_tree)
        if self.nodes_list:
            # We were not able to attach a few things.
            while self.nodes_list:
                node = self.nodes_list.pop(0)
                if self.pages_root[node.pageref] != node.uuid:
                    # This node is not a page root, we can attach it \o/
                    page_root_node = self.get_url_node_by_uuid(self.pages_root[node.pageref])
                    self._make_subtree(page_root_node, [node])
                else:
                    # No luck, let's attach it to the prior page in the list
                    page_before = self.har.har['log']['pages'][0]
                    for page in self.har.har['log']['pages'][1:]:
                        if page['id'] == node.pageref:
                            break
                        page_before = page
                    page_root_node = self.get_url_node_by_uuid(self.pages_root[page_before['id']])
                    self._make_subtree(page_root_node, [node])

        # Initialize the hostname tree root
        self.hostname_tree.add_url(self.url_tree)
        self.make_hostname_tree(self.url_tree, self.hostname_tree)
        return self.url_tree

    def _make_subtree(self, root: URLNode, nodes_to_attach: List[URLNode]=None):
        matching_urls: List[URLNode]
        if nodes_to_attach is None:
            # We're in the actual root node
            unodes = [self.url_tree]
        else:
            unodes = []
            for unode in nodes_to_attach:
                unodes.append(root.add_child(unode))
        for unode in unodes:
            if hasattr(unode, 'redirect') and not hasattr(unode, 'redirect_to_nothing'):
                # If the subnode has a redirect URL set, we get all the requests matching this URL
                # One may think the entry related to this redirect URL has a referer to the parent. One would be wrong.
                # URL 1 has a referer, and redirects to URL 2. URL 2 has the same referer as URL 1.
                if unode.redirect_url not in self.all_redirects:
                    continue
                self.all_redirects.remove(unode.redirect_url)  # Makes sure we only follow a redirect once
                matching_urls = [url_node for url_node in self.all_url_requests[unode.redirect_url] if url_node in self.nodes_list]
                self.nodes_list = [node for node in self.nodes_list if node not in matching_urls]
                self._make_subtree(unode, matching_urls)
            else:
                if self.all_initiator_url.get(unode.name):
                    # The URL (unode.name) is in the list of known urls initiating calls
                    for u in self.all_initiator_url[unode.name]:
                        matching_urls = [url_node for url_node in self.all_url_requests[u]
                                         if url_node in self.nodes_list and hasattr(url_node, 'initiator_url') and url_node.initiator_url == unode.name]
                        self.nodes_list = [node for node in self.nodes_list if node not in matching_urls]
                        self._make_subtree(unode, matching_urls)
                    if not self.all_initiator_url.get(unode.name):
                        # remove the initiator url from the list if empty
                        self.all_initiator_url.pop(unode.name)
                if self.all_referer.get(unode.name):
                    # The URL (unode.name) is in the list of known referers
                    for u in self.all_referer[unode.name]:
                        matching_urls = [url_node for url_node in self.all_url_requests[u]
                                         if url_node in self.nodes_list and hasattr(url_node, 'referer') and url_node.referer == unode.name]
                        self.nodes_list = [node for node in self.nodes_list if node not in matching_urls]
                        self._make_subtree(unode, matching_urls)
                    if not self.all_referer.get(unode.name):
                        # remove the referer from the list if empty
                        self.all_referer.pop(unode.name)
                if self.all_referer.get(unode.alternative_url_for_referer):
                    # The URL (unode.name) stripped at the first `#` is in the list of known referers
                    for u in self.all_referer[unode.alternative_url_for_referer]:
                        matching_urls = [url_node for url_node in self.all_url_requests[u]
                                         if url_node in self.nodes_list and hasattr(url_node, 'referer') and url_node.referer == unode.alternative_url_for_referer]
                        self.nodes_list = [node for node in self.nodes_list if node not in matching_urls]
                        self._make_subtree(unode, matching_urls)
                    # remove the referer from the list if empty
                    if not self.all_referer.get(unode.alternative_url_for_referer):
                        self.all_referer.pop(unode.alternative_url_for_referer)
                if hasattr(unode, 'external_ressources'):
                    # the url loads external things, and some of them have no referer....
                    for external_tag, links in unode.external_ressources.items():
                        for link in links:
                            if link not in self.all_url_requests:
                                # We have a lot of false positives
                                continue
                            matching_urls = [url_node for url_node in self.all_url_requests[link] if url_node in self.nodes_list]
                            self.nodes_list = [node for node in self.nodes_list if node not in matching_urls]
                            self._make_subtree(unode, matching_urls)


class CrawledTree(object):

    def __init__(self, harfiles: Union[List[str], List[Path]], uuid: Optional[str]=None):
        """ Convert a list of HAR files into a ETE Toolkit tree"""
        self.uuid = uuid
        logger = logging.getLogger(__name__)
        self.logger = Har2TreeLogAdapter(logger, {'uuid': uuid})
        self.hartrees: List[Har2Tree] = self.load_all_harfiles(harfiles)
        if not self.hartrees:
            raise Har2TreeError('No usable HAR files found.')
        self.root_hartree = self.hartrees.pop(0)
        self.find_parents()
        self.join_trees()

    def load_all_harfiles(self, files: Union[List[str], List[Path]]) -> List[Har2Tree]:
        """Open all the HAR files and build the trees"""
        loaded = []
        for har_path in files:
            har2tree = Har2Tree(har_path, capture_uuid=self.uuid)
            if not har2tree.has_entries:
                continue
            har2tree.make_tree()
            loaded.append(har2tree)
        return loaded

    def find_parents(self):
        """Find all the trees where the first entry has a referer.
        Meaning: This is a sub-tree to attach to some other node.
        """
        self.referers: Dict[str, List[Har2Tree]] = defaultdict(list)
        for hartree in self.hartrees:
            if hartree.root_referer:
                self.referers[hartree.root_referer].append(hartree)

    def join_trees(self, root: Optional[Har2Tree]=None, parent_root: Optional[URLNode]=None):
        """Connect the trees together if we have more than one HAR file"""
        if root is None:
            root = self.root_hartree
            parent = root.url_tree
        elif parent_root is not None:
            parent = parent_root
        if root.root_after_redirect:
            # If the first URL is redirected, the referer of the subtree
            # will be the redirect.
            sub_trees = self.referers.pop(root.root_after_redirect, None)
        else:
            sub_trees = self.referers.pop(root.har.root_url, None)
        if not sub_trees:
            # No subtree to attach
            return
        for sub_tree in sub_trees:
            to_attach = copy.deepcopy(sub_tree.url_tree)
            parent.add_child(to_attach)
            self.join_trees(sub_tree, to_attach)
        self.root_hartree.make_hostname_tree(self.root_hartree.url_tree, self.root_hartree.hostname_tree)

    def to_json(self):
        """JSON output for d3js"""
        return self.root_hartree.to_json()

    @property
    def redirects(self) -> List[str]:
        if not self.root_hartree.root_after_redirect:
            return []
        redirect_node = self.root_hartree.url_tree.search_nodes(name=self.root_hartree.root_after_redirect)
        if not redirect_node:
            self.logger.warning(f'Unable to find node {self.root_hartree.root_after_redirect}')
            return []
        elif len(redirect_node) > 1:
            self.logger.warning(f'Too many nodes found for {self.root_hartree.root_after_redirect}: {redirect_node}')
        return [a.name for a in reversed(redirect_node[0].get_ancestors())]

    @property
    def root_url(self) -> str:
        return self.root_hartree.har.root_url

    @property
    def start_time(self) -> datetime:
        return self.root_hartree.start_time

    @property
    def user_agent(self) -> str:
        return self.root_hartree.user_agent


def harnode_json_default(obj: HarTreeNode) -> dict:
    if isinstance(obj, HarTreeNode):
        return obj.to_dict()
