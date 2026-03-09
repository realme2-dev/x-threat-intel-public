"""
보안 뉴스/블로그 RSS 수집기.

피드 목록은 config_v2.json의 rss_feeds 섹션에서 관리합니다.
그룹별로 enabled 설정으로 활성화/비활성화 가능합니다.

사용 예:
  from rss_collector import collect_rss_news
  articles = collect_rss_news(feeds=config.active_rss_feeds)
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


@dataclass
class NewsArticle:
    title: str
    link: str
    summary: str
    source: str
    published_kst: str   # "26/03/09 14:20 KST"


def _parse_date(entry) -> str:
    """feedparser 엔트리에서 KST 날짜 문자열을 반환합니다."""
    try:
        if hasattr(entry, "published") and entry.published:
            dt = parsedate_to_datetime(entry.published)
            return dt.astimezone(KST).strftime("%y/%m/%d %H:%M KST")
    except Exception:
        pass
    return datetime.now(KST).strftime("%y/%m/%d %H:%M KST")


def collect_rss_news(
    feeds,                  # list[RssFeed] — config.active_rss_feeds
    max_per_feed: int = 3,
) -> list[NewsArticle]:
    """
    RSS 피드에서 최신 기사를 수집합니다.

    Args:
        feeds: config.active_rss_feeds (RssFeed 목록)
        max_per_feed: 피드당 최대 수집 기사 수

    Returns:
        NewsArticle 목록
    """
    articles: list[NewsArticle] = []

    for feed_info in feeds:
        name = feed_info.name
        url = feed_info.url
        try:
            feed = feedparser.parse(
                url,
                request_headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SecurityBot/1.0)",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
            )
            if not feed.entries:
                logger.warning("RSS 항목 없음 [%s]%s", name,
                               f": {feed.bozo_exception}" if feed.bozo else "")
                continue

            count = 0
            for entry in feed.entries[:max_per_feed]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                summary = re.sub(r"<[^>]+>", "", summary)[:300].strip()

                if not title or not link:
                    continue

                articles.append(NewsArticle(
                    title=title,
                    link=link,
                    summary=summary,
                    source=name,
                    published_kst=_parse_date(entry),
                ))
                count += 1

            logger.info("RSS 수집 [%s]: %d개", name, count)

        except Exception as e:
            logger.error("RSS 수집 오류 [%s]: %s", name, e)

    return articles


def format_articles_for_llm(articles: list[NewsArticle]) -> str:
    """LLM 프롬프트에 포함할 뉴스 기사 텍스트를 생성합니다."""
    if not articles:
        return ""
    lines = []
    for a in articles:
        lines.append(f"[{a.source}] {a.published_kst}")
        lines.append(f"제목: {a.title}")
        if a.summary:
            lines.append(f"요약: {a.summary[:200]}")
        lines.append(f"링크: {a.link}")
        lines.append("")
    return "\n".join(lines)


def _extract_keywords(text: str) -> list[str]:
    """제목+요약에서 주요 보안 키워드 추출 (CVE + 대문자 고유명사)."""
    keywords: list[str] = []
    # CVE 패턴 우선
    cves = re.findall(r"CVE-\d{4}-\d+", text)
    keywords.extend(cves)
    # 대문자 시작 고유명사 (2글자 이상 연속)
    _skip = {"The", "This", "That", "With", "From", "When", "Where",
             "What", "How", "Also", "More", "Than", "After", "Before", "New"}
    proper = re.findall(r"\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})*\b", text)
    for p in proper:
        if p not in _skip:
            keywords.append(p)
    # 중복 제거 (순서 유지)
    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def format_articles_for_telegram(articles: list[NewsArticle], max_items: int = 8) -> str:
    """텔레그램 알림용 뉴스 기사 섹션을 생성합니다.

    포맷 (출처별 묶음):
      *A블로그*
      [제목](링크)  날짜
      내용 2~3줄
      키워드: 단어1, 단어2

      [제목](링크)  날짜
      내용 2~3줄
      키워드: 단어1, 단어2

      *B뉴스*
      ...
    """
    if not articles:
        return ""

    # 출처별로 묶기 (순서 유지)
    from collections import OrderedDict
    grouped: dict[str, list[NewsArticle]] = OrderedDict()
    for a in articles[:max_items]:
        grouped.setdefault(a.source, []).append(a)

    lines = ["*[보안 뉴스/블로그]*"]

    for source, items in grouped.items():
        lines.append(f"\n*{source}*")
        for a in items:
            title_esc = a.title.replace("*", "\\*").replace("_", "\\_").replace("[", "\\[")
            lines.append(f"[{title_esc}]({a.link})")
            lines.append(f"🕐 {a.published_kst}")
            if a.summary:
                lines.append(a.summary[:150].strip())
            kws = _extract_keywords(a.title + " " + a.summary)
            if kws:
                lines.append(f"🔑 {', '.join(kws[:3])}")
            lines.append("")  # 기사 간 빈 줄

    return "\n".join(lines).rstrip()
