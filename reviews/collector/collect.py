#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WineBook 와인 리뷰 수집기 (GitHub Actions에서 실행).

하는 일
  1) data/sources.json 에 적힌 블로그의 RSS에서 새 글을 찾습니다.
  2) data/backfill.txt (와 POST_URLS)에 적힌 과거 글 주소를 대기열에 넣습니다.
  3) 글 본문을 가져와 Claude API로 와인 정보(점수·등급·산지·품종·양조 방법·좌표)를 정리합니다.
  4) data/wines.json, data/state.json, data/status.json 을 갱신합니다.

환경 변수
  ANTHROPIC_API_KEY   Claude API 키 (저장소 Secret)
  MODE                new(기본) | reprocess | check
  SOURCE              이 블로그 id만 처리 (비우면 전체)
  LIMIT               이번 실행에서 정리할 최대 글 수 (비우면 settings.max_posts_per_run)
  POST_URLS           (선택) 공백·쉼표로 구분한 글 주소
  TIME_BUDGET_MIN     새 글 정리를 시작할 수 있는 최대 시간(분, 기본 45)

외부 패키지 없이 파이썬 표준 라이브러리만 씁니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.environ.get("REVIEWS_DATA_DIR") or os.path.join(HERE, "..", "data"))
P_SOURCES = os.path.join(DATA, "sources.json")
P_WINES = os.path.join(DATA, "wines.json")
P_STATE = os.path.join(DATA, "state.json")
P_STATUS = os.path.join(DATA, "status.json")
P_BACKFILL = os.path.join(DATA, "backfill.txt")

KST = timezone(timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
UA_BOT = "WineBookReviews/1.0" + (f" (+https://github.com/{REPO})" if REPO else "")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1 " + UA_BOT)
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36 " + UA_BOT)

API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5"]
# 달러/100만 토큰 (입력, 출력) — 비용 추정용. 웹 검색은 1회 0.01달러.
PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50), "claude-opus-5-5": (4.0, 20.0)}
WEB_SEARCH_PRICE = 0.01
MAX_TRIES = 3
TEXT_LIMIT = 24000
POLITE_DELAY = float(os.environ.get("POLITE_DELAY", "1.5"))
TYPES = {"red", "white", "rose", "sparkling", "sweet", "fortified", "orange", "other"}


# ----------------------------------------------------------------------------- 기본 도구

def now_iso() -> str:
    return datetime.now(KST).replace(microsecond=0).isoformat()


def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise SystemExit(f"{os.path.basename(path)} 파일을 읽을 수 없습니다(JSON 오류): {e}")


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def prune(d):
    """None·빈 문자열·빈 목록을 뺀 사전."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            v = prune(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(d, list):
        return [prune(x) for x in d if x is not None]
    return d


def s(v, limit=600):
    """문자열 정리(없으면 None)."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = re.sub(r"\s+", " ", v).strip()
    if not v or v.lower() in ("null", "none", "n/a", "unknown", "-"):
        return None
    return v[:limit]


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def short_hash(text, n=12):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def parse_date(v):
    """여러 형식의 날짜를 YYYY-MM-DD(한국 시간)로."""
    if not v:
        return None
    v = str(v).strip()
    try:
        d = parsedate_to_datetime(v)
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=KST)
            return d.astimezone(KST).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=KST)
        return d.astimezone(KST).date().isoformat()
    except ValueError:
        pass
    m = re.search(r"((?:19|20)\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", v)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            return f"{y:04d}-{mo:02d}-{dd:02d}"
    return None


# ----------------------------------------------------------------------------- HTTP

class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def http_get(url, headers=None, timeout=30, tries=3):
    h = {"User-Agent": UA_BOT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6"}
    if headers:
        h.update(headers)
    last = FetchError("요청 실패")
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            last = FetchError(f"HTTP {e.code}", e.code)
            if e.code in (400, 401, 403, 404, 410, 451):
                break
        except Exception as e:  # URLError, timeout, 연결 끊김 등
            last = FetchError(f"{type(e).__name__}: {e}")
        time.sleep(2 * (i + 1))
    raise last


def decode(data: bytes, headers: dict) -> str:
    ct = headers.get("content-type", "")
    m = re.search(r"charset=([\w\-]+)", ct, re.I)
    enc = m.group(1) if m else None
    if not enc:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", data[:4000], re.I)
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return data.decode(enc, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


# ----------------------------------------------------------------------------- HTML → 텍스트

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section", "article",
         "blockquote", "pre", "dd", "dt", "figure", "figcaption", "header", "footer", "br", "hr", "td", "th"}
SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "button", "select", "option", "textarea"}


class Extract(HTMLParser):
    """match(tag, attrs)에 처음 맞는 요소 안의 글자만 모읍니다. match가 None이면 문서 전체."""

    def __init__(self, match=None):
        super().__init__(convert_charrefs=True)
        self.match = match
        self.inside = 0 if match else 1
        self.found = match is None
        self.done = False
        self.skip = 0
        self.out = []
        self.meta = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        if tag == "title":
            self._in_title = True
        if self.done:
            return
        if self.inside:
            if tag in SKIP:
                self.skip += 1
            if tag not in VOID:
                self.inside += 1
            if tag in BLOCK:
                self.out.append("\n")
        elif self.match and self.match(tag, a) and tag not in VOID:
            self.inside = 1
            self.found = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self.done or not self.inside or tag in VOID:
            return
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag in BLOCK:
            self.out.append("\n")
        if self.match:
            self.inside -= 1
            if self.inside <= 0:
                self.inside = 0
                self.done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self.inside and not self.done and not self.skip:
            self.out.append(data)


def clean_text(t: str) -> str:
    t = t.replace("​", "").replace("﻿", "").replace("\xa0", " ")
    out, blank = [], 0
    for ln in t.split("\n"):
        ln = re.sub(r"[ \t\r\f\v]+", " ", ln).strip()
        if not ln:
            blank += 1
            if blank == 1 and out:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def strip_html(fragment: str) -> str:
    if not fragment:
        return ""
    p = Extract(None)
    try:
        p.feed(fragment)
        p.close()
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", fragment))
    return clean_text("".join(p.out))


def has_class(a, *names):
    cls = a.get("class", "").split()
    return any(n in cls for n in names)


NAVER_TARGETS = [
    lambda t, a: has_class(a, "se-main-container"),          # 스마트에디터 ONE
    lambda t, a: a.get("id") == "postViewArea",               # 스마트에디터 2
    lambda t, a: has_class(a, "se_component_wrap"),           # 스마트에디터 3
    lambda t, a: has_class(a, "post_ct"),                     # 모바일 옛 글
    lambda t, a: a.get("id") == "viewTypeSelector",
]
GENERIC_TARGETS = [
    lambda t, a: has_class(a, "tt_article_useless_p_margin", "entry-content", "post-content", "article-content",
                           "contents_style", "post-body", "blog-content", "article_view", "post_content",
                           "markdown-body"),
    lambda t, a: t == "article",
    lambda t, a: t == "main",
    None,
]


