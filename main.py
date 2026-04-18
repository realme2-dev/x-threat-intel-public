"""
X 크롤러 메인 오케스트레이터.

config_v2.json을 읽어 활성화된 키워드 / 계정을 크롤링하고,
결과를 요약·분석·텔레그램으로 전송합니다.

실행:
  python main.py                             # 스케줄 모드 (config 설정 interval)
  python main.py --once                      # 1회 실행 후 종료
  python main.py --once --keywords-only      # 키워드만 크롤링
  python main.py --once --accounts-only      # 계정만 크롤링
  python main.py --once --group security_threats  # 특정 그룹만
  python main.py --once --workers 4          # 멀티스레드 4개로 병렬 크롤링
  python main.py --once --llm                # LLM 분석 활성화
  python main.py --once --llm --llm-backend gemini  # Gemini로 분석
"""

import argparse
import io
import logging
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 인코딩 처리
if sys.stdout.encoding and sys.stdout.encoding.lower() in ("cp949", "cp1252", "ascii"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# .env 파일 자동 로드
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")


from config_loader import load_config, Config
from analyzer import Analyzer
from notifier import TelegramNotifier
from llm_analyzer import run_llm_analysis, run_tweet_selection, list_available_backends, run_korea_tweet_filter, run_llm_compare
from rss_collector import collect_rss_news, format_articles_for_llm, format_articles_for_telegram

# x_crawler 컴포넌트
from x_crawler import (
    AntiBot,
    InstanceManager,
    TweetParser,
    XCrawler,
    Storage,
    NoInstanceError,
    AllInstancesFailedError,
    RetryError,
    CrawlError,
    REQUEST_DELAY_MIN,
    REQUEST_DELAY_MAX,
)

logger = logging.getLogger("main")

# 콘솔 출력 보호용 Lock (멀티스레드 환경에서 print 섞임 방지)
_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


# ─── 로깅 설정 ────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO") -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"crawler_{datetime.now().strftime('%Y%m%d')}.log"

    fmt = "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s"
    datefmt = "%H:%M:%S"

    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt, datefmt=datefmt, handlers=handlers,
    )
    for lib in ["ntscraper", "urllib3", "requests", "playwright",
                "asyncio", "apscheduler", "selenium"]:
        logging.getLogger(lib).setLevel(logging.ERROR)


# ─── 단일 타겟 크롤링 (스레드 작업 단위) ─────────────────────────────────────

def _crawl_one(
    target: str,
    target_type: str,       # "keyword" | "account"
    group_name: str,
    max_tweets: int,
    delay_min: float,
    delay_max: float,
    working_instances: list[str],
    storage: Storage,
    index: int,
    total: int,
) -> dict | None:
    """
    단일 키워드 또는 계정을 크롤링합니다.
    각 스레드에서 독립적인 AntiBot / XCrawler 인스턴스를 사용하므로 thread-safe합니다.
    InstanceManager는 읽기 전용(workingInstances)으로만 접근합니다.
    """
    # 스레드별 독립 컴포넌트 생성 (thread-safe)
    anti_bot = AntiBot(delayMin=delay_min, delayMax=delay_max)
    parser   = TweetParser()

    # 읽기 전용 InstanceManager — 스냅샷된 인스턴스 목록을 사용
    class _StaticInstanceMgr:
        """헬스체크 없이 스냅샷 목록만 반환하는 경량 InstanceManager."""
        def __init__(self, instances: list[str]):
            self._instances = instances
            self._failedInstances: set[str] = set()

        @property
        def workingInstances(self) -> list[str]:
            return [i for i in self._instances if i not in self._failedInstances]

        def reportFailure(self, url: str) -> None:
            self._failedInstances.add(url)

        def refresh(self) -> list[str]:
            return self.workingInstances

    static_mgr = _StaticInstanceMgr(working_instances)
    search_mode = "user" if target_type == "account" else "term"

    crawler = XCrawler(
        instanceManager=static_mgr,
        antiBot=anti_bot,
        parser=parser,
        maxTweets=max_tweets,
        searchMode=search_mode,
    )

    label = f"@{target}" if target_type == "account" else f"'{target}'"
    safe_print(f"  [{index:>3}/{total}] {label} ...", end=" ", flush=True)

    try:
        result = crawler.crawl(target)
        save_key = f"x_user/{target}" if target_type == "account" else f"_kw_{target[:30]}"
        storage.save(save_key, result.toDict())
        safe_print(f"트윗 {result.tweetCount}개 [{result.meta.get('method','?')}]")
        return {
            "target": target,
            "type": target_type,
            "group": group_name,
            "data": result.toDict(),
        }
    except (NoInstanceError, AllInstancesFailedError, RetryError, CrawlError) as e:
        safe_print(f"실패: {str(e)[:60]}")
        return None
    except Exception as e:
        safe_print(f"오류: {str(e)[:60]}")
        return None


