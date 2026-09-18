from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse


_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ARTICLE_RE = re.compile(
    r"<(?:article|div)\b[^>]*class=[\"'][^\"']*\bresult\b[^\"']*[\"'][^>]*>.*?</(?:article|div)>",
    re.IGNORECASE | re.DOTALL,
)
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_SEARXNG_UI_TEXT_RE = re.compile(
    r"(my searxng|about preferences|preferences\s+search syntax|default language|clear\s+search\s+general)",
    re.IGNORECASE,
)
_SEARXNG_TIME_RANGES = {"", "day", "month", "year"}


def clean_searxng_csv(value: Any, *, default: str = "", limit: int = 20) -> str:
    raw_items = str(value or default or "").replace("\n", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        clean = " ".join(str(item or "").strip().split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:80])
        if len(out) >= limit:
            break
    return ",".join(out)


def clean_searxng_language(value: Any) -> str:
    clean = str(value or "all").strip()
    return (clean or "all")[:32]


def clean_searxng_safesearch(value: Any) -> int:
    try:
        return max(0, min(2, int(value if value is not None else 0)))
    except Exception:
        return 0


def clean_searxng_time_range(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in _SEARXNG_TIME_RANGES else ""


def searxng_search_params(
    *,
    categories: str = "general",
    engines: str = "",
    language: str = "all",
    safesearch: int = 0,
    time_range: str = "",
    pageno: int = 1,
) -> dict[str, str]:
    params: dict[str, str] = {}
    clean_categories = clean_searxng_csv(categories, default="general")
    clean_engines = clean_searxng_csv(engines, default="")
    clean_language = clean_searxng_language(language)
    clean_time_range = clean_searxng_time_range(time_range)
    if clean_categories:
        params["categories"] = clean_categories
    if clean_engines:
        params["engines"] = clean_engines
    if clean_language:
        params["language"] = clean_language
    params["safesearch"] = str(clean_searxng_safesearch(safesearch))
    if clean_time_range:
        params["time_range"] = clean_time_range
    try:
        clean_pageno = max(1, min(100, int(pageno or 1)))
    except Exception:
        clean_pageno = 1
    params["pageno"] = str(clean_pageno)
    return params


def clean_searxng_pageno(value: Any) -> int:
    try:
        return max(1, min(100, int(value or 1)))
    except Exception:
        return 1


def normalize_wiki_api_url(raw_url: str) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/w/api.php"
    elif path.endswith("/api.php"):
        path = path
    elif "/wiki/" in path:
        prefix = path.split("/wiki/", 1)[0].rstrip("/")
        path = f"{prefix}/w/api.php" if prefix else "/w/api.php"
    elif path.endswith("/wiki"):
        prefix = path[: -len("/wiki")].rstrip("/")
        path = f"{prefix}/w/api.php" if prefix else "/w/api.php"
    else:
        path = f"{path}/w/api.php"
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def wiki_page_url(api_url: str, title: str) -> str:
    parsed = urlparse(normalize_wiki_api_url(api_url))
    path = parsed.path
    if path.endswith("/w/api.php"):
        root = path[: -len("/w/api.php")]
    elif path.endswith("/api.php"):
        root = path[: -len("/api.php")]
    else:
        root = ""
    page_path = f"{root.rstrip('/')}/wiki/{quote(str(title or '').strip().replace(' ', '_'))}"
    return urlunparse(parsed._replace(path=page_path, query="", fragment=""))


def strip_html(value: str) -> str:
    text_value = _SCRIPT_STYLE_RE.sub(" ", str(value or ""))
    text_value = re.sub(r"<br\s*/?>", "\n", text_value, flags=re.IGNORECASE)
    text_value = _TAG_RE.sub(" ", text_value)
    text_value = html.unescape(text_value)
    text_value = re.sub(r"[ \t\r\f\v]+", " ", text_value)
    text_value = re.sub(r"\n\s+", "\n", text_value)
    return text_value.strip()


def collapse_text(value: str, *, limit: int = 12000) -> str:
    text_value = html.unescape(str(value or ""))
    text_value = re.sub(r"[ \t\r\f\v]+", " ", text_value)
    text_value = re.sub(r"\n{3,}", "\n\n", text_value)
    text_value = text_value.strip()
    return text_value[:limit]


def search_endpoint_from_base(search_base_url: str) -> str:
    raw = str(search_base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"q", "format"}]
    path = parsed.path.rstrip("/")
    if not path:
        path = "/search"
    elif not path.endswith("/search"):
        path = f"{path}/search"
    return urlunparse(parsed._replace(path=path, query=urlencode(params)))


def search_url_with_params(
    search_base_url: str,
    *,
    query: str,
    json_format: bool,
    extra_params: dict[str, str] | None = None,
) -> str:
    endpoint = search_endpoint_from_base(search_base_url)
    parsed = urlparse(endpoint)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"q", "format"}]
    existing_names = {k for k, _v in params}
    for key, value in (extra_params or {}).items():
        if key in {"q", "format"} or key in existing_names:
            continue
        params.append((key, value))
    params.append(("q", query))
    if json_format:
        params.append(("format", "json"))
    return urlunparse(parsed._replace(query=urlencode(params)))