def extract_text(doc: str, targets, accept_found=False):
    """targets를 차례로 시도합니다. accept_found면 영역만 찾아도(글이 짧아도) 그 결과를 씁니다."""
    last = None
    for m in targets:
        p = Extract(m)
        try:
            p.feed(doc)
            p.close()
        except Exception:
            continue
        last = p
        text = clean_text("".join(p.out))
        if (accept_found and m is not None and p.found) or len(text) >= 80:
            return text, p
    return "", last


def find_date(doc: str, meta: dict):
    for k in ("article:published_time", "og:article:published_time", "og:regDate", "date"):
        if meta.get(k):
            d = parse_date(meta[k])
            if d:
                return d
    for m in re.finditer(r'class="[^"]*(?:se_publishDate|blog_date|publish_date|se_date|_postAddDate|date)[^"]*"'
                         r'[^>]*>\s*(?:<[^>]+>\s*)*([^<]{6,40})<', doc):
        d = parse_date(m.group(1))
        if d:
            return d
    return None


# ----------------------------------------------------------------------------- 글 주소

def parse_post_url(url: str):
    """글 주소 → dict(platform, blog, no) 또는 dict(platform='web', url)."""
    u = (url or "").strip().strip("<>\"'")
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    try:
        pu = urllib.parse.urlparse(u)
    except ValueError:
        return None
    host = pu.netloc.lower().split(":")[0]
    if host.endswith("blog.naver.com") and host != "rss.blog.naver.com":
        qs = urllib.parse.parse_qs(pu.query)
        parts = [p for p in pu.path.split("/") if p]
        b = (qs.get("blogId") or [None])[0]
        n = (qs.get("logNo") or [None])[0]
        if not b and parts and "." not in parts[0]:
            b = parts[0]
        if not n and len(parts) >= 2 and parts[1].isdigit():
            n = parts[1]
        if b and n and str(n).isdigit():
            return {"platform": "naver", "blog": b, "no": str(n)}
        return None
    if not host or not pu.path or pu.path == "/":
        return None
    canon = urllib.parse.urlunparse((pu.scheme or "https", pu.netloc, pu.path, "", pu.query, ""))
    return {"platform": "web", "url": canon, "host": host}


def post_key(p):
    if p["platform"] == "naver":
        return f"naver:{p['blog']}:{p['no']}"
    return f"web:{short_hash(p['url'], 16)}"


def post_link(p):
    if p["platform"] == "naver":
        return f"https://blog.naver.com/{p['blog']}/{p['no']}"
    return p["url"]


def wine_base_id(p):
    if p["platform"] == "naver":
        return f"{p['blog']}-{p['no']}"
    return f"w{short_hash(p['url'], 10)}"


def resolve_short(url):
    """naver.me 같은 짧은 주소를 실제 주소로."""
    try:
        _, final, _, _ = http_get(url, headers={"User-Agent": UA_MOBILE}, timeout=20, tries=2)
        return final
    except FetchError:
        return url


# ----------------------------------------------------------------------------- 블로그(출처)

def feed_for(platform, ident, url=""):
    if platform == "naver":
        return f"https://rss.blog.naver.com/{ident}.xml"
    if platform == "tistory":
        return f"https://{ident}.tistory.com/rss"
    return ""


def find_source(sources, p):
    if p["platform"] == "naver":
        for src in sources:
            if src.get("platform") == "naver" and (src.get("blog_id") or src.get("id", "")).lower() == p["blog"].lower():
                return src
        return None
    for src in sources:
        base = src.get("url") or ""
        h = urllib.parse.urlparse(base if "://" in base else "https://" + base).netloc.lower()
        if h and (p["host"] == h or p["host"].endswith("." + h) or h.endswith("." + p["host"])):
            return src
    return None


def unique_id(sources, want):
    want = re.sub(r"[^A-Za-z0-9_\-]", "-", want).strip("-").lower() or "blog"
    ids = {x.get("id") for x in sources}
    if want not in ids:
        return want
    i = 2
    while f"{want}-{i}" in ids:
        i += 1
    return f"{want}-{i}"


def auto_source(cfg, p):
    """모르는 블로그의 과거 글이 들어오면 출처를 만들어 둡니다(RSS 수집은 꺼진 상태)."""
    sources = cfg["sources"]
    if p["platform"] == "naver":
        src = {"id": unique_id(sources, p["blog"]), "name": p["blog"], "platform": "naver", "blog_id": p["blog"],
               "url": f"https://blog.naver.com/{p['blog']}", "rss": feed_for("naver", p["blog"]),
               "enabled": False, "auto": True}
    else:
        h = p["host"]
        ident = h[4:] if h.startswith("www.") else h
        platform = "tistory" if h.endswith(".tistory.com") else "web"
        src = {"id": unique_id(sources, ident.split(".")[0]), "name": ident, "platform": platform,
               "url": f"https://{h}", "rss": feed_for(platform, h.split(".")[0]) if platform == "tistory" else "",
               "enabled": False, "auto": True}
    sources.append(src)
    cfg["_dirty"] = True
    return src


# ----------------------------------------------------------------------------- RSS

def parse_feed(data: bytes):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        txt = data.decode("utf-8", "replace")
        txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", txt)
        root = ET.fromstring(txt.encode("utf-8"))

    def local(tag):
        return str(tag).rsplit("}", 1)[-1].lower()

    channel_title = ""
    for el in root.iter():
        if local(el.tag) in ("channel", "feed"):
            for ch in el:
                if local(ch.tag) == "title" and (ch.text or "").strip():
                    channel_title = strip_html(ch.text.strip())
                    break
            break
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        d = {}
        for ch in el:
            n = local(ch.tag)
            if n == "link":
                href = ch.get("href")
                if href and ch.get("rel", "alternate") == "alternate":
                    d.setdefault("link", href)
                elif (ch.text or "").strip():
                    d.setdefault("link", ch.text.strip())
            elif n in ("title", "guid", "id", "pubdate", "published", "updated", "category",
                       "description", "summary", "content", "encoded"):
                val = (ch.text or "").strip() or ch.get("term", "")
                if val:
                    d.setdefault(n, val)
        link = d.get("link") or d.get("guid") or d.get("id") or ""
        body = d.get("encoded") or d.get("content") or d.get("description") or d.get("summary") or ""
        items.append({
            "title": strip_html(d.get("title", ""))[:300],
            "link": link.strip(),
            "date": parse_date(d.get("pubdate") or d.get("published") or d.get("updated") or ""),
            "category": strip_html(d.get("category", ""))[:80],
            "summary": strip_html(body)[:TEXT_LIMIT],
        })
    return channel_title, items


