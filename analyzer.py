"""
크롤링 결과 분석기.

기능:
  1. 수집 요약 출력 (계정별 / 키워드별 트윗 수, 상위 사용자 등)
  2. 트렌드 분석 (단어 빈도, 핵심 키워드, 해시태그, 멘션)
  3. LLM 기반 정리 (ENABLE_LLM=True + OPENAI_API_KEY 환경변수 설정 시 활성화)

LLM 기능은 현재 off 상태로 구현되어 있으며,
추후 OPENAI_API_KEY 설정 시 자동으로 활성화됩니다.
"""

import json
import logging
import os
import re
import string
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 트렌드 분석에서 제외할 불용어
STOPWORDS: set[str] = {
    # 영어
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their", "what", "which",
    "who", "how", "when", "where", "why", "if", "then", "than", "so",
    "as", "not", "no", "up", "out", "about", "just", "more", "also",
    "new", "can", "get", "all", "one", "into", "its", "via", "now",
    "rt", "amp", "https", "http", "co",
    # 한국어 조사/접속사
    "이", "가", "을", "를", "은", "는", "의", "에", "서", "도", "로", "으로",
    "와", "과", "이나", "나", "하고", "에서", "에게", "한테", "부터", "까지",
    "만", "도", "라도", "이라도", "에도", "하다", "했다", "한다", "합니다",
    "있다", "없다", "있어", "없어", "입니다", "이다",
}


@dataclass
class SampleTweet:
    """텔레그램 알림용 샘플 트윗 정보."""
    text: str
    username: str
    link: str       # https://twitter.com/... 원본 트위터 링크
    date: str


@dataclass
class TweetSummary:
    """단일 타겟(키워드 또는 계정)에 대한 수집 요약."""
    target: str
    target_type: str  # "keyword" | "account"
    group: str
    tweet_count: int
    thread_count: int
    method: str  # "ntscraper" | "playwright"
    instance_used: str
    crawled_at: str
    top_users: list[tuple[str, int]] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    sample_texts: list[str] = field(default_factory=list)   # 하위호환 유지
    sample_tweets: list[SampleTweet] = field(default_factory=list)  # 링크 포함


@dataclass
class TrendReport:
    """전체 크롤링 결과에 대한 트렌드 리포트."""
    generated_at: str
    total_tweets: int
    total_targets: int
    keyword_summary: list[TweetSummary]
    account_summary: list[TweetSummary]
    top_words: list[tuple[str, int]]
    top_hashtags: list[tuple[str, int]]
    top_mentions: list[tuple[str, int]]
    top_users: list[tuple[str, int]]
    llm_summary: str = ""  # LLM 활성화 시 채워짐


