"""Unit tests for the OG/oEmbed metadata fetcher.

Address filter: cloud-metadata IPs always blocked; private/loopback
gated by the `allow_private` flag. URL parsing + parser tests do not
hit the network — HTML fixtures live inline.
"""
import pytest

from benchlog.link_metadata import (
    AddressBlocked,
    InvalidUrl,
    address_allowed,
)


# ---------- cloud-metadata: always blocked ---------- #


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",  # AWS, Azure
        "metadata.google.internal",  # GCP
        "fd00:ec2::254",  # AWS IMDSv2 IPv6
    ],
)
def test_cloud_metadata_always_blocked(host):
    with pytest.raises(AddressBlocked):
        address_allowed(host, allow_private=True)
    with pytest.raises(AddressBlocked):
        address_allowed(host, allow_private=False)


# ---------- private ranges: gated by flag ---------- #


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.20.5.5",
        "192.168.1.50",
        "169.254.0.1",  # link-local (not the meta IP)
        "fe80::1",  # link-local v6
        "fd12:3456:789a::1",  # ULA
        "::1",
    ],
)
def test_private_blocked_when_disallowed(host):
    with pytest.raises(AddressBlocked):
        address_allowed(host, allow_private=False)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.50",
    ],
)
def test_private_allowed_when_flag_on(host):
    # Doesn't raise.
    address_allowed(host, allow_private=True)


# ---------- public: always allowed ---------- #


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "github.com",
        "hackaday.com",
        "youtu.be",
    ],
)
def test_public_addresses_allowed(host):
    address_allowed(host, allow_private=False)
    address_allowed(host, allow_private=True)


# ---------- garbage input ---------- #


def test_empty_host_rejected():
    with pytest.raises(InvalidUrl):
        address_allowed("", allow_private=True)


# ---------- head parser ---------- #


from benchlog.link_metadata import parse_head  # noqa: E402


def test_parse_head_prefers_og_over_twitter_over_title():
    html = """
    <html><head>
      <title>From title</title>
      <meta property="og:title" content="From OG">
      <meta name="twitter:title" content="From Twitter">
      <meta property="og:description" content="OG desc">
      <meta name="twitter:description" content="Tw desc">
      <meta property="og:image" content="https://cdn.example/img.png">
      <meta property="og:site_name" content="Example">
    </head></html>
    """
    md = parse_head(html, base_url="https://example.com/page")
    assert md["title"] == "From OG"
    assert md["description"] == "OG desc"
    assert md["image_url"] == "https://cdn.example/img.png"
    assert md["site_name"] == "Example"


def test_parse_head_falls_back_to_twitter_when_og_missing():
    html = """
    <html><head>
      <meta name="twitter:title" content="Tw only">
      <meta name="twitter:description" content="Tw desc">
      <meta name="twitter:image" content="https://cdn.example/tw.png">
    </head></html>
    """
    md = parse_head(html, base_url="https://example.com/")
    assert md["title"] == "Tw only"
    assert md["description"] == "Tw desc"
    assert md["image_url"] == "https://cdn.example/tw.png"


def test_parse_head_falls_back_to_title_tag_and_meta_description():
    html = """
    <html><head>
      <title>Just a title</title>
      <meta name="description" content="A description">
    </head></html>
    """
    md = parse_head(html, base_url="https://example.com/")
    assert md["title"] == "Just a title"
    assert md["description"] == "A description"
    assert md["image_url"] is None


def test_parse_head_resolves_relative_og_image():
    html = """
    <html><head>
      <meta property="og:title" content="X">
      <meta property="og:image" content="/static/cover.jpg">
    </head></html>
    """
    md = parse_head(html, base_url="https://example.com/sub/page")
    assert md["image_url"] == "https://example.com/static/cover.jpg"


def test_parse_head_uses_link_rel_icon_for_favicon():
    html = """
    <html><head>
      <title>X</title>
      <link rel="icon" href="/favicon.png">
    </head></html>
    """
    md = parse_head(html, base_url="https://example.com/x")
    assert md["favicon_url"] == "https://example.com/favicon.png"


def test_parse_head_default_favicon_when_none_declared():
    html = "<html><head><title>X</title></head></html>"
    md = parse_head(html, base_url="https://example.com/x")
    assert md["favicon_url"] == "https://example.com/favicon.ico"


def test_parse_head_site_name_falls_back_to_hostname():
    html = "<html><head><title>X</title></head></html>"
    md = parse_head(html, base_url="https://www.example.com/path")
    # 'www.' stripped.
    assert md["site_name"] == "example.com"


def test_parse_head_returns_all_none_for_empty_input():
    md = parse_head("", base_url="https://example.com/")
    assert md["title"] is None
    assert md["description"] is None
    assert md["image_url"] is None
    # site_name + favicon still derived from base_url.
    assert md["site_name"] == "example.com"
    assert md["favicon_url"] == "https://example.com/favicon.ico"


# ---------- fetch_metadata ---------- #


import httpx  # noqa: E402