# ----------------------------------------------------------------------------- 본문 가져오기

def fetch_post(p, feed_text=""):
    """글 본문을 가져옵니다. 실패하면 FetchError."""
    errors = []
    if p["platform"] == "naver":
        b, n = p["blog"], p["no"]
        tries = [
            (f"https://m.blog.naver.com/PostView.naver?blogId={b}&logNo={n}",
             {"User-Agent": UA_MOBILE, "Referer": f"https://m.blog.naver.com/{b}"}),
            (f"https://blog.naver.com/PostView.naver?blogId={b}&logNo={n}&redirect=Dlog&widgetTypeCall=true&directAccess=false",
             {"User-Agent": UA_DESKTOP, "Referer": f"https://blog.naver.com/{b}"}),
        ]
        for i, (u, h) in enumerate(tries):
            if i:
                time.sleep(POLITE_DELAY)
            try:
                _, _, data, hdr = http_get(u, headers=h)
            except FetchError as e:
                errors.append(str(e))
                continue
            doc = decode(data, hdr)
            text, parser = extract_text(doc, NAVER_TARGETS, accept_found=True)
            meta = parser.meta if parser else {}
            if parser and parser.found:
                title = meta.get("og:title") or parser.title or ""
                title = re.sub(r"\s*[:|]\s*네이버\s*블로그\s*$", "", title.strip())
                return {"title": title, "date": find_date(doc, meta), "text": text[:TEXT_LIMIT], "partial": False}
            errors.append("본문 영역을 찾지 못함")
    else:
        if feed_text and len(feed_text) >= 1500:
            return {"title": "", "date": None, "text": feed_text[:TEXT_LIMIT], "partial": False}
        try:
            _, _, data, hdr = http_get(p["url"], headers={"User-Agent": UA_DESKTOP})
            doc = decode(data, hdr)
            text, parser = extract_text(doc, GENERIC_TARGETS)
            meta = parser.meta if parser else {}
            if len(text) >= 80:
                title = meta.get("og:title") or (parser.title if parser else "")
                return {"title": (title or "").strip(), "date": find_date(doc, meta),
                        "text": text[:TEXT_LIMIT], "partial": False}
            errors.append(f"본문을 찾지 못함({len(text)}자)")
        except FetchError as e:
            errors.append(str(e))
    raise FetchError("; ".join(errors) or "본문 없음")


# ----------------------------------------------------------------------------- Claude API

class ApiError(Exception):
    def __init__(self, msg, status=None, body=""):
        super().__init__(msg)
        self.status = status
        self.body = body or ""


class Fatal(Exception):
    """실행을 멈춰야 하는 오류(키 오류, 크레딧 부족 등)."""


def call_claude(body, key, timeout=600):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for i in range(5):
        req = urllib.request.Request(API_URL, data=data, method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:3000]
            last = ApiError(f"HTTP {e.code}: {txt[:400]}", e.code, txt)
            if e.code in (408, 429, 500, 502, 503, 504, 529) and i < 4:
                try:
                    wait = float(e.headers.get("retry-after") or 0)
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 5 * 2 ** i), 120))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = ApiError(f"{type(e).__name__}: {e}")
            if i < 4:
                time.sleep(5 * 2 ** i)
                continue
            raise last
    raise last or ApiError("API 호출 실패")