# ─── 유틸 함수 ───────────────────────────────────────────────────────────────

def _deduplicate_tweets(crawl_results: list[dict]) -> list[dict]:
    """
    크롤링 결과에서 중복 트윗을 제거합니다.
    - 동일 link가 있으면 중복 처리
    - link가 없으면 텍스트 앞 80자가 같으면 중복 처리
    """
    seen_links: set[str] = set()
    seen_texts: set[str] = set()

    for item in crawl_results:
        data = item.get("data", {})
        tweets = data.get("tweets", [])
        unique = []
        for t in tweets:
            link = t.get("link", "").strip()
            text_key = t.get("text", "")[:80].strip().lower()
            if link:
                if link in seen_links:
                    continue
                seen_links.add(link)
            else:
                if text_key in seen_texts:
                    continue
                seen_texts.add(text_key)
            unique.append(t)
        data["tweets"] = unique
    return crawl_results


def _filter_tweets_by_date(
    crawl_results: list[dict],
    max_days: int = 2,
    today_only: bool = False,
) -> tuple[list[dict], int, int]:
    """
    수집 시점 기준 max_days일 이내 트윗만 남깁니다.
    today_only=True 시 KST 당일(00:00~) 트윗만 유지합니다.

    Nitter 날짜 포맷: "Mar 9, 2026 · 1:30 PM UTC" 또는 "Mar 9, 2026 · 13:30"
    파싱 실패한 트윗은 통과시킵니다 (보수적 처리).

    Returns:
        (필터링된 crawl_results, 제거된 트윗 수, 전체 트윗 수)
    """
    if today_only:
        KST = timezone(timedelta(hours=9))
        today_kst = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = today_kst.astimezone(timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    removed = 0
    total = 0

    _month_map = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def _parse_nitter_date(date_str: str) -> datetime | None:
        """Nitter 날짜 문자열을 UTC datetime으로 변환합니다."""
        # "Mar 9, 2026 · 1:30 PM UTC" 또는 "Mar 9, 2026 · 13:30"
        m = re.match(
            r"([A-Za-z]+)\s+(\d+),\s*(\d+)\s*[·\·]\s*(\d+:\d+)(?:\s*(AM|PM))?",
            date_str.strip()
        )
        if not m:
            return None
        try:
            month_str, day, year, time_str, ampm = m.groups()
            month = _month_map.get(month_str[:3].lower())
            if not month:
                return None
            hour, minute = map(int, time_str.split(":"))
            if ampm:
                if ampm.upper() == "PM" and hour != 12:
                    hour += 12
                elif ampm.upper() == "AM" and hour == 12:
                    hour = 0
            return datetime(int(year), month, int(day), hour, minute,
                            tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    for item in crawl_results:
        data = item.get("data", {})
        tweets = data.get("tweets", [])
        kept = []
        for t in tweets:
            total += 1
            date_str = t.get("date", "")
            dt = _parse_nitter_date(date_str) if date_str else None
            if dt is None or dt >= cutoff:
                kept.append(t)
            else:
                removed += 1
        data["tweets"] = kept
    return crawl_results, removed, total


def _extract_rising_keywords(
    top_words: list[tuple[str, int]],
    existing_keywords: set[str],
    top_n: int = 5,
) -> list[str]:
    """
    빈도 상위 단어 중 기존 키워드 목록에 없는 보안 관련 신규 키워드를 추출합니다.

    보안 관련성 판단 기준:
    - 4글자 이상 영문/한글 단어
    - 숫자만이거나 불용어가 아닌 것
    - 최소 빈도 3 이상
    """
    # 일반적인 불필요 단어 필터
    skip = {
        "com", "http", "https", "www", "via", "new", "amp",
        "the", "for", "and", "with", "this", "that", "have",
        "from", "not", "are", "was", "been", "will", "can",
        "보안", "사이버", "공격", "위협", "해킹", "정보",  # 이미 키워드에 포함
    }
    rising = []
    for word, count in top_words:
        if count < 3:
            continue
        wl = word.lower()
        if wl in skip:
            continue
        if wl in existing_keywords:
            continue
        if len(word) < 4:
            continue
        if word.isdigit():
            continue
        rising.append(word)
        if len(rising) >= top_n:
            break
    return rising


# ─── 크롤링 실행 핵심 ────────────────────────────────────────────────────────

def run_crawl_job(
    config: Config,
    keywords_only: bool = False,
    accounts_only: bool = False,
    group_filter: str | None = None,
    workers: int = 1,
    enable_llm: bool = False,
    llm_backend: str | None = None,
    compare_backends: list[str] | None = None,
    today_only: bool = False,
) -> None:
    """
    config를 참조하여 키워드/계정 크롤링을 실행하고
    분석 결과를 출력·저장·텔레그램으로 전송합니다.

    Args:
        workers: 동시 크롤링 스레드 수 (1=순차, 2이상=병렬)
                 Nitter 인스턴스 수를 초과하지 않도록 주의
    """
    start_time = datetime.now()
    sep = "=" * 65

    safe_print(f"\n{sep}")
    safe_print(f"  X 크롤러 시작  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    safe_print(f"  모드: {'병렬' if workers > 1 else '순차'} (workers={workers})")
    safe_print(sep)

    cfg = config.settings
    storage  = Storage(Path("data"))
    analyzer = Analyzer()
    notifier = TelegramNotifier()

    # ── 인스턴스 헬스체크 (메인 스레드에서 1회)
    safe_print("\n[1] Nitter 인스턴스 헬스체크 중...")
    instance_mgr = InstanceManager()
    working = instance_mgr.refresh()
    if not working:
        safe_print("  활성 인스턴스 없음 — 작업 중단")
        return
    safe_print(f"  활성 인스턴스: {len(working)}개 → {working}")


    # ── 크롤 대상 수집
    keyword_targets: list[tuple[str, str]] = []
    account_targets: list[tuple[str, str]] = []

    if not accounts_only:
        for group in config.keyword_groups:
            if not group.enabled:
                continue
            if group_filter and group.name != group_filter:
                continue
            for kw in group.keywords:
                keyword_targets.append((kw, group.name))

    if not keywords_only:
        for group in config.account_groups:
            if not group.enabled:
                continue
            if group_filter and group.name != group_filter:
                continue
            for username in group.usernames:
                account_targets.append((username, group.name))

    all_targets = (
        [(t, g, "keyword") for t, g in keyword_targets] +
        [(t, g, "account") for t, g in account_targets]
    )
    total = len(all_targets)
    safe_print(f"\n[2] 크롤링 시작: 키워드 {len(keyword_targets)}개 / 계정 {len(account_targets)}개 / workers={workers}")

    # ── 크롤링 (순차 or 병렬)
    crawl_results: list[dict] = []
    success_count = 0
    fail_count = 0

    common_kwargs = dict(
        delay_min=cfg.request_delay_min,
        delay_max=cfg.request_delay_max,
        working_instances=working,
        storage=storage,
        total=total,
    )

    if workers <= 1:
        # ── 순차 처리
        for i, (target, group_name, target_type) in enumerate(all_targets, 1):
            result = _crawl_one(
                target=target,
                target_type=target_type,
                group_name=group_name,
                max_tweets=config.max_tweets_for(target),
                index=i,
                **common_kwargs,
            )
            if result:
                crawl_results.append(result)
                success_count += 1
            else:
                fail_count += 1
    else:
        # ── 병렬 처리 (ThreadPoolExecutor)
        # workers를 인스턴스 수로 제한 (과도한 병렬은 차단 위험)
        effective_workers = min(workers, len(working), total)
        safe_print(f"  실제 병렬 workers: {effective_workers}개")

        futures_map = {}
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            for i, (target, group_name, target_type) in enumerate(all_targets, 1):
                future = executor.submit(
                    _crawl_one,
                    target=target,
                    target_type=target_type,
                    group_name=group_name,
                    max_tweets=config.max_tweets_for(target),
                    index=i,
                    **common_kwargs,
                )
                futures_map[future] = (target, target_type)

            for future in as_completed(futures_map):
                result = future.result()
                if result:
                    crawl_results.append(result)
                    success_count += 1
                else:
                    fail_count += 1

    elapsed = (datetime.now() - start_time).total_seconds()
    safe_print(f"\n[3] 크롤링 완료: {success_count}/{total} 성공 / {elapsed:.0f}초 소요")

    if not crawl_results:
        safe_print("  수집된 데이터 없음 — 분석 건너뜀")
        return

    # ── 중복 트윗 제거
    safe_print("\n[4] 중복 트윗 제거 중...")
    crawl_results = _deduplicate_tweets(crawl_results)
    total_after_dedup = sum(
        len(r.get("data", {}).get("tweets", [])) for r in crawl_results
    )
    safe_print(f"  중복 제거 후 총 트윗: {total_after_dedup}개")

    # ── 날짜 필터 (2일 이내만 유지)
    crawl_results, removed_count, pre_filter_total = _filter_tweets_by_date(
        crawl_results, max_days=2, today_only=today_only
    )
    total_after_filter = sum(
        len(r.get("data", {}).get("tweets", [])) for r in crawl_results
    )
    safe_print(f"  날짜 필터(2일 이내): {removed_count}개 제거 → {total_after_filter}개 유지")

    # ── 1차 분석 (급상승 키워드 추출용)
    report = analyzer.analyze(crawl_results)

    # ── 급상승 키워드 2차 수집
    safe_print("\n[5] 급상승 키워드 추출 및 2차 수집...")
    existing_keywords = set(kw.lower() for kw in config.active_keywords)
    rising_keywords = _extract_rising_keywords(
        report.top_words, existing_keywords, top_n=5
    )
    if rising_keywords:
        safe_print(f"  급상승 키워드: {rising_keywords}")
        rising_results = []
        for i, kw in enumerate(rising_keywords, 1):
            result = _crawl_one(
                target=kw,
                target_type="keyword",
                group_name="rising",
                max_tweets=cfg.max_tweets_per_target,
                delay_min=cfg.request_delay_min,
                delay_max=cfg.request_delay_max,
                working_instances=working,
                storage=storage,
                index=i,
                total=len(rising_keywords),
            )
            if result:
                rising_results.append(result)

        if rising_results:
            crawl_results.extend(rising_results)
            crawl_results = _deduplicate_tweets(crawl_results)
            report = analyzer.analyze(crawl_results)  # 재분석
            safe_print(f"  2차 수집 완료: {len(rising_results)}개 키워드")
    else:
        safe_print("  새로운 급상승 키워드 없음")

    # ── RSS 뉴스 수집
    safe_print("\n[6] RSS 보안 뉴스 수집 중...")
    active_feeds = config.active_rss_feeds
    safe_print(f"  활성 피드: {len(active_feeds)}개")
    news_articles = collect_rss_news(feeds=active_feeds, max_per_feed=3)
    news_llm_text = format_articles_for_llm(news_articles)
    news_telegram_text = format_articles_for_telegram(news_articles, max_items=8)
    safe_print(f"  RSS 기사: {len(news_articles)}개 수집")

    korea_tweets = []

    # ── LLM 분석 (--llm 옵션 시)
    if enable_llm:
        available = list_available_backends()
        backend_name = llm_backend or None

        if not available:
            safe_print("  [LLM] API 키 미설정 — GEMINI_API_KEY / OPENAI_API_KEY / GROK_API_KEY 중 하나를 .env에 추가하세요")
        else:
            safe_print(f"\n[7] LLM 분석 시작 (backend={backend_name or 'env기본값'}, 가용={available})")
            all_tweets = [
                t
                for item in crawl_results
                for t in item.get("data", {}).get("tweets", [])
            ]

            # 7-1. 위협 중요도 기준 주요 트윗 선별
            safe_print(f"  [7-1] 주요 트윗 선별 중 (전체 {len(all_tweets)}개)...")
            top_tweets = run_tweet_selection(tweets=all_tweets, backend_name=backend_name)
            report.llm_top_tweets = top_tweets
            if top_tweets:
                safe_print(f"  [7-1] 선별 완료: {len(top_tweets)}개")
            else:
                safe_print("  [7-1] 선별 실패 또는 결과 없음")

            # 7-2. 심층 위협 인텔리전스 분석
            safe_print("  [7-2] 심층 분석 중...")
            llm_result = run_llm_analysis(
                tweets=all_tweets,
                top_words=report.top_words,
                top_hashtags=report.top_hashtags,
                backend_name=backend_name,
                news_text=news_llm_text,
            )
            report.llm_summary = llm_result
            if llm_result:
                safe_print("  [7-2] 분석 완료")
            else:
                safe_print("  [7-2] 분석 실패 (로그 확인)")

            # 7-3. 한국 관련 위협 트윗 추출
            safe_print(f"  [7-3] 한국 관련 트윗 추출 중 (전체 {len(all_tweets)}개)...")
            korea_tweets = run_korea_tweet_filter(tweets=all_tweets, backend_name=backend_name)
            if korea_tweets:
                safe_print(f"  [7-3] 한국 관련 트윗 {len(korea_tweets)}개 발견")
            else:
                safe_print("  [7-3] 한국 관련 트윗 없음")

            # 7-4. LLM 비교 분석 (--compare-llm 시)
            if compare_backends:
                safe_print(f"\n  [7-4] LLM 비교 분석 중: {compare_backends}")
                compare_results = run_llm_compare(
                    tweets=all_tweets,
                    top_words=report.top_words,
                    top_hashtags=report.top_hashtags,
                    backends=compare_backends,
                    news_text=news_llm_text,
                )
                report.compare_results = compare_results
                safe_print(f"  [7-4] 비교 완료: {len(compare_results)}개 결과")

    analyzer.print_report(report)
    report_path = analyzer.save_report(report)
    safe_print(f"  리포트 저장: {report_path}")

    # ── 텔레그램 전송
    if notifier.enabled:
        safe_print("\n[8] 텔레그램 전송 중...")
        notify_result = notifier.send_report(report, news_text=news_telegram_text)
        if notify_result.success:
            safe_print(f"  전송 완료 (메시지 {len(notify_result.message_ids)}개)")
        else:
            safe_print(f"  전송 실패: {notify_result.error}")

        # 한국 관련 트윗 별도 전송
        if korea_tweets:
            safe_print("  한국 관련 트윗 전송 중...")
            korea_result = notifier.send_korea_alerts(korea_tweets)
            if korea_result.success:
                safe_print(f"  한국 알림 전송 완료 (메시지 {len(korea_result.message_ids)}개)")
            else:
                safe_print(f"  한국 알림 전송 실패: {korea_result.error}")

        # LLM 비교 결과 전송
        compare_results = getattr(report, "compare_results", [])
        if compare_results:
            safe_print(f"  LLM 비교 결과 전송 중 ({len(compare_results)}개)...")
            notifier.send_text("━━━━━━━━━━━━━━━━━━━━\n🔬 *LLM 비교 분석 결과*\n━━━━━━━━━━━━━━━━━━━━")
            for i, cmp_text in enumerate(compare_results, 1):
                notifier.send_text(cmp_text)
                safe_print(f"  비교 결과 {i}/{len(compare_results)} 전송 완료")
    else:
        safe_print("\n[8] 텔레그램 비활성화 (.env에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요)")


# ─── 결과 조회 ────────────────────────────────────────────────────────────────

def _show_results(keyword: str | None = None) -> None:
    """
    마지막 리포트의 키워드별 수집 결과를 출력합니다.

    Args:
        keyword: 특정 키워드 지정 시 해당 트윗 상세 출력. None이면 전체 요약.
    """
    import json as _json
    data_dir = Path("data")
    reports = sorted(data_dir.glob("_report_*.json"))
    if not reports:
        safe_print("저장된 리포트 없음. 먼저 크롤링을 실행해주세요.")
        return

    latest = reports[-1]
    with open(latest, encoding="utf-8") as f:
        report = _json.load(f)

    generated_at = report.get("generated_at", "")[:16].replace("T", " ")
    total_tweets = report.get("total_tweets", 0)
    safe_print(f"\n{'='*65}")
    safe_print(f"  마지막 리포트: {latest.name}  ({generated_at})")
    safe_print(f"  총 트윗: {total_tweets}개")
    safe_print(f"{'='*65}")

    summaries = report.get("keyword_summary", [])

    if keyword:
        # 특정 키워드 상세 조회
        kw_lower = keyword.lower()
        matched = [s for s in summaries if kw_lower in s.get("target", "").lower()]
        if not matched:
            safe_print(f"\n키워드 '{keyword}'에 해당하는 데이터 없음.")
            safe_print(f"등록된 키워드 목록: {', '.join(s['target'] for s in summaries)}")
            return
        for s in matched:
            target = s.get("target", "")
            count = s.get("tweet_count", 0)
            method = s.get("method", "")
            crawled_at = s.get("crawled_at", "")[:16].replace("T", " ")
            safe_print(f"\n[{target}]  {count}개  ({method})  수집: {crawled_at}")
            safe_print(f"  상위 유저: {', '.join(f'{u[0]}({u[1]})' for u in s.get('top_users', [])[:5])}")
            safe_print(f"  해시태그: {', '.join(s.get('hashtags', [])[:5])}")
            safe_print(f"\n  트윗 샘플:")
            for i, t in enumerate(s.get("sample_tweets", [])[:5], 1):
                date_str = t.get("date", "")[:16]
                user = t.get("username", "?")
                text = t.get("text", "")[:120].replace("\n", " ")
                link = t.get("link", "")
                safe_print(f"  [{i}] @{user}  ({date_str})")
                safe_print(f"      {text}")
                if link:
                    safe_print(f"      {link}")
    else:
        # 전체 키워드 요약
        safe_print(f"\n{'키워드':<25} {'트윗':>5} {'방법':<12} {'수집시각'}")
        safe_print("-" * 65)
        for s in summaries:
            target = s.get("target", "")[:24]
            count = s.get("tweet_count", 0)
            method = s.get("method", "ntscraper")
            crawled_at = s.get("crawled_at", "")[:16].replace("T", " ")
            safe_print(f"  {target:<23} {count:>5}  {method:<12} {crawled_at}")
        safe_print("-" * 65)
        safe_print(f"  총 {len(summaries)}개 키워드/계정  |  {total_tweets}개 트윗")
        safe_print(f"\n  특정 키워드 상세 조회: python main.py --show-keyword <키워드>")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="X 크롤러 메인",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py                                 # 전체 실행
  python main.py --workers 3                     # 3개 스레드 병렬 크롤링
  python main.py --llm                           # LLM 분석 포함
  python main.py --llm --llm-backend gemini      # Gemini로 분석
  python main.py --group security_threats        # 특정 그룹만
        """,
    )
    p.add_argument("--keywords-only", action="store_true", help="키워드 검색만 실행")
    p.add_argument("--accounts-only", action="store_true", help="계정 수집만 실행")
    p.add_argument("--group", default=None, metavar="GROUP",
                   help="특정 그룹 이름만 실행 (예: security_threats, service_outage)")
    p.add_argument("--workers", type=int, default=1, metavar="N",
                   help="병렬 크롤링 스레드 수 (기본 1=순차, 권장 2~4)")
    p.add_argument("--llm", action="store_true",
                   help="LLM 위협 인텔리전스 분석 활성화")
    p.add_argument("--llm-backend", default=None, metavar="BACKEND",
                   choices=["openai", "gemini", "groq", "grok"],
                   help="LLM 백엔드 선택 (openai/gemini/groq/grok, 기본: .env LLM_BACKEND)")
    p.add_argument("--compare-llm", default=None, metavar="BACKENDS",
                   help="동일 프롬프트를 여러 LLM에 전송해 결과 비교 (예: --compare-llm gemini,groq)")
    p.add_argument("--today-only", action="store_true",
                   help="KST 당일 트윗만 수집 (noname 등 실시간 모니터링용)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="로그 레벨 (기본 INFO)")
    p.add_argument("--show-results", action="store_true",
                   help="마지막 리포트의 키워드별 수집 결과 조회")
    p.add_argument("--show-keyword", default=None, metavar="KEYWORD",
                   help="특정 키워드 트윗 상세 조회 (예: --show-keyword CVE)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)

    # ── 결과 조회 모드 (크롤링 없이 빠른 조회)
    if args.show_results or args.show_keyword:
        _show_results(keyword=args.show_keyword)
        return

    config = load_config()

    available_llm = list_available_backends()
    safe_print(f"설정 로드 완료")
    safe_print(f"  활성 키워드: {len(config.active_keywords)}개")
    safe_print(f"  활성 계정:   {len(config.active_accounts)}개")
    safe_print(f"  LLM 백엔드:  {available_llm if available_llm else '미설정 (--llm 사용 불가)'}")

    # --llm 플래그 또는 .env의 ENABLE_LLM=true 중 하나라도 있으면 LLM 활성화
    import os as _os
    enable_llm = args.llm or _os.getenv("ENABLE_LLM", "").lower() in ("1", "true", "yes")

    compare_backends = [b.strip() for b in args.compare_llm.split(",")] if args.compare_llm else None

    def job():
        run_crawl_job(
            config=config,
            keywords_only=args.keywords_only,
            accounts_only=args.accounts_only,
            group_filter=args.group,
            workers=args.workers,
            enable_llm=enable_llm,
            llm_backend=args.llm_backend,
            compare_backends=compare_backends,
            today_only=args.today_only,
        )

    job()


if __name__ == "__main__":
    main()
