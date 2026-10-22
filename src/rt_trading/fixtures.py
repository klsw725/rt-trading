"""가상 시세 fixture 생성기. 적은 봉만 바꾸고 나머지는 채워 PRD 01과 같은 표 구조의 SQLite 파일을 만든다."""

from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from rt_trading import sessions, store

SYMBOLS = ("069500", "114800")


def build(spec: dict, path: Path) -> None:
    """spec대로 1분봉, 일봉, fetch_runs를 만들어 path에 저장한다.

    spec:
      fetched_at: fetch_runs에 적을 수집 시각
      volume: 기본 거래량(1 이상)
      days: 날짜순 목록. 날짜마다
        date: YYYY-MM-DD. 예상 봉 시각은 krx_sessions.csv의 장 일정을 따른다
        prices: {종목: 그날 첫 봉 앞의 가격}. 없으면 전날 종가를 이어 쓴다
        volume: 그날 기본 거래량. 없으면 spec의 volume
        ratio: {종목: 수정 종가 ÷ 원가격 종가}. 없으면 1
        bars: {종목: {HH:MM: [시가, 고가, 저가, 종가, 거래량] 또는 null}}. null은 그 봉을 빼고, 장 일정 밖 시각은 봉을 더한다
        omit: [[종목, "1m" 또는 "1d"]]. 그날 그 계열을 저장하지 않는다
    적지 않은 봉은 직전 봉 종가를 시가·고가·저가·종가로 하고 기본 거래량을 넣는다. 종가 동시호가 접수 구간 봉은 거래량이 0이다.
    일봉은 그날 1분봉으로 만든다. 시가는 처음 거래량이 있는 봉의 시가이고, 수정 종가는 종가 × 비율을 원 단위로 반올림한다.
    """
    table = sessions.exceptions()
    minute, daily, price = [], [], {}
    for day in spec["days"]:
        date, session = day["date"], sessions.session(day["date"], table)
        price.update(day.get("prices", {}))
        omit = {tuple(pair) for pair in day.get("omit", [])}
        volume = day.get("volume", spec["volume"])
        for symbol in SYMBOLS:
            changes = day.get("bars", {}).get(symbol, {})
            rows, last = [], price[symbol]
            for time in sorted(set(session.bars) | set(changes)):
                if time in changes and changes[time] is None:
                    continue
                bar = changes.get(time) or [last, last, last, last, 0 if time in session.window else volume]
                rows.append((symbol, f"{date} {time}", *bar))
                last = bar[3]
            price[symbol] = last
            if (symbol, "1m") not in omit:
                minute += rows
            if (symbol, "1d") not in omit:
                ratio = Decimal(str(day.get("ratio", {}).get(symbol, 1)))
                adj_close = int((last * ratio).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                open_ = next((row[2] for row in rows if row[6]), rows[0][2])
                high, low = max(row[3] for row in rows), min(row[4] for row in rows)
                daily.append((symbol, date, open_, high, low, last, sum(row[6] for row in rows), adj_close))
    runs = []
    for symbol in SYMBOLS:
        for interval, rows in (("1m", minute), ("1d", daily)):
            dates = sorted({row[1][:10] for row in rows if row[0] == symbol})
            if dates:
                bar_count = sum(row[0] == symbol for row in rows)
                runs.append((spec["fetched_at"], symbol, interval, dates[0], dates[-1], bar_count))
    store.replace(path, minute, daily, runs)