SYSTEM_PROMPT = """당신은 와인 블로그 글에서 시음 리뷰를 뽑아 개인용 와인 리뷰 색인(지도·검색 페이지)을 만드는 소믈리에 겸 데이터 편집자입니다.

입력은 블로거의 글 한 편(제목, 날짜, 본문)입니다. 본문은 주로 한국어이고 와인 이름은 원어와 한글이 섞여 있습니다.

할 일
1. 블로거가 실제로 마셔 보고 평가(점수, 등급, 추천 여부, 시음 노트)한 와인을 모두 찾습니다. 시음회 글이면 와인마다 따로 적습니다. 이름만 나오고 시음하지 않은 와인은 뺍니다.
2. 와인마다 record_wines 도구의 항목을 채웁니다. 생산자, 산지, 품종, 분류처럼 와인 자체에 대한 사실은 글에 없어도 알고 있는 지식이나 web_search로 확인해 채웁니다. 확실하지 않으면 null로 둡니다. 지어내지 마세요.
3. 점수와 등급은 블로거가 글에 쓴 것만 옮깁니다. 글에 없으면 null입니다. 평론가 점수(RP, JS, WS, Vinous 등)는 블로거 점수가 아니므로 critic_scores에만 넣습니다.
4. 양조 방법(winemaking)은 이 와인(퀴베)의 실제 방식이 먼저입니다. 수확과 선별, 줄기 제거나 송이째 발효 비율, 발효 용기와 효모, 침용 기간, 젖산발효, 숙성 용기와 새 오크 비율과 기간, 앙금 숙성, 스파클링의 병 숙성 기간과 도자주, 스위트 와인의 당 농축 방식, 주정강화 시점 등을 다룹니다. 글에 있으면 글 내용을 쓰고, 없으면 생산자 기술 자료(tech sheet), 수입사 자료 같은 믿을 만한 출처를 web_search로 찾아 씁니다. 검색은 꼭 필요한 만큼만 합니다. 퀴베 정보를 찾지 못하면 생산자의 일반 방식이나 해당 아펠라시옹의 일반 규정을 쓰고, scope로 그 사실을 밝힙니다. 한국어 2~4문장으로 씁니다. 참고로 주어진 '이전에 정리한 양조 정보'가 같은 퀴베에 맞으면 다시 검색하지 말고 재사용합니다.
5. 저작권: 블로그 문장을 그대로 옮기지 않습니다. tasting_summary에는 블로거 평가의 요지를 당신의 말로 1~2문장(120자 안팎)으로 씁니다.
6. 좌표(lat, lng)는 생산자 마을이나 아펠라시옹처럼 알 수 있는 가장 구체적인 장소의 대략적인 위도·경도(소수 셋째 자리)입니다. 정밀도는 geo_precision에 적습니다.
7. 와인 리뷰 글이 아니면(공지, 음식점 후기만, 일상 등) is_wine_review=false, wines=[]로 제출합니다.
8. 마지막에 record_wines 도구를 정확히 한 번 호출해 결과를 제출합니다.

표기 규칙: 분류와 검색이 맞도록 country, region, grapes.name에는 아래 영어 표준 이름을 그대로 씁니다. 한글 이름은 labels_ko와 grapes.name_ko에 씁니다.
- 나라: France, Italy, Spain, Portugal, Germany, Austria, Switzerland, Hungary, Greece, Georgia, Slovenia, Croatia, Lebanon, Israel, United States, Canada, Argentina, Chile, Uruguay, Brazil, Australia, New Zealand, South Africa, United Kingdom, Japan, South Korea, China (그 밖은 영어 국가명)
- 지역(region)은 업계에서 흔히 묶는 대표 산지입니다.
  France: Bordeaux, Burgundy(샤블리 포함), Beaujolais, Champagne, Rhône, Loire, Alsace, Languedoc-Roussillon, Provence, Jura, Savoie, South West France, Corsica
  Italy: Piedmont, Tuscany, Veneto, Friuli-Venezia Giulia, Trentino-Alto Adige, Lombardy, Emilia-Romagna, Marche, Umbria, Lazio, Abruzzo, Campania, Puglia, Basilicata, Calabria, Sicily, Sardinia, Liguria, Aosta Valley
  Spain: Rioja, Ribera del Duero, Priorat, Rías Baixas, Jerez, Penedès, Toro, Rueda, Bierzo, Navarra, Ribeira Sacra, Canary Islands
  Portugal: Douro, Vinho Verde, Alentejo, Dão, Bairrada, Madeira, Lisboa
  Germany: Mosel, Rheingau, Pfalz, Rheinhessen, Nahe, Franken, Baden, Ahr, Württemberg
  Austria: Wachau, Kamptal, Kremstal, Burgenland, Steiermark, Wien
  United States: Napa Valley, Sonoma County, Central Coast, Santa Barbara County, Willamette Valley, Washington, New York (그 밖의 캘리포니아는 California)
  Australia: Barossa, McLaren Vale, Coonawarra, Clare Valley, Eden Valley, Adelaide Hills, Margaret River, Yarra Valley, Hunter Valley, Tasmania
  New Zealand: Marlborough, Central Otago, Hawke's Bay, Martinborough
  Chile: Maipo, Colchagua, Casablanca, Aconcagua, Maule / Argentina: Mendoza, Salta, Patagonia
  South Africa: Stellenbosch, Swartland, Constantia, Hemel-en-Aarde / Hungary: Tokaj / Greece: Santorini
  (그 밖은 흔히 쓰는 영어 이름)
- subregion은 지역 안의 하위 산지(예: Côte de Nuits, Médoc, Montagne de Reims, Langhe, Barossa Valley), appellation은 라벨의 원산지 명칭(예: Gevrey-Chambertin, Pauillac, Barolo, Brunello di Montalcino)입니다.
- classification(분류 등급)은 공식·관용 등급이 있을 때만 씁니다. 예: Grand Cru, Premier Cru, Grand Cru Classé 1855 (2e), Saint-Émilion Grand Cru Classé, Cru Bourgeois, DOCG, DOC, Gran Selezione, Riserva, VDP Grosse Lage (GG), Erste Lage, Prädikat (Spätlese 등), Gran Reserva, Reserva, Cru (Beaujolais). 없으면 null.
- 품종(grapes.name): Cabernet Sauvignon, Merlot, Cabernet Franc, Petit Verdot, Malbec, Carménère, Pinot Noir, Gamay, Syrah(쉬라즈 포함), Grenache(가르나차 포함), Mourvèdre, Carignan, Cinsault, Nebbiolo, Sangiovese, Barbera, Dolcetto, Corvina, Aglianico, Nero d'Avola, Nerello Mascalese, Montepulciano, Primitivo, Zinfandel, Tempranillo, Touriga Nacional, Blaufränkisch, Zweigelt, Saperavi, Pinotage, Tannat, Chardonnay, Sauvignon Blanc, Sémillon, Riesling, Chenin Blanc, Pinot Gris(피노 그리지오 포함), Pinot Blanc, Gewürztraminer, Grüner Veltliner, Viognier, Marsanne, Roussanne, Albariño, Muscat(모스카토 포함), Furmint, Garganega, Vermentino, Fiano, Greco, Verdicchio, Assyrtiko, Aligoté, Melon de Bourgogne, Meunier, Palomino, Pedro Ximénez, Torrontés, Godello (그 밖은 흔히 쓰는 원어 이름). 비율(pct)은 알 때만.
- type: red, white, rose, sparkling, sweet(귀부·늦수확 등 단맛 위주 스틸), fortified(포트, 셰리, 마데이라 등), orange(껍질 접촉 화이트), other.

점수 옮기는 법
- score_raw: 블로거가 쓴 그대로(예: "93", "17.5/20", "★★★★☆", "4.5점", "A+").
- score_value, score_scale: 숫자 점수면 값과 만점(100, 20, 10, 5)을 넣습니다. 별점은 별 개수(반 개는 .5)와 5입니다.
- score_est100: 숫자 점수는 없지만 블로거의 등급이나 표현이 분명할 때만 100점 기준 추정치를 넣습니다(예: 블로거 체계에서 최고 등급이면 95 안팎). 숫자 점수가 있으면 null입니다.
- score_basis: stated(숫자 점수), estimated(추정), none(평가 없음).
- grade_raw: 블로거 고유의 등급이나 추천 표기(예: "Recommended", "강추", "재구매 의사 있음", "A"). 없으면 null.
"""

GRAPE_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "표준 영어 품종명"},
        "name_ko": {"type": ["string", "null"]},
        "pct": {"type": ["number", "null"], "description": "블렌딩 비율(%)"},
    },
    "required": ["name"],
}

