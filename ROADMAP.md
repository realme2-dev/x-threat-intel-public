# X 크롤러 기술 로드맵 & 구조 분석

## 📌 현재 구현 완료

- [x] Nitter 기반 X(트위터) 크롤링 (ntscraper + Playwright 폴백)
- [x] 키워드 / 계정 그룹 관리 (`config_v2.json`)
- [x] 멀티스레드 병렬 크롤링 (`--workers N`)
- [x] LLM 위협 분석 (Gemini / OpenAI / Grok 멀티 백엔드)
- [x] LLM 503 오류 자동 재시도 (최대 3회, 10초 간격)
- [x] 트윗 선별: 위협도 기준 상위 10개 자동 선별
- [x] 위협 수준 점수 + 색깔 이모지 (🟢🟡🟠🔴)
- [x] Gemini 토큰 사용량 표시
- [x] 텔레그램 알림 (트윗 링크 포함, KST 시간 표시, AI 선별 배지)
- [x] 스케줄링 (APScheduler, 기본 12시간)
- [x] RSS 뉴스/블로그 수집 (24개 피드, IOC 출처 링크 포함)

---

## 🔴 구조적 문제점

### 1. 크롤링 계층 문제

#### ❌ 파서 의존성 (BeautifulSoup만 사용)
**위험:** Nitter HTML 구조 변경 시 전체 파서 실패

**해결안:**
```
다중 파서 체계:
  ├─ Parser 1: BeautifulSoup (기본)
  ├─ Parser 2: 정규식 (폴백)
  └─ Parser 3: ntscraper 내부 구조 (최후)

효과: 특정 인스턴스 장애 시에도 부분 수집 가능
```

---

#### ❌ 인스턴스 신뢰성 검증 부족
**현황:**
```
헬스 체크 = HEAD / → 200? (표면적 확인만)
실제 크롤링 = GET /search?q=test → 404 또는 500 가능
```

**해결안:**
```
강화된 헬스 체크:
  1. HEAD / (인스턴스 활성 확인)
  2. GET /search?q=test (실제 검색 API 테스트)
  3. HTML 유효성 검증 (BeautifulSoup 파싱 가능성)
  4. 응답 시간 측정 (< 3초 요구)

효과: 거짓 양성(false positive) 제거, 신뢰도 향상
```

---

#### ❌ Playwright 블로킹 대기
**문제:**
```
ntscraper 실패 × 3회 → Playwright 시작 (8초 × 42 = 336초)
블로킹 대기로 인한 전체 파이프라인 지연
```

**해결안:**
```
조건부 활성화:
  ├─ 기본: ntscraper만 (2초)
  ├─ 장애 발생 시: 비동기 Playwright (병렬 5개, 4초/각)
  └─ 예상 효과: 8~10분 → 5~7분 (30% 단축)
```

---

### 2. LLM 분석 계층 문제

#### ❌ 고정 프롬프트 (유연성 부족)
**현황:**
```python
# 분석 항목 하드코딩
1. 트렌드
2. 위협 행위자
3. 한국 위협
...
```

**문제:** 새로운 항목 추가 시 코드 수정 필요, 사용자별 커스텀 불가

**해결안:** 프롬프트 템플릿 시스템
```json
{
  "llm_templates": {
    "default": {...},
    "cti_detailed": {...},
    "quick_summary": {...}
  }
}

# 런타임 선택
python main.py --once --llm --template cti_detailed
```

---

#### ❌ 토큰 사용 비효율
**현황:**
```
[7-1] 100개 트윗 → 12,000 토큰 (입력 70%)
[7-2] 50개 트윗 + 뉴스 → 18,000 토큰
합: 30,000 토큰/회

반복 실행 시 중복 입력 불가피
```

**해결안:**
```
1. 입력 압축 (Clustering)
   - 100개 → 10개 군집 대표 (80% 감소)
   - 결과: 12,000 → 3,000 토큰

2. 캐싱
   - 동일 트윗 캐시 히트 → 0 토큰
   - 반복 실행 시 40% 절감

예상 개선: 30,000 → 9,000 토큰 (70% 절감)
```

