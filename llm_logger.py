"""
LLM 요청/응답 로깅 모듈.

LLM API 호출 시 프롬프트와 응답을 JSON으로 저장합니다.
감사(Audit) 추적 및 프롬프트 엔지니어링 개선에 활용.

저장 경로: logs/llm_requests/
  └─ {timestamp}_{request_type}.json
     ├─ timestamp: 2026-03-11_09-11-00
     ├─ request_type: analysis | selection
     └─ 내용: system, user_prompt, response, metadata
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import asdict

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


class LLMLogger:
    """LLM 요청/응답 로깅"""

    def __init__(self, log_dir: str = "logs/llm_requests"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_request(
        self,
        backend: str,
        request_type: str,  # "analysis" | "selection"
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        LLM 요청을 로깅합니다.

        Returns:
            request_id: 응답 로깅할 때 사용
        """
        kst_now = datetime.now(KST)
        timestamp = kst_now.strftime("%Y-%m-%d_%H-%M-%S")
        request_id = f"{timestamp}_{request_type}"

        log_data = {
            "request_id": request_id,
            "timestamp": kst_now.isoformat(),
            "backend": backend,
            "request_type": request_type,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "user_prompt_length": len(user_prompt),
            "system_prompt_length": len(system_prompt),
        }

        log_file = self.log_dir / f"{request_id}_input.json"
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"LLM 요청 로깅: {log_file}")
        except Exception as e:
            logger.error(f"LLM 요청 로깅 실패: {e}")

        return request_id

    def log_response(
        self,
        request_id: str,
        response_text: str,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        duration_ms: float = 0,
        success: bool = True,
        error: str = "",
    ) -> None:
        """
        LLM 응답을 로깅합니다.

        Args:
            request_id: log_request() 반환값
            response_text: LLM 응답 텍스트
            prompt_tokens: 프롬프트 토큰 수
            output_tokens: 출력 토큰 수
            total_tokens: 전체 토큰 수
            duration_ms: 소요 시간 (ms)
            success: 성공 여부
            error: 오류 메시지
        """
        kst_now = datetime.now(KST)

        log_data = {
            "request_id": request_id,
            "timestamp": kst_now.isoformat(),
            "response_text": response_text,
            "response_length": len(response_text),
            "tokens": {
                "prompt": prompt_tokens,
                "output": output_tokens,
                "total": total_tokens,
            },
            "duration_ms": duration_ms,
            "success": success,
            "error": error,
        }

        log_file = self.log_dir / f"{request_id}_output.json"
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"LLM 응답 로깅: {log_file}")
        except Exception as e:
            logger.error(f"LLM 응답 로깅 실패: {e}")

    def log_metadata(
        self,
        request_id: str,
        backend: str,
        request_type: str,
        duration_ms: float,
        tokens: int,
        success: bool,
    ) -> None:
        """
        LLM 메타데이터를 로깅합니다 (빠른 검색용).

        Args:
            request_id: 요청 ID
            backend: LLM 백엔드 (gemini/openai/grok)
            request_type: 요청 유형 (analysis/selection)
            duration_ms: 소요 시간
            tokens: 전체 토큰 수
            success: 성공 여부
        """
        kst_now = datetime.now(KST)

        log_data = {
            "request_id": request_id,
            "timestamp": kst_now.isoformat(),
            "backend": backend,
            "request_type": request_type,
            "duration_ms": duration_ms,
            "tokens": tokens,
            "success": success,
        }

        log_file = self.log_dir / f"{request_id}_metadata.json"
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"LLM 메타데이터 로깅 실패: {e}")


# 전역 로거 인스턴스
_llm_logger: LLMLogger | None = None


def get_llm_logger() -> LLMLogger:
    """전역 LLM 로거 인스턴스 반환"""
    global _llm_logger
    if _llm_logger is None:
        _llm_logger = LLMLogger()
    return _llm_logger
