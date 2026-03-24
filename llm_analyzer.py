"""
멀티 LLM 분석기.

지원 백엔드:
  - openai  : GPT-4o-mini  (OPENAI_API_KEY)
  - gemini  : Gemini 2.0 Flash  (GEMINI_API_KEY)  ← 무료
  - grok    : Grok-3-mini  (GROK_API_KEY)          ← 무료 티어

활성화 방법:
  .env 에 LLM_BACKEND=gemini (또는 openai / grok)
  해당 API 키 설정 후
  ENABLE_LLM=true

사용 예:
  python main.py --once --llm
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Protocol

import requests

from llm_logger import get_llm_logger

logger = logging.getLogger(__name__)

# ─── 환경변수 ─────────────────────────────────────────────────────────────────
LLM_BACKEND: str = os.getenv("LLM_BACKEND", "gemini").lower()  # openai | gemini | grok
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GROK_API_KEY: str = os.getenv("GROK_API_KEY", "")

OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")
GROK_MODEL: str = os.getenv("GROK_MODEL", "grok-3-mini-fast-beta")

LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))

KST = timezone(timedelta(hours=9))


@dataclass
class LLMResult:
    """LLM 분석 결과 + 메타 정보."""
    text: str
    backend: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    kst_time: str = field(default_factory=lambda: datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"))


# ─── 프롬프트 ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a senior cybersecurity threat intelligence analyst specializing in APT tracking, "
    "malware analysis, and vulnerability research. "
    "Respond in Korean for narrative sections. "
    "Use English for technical terms, CVE IDs, malware names, and IOC values. "
    "Be concise, actionable, and precise."
)

def build_analysis_prompt(
    tweets: list[dict],
    top_words: list[tuple[str, int]],
    top_hashtags: list[tuple[str, int]],
    news_text: str = "",
) -> str:
    """크롤링 결과를 LLM에 넘길 심층 분석 프롬프트로 변환합니다."""
    # 트윗 샘플: 한국어/영어만 포함, 언어 표시
    samples = []
    for t in tweets[:50]:
        user = t.get("user", {}).get("username", "?")
        text = t.get("text", "")[:200].replace("\n", " ")
        date = t.get("date", "")[:16]
        link = t.get("link", "")
        # 한국어 포함 여부 감지
        lang = "[KR]" if any("\uac00" <= c <= "\ud7a3" for c in text) else "[EN]"
        samples.append(f"{lang} @{user} ({date}): {text}  {link}")

    words_str = ", ".join(f"{w}({c})" for w, c in top_words[:15])
    tags_str  = ", ".join(f"#{t}({c})" for t, c in top_hashtags[:8])

    news_section = ""
    if news_text:
        news_section = f"\n## 보안 뉴스/블로그 기사 (IOC 포함)\n{news_text}\n"

    return f"""다음은 X(트위터) 사이버보안 모니터링 시스템에서 수집한 실시간 위협 인텔리전스 데이터입니다.
수집 트윗 수: {len(tweets)}개 | [KR]=한국어 트윗, [EN]=영문 트윗

## 트렌드 키워드 (빈도순)
{words_str}

## 주요 해시태그
{tags_str}
{news_section}
## 수집 트윗 샘플 (최대 50개, 언어 구분)
{chr(10).join(samples)}

---

위 데이터를 **심층 분석**하여 아래 항목을 작성하세요.
서술은 한국어, 기술 용어(CVE, 악성코드명, IOC 값)는 영어 원문 유지.

### 1. 현재 주요 사이버보안 트렌드
(3~4문장. 이번 수집에서 두드러지는 공격 벡터/기술 변화 위주)

### 2. 주요 위협 행위자 / 캠페인
- 행위자명 (출처 근거): 활동 내용 및 타겟
- (확인된 항목만, 최대 5개)

### 3. 한국 관련 위협
- 한국 기업·기관·인프라 관련 언급 항목
- 한국어 트윗([KR])에서 감지된 주요 사건
- (없으면 "이번 수집에서 한국 특정 위협 없음")

### 4. 주요 CVE / 취약점 (CVSS 우선)
- CVE-ID: 영향 제품, 심각도, 현재 악용 여부
- (최대 5개, CVSS 높은 순)