WINE_ITEM = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "원어 이름: 생산자 + 퀴베/와인명 + 아펠라시옹 (빈티지 제외)"},
        "name_ko": {"type": ["string", "null"], "description": "한글 이름(블로그 표기 우선)"},
        "producer": {"type": "string"},
        "producer_ko": {"type": ["string", "null"]},
        "cuvee": {"type": ["string", "null"]},
        "vintage": {"type": ["string", "null"], "description": "4자리 연도 또는 NV"},
        "type": {"type": "string", "enum": sorted(TYPES)},
        "country": {"type": "string"},
        "region": {"type": ["string", "null"]},
        "subregion": {"type": ["string", "null"]},
        "appellation": {"type": ["string", "null"]},
        "labels_ko": {
            "type": "object",
            "description": "한글 표기",
            "properties": {"country": {"type": ["string", "null"]}, "region": {"type": ["string", "null"]},
                           "subregion": {"type": ["string", "null"]}, "appellation": {"type": ["string", "null"]}},
        },
        "classification": {"type": ["string", "null"]},
        "grapes": {"type": "array", "items": GRAPE_ITEM},
        "abv": {"type": ["number", "null"]},
        "price": {"type": ["string", "null"], "description": "글에 적힌 가격·구입처를 짧게"},
        "score_raw": {"type": ["string", "null"]},
        "score_value": {"type": ["number", "null"]},
        "score_scale": {"type": ["number", "null"]},
        "score_est100": {"type": ["number", "null"]},
        "score_basis": {"type": "string", "enum": ["stated", "estimated", "none"]},
        "grade_raw": {"type": ["string", "null"]},
        "critic_scores": {"type": "array", "items": {"type": "string"}},
        "tasting_summary": {"type": "string", "description": "블로거 평가의 요지를 자기 말로 1~2문장"},
        "tasted_on": {"type": ["string", "null"], "description": "시음 날짜(YYYY-MM-DD 또는 YYYY-MM), 글에 있을 때만"},
        "drink_window": {"type": ["string", "null"]},
        "food_pairing": {"type": ["string", "null"]},
        "winemaking": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "한국어 2~4문장"},
                "fermentation": {"type": ["string", "null"]},
                "aging": {"type": ["string", "null"]},
                "other": {"type": ["string", "null"]},
                "basis": {"type": "string", "enum": ["post", "web", "knowledge", "mixed"]},
                "scope": {"type": "string", "enum": ["cuvee", "producer", "appellation"]},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "참고한 URL"},
            },
            "required": ["summary", "basis", "scope"],
        },
        "lat": {"type": ["number", "null"]},
        "lng": {"type": ["number", "null"]},
        "geo_precision": {"type": "string", "enum": ["producer", "village", "appellation", "region", "country"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["name", "producer", "type", "country", "grapes", "score_basis", "tasting_summary",
                 "winemaking", "geo_precision", "confidence"],
}

RECORD_TOOL = {
    "name": "record_wines",
    "description": "글에서 정리한 와인 리뷰 목록을 제출합니다. 글 하나에 정확히 한 번 호출합니다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_wine_review": {"type": "boolean", "description": "와인 시음·리뷰 글이면 true"},
            "post_kind": {"type": "string", "description": "글 성격 한 줄(예: 샴페인 6종 비교 시음)"},
            "wines": {"type": "array", "items": WINE_ITEM},
        },
        "required": ["is_wine_review", "wines"],
    },
}


class Ctx:
    """한 번의 실행 동안 공유하는 상태."""

    def __init__(self, settings):
        self.model = settings.get("model") or DEFAULT_MODEL
        self.web_search = bool(settings.get("web_search", True))
        self.max_uses = int(settings.get("web_search_max_uses", 3) or 3)
        self.notes = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "calls": 0}
        self.models_used = set()

    def note(self, msg):
        if msg not in self.notes:
            self.notes.append(msg)
            log("  · " + msg)

    def add_usage(self, u, model):
        if not u:
            return
        self.usage["calls"] += 1
        self.usage["input_tokens"] += int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) \
            + int(u.get("cache_read_input_tokens") or 0)
        self.usage["output_tokens"] += int(u.get("output_tokens") or 0)
        self.usage["web_searches"] += int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        self.models_used.add(model)

    def cost(self):
        if len(self.models_used) != 1:
            return None
        p = PRICES.get(next(iter(self.models_used)))
        if not p:
            return None
        u = self.usage
        return round(u["input_tokens"] / 1e6 * p[0] + u["output_tokens"] / 1e6 * p[1]
                     + u["web_searches"] * WEB_SEARCH_PRICE, 4)


def related_context(text, wines, limit=8):
    """본문에 나오는 생산자의 기존 양조 정보(검색 비용 절약용)."""
    low = text.lower()
    out, per = [], {}
    for w in wines:
        prod = (w.get("producer") or "").strip()
        wm = (w.get("winemaking") or {}).get("summary")
        if len(prod) < 4 or not wm:
            continue
        hit = prod.lower() in low or (w.get("producer_ko") and len(w["producer_ko"]) >= 3 and w["producer_ko"] in text)
        if not hit:
            continue
        k = prod.lower()
        names = per.setdefault(k, set())
        if w.get("name") in names or len(names) >= 2:
            continue
        names.add(w.get("name"))
        out.append(f"- {prod} | {w.get('name', '')} | {wm}")
        if len(out) >= limit:
            break
    return "\n".join(out)


def build_user_message(src, p, content, related):
    lines = [f"블로그: {src.get('name') or src.get('id')} (id: {src.get('id')}, {src.get('platform')})"]
    if src.get("hint"):
        lines.append(f"이 블로그의 점수·등급 체계: {src['hint']}")
    lines += [f"글 제목: {content.get('title') or p.get('title') or ''}",
              f"글 날짜: {content.get('date') or p.get('date') or '모름'}",
              f"글 주소: {post_link(p)}"]
    if content.get("partial"):
        lines.append("주의: 본문 대신 RSS 요약만 있습니다. 확인되는 내용만 정리하세요.")
    if related:
        lines.append("이전에 정리한 같은 생산자 와인의 양조 정보(같은 퀴베면 재사용, 다르면 새로 확인):\n" + related)
    lines.append("<본문>\n" + content["text"] + "\n</본문>")
    return "\n".join(lines)


def extract(src, p, content, key, ctx, wines):
    related = related_context(content["text"], wines)
    messages = [{"role": "user", "content": build_user_message(src, p, content, related)}]
    models = [ctx.model] + [m for m in FALLBACK_MODELS if m != ctx.model]
    forced = False
    turns = 0
    while turns < 6:
        turns += 1
        tools = [RECORD_TOOL]
        if ctx.web_search:
            tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": ctx.max_uses}, RECORD_TOOL]
        body = {"model": models[0], "max_tokens": 16000, "system": SYSTEM_PROMPT, "messages": messages,
                "tools": tools,
                "tool_choice": {"type": "tool", "name": "record_wines"} if forced else {"type": "auto"}}
        try:
            resp = call_claude(body, key)
        except ApiError as e:
            low = e.body.lower()
            if e.status == 401:
                raise Fatal("Claude API 키가 올바르지 않습니다. 저장소 Secret ANTHROPIC_API_KEY를 확인하세요.")
            if e.status == 403:
                raise Fatal("Claude API 키에 이 작업 권한이 없습니다. Console에서 키와 조직 설정을 확인하세요.")
            if e.status == 400 and ("credit" in low or "billing" in low):
                raise Fatal("Claude API 크레딧이 부족합니다. Console의 Billing에서 충전하세요.")
            if e.status == 400 and ctx.web_search and ("web_search" in low or "web search" in low):
                ctx.web_search = False
                ctx.note("웹 검색이 꺼져 있어 양조 정보는 글과 모델 지식으로만 정리합니다. "
                         "Claude Console의 Privacy 설정에서 웹 검색을 켜면 보강됩니다.")
                turns -= 1
                continue
            if e.status == 404 and "model" in low and len(models) > 1:
                ctx.note(f"모델 {models[0]}을 쓸 수 없어 {models[1]}로 바꿉니다.")
                models.pop(0)
                turns -= 1
                continue
            raise
        ctx.add_usage(resp.get("usage"), models[0])
        blocks = resp.get("content") or []
        rec = next((b for b in blocks if b.get("type") == "tool_use" and b.get("name") == "record_wines"), None)
        if rec:
            return rec.get("input") or {}, models[0]
        if resp.get("stop_reason") == "pause_turn":
            messages.append({"role": "assistant", "content": blocks})
            continue
        messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(결과 없음)"}]})
        messages.append({"role": "user", "content": "확인한 내용으로 지금 record_wines 도구를 호출해 결과를 제출하세요."})
        forced = True
    raise ApiError("record_wines 결과를 받지 못했습니다.")


