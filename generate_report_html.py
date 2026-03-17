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


def generate_html(report: dict) -> str:
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

    # 수집 현황 테이블
    summary_rows = ''
    for s in sorted(keyword_summary + account_summary, key=lambda x: -x.get('tweet_count', 0)):
        target = s.get('target', '')
        ttype = s.get('type', '')
        count = s.get('tweet_count', 0)
        method = s.get('method', '')
        crawled_at = s.get('crawled_at', '')[:16].replace('T', ' ')
        icon = '🔍' if ttype == 'keyword' else '👤'
        bar_width = min(100, int(count / max(total_tweets, 1) * 100 * 5))
        summary_rows += f'''
        <tr>
            <td>{icon} {target}</td>
            <td>{count}</td>
            <td><div class="bar" style="width:{bar_width}px"></div></td>
            <td><span class="method-badge">{method}</span></td>
            <td class="time">{crawled_at}</td>
        </tr>'''

    llm_html = markdown_to_html(llm_summary) if llm_summary else '<p>LLM 분석 없음</p>'

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

  <section>
    <h2>🤖 AI 위협 분석 요약</h2>
    <div class="llm-box">{llm_html}</div>
  </section>

  <section>
    <h2>🔥 주요 위협 트윗 TOP {len(llm_top_tweets[:5])}</h2>
    {top_tweet_cards}
  </section>

  <section>
    <h2>📊 트렌드 키워드</h2>
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

    html = generate_html(report)
    output = docs_dir / 'index.html'
    output.write_text(html, encoding='utf-8')
    print(f"HTML 생성 완료: {output}")


if __name__ == '__main__':
    main()