---

#### ❌ 503 오류 시 분석 스킵
**문제:**
```
Gemini 503 × 3회 재시도
  → 분석 결과 없음
  → 사용자는 기본 정보만 수신

특정 시간대 지속적 503 가능
```

**해결안:** 폴백 메커니즘
```
Step 1: Gemini (토큰 효율)
Step 2: OpenAI (안정적, 유료)
Step 3: 로컬 분류기 (규칙 기반, 토큰 0)
Step 4: 기본 통계 (최소 정보라도 제공)

설정:
{
  "llm_fallback_order": [
    "gemini",
    "openai",
    "local_classifier",
    "basic_stats"
  ]
}
```

---

### 3. 데이터 저장 계층 문제

#### ❌ JSON 플랫 저장 (구조화 부족)
**현황:**
```
data/report_20260311_091127.json
├─ 크기: 800KB~1MB/회
├─ 검색: 전체 파일 읽기 필요 (O(n))
└─ 장기 보관: 1달 = 30MB

문제: 과거 데이터 검색 느림, 트렌드 비교 어려움
```

**해결안:** 이중 저장 모델
```sql
CREATE TABLE reports (
  id INTEGER PRIMARY KEY,
  created_at TIMESTAMP,
  total_tweets INT,
  threat_score REAL,
  llm_summary TEXT,
  INDEX(created_at)
);

CREATE TABLE iocs (
  id INTEGER PRIMARY KEY,
  report_id INT,
  ioc_type TEXT,    -- CVE, IP, domain, hash
  value TEXT UNIQUE,
  source_link TEXT,
  UNIQUE(ioc_type, value),
  INDEX(value)
);

효과:
├─ 빠른 검색 (인덱스)
├─ 중복 IOC 제거
├─ 트렌드 분석 용이 (시계열 쿼리)
└─ CTI 연동 준비 완료
```

---

### 4. CTI 시스템 연동 준비 부족

#### ❌ 데이터 포맷 불일치
**현황:**
```json
우리 포맷:
{
  "llm_top_tweets": [{...}],
  "llm_summary": "..."
}

CTI 기대 포맷:
{
  "iocs": [{
    "type": "CVE",
    "value": "CVE-2026-1603",
    "severity": "HIGH",
    "source": "X",
    "references": [...]
  }]
}
```

**해결안:** IOC 정규화 모듈
```python
# ioc_normalizer.py (신규)
class IOCNormalizer:
  def normalize(raw_ioc) → dict:
    return {
      "type": ioc_type,
      "value": ioc_value,
      "severity": calculate_severity(),
      "source": "X|RSS|LLM",
      "timestamp": now(),
      "references": [...],
      "confidence": 0.85,
      "metadata": {...}
    }

# 사용
iocs = rss_collector.extract_iocs()
normalized = [IOCNormalizer.normalize(ioc) for ioc in iocs]
push_to_cti_system(normalized)
```

---

## 📋 개발 로드맵 (우선순위순)

### Phase 1: 안정성 강화 (1~2주) 🔴 긴급

#### P1-1: 파서 다중화
```
□ TweetParser에 parse_methods 리스트 추가
  ├─ parse_beautifulsoup() - 기존
  ├─ parse_regex() - 정규식 폴백
  └─ parse_ntscraper() - 최후

□ 폴백 로직 구현 (try-except)

□ 성공률 로깅
  └─ instance_stats.json에 파서별 성공률 기록
```

**예상 효과:** 특정 인스턴스 장애 시에도 50% 이상 수집

---

#### P1-2: 인스턴스 신뢰도 추적
```
□ 각 인스턴스별 메트릭 저장
  {
    "url": "https://...",
    "health": 0.95,
    "response_time_ms": 1200,
    "success_count": 142,
    "fail_count": 8,
    "trust_score": 92,  # 0~100
    "last_update": "2026-03-11T09:11Z"
  }

□ 신뢰도 기반 라운드로빈
  └─ 단순 순환 → 가중 순환 (신뢰도 高 우선)

□ 신뢰도 자동 갱신
  └─ 매 요청마다 업데이트
```

