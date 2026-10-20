"""
Bluesky / Mastodon 크롤러.

Nitter(X) 수집이 간헐적으로 끊기는 문제를 완화하기 위해 공식 공개 API를
쓰는 대체/보조 소스를 추가한다. 두 플랫폼 모두 결과를 x_crawler.py의
트윗 dict 스키마와 동일한 형태로 변환해서, analyzer.py/llm_analyzer.py를
전혀 손대지 않고 그대로 통과시킨다 (platform 키만 추가로 붙음).

Mastodon: 완전 공개 REST API, 인증 불필요.
Bluesky: 프로필/타임라인 조회는 비로그인 가능하지만 검색(searchPosts)은
  비로그인 403이 확인되어 App Password 로그인이 필요하다.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger("sns_crawler")

REQUEST_TIMEOUT = 15
SNS_TARGETS_PATH = Path(__file__).resolve().parent / "sns_targets.json"


def load_sns_targets(path: Path = SNS_TARGETS_PATH) -> dict:
    """sns_targets.json을 읽어 mastodon_accounts/bluesky_accounts/bluesky_keywords를 반환.
    파일이 없으면 빈 딕셔너리(전부 빈 리스트처럼 동작)를 반환해 호출부가 안전하게 건너뛸 수 있게 한다.
    """
    if not path.exists():
        logger.warning("sns_targets.json 없음(%s) — Bluesky/Mastodon 수집 건너뜀", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("sns_targets.json 파싱 실패: %s", e)
        return {}


def _post_to_tweet_dict(
    text: str,
    username: str,
    display_name: str,
    date_iso: str,
    link: str,
    platform: str,
) -> dict:
    """Bluesky/Mastodon 게시물을 x_crawler.TweetParser와 동일한 키 구조로 변환."""
    return {
        "id": link.rsplit("/", 1)[-1] if link else "",
        "link": link,
        "text": text,
        "user": {
            "name": display_name,
            "username": username,
            "profile_id": "",
            "avatar": "",
        },
        "date": date_iso,
        "is-retweet": False,
        "is-pinned": False,
        "external-link": "",
        "replying-to": [],
        "quoted-post": {},
        "stats": {"comments": 0, "retweets": 0, "quotes": 0, "likes": 0},
        "pictures": [],
        "videos": [],
        "gifs": [],
        "platform": platform,
    }


@dataclass
class SnsCrawlResult:
    """Bluesky/Mastodon 크롤링 결과. x_crawler.CrawlResult와 동일한 toDict() 인터페이스."""
    target: str
    tweets: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def toDict(self) -> dict:
        return {
            "username": self.target,
            "tweets": self.tweets,
            "threads": [],
            "meta": self.meta,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MastodonCrawler:
    """Mastodon 공개 REST API 크롤러. 인증 불필요."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT):
        self._timeout = timeout

    def crawl_account(
        self, username: str, instance: str, max_posts: int = 20
    ) -> SnsCrawlResult:
        base = f"https://{instance}"
        try:
            lookup_resp = requests.get(
                f"{base}/api/v1/accounts/lookup",
                params={"acct": username},
                timeout=self._timeout,
            )
            lookup_resp.raise_for_status()
            account = lookup_resp.json()
            account_id = account["id"]
            display_name = account.get("display_name") or username

            statuses_resp = requests.get(
                f"{base}/api/v1/accounts/{account_id}/statuses",
                params={"limit": max_posts, "exclude_replies": True},
                timeout=self._timeout,
            )
            statuses_resp.raise_for_status()

            tweets = []
            for status in statuses_resp.json():
                text = re.sub(r"<[^>]+>", " ", status.get("content", "")).strip()
                text = re.sub(r"\s+", " ", text)
                if not text:
                    continue
                tweets.append(_post_to_tweet_dict(
                    text=text,
                    username=username,
                    display_name=display_name,
                    date_iso=status.get("created_at", ""),
                    link=status.get("url", ""),
                    platform="mastodon",
                ))

            return SnsCrawlResult(
                target=username,
                tweets=tweets,
                meta={
                    "method": "mastodon_api",
                    "instance_used": instance,
                    "crawled_at": _now_iso(),
                },
            )
        except Exception as e:
            logger.warning("Mastodon 수집 실패 @%s@%s: %s", username, instance, e)
            return SnsCrawlResult(
                target=username,
                tweets=[],
                meta={
                    "method": "mastodon_api",
                    "instance_used": instance,
                    "error": str(e),
                    "crawled_at": _now_iso(),
                },
            )


class BlueskyCrawler:
    """Bluesky AT Protocol 크롤러. atproto SDK 사용, 검색은 로그인 필수."""

    def __init__(self):
        self._client = None

    def login(self, identifier: str, app_password: str) -> bool:
        if not (identifier and app_password):
            logger.warning("BLUESKY_IDENTIFIER/BLUESKY_APP_PASSWORD 미설정 — Bluesky 수집 건너뜀")
            return False
        try:
            from atproto import Client
            client = Client()
            client.login(identifier, app_password)
            self._client = client
            return True
        except Exception as e:
            logger.error("Bluesky 로그인 실패: %s", e)
            return False

    def crawl_account(self, handle: str, max_posts: int = 20) -> SnsCrawlResult:
        if not self._client:
            return SnsCrawlResult(target=handle, meta={"error": "not_logged_in"})
        try:
            feed = self._client.get_author_feed(actor=handle, limit=max_posts)
            tweets = []
            for item in feed.feed:
                post = item.post
                record = post.record
                link = f"https://bsky.app/profile/{handle}/post/{post.uri.rsplit('/', 1)[-1]}"
                display_name = getattr(post.author, "display_name", None) or handle
                tweets.append(_post_to_tweet_dict(
                    text=getattr(record, "text", ""),
                    username=handle,
                    display_name=display_name,
                    date_iso=getattr(record, "created_at", ""),
                    link=link,
                    platform="bluesky",
                ))
            return SnsCrawlResult(
                target=handle,
                tweets=tweets,
                meta={"method": "atproto_feed", "crawled_at": _now_iso()},
            )
        except Exception as e:
            logger.warning("Bluesky 계정 수집 실패 %s: %s", handle, e)
            return SnsCrawlResult(
                target=handle,
                tweets=[],
                meta={"method": "atproto_feed", "error": str(e), "crawled_at": _now_iso()},
            )

    def search_keyword(self, keyword: str, max_posts: int = 20) -> SnsCrawlResult:
        if not self._client:
            return SnsCrawlResult(target=keyword, meta={"error": "not_logged_in"})
        try:
            resp = self._client.app.bsky.feed.search_posts(params={"q": keyword, "limit": max_posts})
            tweets = []
            for post in resp.posts:
                record = post.record
                handle = post.author.handle
                link = f"https://bsky.app/profile/{handle}/post/{post.uri.rsplit('/', 1)[-1]}"
                display_name = getattr(post.author, "display_name", None) or handle
                tweets.append(_post_to_tweet_dict(
                    text=getattr(record, "text", ""),
                    username=handle,
                    display_name=display_name,
                    date_iso=getattr(record, "created_at", ""),
                    link=link,
                    platform="bluesky",
                ))
            return SnsCrawlResult(
                target=keyword,
                tweets=tweets,
                meta={"method": "atproto_search", "crawled_at": _now_iso()},
            )
        except Exception as e:
            logger.warning("Bluesky 키워드 검색 실패 '%s': %s", keyword, e)
            return SnsCrawlResult(
                target=keyword,
                tweets=[],
                meta={"method": "atproto_search", "error": str(e), "crawled_at": _now_iso()},
            )