### 5. IOC 요약 (뉴스/블로그에서 추출)
각 IOC 항목 끝에 출처 기사 링크를 반드시 포함하세요. 형식: `값 ([출처명](링크))`
※ IP 주소는 절대 포함하지 마세요 (버전 번호나 오탐 가능성이 높음).
- 도메인: (있으면 나열, 각각 출처 링크 포함)
- 해시: (있으면 나열, 각각 출처 링크 포함)
- CVE: (있으면 나열, 각각 출처 링크 포함)
- (없으면 "이번 수집에서 IOC 없음")

### 6. 즉각 대응 권고
- (실무자가 지금 당장 확인해야 할 조치, 최대 3개 bullet)

### 7. 위협 수준 평가
- 점수: X/10
- 근거: (한 줄)

간결하고 실무자가 바로 활용 가능하도록 작성하세요."""


# ─── LLM 백엔드 구현 ──────────────────────────────────────────────────────────

class LLMBackend(Protocol):
    def complete(self, system: str, user: str) -> LLMResult: ...
    def is_available(self) -> bool: ...
    @property
    def name(self) -> str: ...


class OpenAIBackend:
    """OpenAI GPT API (유료)."""

    name = "openai"

    def is_available(self) -> bool:
        return bool(OPENAI_API_KEY)

    def complete(self, system: str, user: str) -> LLMResult:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 1500,
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        return LLMResult(
            text=data["choices"][0]["message"]["content"],
            backend=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )


class GeminiBackend:
    """Google Gemini API (무료 티어 제공).

    무료 키 발급: https://aistudio.google.com/apikey
    무료 한도: gemini-2.0-flash 기준 1500 req/day, 100만 토큰/day
    """

    name = "gemini"

    def is_available(self) -> bool:
        return bool(GEMINI_API_KEY)

    def complete(self, system: str, user: str) -> LLMResult:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "system_instruction": {
                "parts": [{"text": system}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": user}]}
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1500,
            },
        }
        resp = requests.post(url, json=payload, timeout=LLM_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        # 토큰 사용량 (usageMetadata)
        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        total_tokens = usage.get("totalTokenCount", 0)

        return LLMResult(
            text=text,
            backend=self.name,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


class GrokBackend:
    """xAI Grok API (무료 티어 제공).

    무료 키 발급: https://console.x.ai/
    무료 한도: grok-3-mini 기준 월 25달러 크레딧 제공
    OpenAI 호환 엔드포인트 사용.
    """

    name = "grok"

    def is_available(self) -> bool:
        return bool(GROK_API_KEY)

    def complete(self, system: str, user: str) -> LLMResult:
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROK_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": 1500,
            },
            timeout=LLM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {})
        return LLMResult(
            text=data["choices"][0]["message"]["content"],
            backend=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )


# ─── 팩토리 및 메인 분석기 ────────────────────────────────────────────────────

_BACKENDS: dict[str, type] = {
    "openai": OpenAIBackend,
    "gemini": GeminiBackend,
    "grok":   GrokBackend,
}


def get_backend(name: str | None = None) -> LLMBackend | None:
    """
    지정된 백엔드 인스턴스를 반환합니다.
    name이 None이면 LLM_BACKEND 환경변수 사용.
    API 키가 없으면 None 반환.
    """
    backend_name = (name or LLM_BACKEND).lower()
    cls = _BACKENDS.get(backend_name)
    if cls is None:
        logger.warning("알 수 없는 LLM 백엔드: %s (openai/gemini/grok 중 선택)", backend_name)
        return None

    backend = cls()
    if not backend.is_available():
        key_env = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "grok":   "GROK_API_KEY",
        }.get(backend_name, "API_KEY")
        logger.warning("LLM 백엔드 '%s' 비활성화: %s 환경변수 미설정", backend_name, key_env)
        return None

    return backend


def _threat_level_emoji(text: str) -> str:
    """LLM 응답에서 위협 수준 점수를 파싱해 색깔 이모지를 반환합니다."""
    import re
    # "점수: 7/10" 또는 "7/10" 패턴 검색
    m = re.search(r"점수[:\s]*(\d+)/10", text)
    if not m:
        m = re.search(r"(\d+)/10", text)
    if not m:
        return ""
    score = int(m.group(1))
    if score <= 3:
        return f"🟢 위협 점수: {score}/10 (낮음)"
    elif score <= 6:
        return f"🟡 위협 점수: {score}/10 (보통)"
    elif score <= 8:
        return f"🟠 위협 점수: {score}/10 (높음)"
    else:
        return f"🔴 위협 점수: {score}/10 (심각)"


# Gemini 무료 일일 한도 (모델별)
GEMINI_DAILY_LIMITS: dict[str, int] = {
    "gemini-3.1-flash-lite-preview": 1_000_000,  # 무료 1M tokens/day (추정)
    "gemini-2.5-flash":              500_000,
    "gemini-2.5-flash-lite":         500_000,
    "gemini-2.0-flash":              1_000_000,
}

# 세션 누적 토큰 (프로세스 수명 내)
_session_total_tokens: int = 0


def format_llm_result(result: LLMResult) -> str:
    """LLMResult를 텔레그램용 문자열로 포맷합니다 (토큰 사용량 + KST 시간 포함)."""
    global _session_total_tokens
    _session_total_tokens += result.total_tokens

    threat_badge = _threat_level_emoji(result.text)

    token_info = ""
    if result.total_tokens > 0:
        # Gemini 일일 한도 표시
        daily_limit = GEMINI_DAILY_LIMITS.get(GEMINI_MODEL, 0)
        if result.backend == "gemini" and daily_limit:
            used_pct = (_session_total_tokens / daily_limit) * 100
            remaining = max(0, daily_limit - _session_total_tokens)
            token_info = (
                f"\n\n📊 토큰: 이번 요청 {result.total_tokens:,}개 "
                f"(입력 {result.prompt_tokens:,} / 출력 {result.output_tokens:,})\n"
                f"📈 일일 한도: {_session_total_tokens:,} / {daily_limit:,} 사용 "
                f"({used_pct:.1f}%) | 잔여 {remaining:,}개"
            )
        else:
            token_info = (
                f"\n\n📊 토큰 사용: {result.total_tokens:,}개 "
                f"(입력 {result.prompt_tokens:,} / 출력 {result.output_tokens:,})"
            )

    header = f"🤖 [{result.backend.upper()} 분석] {result.kst_time}"
    if threat_badge:
        header += f"\n{threat_badge}"

    return f"{header}\n\n{result.text}{token_info}"


def run_llm_analysis(
    tweets: list[dict],
    top_words: list[tuple[str, int]],
    top_hashtags: list[tuple[str, int]],
    backend_name: str | None = None,
    news_text: str = "",
) -> str:
    """
    트윗 데이터를 LLM으로 분석하여 위협 인텔리전스 리포트를 반환합니다.

    Args:
        tweets: 크롤링된 트윗 리스트
        top_words: 빈도 상위 단어 [(word, count), ...]
        top_hashtags: 빈도 상위 해시태그
        backend_name: "openai" | "gemini" | "grok" | None (env 기본값 사용)
        news_text: RSS 뉴스 기사 텍스트 (선택)

    Returns:
        분석 결과 문자열 (실패 시 빈 문자열)
    """
    backend = get_backend(backend_name)
    if backend is None:
        return ""

    prompt = build_analysis_prompt(tweets, top_words, top_hashtags, news_text=news_text)

    # 로깅 시작
    llm_logger = get_llm_logger()
    request_id = llm_logger.log_request(backend.name, "analysis", SYSTEM_PROMPT, prompt)
    start_time = time.time()

    try:
        logger.info("LLM 분석 시작 (backend=%s, tweets=%d개)", backend.name, len(tweets))
        result = None
        for attempt in range(5):
            try:
                result = backend.complete(SYSTEM_PROMPT, prompt)
                break
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 503 and attempt < 4:
                    wait = 30 * (attempt + 1)
                    logger.warning("LLM 503 오류, %d초 후 재시도 (%d/5)...", wait, attempt + 1)
                    time.sleep(wait)
                else:
                    raise
        if result is None:
            duration_ms = (time.time() - start_time) * 1000
            llm_logger.log_response(request_id, "", duration_ms=duration_ms, success=False, error="No result")
            return ""
        logger.info(
            "LLM 분석 완료 (backend=%s, tokens=%d)",
            backend.name, result.total_tokens
        )
        # 응답 로깅
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(
            request_id,
            result.text,
            prompt_tokens=result.prompt_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            duration_ms=duration_ms,
            success=True,
        )
        return format_llm_result(result)
    except requests.HTTPError as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error=str(e), duration_ms=duration_ms)
        logger.error("LLM API HTTP 오류 [%s]: %s", backend.name, e)
        return ""
    except requests.Timeout:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error=f"Timeout after {LLM_TIMEOUT}s", duration_ms=duration_ms)
        logger.error("LLM API 타임아웃 [%s]: %d초 초과", backend.name, LLM_TIMEOUT)
        return ""
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error=str(e), duration_ms=duration_ms)
        logger.error("LLM 분석 실패 [%s]: %s", backend.name, e)
        return ""


@dataclass
class TopTweet:
    """LLM이 선별한 주요 트윗."""
    rank: int
    threat_level: str   # "HIGH" | "MEDIUM" | "LOW"
    username: str
    date: str
    link: str
    text: str
    reason: str         # 선별 이유 (한 줄)


def build_tweet_selection_prompt(tweets: list[dict]) -> str:
    """전체 트윗에서 위협 중요도 기준 상위 10개를 선별하는 프롬프트."""
    lines = []
    for i, t in enumerate(tweets[:100]):  # 최대 100개 입력
        user = t.get("user", {}).get("username", "?")
        text = t.get("text", "")[:100].replace("\n", " ")
        date = t.get("date", "")[:16]
        link = t.get("link", "")
        lang = "[KR]" if any("\uac00" <= c <= "\ud7a3" for c in text) else "[EN]"
        lines.append(f"[{i}] {lang} @{user} ({date}) {link}\n{text}")

    tweets_block = "\n\n".join(lines)
    return f"""다음은 사이버보안 트위터 모니터링에서 수집된 트윗 {len(tweets[:100])}개입니다.