**예상 효과:** 인스턴스 선택 최적화, 평균 성공률 상향

---

### Phase 2: LLM 고도화 (2~3주) 🟠 중요

#### P2-1: 프롬프트 템플릿 시스템
```
□ config_v2.json에 llm_templates 섹션 추가
  {
    "llm_templates": {
      "default": {
        "sections": [
          {"id": "trend", "required": true, ...},
          {"id": "korea", "required": false, ...}
        ]
      }
    }
  }

□ 템플릿 엔진 (llm_analyzer.py)
  └─ Jinja2 또는 직접 구현

□ CLI 옵션 추가
  └─ --template <template_name>
```

**예상 효과:** 분석 커스터마이징, 운영 효율성 향상

---

#### P2-2: 503 폴백 메커니즘
```
□ 로컬 분류기 구현
  ├─ 간단한 규칙 기반
  │  └─ CVE 언급 → HIGH, 랜섬웨어 → MEDIUM 등
  ├─ 경량 모델 (100KB 이하)
  └─ 토큰 0 사용

□ 폴백 순서 설정
  {
    "llm_fallback_order": [
      "gemini",
      "openai",
      "local_classifier"
    ]
  }

□ 결과에 출처 명시
  └─ "[Gemini 분석]" vs "[로컬 분석]"
```

**예상 효과:** 503 장애 시에도 최소한의 분석 제공

---

### Phase 3: CTI 연동 (3~4주) 🟡 필수

#### P3-1: IOC 정규화 및 저장
```
□ ioc_normalizer.py 구현
  ├─ normalize_cve()
  ├─ normalize_ip()
  ├─ normalize_domain()
  └─ normalize_hash()

□ 심각도 자동 계산
  └─ NVD API 연동 (CVSS 점수)

□ SQLite 저장
  CREATE TABLE iocs (...)

□ 중복 제거
  └─ UNIQUE(type, value) 제약
```

**예상 효과:** 표준 CTI 포맷 지원, 외부 시스템 연동 준비

---

#### P3-2: REST API 엔드포인트 (선택)
```
□ FastAPI 엔드포인트
  ├─ GET /api/iocs?type=CVE&days=7
  ├─ POST /api/iocs (외부 추가)
  └─ 인증 (API 토큰)

□ STIX 2.1 JSON 변환 (선택)
  └─ stix2 라이브러리

□ 외부 CTI 시스템 연동
  ├─ MISP, ThreatStream
  └─ 정기적 푸시 (일 1회)
```

**예상 효과:** 기업 내 SOAR/SIEM 통합 준비

---

### Phase 4: 성능 최적화 (4~8주) 🟢 선택

#### P4-1: 캐싱 시스템
```
□ 입력 캐시 (트윗 집합)
  ├─ 키: SHA256(트윗들)
  ├─ 값: LLM 응답
  ├─ TTL: 24시간
  └─ 저장: SQLite

□ 트윗 중복 감지
  └─ 캐시 히트 → LLM 호출 스킵

효과: 반복 실행 시 40% 토큰 절감
```

---

#### P4-2: 비동기 크롤링 (asyncio)
```
□ aiohttp로 네트워크 요청 병렬화
  └─ 동시 연결: 10~20개

□ 기존 코드 호환
  └─ ThreadPoolExecutor와 asyncio 교체

예상 효과: 8~10분 → 4~6분 (50% 단축)
```

---

---

## 📊 CTI 시스템 연동 아키텍처

```
┌─────────────────────────────────┐
│ 우리 시스템                      │
│ ├─ data/report_*.json           │
│ ├─ SQLite iocs 테이블           │
│ └─ IOC 정규화 모듈              │
└──────────────┬──────────────────┘
               │ (IOC 정규화)
               ▼
        ┌──────────────┐
        │ STIX 2.1    │
        │ Bundle      │
        └──────────────┘
               │
               ▼
    ┌────────────────────────┐
    │ CTI 시스템 (MISP등)    │
    │ ↕ (양방향 동기화)      │
    └────────────────────────┘
```