def search_endpoint_has_param(search_base_url: str, name: str) -> bool:
    endpoint = search_endpoint_from_base(search_base_url)
    parsed = urlparse(endpoint)
    return any(k == name for k, _v in parse_qsl(parsed.query, keep_blank_values=True))


def normalize_result_url(raw_url: str, *, base_url: str) -> str:
    url = html.unescape(str(raw_url or "").strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base_url, url)


def is_search_engine_internal_url(url: str, *, search_url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    search_parsed = urlparse(search_endpoint_from_base(search_url))
    hostname = (parsed.hostname or "").lower()
    if hostname in {"searx.space", "www.searx.space"}:
        return True
    if not parsed.netloc or not search_parsed.netloc:
        return False
    if parsed.netloc.lower() != search_parsed.netloc.lower():
        return False
    path = parsed.path.rstrip("/").lower()
    search_path = search_parsed.path.rstrip("/").lower()
    search_root = search_path[: -len("/search")] if search_path.endswith("/search") else ""
    if path in {"", "/"}:
        return True
    if search_root and path == search_root:
        return True
    return (
        path == search_path
        or path.startswith(f"{search_root}/info")
        or path.startswith(f"{search_root}/preferences")
        or path.startswith(f"{search_root}/stats")
        or path.startswith(f"{search_root}/config")
        or path.startswith(f"{search_root}/about")
    )


def is_fetchable_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    expected_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    if expected_port is None or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port is not None and port != expected_port:
        return False
    return True


def url_with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    for name, value in params.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((str(name), str(item)) for item in value)
        else:
            pairs.append((str(name), str(value)))
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


def parse_search_json(data: Any) -> list[dict[str, Any]]:
    raw_results = data.get("results") if isinstance(data, dict) else None
    if raw_results is None and isinstance(data, list):
        raw_results = data
    if not isinstance(raw_results, list):
        return []

    out: list[dict[str, Any]] = []
    for item in raw_results[:10]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or item.get("description") or "").strip()
        if not title and not snippet:
            continue
        if _SEARXNG_UI_TEXT_RE.search(" ".join([title, snippet])):
            continue
        out.append({"title": strip_html(title), "url": url, "snippet": strip_html(snippet)[:800]})
    return out


def filter_search_results(results: list[dict[str, Any]], *, search_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if not url or is_search_engine_internal_url(url, search_url=search_url):
            continue
        if _SEARXNG_UI_TEXT_RE.search(" ".join([title, snippet])):
            continue
        if title.lower() in {"about", "preferences", "search syntax"} and "searxng" in snippet.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": snippet[:800]})
        if len(out) >= 8:
            break
    return out


def parse_search_html(html_value: str, *, base_url: str) -> list[dict[str, Any]]:
    source = str(html_value or "")
    chunks = _ARTICLE_RE.findall(source)
    if not chunks:
        chunks = re.findall(r"<article\b[^>]*>.*?</article>", source, flags=re.IGNORECASE | re.DOTALL)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks[:20]:
        anchors = _ANCHOR_RE.findall(chunk)
        if not anchors:
            continue
        title = ""
        url = ""
        for href, label_html in anchors:
            candidate_url = normalize_result_url(href, base_url=base_url)
            if not candidate_url or is_search_engine_internal_url(candidate_url, search_url=base_url):
                continue
            parsed = urlparse(candidate_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            clean_label = strip_html(label_html)
            if not clean_label or clean_label.lower() in {"about", "preferences", "search syntax"}:
                continue
            title = clean_label[:300]
            url = candidate_url
            break
        if not title or not url or url in seen:
            continue
        seen.add(url)
        text_value = strip_html(chunk)
        snippet = text_value.replace(title, "", 1).strip()
        out.append({"title": title, "url": url, "snippet": snippet[:800]})
        if len(out) >= 8:
            break
    return out


def extract_page_text(html_value: str) -> str:
    source = _SCRIPT_STYLE_RE.sub(" ", str(html_value or ""))
    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", source, flags=re.IGNORECASE | re.DOTALL)
    title = strip_html(title_match.group(1)) if title_match else ""
    main_match = re.search(r"<main\b[^>]*>(.*?)</main>", source, flags=re.IGNORECASE | re.DOTALL)
    article_match = re.search(r"<article\b[^>]*>(.*?)</article>", source, flags=re.IGNORECASE | re.DOTALL)
    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", source, flags=re.IGNORECASE | re.DOTALL)
    body = article_match or main_match or body_match
    body_text = strip_html(body.group(1) if body else source)
    joined = "\n\n".join([x for x in [title, body_text] if x])
    return collapse_text(joined, limit=12000)