{tweets_block}

---

위 트윗 목록에서 보안 위협 중요도가 높은 상위 10개를 선별하세요.

선별 기준 (우선순위 순):
1. 새로운 CVE/취약점 공개 또는 PoC 코드 배포
2. 실제 공격/침해 사고 보고 (랜섬웨어, APT, 데이터 유출)
3. 위협 행위자/그룹 활동 정보
4. 한국 관련 위협 또는 한국어 트윗
5. 즉각 조치가 필요한 보안 경보

반드시 아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{{
  "top_tweets": [
    {{
      "rank": 1,
      "tweet_index": <[N] 번호>,
      "threat_level": "HIGH",
      "reason": "선별 이유 한 줄 (한국어)"
    }},
    ...
  ]
}}

threat_level은 HIGH/MEDIUM/LOW 중 하나."""


def run_tweet_selection(
    tweets: list[dict],
    backend_name: str | None = None,
) -> list[TopTweet]:
    """
    LLM으로 전체 트윗에서 위협 중요도 기준 상위 트윗을 선별합니다.

    Returns:
        TopTweet 리스트 (최대 10개), 실패 시 빈 리스트
    """
    if not tweets:
        return []

    backend = get_backend(backend_name)
    if backend is None:
        return []

    prompt = build_tweet_selection_prompt(tweets)
    system = (
        "You are a cybersecurity triage analyst. "
        "Select the most threat-relevant tweets and respond ONLY with valid JSON."
    )

    # 로깅 시작
    llm_logger = get_llm_logger()
    request_id = llm_logger.log_request(backend.name, "selection", system, prompt)
    start_time = time.time()

    try:
        logger.info("LLM 트윗 선별 시작 (backend=%s, tweets=%d개)", backend.name, len(tweets))
        result = None
        for attempt in range(5):
            try:
                result = backend.complete(system, prompt)
                break
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 503 and attempt < 4:
                    wait = 30 * (attempt + 1)
                    logger.warning("LLM 트윗 선별 503 오류, %d초 후 재시도 (%d/5)...", wait, attempt + 1)
                    time.sleep(wait)
                else:
                    raise
        if result is None:
            duration_ms = (time.time() - start_time) * 1000
            llm_logger.log_response(request_id, "", success=False, error="No result", duration_ms=duration_ms)
            return []
        logger.info("LLM 트윗 선별 완료 (tokens=%d)", result.total_tokens)

        # JSON 파싱
        text = result.text.strip()
        # 코드블록 제거
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```").strip()
        data = json.loads(text)

        # 응답 로깅
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(
            request_id,
            result.text,
            prompt_tokens=result.prompt_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            duration_ms=duration_ms,
            success=True,
        )

        top_tweets: list[TopTweet] = []
        tweet_pool = tweets[:100]
        for item in data.get("top_tweets", [])[:10]:
            idx = item.get("tweet_index")
            if idx is None or idx >= len(tweet_pool):
                continue
            t = tweet_pool[idx]
            user = t.get("user", {}).get("username", "?").lstrip("@")
            top_tweets.append(TopTweet(
                rank=item.get("rank", len(top_tweets) + 1),
                threat_level=item.get("threat_level", "MEDIUM"),
                username=user,
                date=t.get("date", ""),
                link=t.get("link", ""),
                text=t.get("text", "")[:200],
                reason=item.get("reason", ""),
            ))
        return top_tweets

    except (json.JSONDecodeError, KeyError) as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, result.text if result else "", success=False, error=f"JSON parse error: {e}", duration_ms=duration_ms)
        logger.error("LLM 트윗 선별 JSON 파싱 실패: %s", e)
        return []
    except requests.Timeout:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error="Timeout", duration_ms=duration_ms)
        logger.error("LLM 트윗 선별 타임아웃")
        return []
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error=str(e), duration_ms=duration_ms)
        logger.error("LLM 트윗 선별 실패: %s", e)
        return []


def list_available_backends() -> list[str]:
    """현재 API 키가 설정된 백엔드 목록을 반환합니다."""
    available = []
    for name, cls in _BACKENDS.items():
        if cls().is_available():
            available.append(name)
    return available


# ─── 한국 관련 트윗 추출 ─────────────────────────────────────────────────────

KOREA_SELECTION_SYSTEM = (
    "You are a cybersecurity analyst specializing in Korean cyber threat intelligence. "
    "Identify tweets related to South Korea, Korean organizations, Korean infrastructure, "
    "or threats targeting Korea. Respond ONLY with valid JSON."
)

KOREA_SELECTION_PROMPT_TMPL = """아래 트윗 목록에서 한국(South Korea / 🇰🇷 / Korea / 한국 / 대한민국 / .kr / Korean)과
직접적으로 관련된 위협 정보를 모두 찾아주세요.