from benchlog.link_metadata import fetch_metadata  # noqa: E402


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_metadata_returns_og_fields_for_html_response():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                '<html><head>'
                '<meta property="og:title" content="A Title">'
                '<meta property="og:description" content="A desc">'
                '<meta property="og:image" content="https://cdn.example/img.png">'
                '<meta property="og:site_name" content="Example">'
                '</head></html>'
            ),
        )

    md = await fetch_metadata(
        "https://example.com/article",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert md["title"] == "A Title"
    assert md["image_url"] == "https://cdn.example/img.png"
    assert md["warning"] is None


@pytest.mark.asyncio
async def test_fetch_metadata_returns_warning_on_blocked_address():
    md = await fetch_metadata(
        "http://10.0.0.5/whatever",
        allow_private=False,
        transport=_mock_transport(lambda r: httpx.Response(200, text="should not fetch")),
    )
    assert md["title"] is None
    assert md["warning"] is not None
    assert "private" in md["warning"].lower() or "blocked" in md["warning"].lower()


@pytest.mark.asyncio
async def test_fetch_metadata_skips_non_html_content_type():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4 ...",
        )

    md = await fetch_metadata(
        "https://example.com/file.pdf",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert md["title"] is None
    # site_name + favicon still derived from base_url even when body skipped.
    assert md["site_name"] == "example.com"


@pytest.mark.asyncio
async def test_fetch_metadata_handles_4xx_with_warning():
    def handler(request):
        return httpx.Response(404, headers={"content-type": "text/html"}, text="nope")

    md = await fetch_metadata(
        "https://example.com/missing",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert md["title"] is None
    assert md["warning"] is not None


@pytest.mark.asyncio
async def test_fetch_metadata_rejects_non_http_schemes():
    md = await fetch_metadata(
        "mailto:alice@example.com",
        allow_private=False,
        transport=_mock_transport(lambda r: httpx.Response(200, text="x")),
    )
    assert md["title"] is None
    assert md["warning"] is not None


# ---------- Reddit special case ---------- #


from benchlog.link_metadata import _is_reddit  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/foo/comments/abc/title/",
        "https://reddit.com/r/foo/comments/abc/title/",
        "https://old.reddit.com/r/foo/comments/abc/title/",
        "https://m.reddit.com/r/foo",
        "https://np.reddit.com/r/foo",
        "https://redd.it/abc",
    ],
)
def test_is_reddit_recognises_common_subdomains(url):
    assert _is_reddit(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/reddit",
        "https://reddit.example.com/",
        "https://youtube.com/watch?v=abc",
    ],
)
def test_is_reddit_rejects_non_reddit(url):
    assert not _is_reddit(url)


@pytest.mark.asyncio
async def test_fetch_metadata_uses_reddit_json_endpoint():
    """Reddit URLs should hit `<url>.json` instead of the HTML page so
    we sidestep the anti-bot interstitial."""
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=[
                {
                    "kind": "Listing",
                    "data": {
                        "children": [
                            {
                                "kind": "t3",
                                "data": {
                                    "title": "Budget floor jack recommendations",
                                    "selftext": "Looking for cheap but solid options.",
                                    "subreddit_name_prefixed": "r/MechanicAdvice",
                                    "thumbnail": "https://b.thumbs.redditmedia.com/abc.jpg",
                                    "preview": {
                                        "images": [
                                            {
                                                "source": {
                                                    "url": "https://preview.redd.it/abc.jpg?width=640&amp;auto=webp"
                                                }
                                            }
                                        ]
                                    },
                                },
                            }
                        ]
                    },
                },
                {"kind": "Listing", "data": {"children": []}},
            ],
        )

    md = await fetch_metadata(
        "https://www.reddit.com/r/MechanicAdvice/comments/xyz/budget_jack/",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert captured["url"].endswith(".json")
    assert md["title"] == "Budget floor jack recommendations"
    assert md["description"] == "Looking for cheap but solid options."
    assert md["site_name"] == "r/MechanicAdvice"
    # The `&amp;` in the preview URL should be unescaped.
    assert "&amp;" not in (md["image_url"] or "")
    assert "auto=webp" in md["image_url"]
    assert md["warning"] is None


@pytest.mark.asyncio
async def test_fetch_metadata_reddit_handles_non_200():
    def handler(request):
        return httpx.Response(403, text="forbidden")

    md = await fetch_metadata(
        "https://www.reddit.com/r/foo/comments/abc/x/",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert md["title"] is None
    assert md["site_name"] == "Reddit"
    assert md["warning"] is not None


@pytest.mark.asyncio
async def test_fetch_metadata_reddit_truncates_long_selftext():
    long_text = "x" * 1000

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=[
                {
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "Long post",
                                    "selftext": long_text,
                                    "subreddit_name_prefixed": "r/foo",
                                }
                            }
                        ]
                    }
                },
                {"data": {"children": []}},
            ],
        )

    md = await fetch_metadata(
        "https://www.reddit.com/r/foo/comments/abc/title/",
        allow_private=False,
        transport=_mock_transport(handler),
    )
    assert len(md["description"]) <= 500
    assert md["description"].endswith("…")
