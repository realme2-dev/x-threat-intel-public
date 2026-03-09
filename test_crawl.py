"""
Docker 환경에서 X(트위터) 크롤링 동작을 검증하는 테스트 스크립트.

실행: python test_crawl.py [--account ACCOUNT] [--mode user|term|hashtag] [--max N]
"""

import argparse
import io
import json
import logging
import os
import sys

# Windows cp949 인코딩 문제 우회
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('cp949', 'cp1252', 'ascii'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 로깅 기본 설정 (x_crawler 임포트 전) ──────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
# 노이즈 억제
for _lib in ["ntscraper", "urllib3", "requests", "playwright", "asyncio", "apscheduler"]:
    logging.getLogger(_lib).setLevel(logging.ERROR)

logger = logging.getLogger("test_crawl")

# ─── x_crawler 컴포넌트 임포트 ────────────────────────────────────────────
try:
    from x_crawler import (
        AntiBot,
        InstanceManager,
        TweetParser,
        XCrawler,
        Storage,
        FALLBACK_INSTANCES,
        setupLogging,
    )
except ImportError as e:
    logger.error("x_crawler.py 임포트 실패: %s", e)
    sys.exit(1)


# ─── 테스트 함수 ──────────────────────────────────────────────────────────

def test_instance_health(manager: InstanceManager) -> list[str]:
    """Nitter 인스턴스 헬스체크 테스트."""
    print("\n" + "=" * 60)
    print("  [1/4] Nitter 인스턴스 헬스체크")
    print("=" * 60)

    start = time.time()
    working = manager.refresh()
    elapsed = time.time() - start

    print(f"  헬스체크 완료: {len(working)}개 활성 / {elapsed:.1f}초 소요")
    for url in working[:5]:
        print(f"    ✓ {url}")
    if len(working) > 5:
        print(f"    ... 외 {len(working) - 5}개")

    if not working:
        print("  ✗ 활성 인스턴스 없음 — 폴백 목록 사용")
        return FALLBACK_INSTANCES[:3]

    return working


def test_single_crawl(
    crawler: XCrawler,
    storage: Storage,
    account: str,
    save: bool = True,
) -> dict | None:
    """단일 계정 크롤링 테스트."""
    print("\n" + "=" * 60)
    print(f"  [2/4] 크롤링 테스트 — 계정: @{account}")
    print("=" * 60)

    start = time.time()
    try:
        result = crawler.crawl(account)
        elapsed = time.time() - start

        print(f"  ✓ 완료: 트윗 {result.tweetCount}개 / 스레드 {len(result.threads)}개 / {elapsed:.1f}초")
        print(f"  사용 인스턴스: {result.meta.get('instance_used', 'N/A')}")
        print(f"  크롤링 방법:   {result.meta.get('method', 'N/A')}")

        if result.tweets:
            print("\n  ─── 최신 트윗 미리보기 (최대 3개) ───")
            for i, tweet in enumerate(result.tweets[:3], 1):
                text = tweet.get("text", "")[:120].replace("\n", " ")
                date = tweet.get("date", "N/A")
                link = tweet.get("link", "")
                print(f"\n  [{i}] {date}")
                print(f"      {text}")
                if link:
                    print(f"      🔗 {link}")

        if save and not result.isEmpty:
            saved = storage.save(account, result.toDict())
            print(f"\n  💾 저장 완료: {saved}")

        return result.toDict()

    except Exception as e:
        elapsed = time.time() - start
        print(f"  ✗ 크롤링 실패 ({elapsed:.1f}초): {e}")
        logger.debug("크롤링 오류 상세:", exc_info=True)
        return None


def test_parser(html_sample: str | None = None) -> bool:
    """TweetParser 단독 동작 확인."""
    print("\n" + "=" * 60)
    print("  [3/4] TweetParser 단위 테스트")
    print("=" * 60)

    parser = TweetParser()

    # 최소 HTML로 파싱이 크래시하지 않는지 확인
    dummy_html = html_sample or """
    <html><body>
      <div class="timeline-item">
        <a class="fullname">Test User</a>
        <a class="username">@testuser</a>
        <div class="tweet-content"><div class="media-body">테스트 트윗 내용입니다.</div></div>
        <span class="tweet-date"><a href="/testuser/status/123456" title="Jan 1, 2025 12:00 PM">1h</a></span>
      </div>
    </body></html>
    """

    try:
        tweets, threads = parser.parse_timeline(dummy_html, maxTweets=10)
        print(f"  ✓ 파싱 성공: 트윗 {len(tweets)}개, 스레드 {len(threads)}개")
        if tweets:
            print(f"    샘플: {tweets[0].get('text', '')[:80]}")
        return True
    except Exception as e:
        print(f"  ✗ 파싱 실패: {e}")
        return False


def test_playwright_available() -> bool:
    """Playwright 설치 및 Chromium 실행 가능 여부 확인."""
    print("\n" + "=" * 60)
    print("  [4/4] Playwright 브라우저 가용성 확인")
    print("=" * 60)

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.set_content("<html><body><h1>Hello Playwright</h1></body></html>")
            title = page.inner_text("h1")
            browser.close()

        print(f"  ✓ Chromium 정상 동작: '{title}'")
        return True

    except Exception as e:
        print(f"  ✗ Playwright 실패: {e}")
        logger.debug("Playwright 오류:", exc_info=True)
        return False


def print_summary(results: dict) -> None:
    """테스트 요약 출력."""
    print("\n" + "=" * 60)
    print("  테스트 요약")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("  모든 테스트 통과!")
    else:
        print("  일부 테스트 실패 — 위 로그를 확인하세요.")
    print("=" * 60)


# ─── CLI 진입점 ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="X 크롤러 Docker 테스트")
    p.add_argument(
        "--account", "-a",
        default=os.getenv("TEST_ACCOUNT", "DarkWebInformer"),
        help="테스트할 X 계정 (기본: DarkWebInformer)",
    )
    p.add_argument(
        "--mode", "-m",
        default=os.getenv("SEARCH_MODE", "user"),
        choices=["user", "term", "hashtag"],
        help="검색 모드 (기본: user)",
    )
    p.add_argument(
        "--max", "-n",
        type=int,
        default=int(os.getenv("MAX_TWEETS", "5")),
        help="수집할 최대 트윗 수 (기본: 5)",
    )
    p.add_argument(
        "--skip-playwright",
        action="store_true",
        default=os.getenv("SKIP_PLAYWRIGHT", "").lower() in ("1", "true", "yes"),
        help="Playwright 테스트 건너뛰기",
    )
    p.add_argument(
        "--no-save",
        action="store_true",
        help="크롤링 결과를 파일로 저장하지 않음",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print("\n" + "=" * 60)
    print("  X 크롤러 Docker 테스트")
    print(f"  실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  계정:     @{args.account}")
    print(f"  모드:      {args.mode}")
    print(f"  최대 트윗: {args.max}개")
    print("=" * 60)

    # 컴포넌트 초기화
    manager = InstanceManager()
    anti_bot = AntiBot()
    parser_obj = TweetParser()
    crawler = XCrawler(
        instanceManager=manager,
        antiBot=anti_bot,
        parser=parser_obj,
        maxTweets=args.max,
        searchMode=args.mode,
    )
    storage = Storage(Path("data"))

    results: dict[str, bool] = {}

    # 1. 인스턴스 헬스체크
    working = test_instance_health(manager)
    results["인스턴스 헬스체크"] = len(working) > 0

    # 2. 크롤링 테스트
    crawl_data = test_single_crawl(crawler, storage, args.account, save=not args.no_save)
    results["크롤링 (ntscraper / playwright)"] = crawl_data is not None

    # 3. 파서 단위 테스트
    results["TweetParser 단위 테스트"] = test_parser()

    # 4. Playwright 가용성 확인
    if not args.skip_playwright:
        results["Playwright Chromium"] = test_playwright_available()
    else:
        print("\n  [4/4] Playwright 테스트 건너뜀 (--skip-playwright)")

    print_summary(results)

    # 실패한 테스트가 있으면 비정상 종료 코드 반환
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