관련 기준:
- 한국 기업/기관/정부를 대상으로 한 공격
- 한국 인프라(.kr 도메인 포함)에 대한 위협
- 한국 관련 랜섬웨어/DDoS/데이터 유출 피해
- 한국을 언급하는 위협 행위자 캠페인

트윗 목록:
{tweet_list}

응답 형식 (관련 없으면 korea_tweets를 빈 배열로):
{{
  "korea_tweets": [
    {{
      "tweet_index": <트윗 번호(0부터 시작)>,
      "relevance": "HIGH/MEDIUM/LOW",
      "reason": "한국 관련 이유 한 줄 (한국어)"
    }}
  ]
}}"""


@dataclass
class KoreaTweet:
    """한국 관련 트윗 정보."""
    relevance: str
    username: str
    date: str
    link: str
    text: str
    reason: str


def run_korea_tweet_filter(
    tweets: list[dict],
    backend_name: str | None = None,
) -> list[KoreaTweet]:
    """
    전체 트윗에서 한국 관련 위협 트윗을 AI로 추출합니다.

    1차: 키워드 사전 필터 (korea/한국/.kr 등 포함 트윗만 LLM에 전달)
    2차: LLM으로 실제 관련성 판단

    Returns:
        KoreaTweet 리스트, 실패 시 빈 리스트
    """
    if not tweets:
        return []

    # 1차 키워드 사전 필터
    KOREA_KEYWORDS = [
        "korea", "korean", "한국", "대한민국", "코리아", "서울",
        "🇰🇷", ".kr", "kornet", "kisa", "ahnlab", "리퍼섹", "rippersec",
        "megamedusa", "kimsuky", "lazarus", "apt38", "apt37",
    ]
    candidate_indices = []
    for i, t in enumerate(tweets):
        text_lower = t.get("text", "").lower()
        if any(kw.lower() in text_lower for kw in KOREA_KEYWORDS):
            candidate_indices.append(i)

    if not candidate_indices:
        logger.info("한국 관련 키워드 트윗 없음 (전체 %d개)", len(tweets))
        return []

    candidates = [tweets[i] for i in candidate_indices]
    logger.info("한국 관련 키워드 1차 필터: %d개 → LLM 분석", len(candidates))

    backend = get_backend(backend_name)
    if backend is None:
        # LLM 없으면 키워드 매칭만으로 반환
        result = []
        for t in candidates:
            user = t.get("user", {}).get("username", "?").lstrip("@")
            result.append(KoreaTweet(
                relevance="MEDIUM",
                username=user,
                date=t.get("date", ""),
                link=t.get("link", ""),
                text=t.get("text", "")[:200],
                reason="키워드 매칭 (LLM 미설정)",
            ))
        return result

    # 2차 LLM 판단
    tweet_list = "\n".join(
        f"[{i}] @{t.get('user',{}).get('username','?').lstrip('@')} ({t.get('date','')}): {t.get('text','')[:150]}"
        for i, t in enumerate(candidates)
    )
    prompt = KOREA_SELECTION_PROMPT_TMPL.format(tweet_list=tweet_list)

    llm_logger = get_llm_logger()
    request_id = llm_logger.log_request(backend.name, "korea_filter", KOREA_SELECTION_SYSTEM, prompt)
    start_time = time.time()

    try:
        result_raw = None
        for attempt in range(5):
            try:
                result_raw = backend.complete(KOREA_SELECTION_SYSTEM, prompt)
                break
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 503 and attempt < 4:
                    wait = 30 * (attempt + 1)
                    logger.warning("LLM 한국 선별 503 오류, %d초 후 재시도 (%d/5)...", wait, attempt + 1)
                    time.sleep(wait)
                else:
                    raise

        if result_raw is None:
            duration_ms = (time.time() - start_time) * 1000
            llm_logger.log_response(request_id, "", success=False, error="No result", duration_ms=duration_ms)
            return []

        duration_ms = (time.time() - start_time) * 1000
        text = result_raw.text.strip()
        text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```").strip()
        data = json.loads(text)

        llm_logger.log_response(
            request_id, result_raw.text,
            prompt_tokens=result_raw.prompt_tokens,
            output_tokens=result_raw.output_tokens,
            total_tokens=result_raw.total_tokens,
            duration_ms=duration_ms, success=True,
        )

        korea_tweets: list[KoreaTweet] = []
        for item in data.get("korea_tweets", []):
            idx = item.get("tweet_index")
            if idx is None or idx >= len(candidates):
                continue
            t = candidates[idx]
            user = t.get("user", {}).get("username", "?").lstrip("@")
            korea_tweets.append(KoreaTweet(
                relevance=item.get("relevance", "MEDIUM"),
                username=user,
                date=t.get("date", ""),
                link=t.get("link", ""),
                text=t.get("text", "")[:200],
                reason=item.get("reason", ""),
            ))

        # 내용 유사도 기반 중복 제거 (RT 등으로 동일 내용이 여러 계정에서 포함되는 경우)
        seen_prefixes: set[str] = set()
        deduped: list[KoreaTweet] = []
        for kt in korea_tweets:
            prefix = kt.text[:50].strip().lower()
            if prefix not in seen_prefixes:
                seen_prefixes.add(prefix)
                deduped.append(kt)

        logger.info("한국 관련 트윗 최종: %d개 (중복 제거 전 %d개)", len(deduped), len(korea_tweets))
        return deduped

    except (json.JSONDecodeError, KeyError) as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, result_raw.text if result_raw else "", success=False, error=str(e), duration_ms=duration_ms)
        logger.error("한국 트윗 필터 JSON 파싱 실패: %s", e)
        return []
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        llm_logger.log_response(request_id, "", success=False, error=str(e), duration_ms=duration_ms)
        logger.error("한국 트윗 필터 실패: %s", e)
        return []
