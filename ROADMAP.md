# X 크롤러 기능 로드맵

## 현재 구현 완료

- [x] Nitter 기반 X(트위터) 크롤링 (ntscraper + Playwright 폴백)
- [x] 키워드 / 계정 그룹 관리 (`config_v2.json`)
- [x] 멀티스레드 병렬 크롤링 (`--workers N`)
- [x] LLM 위협 분석 (Gemini / OpenAI / Grok 멀티 백엔드)
- [x] 위협 수준 점수 + 색깔 이모지 (🟢🟡🟠🔴)
- [x] Gemini 토큰 사용량 표시
- [x] 텔레그램 알림 (트윗 링크 포함, KST 시간 표시)
- [x] 스케줄링 (APScheduler, 기본 12시간)

---

## 단기 개선 (1~2주)

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
