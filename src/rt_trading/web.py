"""현황 웹페이지 모양: 스타일, 요약 칸, 막대, 판단 지도 칸, 수익률 색(web SPEC)."""

import html

NAMES = {"069500": "KODEX 200", "114800": "KODEX 인버스"}
WEEKDAYS = "월화수목금토일"
STYLE = """
:root{--bg:#f4f5f7;--card:#fff;--text:#1f2329;--muted:#6b7280;--line:#e6e8eb;--head:#f6f7f9;
--good:#15803d;--good-bg:#dcfce7;--warn:#a16207;--warn-bg:#fef3c7;--bad:#b91c1c;--bad-bg:#fee2e2;
--wait:#6b7280;--wait-bg:#eef0f3;--up:#e0245e;--down:#1d6fe0;--accent:#4f46e5;--log:#111318;--log-text:#d4d7dd}
@media (prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#171a20;--text:#e6e8eb;--muted:#9aa1ab;--line:#272c34;--head:#1d2128;
--good:#4ade80;--good-bg:#12301d;--warn:#facc15;--warn-bg:#352a0b;--bad:#f87171;--bad-bg:#3a1414;
--wait:#9aa1ab;--wait-bg:#232830;--up:#fb7185;--down:#60a5fa;--accent:#818cf8;--log:#0a0b0e}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif;font-variant-numeric:tabular-nums}
main{max-width:1100px;margin:0 auto;padding:28px 16px 56px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 16px;margin-bottom:18px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}
.muted{color:var(--muted)}
header .muted{font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card,section{background:var(--card);border:1px solid var(--line);border-radius:14px}
.card{padding:14px 16px}
.card h2{font-size:12px;font-weight:600;color:var(--muted);margin:0 0 6px;letter-spacing:.02em}
.card p{margin:3px 0;font-size:13px}
.card .lead{font-size:15px;font-weight:600}
.big{font-size:26px;font-weight:700;line-height:1.25;margin:2px 0 4px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px;background:var(--wait)}
.dot.run{background:var(--good);box-shadow:0 0 0 3px var(--good-bg)}
.alert{color:var(--bad);font-weight:600}
.hold{display:inline-block;margin:4px 0 2px;padding:2px 10px;border-radius:999px;background:var(--head);border:1px solid var(--line);font-size:13px;font-weight:600}
progress{-webkit-appearance:none;appearance:none;display:block;width:100%;height:6px;border:0;border-radius:3px;background:var(--wait-bg);overflow:hidden;margin:8px 0 6px}
progress::-webkit-progress-bar{background:var(--wait-bg);border-radius:3px}
progress::-webkit-progress-value{background:var(--accent);border-radius:3px}
progress::-moz-progress-bar{background:var(--accent);border-radius:3px}
.up{color:var(--up)}.down{color:var(--down)}
.tone-good{color:var(--good)}.tone-bad{color:var(--bad)}
section{margin-top:14px;padding:16px 18px}
section h2{font-size:16px;margin:0 0 12px}
section>p{margin:0 0 10px;font-size:14px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin:-4px 0 10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.legend i.good{background:var(--good)}.legend i.warn{background:var(--warn)}.legend i.bad{background:var(--bad)}.legend i.wait{background:var(--wait)}
.day{display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.day:first-of-type{border-top:0}
.date{flex:0 0 84px;font-size:13px;font-weight:600;padding-top:5px}
.date.today{color:var(--accent)}
.slots{display:flex;flex-wrap:wrap;gap:6px}
.slot{min-width:88px;border-radius:8px;padding:4px 9px;font-size:12px;line-height:1.35}
.slot b{display:block;font-size:12px}
.good{background:var(--good-bg);color:var(--good)}.warn{background:var(--warn-bg);color:var(--warn)}
.bad{background:var(--bad-bg);color:var(--bad)}.wait{background:var(--wait-bg);color:var(--wait)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:var(--head);color:var(--muted);font-weight:600;text-align:left;white-space:nowrap}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.decisions td{white-space:nowrap}
.decisions td:last-child{white-space:normal;min-width:320px;line-height:1.5}
.accounts th:nth-child(n+2),.accounts td:nth-child(n+2){text-align:right}
.accounts tr:nth-child(2) td{font-weight:600;background:var(--head)}
pre{margin:0;background:var(--log);color:var(--log-text);border-radius:10px;padding:12px 14px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
"""
LEGEND = (
    '<div class="legend"><span><i class="good"></i>정상(ok)</span><span><i class="warn"></i>교체 한도로 기록만(capped)</span>'
    '<span><i class="bad"></i>실패(late·invalid·failed·missing·없음)</span><span><i class="wait"></i>대기</span></div>'
)


def document(title: str, heading: str, body: str) -> str:
    """60초마다 새로 고치는 페이지. 서버 밖의 파일과 스크립트를 쓰지 않는다."""
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>\n<body><main>"
        f'<header><h1>{html.escape(heading)}</h1><span class="muted">60초마다 새로 고침</span></header>\n{body}\n</main></body></html>\n'
    )


def card(title: str, body: str) -> str:
    return f'<div class="card"><h2>{html.escape(title)}</h2>{body}</div>'


def bar(value: float, maximum: float) -> str:
    return f'<progress value="{value}" max="{maximum}"></progress>'


def signed(text: str, value: float) -> str:
    """수익률 색. 플러스는 빨강, 마이너스는 파랑, 0은 기본 색이다."""
    tone = "up" if value > 0 else "down" if value < 0 else ""
    return f'<span class="{tone}">{html.escape(text)}</span>' if tone else html.escape(text)


def slot(time: str, status: str, subordinate: str) -> str:
    """판단 지도의 칸 하나. 구분은 ok가 good, capped가 warn, 대기가 wait, 나머지(실패와 없음)가 bad다."""
    tone = {"ok": "good", "capped": "warn", "대기": "wait"}.get(status, "bad")
    label = f"{status} · {subordinate}" if subordinate else status
    return f'<div class="slot {tone}"><b>{html.escape(time)}</b>{html.escape(label)}</div>'