class Analyzer:
    """크롤링 결과를 분석하여 요약 및 트렌드 리포트를 생성합니다."""

    def __init__(self, data_dir: Path = Path("data")):
        self._data_dir = data_dir

    # ─── 공개 API ────────────────────────────────────────────────────────────

    def analyze(
        self,
        crawl_results: list[dict],
    ) -> TrendReport:
        """
        크롤링 결과 리스트를 받아 TrendReport를 생성합니다.

        Args:
            crawl_results: [{"target": str, "type": "keyword"|"account",
                             "group": str, "data": CrawlResult.toDict()}, ...]
        """
        all_tweets: list[dict] = []
        keyword_summaries: list[TweetSummary] = []
        account_summaries: list[TweetSummary] = []

        for item in crawl_results:
            data = item.get("data", {})
            tweets = data.get("tweets", [])
            threads = data.get("threads", [])
            meta = data.get("meta", {})
            target = item.get("target", "")
            target_type = item.get("type", "keyword")
            group = item.get("group", "")

            all_tweets.extend(tweets)

            summary = TweetSummary(
                target=target,
                target_type=target_type,
                group=group,
                tweet_count=len(tweets),
                thread_count=len(threads),
                method=meta.get("method", "?"),
                instance_used=meta.get("instance_used", "?"),
                crawled_at=meta.get("crawled_at", ""),
                top_users=self._top_users(tweets, n=3),
                hashtags=self._extract_hashtags(tweets)[:5],
                sample_texts=[t.get("text", "")[:100] for t in tweets[:2]],
                sample_tweets=[
                    SampleTweet(
                        text=t.get("text", "")[:150],
                        username=t.get("user", {}).get("username", "").lstrip("@"),
                        link=t.get("link", ""),
                        date=t.get("date", ""),
                    )
                    for t in tweets[:3]
                    if t.get("link")  # 링크 있는 것만
                ],
            )

            if target_type == "keyword":
                keyword_summaries.append(summary)
            else:
                account_summaries.append(summary)

        # 전체 트렌드 분석
        top_words = self._top_words(all_tweets, n=20)
        top_hashtags = self._top_hashtags_global(all_tweets, n=10)
        top_mentions = self._top_mentions(all_tweets, n=10)
        top_users = self._top_users(all_tweets, n=10)

        report = TrendReport(
            generated_at=datetime.now().isoformat(),
            total_tweets=len(all_tweets),
            total_targets=len(crawl_results),
            keyword_summary=keyword_summaries,
            account_summary=account_summaries,
            top_words=top_words,
            top_hashtags=top_hashtags,
            top_mentions=top_mentions,
            top_users=top_users,
        )

        # llm_summary는 main.py에서 --llm 옵션 시 외부에서 채워짐
        report.llm_summary = ""
        return report

    # ─── 텍스트 분석 헬퍼 ────────────────────────────────────────────────────

    def _clean_text(self, text: str) -> list[str]:
        """트윗 텍스트를 토큰화하고 불용어를 제거합니다."""
        # URL 제거
        text = re.sub(r"https?://\S+", "", text)
        # 특수문자 제거 (한글/영문/숫자만 유지)
        text = re.sub(r"[^\w\s가-힣]", " ", text)
        tokens = text.lower().split()
        return [
            t for t in tokens
            if t not in STOPWORDS and len(t) > 1 and not t.isdigit()
        ]

    def _top_words(self, tweets: list[dict], n: int = 20) -> list[tuple[str, int]]:
        counter: Counter = Counter()
        for t in tweets:
            tokens = self._clean_text(t.get("text", ""))
            counter.update(tokens)
        return counter.most_common(n)

    def _extract_hashtags(self, tweets: list[dict]) -> list[str]:
        tags: list[str] = []
        for t in tweets:
            found = re.findall(r"#(\w+)", t.get("text", ""))
            tags.extend(found)
        return tags

    def _top_hashtags_global(self, tweets: list[dict], n: int = 10) -> list[tuple[str, int]]:
        counter: Counter = Counter()
        for t in tweets:
            tags = re.findall(r"#(\w+)", t.get("text", ""))
            counter.update(tag.lower() for tag in tags)
        return counter.most_common(n)

    def _top_mentions(self, tweets: list[dict], n: int = 10) -> list[tuple[str, int]]:
        counter: Counter = Counter()
        for t in tweets:
            mentions = re.findall(r"@(\w+)", t.get("text", ""))
            counter.update(m.lower() for m in mentions)
        return counter.most_common(n)

    def _top_users(self, tweets: list[dict], n: int = 10) -> list[tuple[str, int]]:
        counter: Counter = Counter()
        for t in tweets:
            username = t.get("user", {}).get("username", "")
            if username:
                counter[username] += 1
        return counter.most_common(n)

    # ─── LLM 분석 (추후 활성화) ──────────────────────────────────────────────

    # ─── 리포트 출력 ─────────────────────────────────────────────────────────

    def print_report(self, report: TrendReport) -> None:
        """TrendReport를 콘솔에 보기 좋게 출력합니다."""
        sep = "=" * 65

        print(f"\n{sep}")
        print(f"  [수집 요약]  {report.generated_at[:19]}")
        print(f"  총 트윗: {report.total_tweets}개 / 총 타겟: {report.total_targets}개")
        print(sep)

        # 키워드별 요약
        if report.keyword_summary:
            print("\n[키워드 검색 결과]")
            print(f"  {'키워드':<22} {'그룹':<20} {'트윗':>5}  방법")
            print(f"  {'-'*22} {'-'*20} {'-'*5}  {'-'*10}")
            for s in report.keyword_summary:
                kw = s.target[:21]
                grp = s.group[:19]
                print(f"  {kw:<22} {grp:<20} {s.tweet_count:>5}  {s.method}")

        # 계정별 요약
        if report.account_summary:
            print("\n[계정 수집 결과]")
            print(f"  {'계정':<22} {'그룹':<20} {'트윗':>5}  방법")
            print(f"  {'-'*22} {'-'*20} {'-'*5}  {'-'*10}")
            for s in report.account_summary:
                print(f"  @{s.target:<21} {s.group:<20} {s.tweet_count:>5}  {s.method}")

        # 트렌드 분석
        print(f"\n{sep}")
        print("  [트렌드 분석]")
        print(sep)

        if report.top_words:
            print("\n  상위 키워드 (빈도순):")
            words_line = "  " + "  |  ".join(
                f"{w} ({c})" for w, c in report.top_words[:10]
            )
            print(words_line)

        if report.top_hashtags:
            print("\n  상위 해시태그:")
            tags_line = "  " + "  ".join(
                f"#{t}({c})" for t, c in report.top_hashtags[:8]
            )
            print(tags_line)

        if report.top_mentions:
            print("\n  상위 멘션:")
            mentions_line = "  " + "  ".join(
                f"@{m}({c})" for m, c in report.top_mentions[:8]
            )
            print(mentions_line)

        if report.top_users:
            print("\n  가장 많이 트윗한 계정:")
            for user, cnt in report.top_users[:5]:
                print(f"    @{user}: {cnt}개")

        # LLM 요약
        print(f"\n{sep}")
        print("  [LLM 위협 인텔리전스 요약]")
        print(sep)
        if report.llm_summary:
            for line in report.llm_summary.splitlines():
                print(f"  {line}")
        else:
            print("  비활성화 상태 — python main.py --once --llm 으로 활성화")
            print("  지원 백엔드: gemini (무료) / openai / grok")
            print("  .env 에 GEMINI_API_KEY / OPENAI_API_KEY / GROK_API_KEY 추가 필요")

        print(f"\n{sep}\n")

    def save_report(self, report: TrendReport, out_dir: Path = Path("data")) -> Path:
        """리포트를 JSON으로 저장합니다."""
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out_dir / f"_report_{ts}.json"

        def to_serializable(obj):
            if isinstance(obj, TrendReport):
                return {
                    "generated_at": obj.generated_at,
                    "total_tweets": obj.total_tweets,
                    "total_targets": obj.total_targets,
                    "keyword_summary": [to_serializable(s) for s in obj.keyword_summary],
                    "account_summary": [to_serializable(s) for s in obj.account_summary],
                    "top_words": obj.top_words,
                    "top_hashtags": obj.top_hashtags,
                    "top_mentions": obj.top_mentions,
                    "top_users": obj.top_users,
                    "llm_summary": obj.llm_summary,
                }
            if isinstance(obj, TweetSummary):
                return {
                    "target": obj.target,
                    "type": obj.target_type,
                    "group": obj.group,
                    "tweet_count": obj.tweet_count,
                    "thread_count": obj.thread_count,
                    "method": obj.method,
                    "instance_used": obj.instance_used,
                    "crawled_at": obj.crawled_at,
                    "top_users": obj.top_users,
                    "hashtags": obj.hashtags,
                    "sample_texts": obj.sample_texts,
                    "sample_tweets": [
                        {"text": t.text, "username": t.username,
                         "link": t.link, "date": t.date}
                        for t in obj.sample_tweets
                    ],
                }
            if isinstance(obj, SampleTweet):
                return {"text": obj.text, "username": obj.username,
                        "link": obj.link, "date": obj.date}
            return obj

        with open(path, "w", encoding="utf-8") as f:
            json.dump(to_serializable(report), f, ensure_ascii=False, indent=2)

        return path
