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
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Protocol

import requests

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

SYSTEM_PROMPT = "You are a cybersecurity threat intelligence analyst. Always respond in Korean."

def build_analysis_prompt(
    tweets: list[dict],
    top_words: list[tuple[str, int]],
    top_hashtags: list[tuple[str, int]],
    news_text: str = "",
) -> str:
    """크롤링 결과를 LLM에 넘길 프롬프트로 변환합니다."""
    samples = []
    for t in tweets[:40]:
        user = t.get("user", {}).get("username", "?")
        text = t.get("text", "")[:200].replace("\n", " ")
        date = t.get("date", "")[:16]
        link = t.get("link", "")
        samples.append(f"@{user} ({date}): {text}  {link}")

    words_str = ", ".join(f"{w}({c})" for w, c in top_words[:15])
    tags_str  = ", ".join(f"#{t}({c})" for t, c in top_hashtags[:8])

    news_section = ""
    if news_text:
        news_section = f"\n## 보안 뉴스/블로그 기사\n{news_text}\n"

    return f"""다음은 X(트위터)에서 사이버보안 키워드로 수집한 트윗 {len(tweets)}개의 분석 데이터입니다.

## 빈도 높은 단어
{words_str}

## 주요 해시태그
{tags_str}
{news_section}
## 트윗 샘플 (최대 40개)
{chr(10).join(samples)}

---

위 데이터를 분석하여 **한국어**로 아래 항목을 작성하세요:

### 1. 현재 사이버보안 트렌드 (2~3문장)

### 2. 주요 위협 행위자 / 캠페인
- (각 항목을 bullet로)

### 3. 한국 관련 위협 또는 서비스 장애
- (없으면 "해당 없음")

### 4. 주목할 CVE / 취약점
- (없으면 "해당 없음")

### 5. 위협 수준 평가
- 전반적 위협 수준: 낮음(1-3) / 보통(4-6) / 높음(7-8) / 심각(9-10)
- 점수: X/10  (숫자만 기입, 예: 7/10)
- 근거: (한 줄)

간결하고 실무자가 바로 활용할 수 있도록 작성하세요."""


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

    try:
        logger.info("LLM 분석 시작 (backend=%s, tweets=%d개)", backend.name, len(tweets))
        result = backend.complete(SYSTEM_PROMPT, prompt)
        logger.info(
            "LLM 분석 완료 (backend=%s, tokens=%d)",
            backend.name, result.total_tokens
        )
        return format_llm_result(result)
    except requests.HTTPError as e:
        logger.error("LLM API HTTP 오류 [%s]: %s", backend.name, e)
        return ""
    except requests.Timeout:
        logger.error("LLM API 타임아웃 [%s]: %d초 초과", backend.name, LLM_TIMEOUT)
        return ""
    except Exception as e:
        logger.error("LLM 분석 실패 [%s]: %s", backend.name, e)
        return ""


def list_available_backends() -> list[str]:
    """현재 API 키가 설정된 백엔드 목록을 반환합니다."""
    available = []
    for name, cls in _BACKENDS.items():
        if cls().is_available():
            available.append(name)
    return available
