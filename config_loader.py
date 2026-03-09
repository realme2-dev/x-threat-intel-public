"""
config_v2.json 로더.
키워드 그룹 / 계정 그룹을 파싱하고 활성화된 타겟만 반환합니다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config_v2.json"


@dataclass
class Settings:
    max_tweets_per_target: int = 20
    request_delay_min: float = 2.0
    request_delay_max: float = 5.0
    retry_max_attempts: int = 3
    retry_base_delay: float = 5.0
    playwright_wait_sec: int = 10
    schedule_interval_hours: float = 12.0


@dataclass
class KeywordGroup:
    name: str
    description: str
    enabled: bool
    keywords: list[str]


@dataclass
class AccountGroup:
    name: str
    description: str
    enabled: bool
    usernames: list[str]


@dataclass
class RssFeed:
    name: str
    url: str


@dataclass
class RssFeedGroup:
    name: str
    description: str
    enabled: bool
    feeds: list[RssFeed]


@dataclass
class Config:
    settings: Settings
    keyword_groups: list[KeywordGroup]
    account_groups: list[AccountGroup]
    rss_feed_groups: list[RssFeedGroup] = field(default_factory=list)

    @property
    def active_keywords(self) -> list[str]:
        """활성화된 그룹의 중복 제거 키워드 목록."""
        seen: set[str] = set()
        result: list[str] = []
        for group in self.keyword_groups:
            if group.enabled:
                for kw in group.keywords:
                    if kw not in seen:
                        seen.add(kw)
                        result.append(kw)
        return result

    @property
    def active_rss_feeds(self) -> list[RssFeed]:
        """활성화된 그룹의 RSS 피드 목록."""
        result: list[RssFeed] = []
        for group in self.rss_feed_groups:
            if group.enabled:
                result.extend(group.feeds)
        return result

    @property
    def active_accounts(self) -> list[str]:
        """활성화된 그룹의 중복 제거 계정 목록."""
        seen: set[str] = set()
        result: list[str] = []
        for group in self.account_groups:
            if group.enabled:
                for u in group.usernames:
                    if u not in seen:
                        seen.add(u)
                        result.append(u)
        return result

    def keyword_group_by_keyword(self, keyword: str) -> str:
        """키워드가 속한 그룹명 반환."""
        for group in self.keyword_groups:
            if keyword in group.keywords:
                return group.name
        return "unknown"


def load_config(path: Path = CONFIG_PATH) -> Config:
    """config_v2.json을 파싱하여 Config 객체 반환."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    # settings
    s = raw.get("settings", {})
    settings = Settings(
        max_tweets_per_target=s.get("max_tweets_per_target", 20),
        request_delay_min=s.get("request_delay_min", 2.0),
        request_delay_max=s.get("request_delay_max", 5.0),
        retry_max_attempts=s.get("retry_max_attempts", 3),
        retry_base_delay=s.get("retry_base_delay", 5.0),
        playwright_wait_sec=s.get("playwright_wait_sec", 10),
        schedule_interval_hours=s.get("schedule_interval_hours", 12.0),
    )

    # keyword_groups
    keyword_groups: list[KeywordGroup] = []
    for name, group in raw.get("keyword_groups", {}).items():
        if name.startswith("_"):
            continue
        keyword_groups.append(KeywordGroup(
            name=name,
            description=group.get("description", ""),
            enabled=group.get("enabled", True),
            keywords=group.get("keywords", []),
        ))

    # account_groups
    account_groups: list[AccountGroup] = []
    for name, group in raw.get("accounts", {}).items():
        if name.startswith("_"):
            continue
        account_groups.append(AccountGroup(
            name=name,
            description=group.get("description", ""),
            enabled=group.get("enabled", True),
            usernames=group.get("usernames", []),
        ))

    # rss_feeds
    rss_feed_groups: list[RssFeedGroup] = []
    for name, group in raw.get("rss_feeds", {}).items():
        if name.startswith("_"):
            continue
        feeds = [
            RssFeed(name=f.get("name", ""), url=f.get("url", ""))
            for f in group.get("feeds", [])
            if f.get("url")
        ]
        rss_feed_groups.append(RssFeedGroup(
            name=name,
            description=group.get("description", ""),
            enabled=group.get("enabled", True),
            feeds=feeds,
        ))

    return Config(
        settings=settings,
        keyword_groups=keyword_groups,
        account_groups=account_groups,
        rss_feed_groups=rss_feed_groups,
    )
