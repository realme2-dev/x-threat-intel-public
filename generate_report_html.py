"""
최신 _report_*.json을 읽어 docs/index.html을 생성합니다.
GitHub Pages에서 서빙됩니다.
"""
import json
import glob
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


def markdown_to_html(text: str) -> str:
    """간단한 마크다운 → HTML 변환."""
    # 코드블록
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # ### 헤더
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    # 리스트 항목
    text = re.sub(r'^\*   (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    text = re.sub(r'^\* (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    # URL 링크
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<a href="\2" target="_blank">\1</a>', text)
    # 줄바꿈
    text = text.replace('\n', '<br>')
    return text


def threat_score_color(summary: str) -> str:
    m = re.search(r'위협 점수.*?(\d+)/10', summary)
    if m:
        score = int(m.group(1))
        if score >= 8:
            return '#ef4444', '높음'
        elif score >= 5:
            return '#f97316', '중간'
        else:
            return '#22c55e', '낮음'
    return '#6b7280', '알 수 없음'


def extract_threat_score(summary: str):
    """LLM 요약 텍스트에서 위협 점수(0~10)를 추출."""
    if not summary:
        return None
    m = re.search(r'위협 점수.*?(\d+(?:\.\d+)?)/10', summary)
    if m:
        return float(m.group(1))
    return None


def extract_key_cves(summary: str, limit: int = 5):
    """LLM 요약 텍스트의 '주요 CVE / 취약점' 목록 항목을 추출.

    번호가 붙은 섹션 헤더(### 4. ...)에 의존하지 않고 CVE ID가 포함된
    리스트 항목 줄을 직접 찾는다 (헤더 번호가 리포트마다 흔들리는 경우가 있어서).
    """
    if not summary:
        return []
    items = []
    seen = set()
    for line in summary.splitlines():
        line = line.strip()
        m = re.match(r'^\*\s+\*\*(CVE-\d{4}-\d+):?\*\*:?\s*(.*)$', line)
        if not m:
            continue
        cve_id, rest = m.group(1), m.group(2).strip(' :')
        if cve_id in seen:
            continue
        seen.add(cve_id)
        rest_clean = re.sub(r'\*\*', '', rest)
        severity = ''
        for sev in ('Critical', 'High', '심각', '높음'):
            if sev.lower() in rest_clean.lower():
                severity = sev
                break
        # 마크다운 링크 [출처](url) -> <a>
        rest_html = re.sub(
            r'\[([^\]]+)\]\((https?://[^\)]+)\)',
            r'<a href="\2" target="_blank">\1</a>',
            rest_clean,
        )
        items.append({'cve': cve_id, 'detail': rest_html, 'severity': severity})
        if len(items) >= limit:
            break
    return items


def render_news_breakdown(report: dict) -> str:
    """트위터 크롤링 / 해외 뉴스 / 국내 뉴스 수집 건수를 분류해 카드로 렌더링."""
    total_tweets = report.get('total_tweets', 0)
    news_articles = report.get('news_articles', [])
    domestic = [a for a in news_articles if a.get('region') == 'domestic']
    international = [a for a in news_articles if a.get('region') != 'domestic']

    def source_breakdown(articles):
        counts = {}
        for a in articles:
            src = a.get('source', '기타')
            counts[src] = counts.get(src, 0) + 1
        return ', '.join(f'{k}({v})' for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))

    intl_detail = source_breakdown(international) or '수집된 기사 없음'
    dom_detail = source_breakdown(domestic) or '수집된 기사 없음'

    return f'''
  <section>
    <h2>🗂️ 수집 채널별 분류</h2>
    <div class="channel-grid">
      <div class="channel-card">
        <div class="channel-icon">🐦</div>
        <div class="channel-num">{total_tweets}</div>
        <div class="channel-label">트위터(X) 크롤링</div>
        <div class="channel-detail">키워드/계정 기반 실시간 트윗</div>
      </div>
      <div class="channel-card">
        <div class="channel-icon">🌍</div>
        <div class="channel-num">{len(international)}</div>
        <div class="channel-label">해외 뉴스 · 블로그</div>
        <div class="channel-detail">{intl_detail}</div>
      </div>
      <div class="channel-card">
        <div class="channel-icon">🇰🇷</div>
        <div class="channel-num">{len(domestic)}</div>
        <div class="channel-label">국내 뉴스 · 블로그</div>
        <div class="channel-detail">{dom_detail}</div>
      </div>
    </div>
  </section>'''


def render_top_keywords(top_words, min_count: int = 5, max_count: int = 10) -> str:
    """이번 수집의 TOP 키워드를 5~10개 배지로 렌더링."""
    n = max(min_count, min(max_count, len(top_words)))
    selected = top_words[:n]
    if not selected:
        return '<p class="trend-empty">이번 수집에서 추출된 키워드가 없습니다.</p>'
    badges = ''
    for rank, (word, count) in enumerate(selected, 1):
        badges += f'<span class="keyword-badge"><span class="kw-rank">{rank}</span>{word} <small>({count})</small></span>'
    return f'<div class="keyword-badge-row">{badges}</div>'


def extract_threat_history(max_points: int = 14):
    """최근 리포트 파일들에서 (일시, 위협점수)를 시간순으로 추출."""
    data_dir = Path('data')
    reports = sorted(data_dir.glob('_report_*.json'))
    history = []
    for path in reports:
        try:
            with open(path, encoding='utf-8') as f:
                r = json.load(f)
        except Exception:
            continue
        score = extract_threat_score(r.get('llm_summary', ''))
        if score is None:
            continue
        ts = r.get('generated_at', '')
        try:
            dt = datetime.fromisoformat(ts).astimezone(timezone(timedelta(hours=9)))
        except Exception:
            continue
        history.append((dt, score))
    return history[-max_points:]


def _score_band_color(score: float) -> str:
    if score >= 8:
        return '#ef4444'
    elif score >= 5:
        return '#f97316'
    return '#22c55e'


def render_trend_svg(history) -> str:
    """위협 점수 추이를 다크 배경에 어울리는 단일 시리즈 SVG 라인차트로 렌더링.

    - 0/2.5/5/7.5/10 그리드 + 위험 구간(고/중/저) 배경 밴드
    - 평균선
    - 모든 점에 값 직접 라벨 + 전 대비 증감 뱃지
    - 점수 구간(고/중/저)별 마커 색상
    """
    if len(history) < 2:
        return '<p class="trend-empty">추이를 그리기에 데이터가 부족합니다. (최소 2개 리포트 필요)</p>'

    width, height = 720, 260
    pad_left, pad_right, pad_top, pad_bottom = 40, 20, 20, 34
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(history)

    def x_at(i):
        return pad_left + (plot_w * i / (n - 1) if n > 1 else 0)

    def y_at(score):
        return pad_top + plot_h * (1 - score / 10)

    points = [(x_at(i), y_at(score)) for i, (_, score) in enumerate(history)]
    line_path = 'M ' + ' L '.join(f'{x:.1f},{y:.1f}' for x, y in points)
    area_path = line_path + f' L {points[-1][0]:.1f},{pad_top + plot_h:.1f} L {points[0][0]:.1f},{pad_top + plot_h:.1f} Z'

    # 위험 구간 배경 밴드 (고 8-10 / 중 5-8 / 저 0-5)
    bands = ''
    band_defs = [(8, 10, '#ef4444', 0.07), (5, 8, '#f97316', 0.06), (0, 5, '#22c55e', 0.05)]
    for lo, hi, color, opacity in band_defs:
        y_hi, y_lo = y_at(hi), y_at(lo)
        bands += f'<rect x="{pad_left}" y="{y_hi:.1f}" width="{plot_w}" height="{(y_lo - y_hi):.1f}" fill="{color}" opacity="{opacity}"/>'

    # 가로 그리드라인 (0/2.5/5/7.5/10)
    gridlines = ''
    for val in (0, 2.5, 5, 7.5, 10):
        y = y_at(val)
        gridlines += f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" class="grid-line"/>'
        label = f'{val:g}'
        gridlines += f'<text x="{pad_left - 8}" y="{y + 4:.1f}" class="axis-label" text-anchor="end">{label}</text>'

    # 평균선
    avg = sum(s for _, s in history) / n
    avg_y = y_at(avg)
    avg_line = (
        f'<line x1="{pad_left}" y1="{avg_y:.1f}" x2="{width - pad_right}" y2="{avg_y:.1f}" class="avg-line"/>'
        f'<text x="{width - pad_right}" y="{avg_y - 5:.1f}" class="avg-label" text-anchor="end">평균 {avg:.1f}</text>'
    )

    # x축 라벨: 겹치지 않는 선에서 최대한 표시 (최대 7개)
    max_labels = min(n, 7)
    step = max(1, round((n - 1) / max(max_labels - 1, 1)))
    label_idx = sorted(set(list(range(0, n, step)) + [n - 1]))
    x_labels = ''
    for i in label_idx:
        dt, _ = history[i]
        x_labels += f'<text x="{x_at(i):.1f}" y="{height - 10}" class="axis-label" text-anchor="middle">{dt.strftime("%m/%d %H시")}</text>'

    # 마커 + 값 라벨 + 증감 뱃지(직전 대비)
    markers = ''
    for i, (dt, score) in enumerate(history):
        x, y = points[i]
        color = _score_band_color(score)
        delta_str = ''
        if i > 0:
            prev = history[i - 1][1]
            diff = score - prev
            if diff > 0:
                delta_str = f' (전회 대비 +{diff:g})'
            elif diff < 0:
                delta_str = f' (전회 대비 {diff:g})'
            else:
                delta_str = ' (전회 대비 변동 없음)'
        tooltip = f'{dt.strftime("%Y-%m-%d %H:%M KST")} · 위협 점수 {score:g}/10{delta_str}'
        markers += (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" class="trend-dot">'
            f'<title>{tooltip}</title></circle>'
        )
        # 값 라벨은 점 위/아래 번갈아 배치해 겹침 완화
        label_y = y - 12 if i % 2 == 0 else y + 20
        markers += f'<text x="{x:.1f}" y="{label_y:.1f}" class="trend-value-label" text-anchor="middle">{score:g}</text>'

    first_score, last_score = history[0][1], history[-1][1]
    overall_diff = last_score - first_score
    trend_word = '상승' if overall_diff > 0 else ('하락' if overall_diff < 0 else '변동 없음')
    caption = (
        f'<div class="trend-caption">기간 내 평균 <strong>{avg:.1f}/10</strong> · '
        f'최고 <strong>{max(s for _, s in history):g}</strong> · '
        f'최저 <strong>{min(s for _, s in history):g}</strong> · '
        f'첫 리포트 대비 <strong>{trend_word}</strong> '
        f'({first_score:g} → {last_score:g})</div>'
    )

    svg = f'''<svg viewBox="0 0 {width} {height}" class="trend-svg" preserveAspectRatio="xMidYMid meet">
      {bands}
      {gridlines}
      <path d="{area_path}" class="trend-area"/>
      {avg_line}
      <path d="{line_path}" class="trend-line"/>
      {markers}
      {x_labels}
    </svg>'''
    return svg + caption


def generate_html(report: dict, threat_history=None) -> str:
    generated_at = report.get('generated_at', '')[:16].replace('T', ' ')
    # KST 변환
    try:
        dt = datetime.fromisoformat(report['generated_at'])
        kst = dt.astimezone(timezone(timedelta(hours=9)))
        generated_at = kst.strftime('%Y-%m-%d %H:%M KST')
    except Exception:
        pass

    total_tweets = report.get('total_tweets', 0)
    total_targets = report.get('total_targets', 0)
    llm_summary = report.get('llm_summary', '')
    llm_top_tweets = report.get('llm_top_tweets', [])
    top_words = report.get('top_words', [])[:10]
    top_hashtags = report.get('top_hashtags', [])[:8]
    keyword_summary = report.get('keyword_summary', [])
    account_summary = report.get('account_summary', [])

    score_color, score_label = threat_score_color(llm_summary)
    score_match = re.search(r'위협 점수.*?(\d+)/10', llm_summary)
    score_num = score_match.group(1) if score_match else '?'

    # 상위 트윗 카드
    top_tweet_cards = ''
    for t in llm_top_tweets[:5]:
        rank = t.get('rank', '')
        level = t.get('threat_level', '')
        username = t.get('username', '')
        date = t.get('date', '')[:16]
        text = t.get('text', '')
        link = t.get('link', '')
        reason = t.get('reason', '')
        level_color = '#ef4444' if '심각' in level or '높음' in level or 'HIGH' in level.upper() else '#f97316'
        top_tweet_cards += f'''
        <div class="tweet-card">
            <div class="tweet-header">
                <span class="rank">#{rank}</span>
                <span class="threat-badge" style="background:{level_color}">{level}</span>
                <a href="{link}" target="_blank" class="username">@{username}</a>
                <span class="date">{date}</span>
            </div>
            <div class="tweet-text">{text}</div>
            <div class="reason">💡 {reason}</div>
        </div>'''

    # 키워드 워드클라우드 (크기 기반)
    word_tags = ''
    if top_words:
        max_count = top_words[0][1] if top_words else 1
        for word, count in top_words:
            size = 14 + int((count / max_count) * 20)
            word_tags += f'<span class="word-tag" style="font-size:{size}px">{word} <small>({count})</small></span> '

    # 해시태그
    hashtag_tags = ''
    for tag, count in top_hashtags:
        hashtag_tags += f'<span class="hashtag-tag">{tag} <small>{count}</small></span> '

    # 수집 현황 테이블 + 모달용 트윗 데이터
    summary_rows = ''
    modal_data = {}  # target -> list of tweet dicts
    for s in sorted(keyword_summary + account_summary, key=lambda x: -x.get('tweet_count', 0)):
        target = s.get('target', '')
        ttype = s.get('type', '')
        count = s.get('tweet_count', 0)
        method = s.get('method', '')
        crawled_at = s.get('crawled_at', '')[:16].replace('T', ' ')
        icon = '🔍' if ttype == 'keyword' else '👤'
        bar_width = min(100, int(count / max(total_tweets, 1) * 100 * 5))
        # sample_tweets 수집
        tweets = s.get('sample_tweets', [])
        target_id = re.sub(r'[^a-zA-Z0-9_]', '_', target)
        modal_data[target_id] = {
            'title': f"{icon} {target}",
            'type': ttype,
            'tweets': tweets
        }
        summary_rows += f'''
        <tr class="clickable-row" onclick="openModal('{target_id}')" title="클릭하여 트윗 보기">
            <td>{icon} {target} <span class="view-icon">👁</span></td>
            <td>{count}</td>
            <td><div class="bar" style="width:{bar_width}px"></div></td>
            <td><span class="method-badge">{method}</span></td>
            <td class="time">{crawled_at}</td>
        </tr>'''

    modal_json = json.dumps(modal_data, ensure_ascii=False)

    llm_html = markdown_to_html(llm_summary) if llm_summary else '<p>LLM 분석 없음</p>'

    # 주요 CVE / 취약점 하이라이트
    key_cves = extract_key_cves(llm_summary)
    cve_cards = ''
    for c in key_cves:
        sev = c['severity']
        sev_badge = f'<span class="cve-severity">{sev}</span>' if sev else ''
        cve_cards += f'''
        <div class="cve-card">
            <div class="cve-id">⚠️ {c['cve']} {sev_badge}</div>
            <div class="cve-detail">{c['detail']}</div>
        </div>'''
    cve_section = ''
    if cve_cards:
        cve_section = f'''
  <section>
    <h2>⚠️ 주의해야 할 핵심 취약점</h2>
    <div class="cve-grid">{cve_cards}</div>
  </section>'''

    # 위협 점수 추이 (일주일 추이, 상단 배치)
    trend_section = ''
    if threat_history:
        trend_svg = render_trend_svg(threat_history)
        trend_section = f'''
  <section>
    <h2>📈 위협 점수 추이 (최근 {len(threat_history)}회 수집)</h2>
    <div class="trend-box">{trend_svg}</div>
  </section>'''

    # 이번 수집 TOP 키워드 (5~10개)
    top_keywords_html = render_top_keywords(report.get('top_words', []))
    keyword_highlight_section = f'''
  <section>
    <h2>🔑 이번 수집 TOP 키워드</h2>
    <div class="words-box">{top_keywords_html}</div>
  </section>'''

    # 수집 채널 분류 (트위터 / 해외뉴스 / 국내뉴스)
    news_breakdown_section = render_news_breakdown(report)

    return f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>X 위협 인텔리전스 대시보드</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e2e8f0; line-height: 1.6; }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
  header {{ border-bottom: 1px solid #1e293b; padding-bottom: 20px; margin-bottom: 28px; }}
  header h1 {{ font-size: 1.6rem; color: #f1f5f9; }}
  header .meta {{ color: #64748b; font-size: 0.85rem; margin-top: 4px; }}
  .stats-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 28px; }}
  .stat-card {{ background: #1e293b; border-radius: 10px; padding: 18px; text-align: center; }}
  .stat-card .num {{ font-size: 2rem; font-weight: 700; }}
  .stat-card .label {{ font-size: 0.8rem; color: #94a3b8; margin-top: 4px; }}
  .threat-score {{ color: {score_color}; }}
  section {{ margin-bottom: 32px; }}
  section h2 {{ font-size: 1.1rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 14px; border-left: 3px solid #3b82f6; padding-left: 10px; }}
  .llm-box {{ background: #1e293b; border-radius: 10px; padding: 20px; font-size: 0.9rem; line-height: 1.8; }}
  .llm-box h2, .llm-box h3 {{ color: #60a5fa; margin: 12px 0 6px; }}
  .llm-box li {{ margin-left: 20px; margin-bottom: 4px; }}
  .llm-box strong {{ color: #fbbf24; }}
  .llm-box code {{ background: #0f172a; padding: 1px 5px; border-radius: 3px; font-size: 0.85em; }}
  .llm-box a {{ color: #7dd3fc; text-decoration: underline; text-underline-offset: 2px; }}
  .llm-box a:hover {{ color: #bae6fd; }}
  .cve-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }}
  .cve-card {{ background: #1e293b; border-radius: 10px; padding: 14px 16px; border-left: 3px solid #ef4444; }}
  .cve-id {{ font-weight: 700; color: #fca5a5; font-size: 0.9rem; display: flex; align-items: center; gap: 8px; }}
  .cve-severity {{ background: #7f1d1d; color: #fecaca; font-size: 0.7rem; padding: 2px 8px; border-radius: 20px; font-weight: 600; }}
  .cve-detail {{ font-size: 0.83rem; color: #cbd5e1; margin-top: 6px; line-height: 1.5; }}
  .trend-box {{ background: #1e293b; border-radius: 10px; padding: 20px; }}
  .trend-svg {{ width: 100%; height: auto; overflow: visible; }}
  .trend-svg .grid-line {{ stroke: #334155; stroke-width: 1; }}
  .trend-svg .axis-label {{ fill: #64748b; font-size: 10px; }}
  .trend-svg .avg-line {{ stroke: #64748b; stroke-width: 1; stroke-dasharray: 4 3; }}
  .trend-svg .avg-label {{ fill: #94a3b8; font-size: 10px; font-style: italic; }}
  .trend-svg .trend-line {{ fill: none; stroke: #3b82f6; stroke-width: 2.5; stroke-linejoin: round; stroke-linecap: round; }}
  .trend-svg .trend-area {{ fill: #3b82f6; opacity: 0.10; }}
  .trend-svg .trend-dot {{ stroke: #1e293b; stroke-width: 2; cursor: pointer; }}
  .trend-svg .trend-value-label {{ fill: #cbd5e1; font-size: 10px; font-weight: 600; }}
  .trend-caption {{ margin-top: 10px; font-size: 0.82rem; color: #94a3b8; text-align: center; }}
  .trend-caption strong {{ color: #e2e8f0; }}
  .trend-empty {{ color: #64748b; font-size: 0.85rem; text-align: center; padding: 20px 0; }}
  .keyword-badge-row {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  .keyword-badge {{ display: inline-flex; align-items: center; gap: 6px; background: #0f172a; border: 1px solid #334155; color: #e2e8f0; padding: 6px 14px; border-radius: 20px; font-size: 0.88rem; font-weight: 600; }}
  .keyword-badge small {{ color: #64748b; font-weight: 500; }}
  .kw-rank {{ display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; background: #3b82f6; color: white; border-radius: 50%; font-size: 0.7rem; font-weight: 700; }}
  .channel-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }}
  .channel-card {{ background: #1e293b; border-radius: 10px; padding: 18px; text-align: center; }}
  .channel-icon {{ font-size: 1.6rem; }}
  .channel-num {{ font-size: 1.8rem; font-weight: 700; color: #f1f5f9; margin-top: 4px; }}
  .channel-label {{ font-size: 0.85rem; color: #94a3b8; margin-top: 2px; font-weight: 600; }}
  .channel-detail {{ font-size: 0.75rem; color: #64748b; margin-top: 6px; line-height: 1.5; }}
  .tweet-card {{ background: #1e293b; border-radius: 10px; padding: 16px; margin-bottom: 12px; border-left: 3px solid #3b82f6; }}
  .tweet-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }}
  .rank {{ font-weight: 700; color: #60a5fa; }}
  .threat-badge {{ padding: 2px 8px; border-radius: 20px; font-size: 0.75rem; color: white; }}
  .username {{ color: #60a5fa; text-decoration: none; font-weight: 600; }}
  .username:hover {{ text-decoration: underline; }}
  .date {{ color: #64748b; font-size: 0.8rem; margin-left: auto; }}
  .tweet-text {{ font-size: 0.9rem; margin-bottom: 8px; }}
  .reason {{ font-size: 0.8rem; color: #94a3b8; background: #0f172a; padding: 6px 10px; border-radius: 6px; }}
  .words-box {{ background: #1e293b; border-radius: 10px; padding: 20px; }}
  .word-tag {{ display: inline-block; margin: 4px; color: #60a5fa; font-weight: 600; }}
  .word-tag small {{ color: #64748b; font-size: 0.7em; }}
  .hashtag-tag {{ display: inline-block; background: #1e3a5f; color: #93c5fd; padding: 3px 10px; border-radius: 20px; margin: 4px; font-size: 0.85rem; }}
  .hashtag-tag small {{ color: #64748b; }}
  table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; }}
  th {{ background: #0f172a; color: #64748b; font-size: 0.75rem; text-transform: uppercase; padding: 10px 14px; text-align: left; }}
  td {{ padding: 10px 14px; border-bottom: 1px solid #0f172a; font-size: 0.87rem; }}
  tr:last-child td {{ border-bottom: none; }}
  .bar {{ height: 6px; background: #3b82f6; border-radius: 3px; min-width: 4px; }}
  .method-badge {{ background: #064e3b; color: #6ee7b7; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; }}
  .time {{ color: #64748b; font-size: 0.8rem; }}
  .clickable-row {{ cursor: pointer; transition: background 0.15s; }}
  .clickable-row:hover {{ background: #263248 !important; }}
  .view-icon {{ font-size: 0.75rem; opacity: 0.5; margin-left: 4px; }}
  .clickable-row:hover .view-icon {{ opacity: 1; }}
  /* 모달 */
  .modal-overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.75); z-index: 1000; overflow-y: auto; padding: 20px; }}
  .modal-overlay.open {{ display: flex; align-items: flex-start; justify-content: center; }}
  .modal-box {{ background: #1e293b; border-radius: 14px; width: 100%; max-width: 760px; margin: auto; overflow: hidden; }}
  .modal-header {{ background: #0f172a; padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; }}
  .modal-header h3 {{ font-size: 1rem; color: #f1f5f9; }}
  .modal-header .close-btn {{ background: none; border: none; color: #94a3b8; font-size: 1.4rem; cursor: pointer; line-height: 1; }}
  .modal-header .close-btn:hover {{ color: #f1f5f9; }}
  .modal-body {{ padding: 16px 20px; max-height: 70vh; overflow-y: auto; }}
  .modal-tweet {{ border-bottom: 1px solid #0f172a; padding: 12px 0; }}
  .modal-tweet:last-child {{ border-bottom: none; }}
  .modal-tweet-meta {{ display: flex; gap: 10px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }}
  .modal-tweet-user {{ color: #60a5fa; font-weight: 600; font-size: 0.85rem; text-decoration: none; }}
  .modal-tweet-user:hover {{ text-decoration: underline; }}
  .modal-tweet-date {{ color: #64748b; font-size: 0.78rem; margin-left: auto; }}
  .modal-tweet-text {{ font-size: 0.87rem; line-height: 1.6; color: #cbd5e1; }}
  .modal-tweet-link {{ display: inline-block; margin-top: 6px; font-size: 0.78rem; color: #3b82f6; text-decoration: none; }}
  .modal-tweet-link:hover {{ text-decoration: underline; }}
  .modal-empty {{ color: #64748b; text-align: center; padding: 30px 0; font-size: 0.9rem; }}
  footer {{ text-align: center; color: #334155; font-size: 0.8rem; padding: 20px 0; border-top: 1px solid #1e293b; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🛡️ X 사이버 위협 인텔리전스 대시보드</h1>
    <div class="meta">마지막 업데이트: {generated_at}</div>
  </header>

  <div class="stats-row">
    <div class="stat-card">
      <div class="num">{total_tweets}</div>
      <div class="label">수집된 트윗</div>
    </div>
    <div class="stat-card">
      <div class="num">{total_targets}</div>
      <div class="label">모니터링 타겟</div>
    </div>
    <div class="stat-card">
      <div class="num threat-score">{score_num}/10</div>
      <div class="label">위협 수준 ({score_label})</div>
    </div>
    <div class="stat-card">
      <div class="num">{len(llm_top_tweets)}</div>
      <div class="label">주요 위협 트윗</div>
    </div>
  </div>

{trend_section}
{keyword_highlight_section}
{news_breakdown_section}

  <section>
    <h2>🤖 AI 위협 분석 요약</h2>
    <div class="llm-box">{llm_html}</div>
  </section>
{cve_section}

  <section>
    <h2>🔥 주요 위협 트윗 TOP {len(llm_top_tweets[:5])}</h2>
    {top_tweet_cards}
  </section>

  <section>
    <h2>📊 트렌드 키워드 (전체)</h2>
    <div class="words-box">{word_tags}</div>
  </section>

  <section>
    <h2>#️⃣ 해시태그</h2>
    <div class="words-box">{hashtag_tags}</div>
  </section>

  <section>
    <h2>📋 수집 현황</h2>
    <table>
      <thead><tr><th>타겟</th><th>트윗 수</th><th>비율</th><th>방법</th><th>수집 시각</th></tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </section>

  <footer>
    X Threat Intelligence Crawler · GitHub Actions 자동 업데이트 · {generated_at}
  </footer>
</div>

<!-- 트윗 모달 -->
<div class="modal-overlay" id="tweetModal" onclick="handleOverlayClick(event)">
  <div class="modal-box">
    <div class="modal-header">
      <h3 id="modalTitle">트윗 목록</h3>
      <button class="close-btn" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>

<script>
const MODAL_DATA = {modal_json};

function openModal(targetId) {{
  const data = MODAL_DATA[targetId];
  if (!data) return;

  document.getElementById('modalTitle').textContent = data.title + ' 수집 트윗';
  const body = document.getElementById('modalBody');

  if (!data.tweets || data.tweets.length === 0) {{
    body.innerHTML = '<div class="modal-empty">저장된 샘플 트윗이 없습니다.</div>';
  }} else {{
    body.innerHTML = data.tweets.map(t => {{
      const username = t.username || '';
      const text = (t.text || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const link = t.link || '#';
      const date = (t.date || '').substring(0, 22);
      return `<div class="modal-tweet">
        <div class="modal-tweet-meta">
          <a class="modal-tweet-user" href="https://twitter.com/${{username}}" target="_blank">@${{username}}</a>
          <span class="modal-tweet-date">${{date}}</span>
        </div>
        <div class="modal-tweet-text">${{text}}</div>
        <a class="modal-tweet-link" href="${{link}}" target="_blank">🔗 트윗 원문 보기</a>
      </div>`;
    }}).join('');
  }}

  document.getElementById('tweetModal').classList.add('open');
  document.body.style.overflow = 'hidden';
}}

function closeModal() {{
  document.getElementById('tweetModal').classList.remove('open');
  document.body.style.overflow = '';
}}

function handleOverlayClick(e) {{
  if (e.target === document.getElementById('tweetModal')) closeModal();
}}

document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});
</script>
</body>
</html>'''


def main():
    data_dir = Path('data')
    reports = sorted(data_dir.glob('_report_*.json'))
    if not reports:
        print("리포트 파일 없음. 먼저 크롤링을 실행하세요.")
        sys.exit(1)

    latest = reports[-1]
    print(f"리포트 로드: {latest}")

    with open(latest, encoding='utf-8') as f:
        report = json.load(f)

    docs_dir = Path('docs')
    docs_dir.mkdir(exist_ok=True)

    threat_history = extract_threat_history()
    html = generate_html(report, threat_history)
    output = docs_dir / 'index.html'
    output.write_text(html, encoding='utf-8')
    print(f"HTML 생성 완료: {output}")


if __name__ == '__main__':
    main()