**연동 방식 비교:**

| 방식 | 난이도 | 속도 | 신뢰도 | 추천 |
|------|--------|------|--------|------|
| REST API | ⭐⭐ | 빠름 | 높음 | ✅ P3-1부터 |
| STIX JSON | ⭐⭐⭐ | 느림 | 높음 | P3-2 |
| 양방향 동기화 | ⭐⭐⭐⭐ | 중간 | 높음 | P3-2 |

---

## ✅ 기능 구현 완료

### 단기 개선 (1~2주)

### ✅ 급상승 키워드 자동 추가 수집 (완료)
- 크롤링된 트윗에서 빈도 급상승 단어를 추출
- 기존 키워드 목록에 없는 신규 보안 키워드를 자동 탐지
- 탐지된 키워드로 즉시 추가 크롤링 수행

### ✅ 뉴스/블로그 기사 수집 (완료)
- RSS 피드: TheHackerNews, SecurityWeek, DarkReading, SANS ISC, AhnLab ASEC, NIST NVD
- `rss_collector.py` 구현, LLM 분석 및 텔레그램 알림에 포함

### ✅ 중복 트윗 제거 (완료)
- 동일 URL 기준 중복 제거, link 없을 경우 텍스트 앞 80자 기준 제거

---

## 중기 개선 (1개월)

### CVE 자동 조회
- 트윗/기사에서 `CVE-YYYY-XXXXX` 패턴 추출
- NVD API 연동으로 CVSS 점수 / 설명 / 영향 받는 제품 자동 조회
- 심각도 9.0 이상 CVE는 텔레그램 즉시 알림
- 구현 파일: `cve_lookup.py` (신규)

### 위협 행위자 추적
- 특정 APT 그룹 / 랜섬웨어 그룹 동향 자동 태깅
- 과거 데이터와 비교하여 활동 급증 감지
- 예: "Lazarus Group 언급 3배 증가" 알림

### 한국 관련 위협 강조
- 한국어 트윗 / 한국 기업·기관 언급 자동 감지
- 별도 [한국 위협] 섹션으로 텔레그램 알림
- KISA, KrCERT 공지 연동

### 데이터 저장소 개선
- 현재: JSON 파일
- 개선: SQLite 또는 PostgreSQL로 전환
- 시계열 트렌드 분석 (7일, 30일 트렌드 비교)

---

## 장기 개선 (3개월+)

### 웹 대시보드
- FastAPI + 간단한 HTML 대시보드
- 실시간 위협 트렌드 차트 (Chart.js)
- CVE 목록, 위협 행위자 맵, 키워드 히트맵

### 다중 소스 통합
- X(트위터) 외 추가 소스:
  - Reddit r/netsec, r/cybersecurity
  - GitHub Security Advisories
  - Shodan/Censys 노출 자산 모니터링
  - Pastebin / 다크웹 모니터링 (합법적 범위)

### 알림 고도화
- 위협 수준에 따른 알림 채널 분리 (일반/긴급)
- 슬랙 / 이메일 알림 추가
- 반복 알림 방지 (동일 내용 24시간 내 재알림 차단)

### LLM 분석 고도화
- 멀티턴 분석: 트렌드 변화 요약 ("지난주 대비 랜섬웨어 언급 40% 증가")
- 한국 특화 프롬프트: 국내 법령(개인정보보호법, 정보통신망법) 위반 가능성 평가
- 분석 결과 검증: 여러 LLM 교차 검증

---

## 기술 부채 / 리팩토링

- [ ] Playwright 세션 재사용 (현재 매 요청마다 새 브라우저 — 속도 개선 가능)
- [ ] Nitter 인스턴스 자동 발굴 (현재 LibRedirect API 의존)
- [ ] 에러 알림: 크롤링 실패율 50% 이상 시 텔레그램 경고
- [ ] 설정 핫리로드: `config_v2.json` 변경 시 재시작 없이 반영
- [ ] Docker 컨테이너화: `Dockerfile` + `docker-compose.yml` 작성
