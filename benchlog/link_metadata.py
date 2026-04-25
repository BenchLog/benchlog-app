"""Server-side OG / oEmbed metadata fetcher.

Architecture:
- `address_allowed(host, *, allow_private)` is the SSRF gate. Cloud
  metadata IPs are blocked unconditionally; loopback / RFC1918 / link-
  local / ULA are gated by the config flag. Public addresses pass.
- `fetch_metadata(url, *, allow_private)` is the public entry point. It
  resolves, validates, fetches, parses, and returns a dict with the OG
  fields (any may be None) plus an optional `warning`.
- YouTube URLs short-circuit to the oEmbed endpoint.

The fetcher is kept dependency-light (httpx + selectolax) and stateless
so it can be reused from a future background-refresh task without
revisiting the safety rails.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser


_FETCH_TIMEOUT = httpx.Timeout(3.0)
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_REDIRECTS = 3
# `Mozilla/5.0 (compatible; …)` is the long-standing convention for
# self-identifying bots that want to be recognised as legitimate
# scrapers rather than browser-impersonators (Googlebot, Bingbot, most
# link-preview services use this form). Identifies us as BenchLog while
# avoiding the UA-only gates many sites apply to generic Python UAs.
_USER_AGENT = "Mozilla/5.0 (compatible; BenchLog/1.0; link-preview)"


class InvalidUrl(ValueError):
    """The supplied URL or host is malformed."""


class AddressBlocked(ValueError):
    """The resolved address is on the denylist."""


# Cloud-metadata endpoints — credential-leak primitives. Blocked
# unconditionally regardless of `allow_private`. Add new entries here as
# providers expose more.
_CLOUD_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata",  # short Azure form on Linux WSL
    }
)
_CLOUD_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),  # AWS, Azure
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv2 IPv6
    }
)


def _resolve_host(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve a host to a list of IP addresses. Returns the parsed
    address itself if `host` is already an IP literal."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise InvalidUrl(f"DNS resolution failed for {host}") from e
    seen: list[ipaddress._BaseAddress] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (IndexError, ValueError):
            continue
        if ip not in seen:
            seen.append(ip)
    if not seen:
        raise InvalidUrl(f"No addresses resolved for {host}")
    return seen


def _is_cloud_metadata(host: str, ips: list[ipaddress._BaseAddress]) -> bool:
    if host.lower() in _CLOUD_METADATA_HOSTS:
        return True
    return any(ip in _CLOUD_METADATA_IPS for ip in ips)


def _is_private(ip: ipaddress._BaseAddress) -> bool:
    """`ipaddress.is_private` covers loopback + RFC1918 + ULA + link-
    local for both v4 and v6. We treat them all as 'private' for the
    address gate."""
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
    )


def _meta(
    tree: HTMLParser, *, prop: str | None = None, name: str | None = None
) -> str | None:
    if prop is not None:
        node = tree.css_first(f'meta[property="{prop}"]')
        if node is not None:
            value = node.attributes.get("content")
            if value:
                return value.strip()
    if name is not None:
        node = tree.css_first(f'meta[name="{name}"]')
        if node is not None:
            value = node.attributes.get("content")
            if value:
                return value.strip()
    return None


def _resolve(base_url: str, candidate: str | None) -> str | None:
    if not candidate:
        return None
    return urljoin(base_url, candidate.strip())


def parse_head(html: str, *, base_url: str) -> dict:
    """Pull OG / Twitter / fallback metadata from the document `<head>`.

    Returns a dict with keys: title, description, image_url, site_name,
    favicon_url. Any value may be None; site_name and favicon_url have
    last-resort fallbacks derived from `base_url`.
    """
    tree = HTMLParser(html or "")
    title = (
        _meta(tree, prop="og:title")
        or _meta(tree, name="twitter:title")
    )
    if title is None:
        node = tree.css_first("title")
        if node is not None and node.text():
            title = node.text().strip()

    description = (
        _meta(tree, prop="og:description")
        or _meta(tree, name="twitter:description")
        or _meta(tree, name="description")
    )

    image = (
        _meta(tree, prop="og:image")
        or _meta(tree, name="twitter:image")
    )
    image_url = _resolve(base_url, image)

    site_name = _meta(tree, prop="og:site_name")
    if not site_name:
        host = urlparse(base_url).netloc
        site_name = host.removeprefix("www.") if host else None

    favicon = None
    for selector in ('link[rel="icon"]', 'link[rel="shortcut icon"]'):
        node = tree.css_first(selector)
        if node is not None:
            href = node.attributes.get("href")
            if href:
                favicon = _resolve(base_url, href)
                break
    if not favicon:
        favicon = _resolve(base_url, "/favicon.ico")

    return {
        "title": title,
        "description": description,
        "image_url": image_url,
        "site_name": site_name,
        "favicon_url": favicon,
    }


def address_allowed(host: str, *, allow_private: bool) -> None:
    """Raise `AddressBlocked` / `InvalidUrl` if `host` should be refused.

    Returns None on success — caller proceeds with the fetch.
    """
    if not host:
        raise InvalidUrl("empty host")
    # Hostname-only check first — `metadata.google.internal` may not
    # resolve outside its cloud, but it's still an unconditional block.
    if host.lower() in _CLOUD_METADATA_HOSTS:
        raise AddressBlocked(f"cloud-metadata host blocked: {host}")
    ips = _resolve_host(host)
    if any(ip in _CLOUD_METADATA_IPS for ip in ips):
        raise AddressBlocked(f"cloud-metadata IP blocked: {host}")
    if not allow_private:
        for ip in ips:
            if _is_private(ip):
                raise AddressBlocked(f"private address blocked: {ip}")


# ---------------------------------------------------------------------------
# Provider shortcuts — sites that block generic scrapes (anti-bot gates,
# JavaScript-only renders, etc.) but expose a clean structured endpoint.
#
# Each provider is a `Provider` tuple:
#   - `name`  : short identifier, used only in logs / debugging
#   - `match` : sync `(url) -> bool` predicate
#   - `fetch` : async `(url, *, transport) -> dict` returning the standard
#               metadata dict (see `_metadata_dict` for the shape).
#
# The first matching provider wins. To add a new one:
#   1. Write `_is_<provider>(url)` predicate.
#   2. Write `_fetch_<provider>(url, *, transport)` returning a
#      metadata dict via `_metadata_dict(...)`.
#   3. Append a `Provider(...)` to `_PROVIDERS`.
# Generic OG/Twitter scraping is the fallback when no provider matches.
# ---------------------------------------------------------------------------


from typing import Awaitable, Callable, NamedTuple


class Provider(NamedTuple):
    name: str
    match: Callable[[str], bool]
    fetch: Callable[..., Awaitable[dict]]


def _metadata_dict(
    *,
    title: str | None = None,
    description: str | None = None,
    image_url: str | None = None,
    site_name: str | None = None,
    favicon_url: str | None = None,
    warning: str | None = None,
) -> dict:
    """Single source of truth for the metadata-dict shape. All
    `fetch_metadata` paths produce dicts with exactly these keys."""
    return {
        "title": title,
        "description": description,
        "image_url": image_url,
        "site_name": site_name,
        "favicon_url": favicon_url,
        "warning": warning,
    }


# ---------- YouTube (oEmbed) ---------- #


def _is_youtube(url: str) -> bool:
    host = urlparse(url).hostname or ""
    host = host.lower().removeprefix("www.")
    return host in {"youtube.com", "youtu.be", "m.youtube.com"}


async def _fetch_youtube_oembed(
    url: str, *, transport: httpx.BaseTransport | None
) -> dict:
    """oEmbed gives clean title + author + thumbnail without scraping."""
    favicon = "https://www.youtube.com/favicon.ico"
    oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
    async with httpx.AsyncClient(
        timeout=_FETCH_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
        transport=transport,
    ) as client:
        resp = await client.get(oembed_url)
    if resp.status_code != 200:
        return _metadata_dict(
            site_name="YouTube",
            favicon_url=favicon,
            warning=f"YouTube oEmbed returned {resp.status_code}",
        )
    payload = resp.json()
    return _metadata_dict(
        title=payload.get("title"),
        image_url=payload.get("thumbnail_url"),
        site_name=payload.get("provider_name") or "YouTube",
        favicon_url=favicon,
    )


# ---------- Reddit (`.json` endpoint) ---------- #


def _is_reddit(url: str) -> bool:
    host = urlparse(url).hostname or ""
    host = host.lower()
    # Strip the most-common subdomains; everything else (npd, en, etc.)
    # falls through to whatever Reddit serves.
    for prefix in ("www.", "old.", "m.", "np.", "new."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host in {"reddit.com", "redd.it"}


async def _fetch_reddit_json(
    url: str, *, transport: httpx.BaseTransport | None
) -> dict:
    """Reddit's HTML page is heavily JS-rendered (the post body lives
    in a shreddit web component) and the OG tags it serves are a
    truncated subset of the actual post data. The public `.json`
    endpoint — just append `.json` to any post / subreddit / user URL —
    returns the full structured data, including:
      - clean post `selftext` instead of the truncated og:description
      - high-res `preview.images[0].source.url` instead of the smaller
        og:image
      - `r/<subreddit>` as a site name instead of plain "Reddit"
      - schema that has been stable for years across Reddit's recurring
        frontend rewrites

    The HTML URL the user saved is preserved on the link row; only the
    metadata fetch goes to the JSON variant.
    """
    parsed = urlparse(url)
    json_path = parsed.path.rstrip("/") + ".json"
    json_url = parsed._replace(path=json_path, fragment="").geturl()

    favicon = "https://www.redditstatic.com/desktop2x/img/favicon/favicon-32x32.png"
    fallback = _metadata_dict(site_name="Reddit", favicon_url=favicon)

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
            },
            transport=transport,
        ) as client:
            resp = await client.get(json_url)
    except httpx.HTTPError as e:
        return {**fallback, "warning": f"Reddit fetch failed: {e.__class__.__name__}"}

    if resp.status_code != 200:
        return {**fallback, "warning": f"Reddit returned {resp.status_code}"}

    try:
        data = resp.json()
    except (ValueError, TypeError):
        return {**fallback, "warning": "Reddit response was not JSON"}

    # Post pages return [post_listing, comments_listing]; subreddit /
    # user feeds return a single listing dict.
    listing = None
    if isinstance(data, list) and data:
        listing = data[0]
    elif isinstance(data, dict):
        listing = data
    children = ((listing or {}).get("data") or {}).get("children") or []
    post_data = (children[0].get("data") if children else None) or {}

    if not post_data:
        return {**fallback, "warning": "No post data in Reddit response"}

    title = post_data.get("title") or None
    selftext = post_data.get("selftext") or None
    if selftext and len(selftext) > 500:
        selftext = selftext[:497] + "…"

    # Prefer the high-res preview image when present; fall back to the
    # post thumbnail. Reddit URL-encodes ampersands as `&amp;` in the
    # JSON response — undo that so the URL actually fetches.
    image_url = None
    images = ((post_data.get("preview") or {}).get("images")) or []
    if images:
        source = (images[0] or {}).get("source") or {}
        raw = source.get("url")
        if raw:
            image_url = raw.replace("&amp;", "&")
    if not image_url:
        thumb = post_data.get("thumbnail")
        if thumb and isinstance(thumb, str) and thumb.startswith("http"):
            image_url = thumb

    site_name = post_data.get("subreddit_name_prefixed") or "Reddit"

    return _metadata_dict(
        title=title,
        description=selftext,
        image_url=image_url,
        site_name=site_name,
        favicon_url=favicon,
    )


_PROVIDERS: list[Provider] = [
    Provider("youtube", _is_youtube, _fetch_youtube_oembed),
    Provider("reddit", _is_reddit, _fetch_reddit_json),
]


async def fetch_metadata(
    url: str,
    *,
    allow_private: bool,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Public entry point. Returns the metadata dict (same shape as
    `parse_head`) plus a `warning` field that is non-null when something
    failed but the link is still saveable.

    `transport` is for tests — production callers omit it.
    """
    parsed = urlparse(url or "")
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return {
            "title": None,
            "description": None,
            "image_url": None,
            "site_name": None,
            "favicon_url": None,
            "warning": "Only http/https URLs can be previewed.",
        }
    host = parsed.hostname or ""
    try:
        address_allowed(host, allow_private=allow_private)
    except (AddressBlocked, InvalidUrl) as e:
        return {
            "title": None,
            "description": None,
            "image_url": None,
            "site_name": host.removeprefix("www.") if host else None,
            "favicon_url": None,
            "warning": str(e),
        }

    for provider in _PROVIDERS:
        if provider.match(url):
            return await provider.fetch(url, transport=transport)

    base_fallback = {
        "title": None,
        "description": None,
        "image_url": None,
        "site_name": host.removeprefix("www.") if host else None,
        "favicon_url": None,
    }

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            transport=transport,
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return {**base_fallback, "warning": f"Fetch failed: {e.__class__.__name__}"}

    if resp.status_code >= 400:
        return {**base_fallback, "warning": f"HTTP {resp.status_code}"}

    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        head_md = parse_head("", base_url=str(resp.url))
        return {**head_md, "warning": f"Non-HTML response ({content_type or 'unknown type'})"}

    body = resp.content
    if len(body) > _MAX_RESPONSE_BYTES:
        body = body[:_MAX_RESPONSE_BYTES]
    try:
        text = body.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, ValueError):
        text = body.decode("utf-8", errors="replace")
    head_md = parse_head(text, base_url=str(resp.url))
    return {**head_md, "warning": None}