# ----------------------------------------------------------------------------- 결과 정리

def clean_wine(w, idx, base_id, src, p, content, model):
    def lab(k):
        return s((w.get("labels_ko") or {}).get(k), 120) if isinstance(w.get("labels_ko"), dict) else None

    vint = s(w.get("vintage"), 12)
    if vint:
        m = re.search(r"(?:19|20)\d{2}", vint)
        vint = m.group(0) if m else ("NV" if re.search(r"\bN\.?V\b|논빈|넌빈", vint, re.I) else vint)
    typ = (s(w.get("type"), 20) or "other").lower()
    typ = {"rosé": "rose", "sparkling wine": "sparkling", "dessert": "sweet"}.get(typ, typ)
    if typ not in TYPES:
        typ = "other"
    grapes, seen = [], set()
    for g in w.get("grapes") or []:
        if isinstance(g, str):
            g = {"name": g}
        if not isinstance(g, dict):
            continue
        name = s(g.get("name"), 60)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        pct = num(g.get("pct"))
        grapes.append(prune({"name": name, "name_ko": s(g.get("name_ko"), 60),
                             "pct": pct if pct is not None and 0 < pct <= 100 else None}))
    lat, lng = num(w.get("lat")), num(w.get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        lat = lng = None
    value, scale = num(w.get("score_value")), num(w.get("score_scale"))
    if value is not None and scale is None:
        scale = next((x for x in (5, 10, 20, 100) if value <= x), None)
    if value is None or scale is None or scale <= 0 or value < 0 or value > scale:
        value = scale = None
    est = num(w.get("score_est100"))
    if est is not None and not (40 <= est <= 100):
        est = None
    basis = s(w.get("score_basis"), 12) or ("stated" if value is not None else "estimated" if est is not None else "none")
    wm = w.get("winemaking") if isinstance(w.get("winemaking"), dict) else {}
    srcs = [u for u in (wm.get("sources") or []) if isinstance(u, str) and re.match(r"https?://", u)][:6]
    rec = {
        "id": f"{base_id}-{idx + 1}",
        "source": src.get("id"),
        "post": {"key": post_key(p), "url": post_link(p), "title": s(content.get("title") or p.get("title"), 300),
                 "date": content.get("date") or p.get("date")},
        "name": s(w.get("name"), 240),
        "name_ko": s(w.get("name_ko"), 240),
        "producer": s(w.get("producer"), 160),
        "producer_ko": s(w.get("producer_ko"), 160),
        "cuvee": s(w.get("cuvee"), 160),
        "vintage": vint,
        "type": typ,
        "country": s(w.get("country"), 60),
        "region": s(w.get("region"), 80),
        "subregion": s(w.get("subregion"), 80),
        "appellation": s(w.get("appellation"), 120),
        "labels_ko": {"country": lab("country"), "region": lab("region"), "subregion": lab("subregion"),
                      "appellation": lab("appellation")},
        "classification": s(w.get("classification"), 120),
        "grapes": grapes,
        "abv": (lambda a: a if a is not None and 3 <= a <= 25 else None)(num(w.get("abv"))),
        "price": s(w.get("price"), 120),
        "score": {"raw": s(w.get("score_raw"), 40), "value": value, "scale": scale, "est100": est, "basis": basis},
        "grade": {"raw": s(w.get("grade_raw"), 60)},
        "critic_scores": [x for x in (s(c, 40) for c in (w.get("critic_scores") or [])) if x][:8],
        "summary": s(w.get("tasting_summary"), 400),
        "tasted_on": s(w.get("tasted_on"), 20),
        "drink_window": s(w.get("drink_window"), 80),
        "food_pairing": s(w.get("food_pairing"), 160),
        "winemaking": {"summary": s(wm.get("summary"), 900), "fermentation": s(wm.get("fermentation"), 400),
                       "aging": s(wm.get("aging"), 400), "other": s(wm.get("other"), 400),
                       "basis": s(wm.get("basis"), 12), "scope": s(wm.get("scope"), 12), "sources": srcs},
        "geo": {"lat": round(lat, 4) if lat is not None else None, "lng": round(lng, 4) if lng is not None else None,
                "precision": s(w.get("geo_precision"), 12)},
        "confidence": s(w.get("confidence"), 8),
        "partial": bool(content.get("partial")) or None,
        "model": model,
        "updated": now_iso(),
    }
    rec = prune(rec)
    rec.setdefault("name", rec.get("name_ko") or "이름 없는 와인")
    return rec


def merge(wines_doc, key, new):
    keep = [w for w in wines_doc.get("wines", []) if (w.get("post") or {}).get("key") != key]
    keep.extend(new)
    keep.sort(key=lambda w: ((w.get("post") or {}).get("date") or "", w.get("id") or ""), reverse=True)
    wines_doc["wines"] = keep


# ----------------------------------------------------------------------------- 대기열

def read_backfill():
    try:
        with open(P_BACKFILL, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    urls = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        urls += re.findall(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s,<>\"']*)?", ln, re.I)
    return urls


def enqueue_url(url, cfg, state, origin):
    u = url
    if re.search(r"(^|//)naver\.me/", u):
        u = resolve_short(u if "://" in u else "https://" + u)
    p = parse_post_url(u)
    if not p:
        return None
    k = post_key(p)
    st = state["posts"].get(k)
    if st and st.get("status") in ("ok", "skip"):
        return None
    if st and st.get("status") == "error" and st.get("tries", 0) >= MAX_TRIES:
        return None
    src = find_source(cfg["sources"], p) or auto_source(cfg, p)
    if not st:
        st = state["posts"][k] = {"status": "queued", "tries": 0}
    st.update({"source": src["id"], "platform": p["platform"], "url": post_link(p)})
    st.setdefault("origin", origin)
    if p["platform"] == "naver":
        st.update({"blog": p["blog"], "no": p["no"]})
    st.setdefault("queued_at", now_iso())
    return k


def post_from_state(st):
    if st.get("platform") == "naver":
        return {"platform": "naver", "blog": st["blog"], "no": st["no"]}
    p = parse_post_url(st.get("url", ""))
    return p


# ----------------------------------------------------------------------------- 실행

def poll_feeds(cfg, state, only, report, feed_cache):
    for src in cfg["sources"]:
        if only and src.get("id") != only:
            continue
        r = {"id": src.get("id"), "name": src.get("name")}
        report.append(r)
        if not src.get("enabled", True):
            r["rss"] = "off"
            continue
        if not src.get("rss"):
            r["rss"] = "none"
            continue
        try:
            _, _, data, _ = http_get(src["rss"], headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
            title, items = parse_feed(data)
        except (FetchError, ET.ParseError) as e:
            r["rss"] = "error"
            r["error"] = str(e)[:200]
            log(f"[{src['id']}] RSS 실패: {e}")
            continue
        if title and (not src.get("name") or src.get("name") == src.get("id")):
            src["name"] = title[:80]
            r["name"] = src["name"]
            cfg["_dirty"] = True
        new = 0
        for it in items:
            p = parse_post_url(it["link"])
            if not p:
                continue
            k = post_key(p)
            feed_cache[k] = it
            st = state["posts"].get(k)
            if st is None:
                state["posts"][k] = {"status": "queued", "tries": 0, "source": src["id"], "platform": p["platform"],
                                     "url": post_link(p), "title": it["title"], "date": it["date"], "origin": "rss",
                                     "queued_at": now_iso()}
                if p["platform"] == "naver":
                    state["posts"][k].update({"blog": p["blog"], "no": p["no"]})
                new += 1
            else:
                st.setdefault("title", it["title"])
                st.setdefault("date", it["date"])
        r.update({"rss": "ok", "items": len(items), "new": new,
                  "latest": max((it["date"] for it in items if it["date"]), default=None)})
        log(f"[{src['id']}] RSS {len(items)}개, 새 글 {new}개")
        time.sleep(POLITE_DELAY)


def pending_keys(state, only, mode):
    keys = []
    for k, st in state["posts"].items():
        if only and st.get("source") != only:
            continue
        if mode == "reprocess":
            if st.get("status") in ("ok", "skip"):
                keys.append(k)
        elif st.get("status") == "queued" or (st.get("status") == "error" and st.get("tries", 0) < MAX_TRIES):
            keys.append(k)
    if mode == "reprocess":
        keys.sort(key=lambda k: state["posts"][k].get("at") or "")
    else:
        # RSS 새 글 먼저(최근 글부터), 그다음 과거 글, 마지막에 다시 시도할 글
        order = {"rss": 0, "backfill": 1, "manual": 1}
        keys.sort(key=lambda k: state["posts"][k].get("date") or "", reverse=True)
        keys.sort(key=lambda k: (1 if state["posts"][k].get("status") == "error" else 0,
                                 order.get(state["posts"][k].get("origin"), 2)))
    return keys


def run_check(cfg, state, only, status_doc, started):
    report = []
    for src in cfg["sources"]:
        if only and src.get("id") != only:
            continue
        r = {"id": src.get("id"), "name": src.get("name")}
        report.append(r)
        if not src.get("enabled", True):
            r["rss"] = "off"
            continue
        if not src.get("rss"):
            r["rss"] = "none"
            continue
        try:
            _, _, data, _ = http_get(src["rss"])
            title, items = parse_feed(data)
            r.update({"rss": "ok", "items": len(items), "channel": title[:80],
                      "latest": max((it["date"] for it in items if it["date"]), default=None)})
        except Exception as e:
            r.update({"rss": "error", "error": str(e)[:200]})
            continue
        for it in items[:1]:
            p = parse_post_url(it["link"])
            if not p:
                r["post_fetch"] = {"ok": False, "error": "글 주소를 해석하지 못함"}
                continue
            time.sleep(POLITE_DELAY)
            try:
                c = fetch_post(p, it.get("summary", ""))
                r["post_fetch"] = {"ok": True, "chars": len(c["text"]), "date_found": bool(c.get("date"))}
            except FetchError as e:
                r["post_fetch"] = {"ok": False, "error": str(e)[:200]}
                r["rss_summary_chars"] = len(it.get("summary") or "")
        time.sleep(POLITE_DELAY)
    ok = all(x.get("rss") in ("ok", "none", "off") and (x.get("post_fetch") or {}).get("ok", True) for x in report)
    msgs = []
    for x in report:
        if x.get("rss") == "off":
            continue
        if x.get("rss") == "ok":
            pf = x.get("post_fetch") or {}
            msgs.append(f"{x['id']}: RSS {x.get('items', 0)}개" + (", 본문 OK" if pf.get("ok") else f", 본문 실패({pf.get('error', '')})"))
        else:
            msgs.append(f"{x['id']}: RSS {x.get('rss')}" + (f"({x.get('error')})" if x.get("error") else ""))
    return {"at": started, "finished": now_iso(), "mode": "check", "ok": ok,
            "message": "접속 점검 · " + " / ".join(msgs), "sources": report}


def summarize(n_ok, n_skip, n_err, added, pending, notes, fatal):
    parts = []
    if fatal:
        parts.append(fatal)
    if n_ok:
        parts.append(f"글 {n_ok}개 정리 · 와인 {added}개 반영")
    if n_skip:
        parts.append(f"와인 글이 아닌 글 {n_skip}개 건너뜀")
    if n_err:
        parts.append(f"실패 {n_err}개")
    if pending:
        parts.append(f"남은 글 {pending}개(다음 실행에서 이어서)")
    if not parts:
        parts.append("새 글 없음")
    return " · ".join(parts)


def main():
    started = now_iso()
    t0 = time.time()
    mode = (os.environ.get("MODE") or "new").strip().lower()
    if mode not in ("new", "reprocess", "check"):
        mode = "new"
    only = (os.environ.get("SOURCE") or "").strip() or None

    cfg = load_json(P_SOURCES, {"sources": [], "settings": {}})
    cfg.setdefault("sources", [])
    settings = cfg.setdefault("settings", {})
    state = load_json(P_STATE, {"posts": {}})
    state.setdefault("posts", {})
    wines_doc = load_json(P_WINES, {"version": 1, "wines": []})
    wines_doc.setdefault("wines", [])
    status_doc = load_json(P_STATUS, {"history": []})
    try:
        limit = int(os.environ.get("LIMIT") or settings.get("max_posts_per_run") or 20)
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 200))
    budget = float(os.environ.get("TIME_BUDGET_MIN") or 45) * 60

    run_url = None
    if os.environ.get("GITHUB_RUN_ID") and REPO:
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}"

    def write_status(last):
        last["run_url"] = run_url
        status_doc["updated"] = now_iso()
        status_doc["last_run"] = prune(last) | {"ok": last.get("ok", False)}
        hist = status_doc.setdefault("history", [])
        hist.insert(0, prune({k: last.get(k) for k in ("at", "mode", "ok", "message", "processed", "added", "failed")}))
        del hist[20:]
        status_doc["totals"] = {"wines": len(wines_doc["wines"]),
                                "posts": sum(1 for x in state["posts"].values() if x.get("status") == "ok"),
                                "pending": sum(1 for x in state["posts"].values()
                                               if x.get("status") == "queued" or
                                               (x.get("status") == "error" and x.get("tries", 0) < MAX_TRIES))}
        save_json(P_STATUS, status_doc)

    log(f"모드 {mode} · 블로그 {only or '전체'} · 최대 {limit}개")

    if mode == "check":
        last = run_check(cfg, state, only, status_doc, started)
        write_status(last)
        log(last["message"])
        return 0

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    report, feed_cache = [], {}
    if mode == "new":
        poll_feeds(cfg, state, only, report, feed_cache)
        extra = re.split(r"[\s,]+", os.environ.get("POST_URLS") or "")
        for u in read_backfill() + [x for x in extra if x]:
            enqueue_url(u, cfg, state, "backfill")
    if cfg.pop("_dirty", False):
        save_json(P_SOURCES, cfg)
    save_json(P_STATE, state)

    keys = pending_keys(state, only, mode)
    todo = keys[:limit]
    log(f"정리할 글 {len(keys)}개 중 이번에 {len(todo)}개")

    if todo and not key:
        msg = "Claude API 키가 없습니다. 저장소 Settings → Secrets and variables → Actions에 ANTHROPIC_API_KEY를 넣어 주세요."
        write_status({"at": started, "finished": now_iso(), "mode": mode, "ok": False, "message": msg,
                      "sources": report, "processed": 0, "added": 0, "failed": 0, "pending": len(keys)})
        log(msg)
        return 1

    ctx = Ctx(settings)
    src_by_id = {x.get("id"): x for x in cfg["sources"]}
    n_ok = n_skip = n_err = added = 0
    errors = []
    fatal = None
    done = 0
    for k in todo:
        if time.time() - t0 > budget:
            ctx.note("시간 제한에 가까워 남은 글은 다음 실행에서 이어서 정리합니다.")
            break
        st = state["posts"][k]
        p = post_from_state(st)
        if not p:
            st.update({"status": "error", "tries": MAX_TRIES, "error": "글 주소를 해석하지 못했습니다."})
            continue
        src = src_by_id.get(st.get("source")) or find_source(cfg["sources"], p) or auto_source(cfg, p)
        src_by_id[src["id"]] = src
        log(f"- {post_link(p)}")
        done += 1
        feed_item = feed_cache.get(k) or {}
        try:
            try:
                content = fetch_post(p, feed_item.get("summary", ""))
            except FetchError as e:
                summary = feed_item.get("summary") or ""
                if len(summary) >= 80:
                    content = {"title": feed_item.get("title"), "date": feed_item.get("date"), "text": summary,
                               "partial": True}
                    ctx.note("본문을 가져오지 못한 글은 RSS 요약만으로 정리했습니다(상세에 '요약만' 표시).")
                else:
                    raise
            # RSS의 제목·날짜가 더 정확하므로 있으면 그것을 씁니다.
            content["title"] = st.get("title") or feed_item.get("title") or content.get("title")
            content["date"] = st.get("date") or feed_item.get("date") or content.get("date")
            if len((content.get("text") or "").strip()) < 5:
                # 사진만 있는 글 등: API를 부르지 않고 건너뜁니다.
                merge(wines_doc, k, [])
                st.update({"status": "skip", "wines": 0, "at": now_iso(), "kind": "본문에 글자가 없음",
                           "title": s(content.get("title"), 300), "date": content.get("date")})
                st.pop("error", None)
                n_skip += 1
                continue
            result, model = extract(src, p, content, key, ctx, wines_doc["wines"])
            is_wine = bool(result.get("is_wine_review")) and bool(result.get("wines"))
            base = wine_base_id(p)
            new = [clean_wine(w, i, base, src, p, content, model)
                   for i, w in enumerate(result.get("wines") or []) if isinstance(w, dict)] if is_wine else []
            merge(wines_doc, k, new)
            st.update({"status": "ok" if is_wine else "skip", "wines": len(new), "at": now_iso(), "model": model,
                       "title": s(content.get("title"), 300), "date": content.get("date"),
                       "kind": s(result.get("post_kind"), 120), "partial": bool(content.get("partial")) or None})
            st.pop("error", None)
            st = prune(st)
            state["posts"][k] = st
            if is_wine:
                n_ok += 1
                added += len(new)
            else:
                n_skip += 1
        except Fatal as e:
            fatal = str(e)
            log("중단: " + fatal)
            break
        except (FetchError, ApiError) as e:
            n_err += 1
            st["tries"] = int(st.get("tries", 0)) + 1
            st.update({"status": "error", "error": str(e)[:300], "at": now_iso()})
            errors.append({"post": post_link(p), "title": st.get("title"), "error": str(e)[:300]})
            log(f"  실패: {e}")
        except Exception as e:  # 예상 못 한 오류도 기록하고 다음 글로
            n_err += 1
            st["tries"] = int(st.get("tries", 0)) + 1
            st.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300], "at": now_iso()})
            errors.append({"post": post_link(p), "title": st.get("title"), "error": f"{type(e).__name__}: {e}"[:300]})
            traceback.print_exc()
        wines_doc["updated"] = now_iso()
        save_json(P_WINES, wines_doc)
        save_json(P_STATE, state)
        if cfg.pop("_dirty", False):
            save_json(P_SOURCES, cfg)
        time.sleep(POLITE_DELAY)

    remaining = len(pending_keys(state, only, "new")) if mode == "new" else max(0, len(keys) - done)
    msg = summarize(n_ok, n_skip, n_err, added, remaining, ctx.notes, fatal)
    usage = dict(ctx.usage)
    usage["cost_usd"] = ctx.cost()
    usage["model"] = ", ".join(sorted(ctx.models_used)) or None
    write_status({"at": started, "finished": now_iso(), "mode": mode, "ok": fatal is None and n_err == 0,
                  "message": msg, "sources": report, "processed": n_ok, "skipped": n_skip, "failed": n_err,
                  "added": added, "pending": remaining, "errors": errors[:30], "notes": ctx.notes, "usage": usage})
    wines_doc["updated"] = now_iso()
    save_json(P_WINES, wines_doc)
    log(msg)
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
