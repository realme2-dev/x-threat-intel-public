"""
텔레그램 알림 모듈.

설정 방법:
  .env 또는 환경변수에 아래 두 값 설정:
    TELEGRAM_BOT_TOKEN=<BotFather에서 발급받은 토큰>
    TELEGRAM_CHAT_ID=<채널 또는 사용자 chat_id>

비활성화:
  위 환경변수가 없으면 알림을 건너뜁니다 (에러 없음).
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests

from analyzer import TrendReport, TweetSummary, SampleTweet

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE: str = "https://api.telegram.org/bot{token}/{method}"

MAX_MESSAGE_LENGTH = 4000
KST = timezone(timedelta(hours=9))


def _to_kst(date_str: str) -> str:
    """ISO/Twitter/Nitter 날짜를 KST 문자열로 변환. 파싱 실패 시 원본 반환."""
    if not date_str:
        return ""
    s = date_str.strip()
    # Nitter 포맷: "Mar 9, 2026 · 1:30" 또는 "Mar 09, 2026 · 13:30"
    nitter_match = re.match(
        r"([A-Za-z]+)\s+(\d+),\s*(\d+)\s*[·\·]\s*(\d+:\d+)", s
    )
    if nitter_match:
        try:
            month_str, day, year, time_str = nitter_match.groups()
            # 12시간제 여부 확인 후 파싱 시도
            dt_str = f"{month_str} {day} {year} {time_str}"
            try:
                dt = datetime.strptime(dt_str, "%b %d %Y %I:%M")
            except ValueError:
                dt = datetime.strptime(dt_str, "%b %d %Y %H:%M")
            # Nitter 시간은 UTC 기준
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(KST).strftime("%m/%d %H:%M KST")
        except Exception:
            pass
    # ISO 포맷: "2026-03-09T06:30:00+00:00" 또는 "2026-03-09 06:30"
    try:
        date_str_clean = s[:19].replace("T", " ")
        dt = datetime.fromisoformat(date_str_clean).replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%m/%d %H:%M KST")
    except Exception:
        return s[:16]


def _twitter_profile_md(username: str) -> str:
    u = username.lstrip("@")
    return f"[@{u}](https://twitter.com/{u})"


def _twitter_hashtag_md(tag: str) -> str:
    t = tag.lstrip("#")
    return f"[#{t}](https://twitter.com/hashtag/{quote(t)})"


def _esc(text: str) -> str:
    """Markdown(v1) 특수문자 이스케이프 — * _ ` [ 만 처리."""
    return (
        text
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
    )


def _clean_llm_text(text: str) -> str:
    """LLM 응답에서 Markdown 마크업을 텔레그램 Markdown v1 호환 형식으로 변환."""
    # **bold** → 그냥 텍스트 (Markdown v1은 *italic*만 지원)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # ### heading → 줄 앞에 🔸 추가
    text = re.sub(r"^#{1,3}\s*", "🔸 ", text, flags=re.MULTILINE)
    # * bullet → • 로 변환 (Markdown 충돌 방지)
    text = re.sub(r"^\*   ", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^\*\s+", "• ", text, flags=re.MULTILINE)
    # 인라인 코드 `도메인명` 중 백틱 제거 (Markdown v1 코드블록 오파싱 방지)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # 언더스코어 이스케이프
    text = text.replace("_", "\\_")
    # 남은 대괄호 이스케이프 (링크가 아닌 것)
    text = re.sub(r"\[(?![^\]\n]*\]\(https?://)", r"\\[", text)
    return text


@dataclass
class NotifyResult:
    success: bool
    message_ids: list[int]
    error: str = ""


class TelegramNotifier:
    """텔레그램 Bot API를 통해 크롤링 결과를 전송합니다."""

    def __init__(
        self,
        token: str = TELEGRAM_BOT_TOKEN,
        chat_id: str = TELEGRAM_CHAT_ID,
    ):
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)

        if not self._enabled:
            logger.info("텔레그램 알림 비활성화 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정)")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─── 공개 API ────────────────────────────────────────────────────────────

    def send_report(self, report: TrendReport, news_text: str = "") -> NotifyResult:
        """TrendReport를 텔레그램으로 전송합니다."""
        if not self._enabled:
            return NotifyResult(success=True, message_ids=[], error="disabled")

        messages = self._build_messages(report, news_text)
        sent_ids: list[int] = []

        for msg in messages:
            result = self._send_message(msg)
            if result:
                sent_ids.append(result)
            else:
                return NotifyResult(success=False, message_ids=sent_ids, error="전송 실패")

        return NotifyResult(success=True, message_ids=sent_ids)

    def send_text(self, text: str) -> bool:
        if not self._enabled:
            return False
        return self._send_message(text) is not None

    def test_connection(self) -> bool:
        if not self._enabled:
            logger.warning("텔레그램 설정이 없어 연결 테스트를 건너뜁니다.")
            return False
        try:
            url = TELEGRAM_API_BASE.format(token=self._token, method="getMe")
            resp = requests.get(url, timeout=10)
            if resp.ok:
                bot_name = resp.json().get("result", {}).get("username", "?")
                logger.info("텔레그램 Bot 연결 성공: @%s", bot_name)
                return True
            else:
                logger.error("텔레그램 Bot 연결 실패: %s", resp.text)
                return False
        except Exception as e:
            logger.error("텔레그램 연결 오류: %s", e)
            return False

    # ─── 메시지 빌더 ─────────────────────────────────────────────────────────

    def _build_messages(self, report: TrendReport, news_text: str = "") -> list[str]:
        """리포트를 청크 목록으로 변환합니다. AI 요약은 별도 메시지로 분리."""
        parts: list[str] = []

        # ── 1. 헤더 + 통계
        kst_now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
        header = (
            f"🔍 *X 크롤러 수집 결과*\n"
            f"📅 {kst_now}\n"
            f"📊 트윗 *{report.total_tweets}개* | 타겟 *{report.total_targets}개*"
        )

        # 수집 현황 (간소화: 성공/전체 수만 표시)
        kw_ok = sum(1 for s in report.keyword_summary if s.tweet_count > 0)
        acc_ok = sum(1 for s in report.account_summary if s.tweet_count > 0)
        if report.keyword_summary:
            header += f"\n🔎 키워드 {kw_ok}/{len(report.keyword_summary)}개 수집"
        if report.account_summary:
            header += f"\n👤 계정 {acc_ok}/{len(report.account_summary)}개 수집"

        parts.append(header)

        # ── 2. 트렌드 분석 (간소화)
        trend_lines = ["*[트렌드 분석]*"]

        if report.top_words:
            top20 = report.top_words[:20]
            half = len(top20) // 2
            line1 = "  ".join(f"`{w}`({c})" for w, c in top20[:half])
            line2 = "  ".join(f"`{w}`({c})" for w, c in top20[half:])
            trend_lines.append(f"🔑 키워드:\n{line1}\n{line2}")

        if report.top_hashtags:
            tags = "  ".join(
                _twitter_hashtag_md(t) + f"({c})"
                for t, c in report.top_hashtags[:10]
            )
            trend_lines.append(f"#️⃣ 해시태그:\n{tags}")

        if report.top_users:
            users = "  ".join(
                _twitter_profile_md(u) + f"({c})"
                for u, c in report.top_users[:5]
            )
            trend_lines.append(f"👤 활발한 계정:\n{users}")

        parts.append("\n".join(trend_lines))

        # ── 3. LLM 선별 주요 트윗 (위협도 기준 상위 10개)
        if report.llm_top_tweets:
            top_section = self._build_top_tweets_section(report.llm_top_tweets)
            if top_section:
                parts.append(top_section)
        else:
            # LLM 없으면 기존 방식 폴백
            acc_sample = self._build_account_samples(report)
            if acc_sample:
                parts.append(acc_sample)
            kw_sample = self._build_keyword_samples(report)
            if kw_sample:
                parts.append(kw_sample)

        # ── 4. 뉴스 기사 (별도 파트)
        if news_text:
            parts.append(news_text)

        # AI 요약은 별도 send (분리된 메시지)
        result_chunks = self._chunk_messages(parts)

        # ── 6. AI 요약 별도 메시지
        if report.llm_summary:
            # LLM 응답의 **bold** → plain, ### → 제거, 이스케이프 처리
            ai_text = _clean_llm_text(report.llm_summary[:3500])
            ai_msg = f"*[AI 위협 인텔리전스 요약]*\n{ai_text}"
            result_chunks.extend(self._chunk_messages([ai_msg]))

        return result_chunks

    def _build_account_samples(self, report: TrendReport) -> str:
        """계정 주요 트윗 — @계정 | 날짜 / 내용 형식."""
        lines = ["*[계정 주요 트윗]*"]
        shown = 0
        for s in report.account_summary:
            if shown >= 5:
                break
            if not s.sample_tweets:
                continue
            tw = s.sample_tweets[0]
            kst_date = _to_kst(tw.date)
            user_link = _twitter_profile_md(s.target)
            text_esc = _esc(tw.text[:120])

            if tw.link:
                header = f"{user_link} | [{kst_date}]({tw.link})"
            else:
                header = f"{user_link} | {kst_date}"

            lines.append(f"\n{header}")
            lines.append(text_esc)
            shown += 1
        if shown == 0:
            return ""
        return "\n".join(lines)

    def _build_top_tweets_section(self, top_tweets: list) -> str:
        """LLM 선별 주요 트윗 섹션 — 위협도 배지 + 이유 표시."""
        level_badge = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}
        lines = ["*[🤖 AI 선별 주요 트윗]*"]

        for tw in top_tweets:
            badge = level_badge.get(tw.threat_level, "⚪")
            kst_date = _to_kst(tw.date)
            user_link = _twitter_profile_md(tw.username)
            text_esc = _esc(tw.text[:140])
            reason_plain = tw.reason.replace("*", "").replace("_", "").replace("`", "")

            if tw.link:
                header = f"{badge} {user_link} | [{kst_date}]({tw.link})"
            else:
                header = f"{badge} {user_link} | {kst_date}"

            lines.append(f"\n{header}")
            lines.append(text_esc)
            if reason_plain:
                lines.append(f"💡 {reason_plain}")

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def _build_keyword_samples(self, report: TrendReport) -> str:
        """키워드 주요 트윗 — #키워드 헤더 아래 - 날짜 | @계정 / 내용 형식."""
        lines = ["*[키워드 주요 트윗]*"]
        shown = 0
        for s in report.keyword_summary:
            if shown >= 5:
                break
            if not s.sample_tweets:
                continue

            # 키워드 헤더 (트위터 검색 링크)
            kw_encoded = quote(s.target)
            kw_link = f"[#{_esc(s.target)}](https://twitter.com/search?q={kw_encoded})"
            lines.append(f"\n{kw_link}")

            for tw in s.sample_tweets[:2]:
                kst_date = _to_kst(tw.date)
                user_link = _twitter_profile_md(tw.username)
                text_esc = _esc(tw.text[:120])

                if tw.link:
                    date_part = f"[{kst_date}]({tw.link})"
                else:
                    date_part = kst_date

                lines.append(f"- {date_part} | {user_link}")
                lines.append(text_esc)

            shown += 1

        if shown == 0:
            return ""
        return "\n".join(lines)

    def _chunk_messages(self, parts: list[str]) -> list[str]:
        """parts를 MAX_MESSAGE_LENGTH 이하의 메시지로 합칩니다."""
        chunks: list[str] = []
        current = ""

        for part in parts:
            if not part.strip():
                continue
            candidate = current + "\n\n" + part if current else part
            if len(candidate) > MAX_MESSAGE_LENGTH:
                if current:
                    chunks.append(current)
                if len(part) > MAX_MESSAGE_LENGTH:
                    for i in range(0, len(part), MAX_MESSAGE_LENGTH):
                        chunks.append(part[i:i + MAX_MESSAGE_LENGTH])
                    current = ""
                else:
                    current = part
            else:
                current = candidate

        if current:
            chunks.append(current)

        return chunks

    # ─── 전송 ────────────────────────────────────────────────────────────────

    def _send_message(self, text: str) -> int | None:
        """텔레그램 sendMessage API 호출. 성공 시 message_id 반환."""
        url = TELEGRAM_API_BASE.format(token=self._token, method="sendMessage")

        for parse_mode in ["Markdown", ""]:
            payload = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.ok:
                    return resp.json().get("result", {}).get("message_id")
                else:
                    err = resp.json().get("description", resp.text[:100])
                    logger.warning("텔레그램 전송 실패 (parse_mode=%s): %s", parse_mode, err)
            except Exception as e:
                logger.error("텔레그램 전송 오류: %s", e)
                return None

        logger.error("텔레그램 전송 최종 실패")
        return None
