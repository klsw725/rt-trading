"""v0 acceptance: 손으로 계산한 작은 가상 일봉으로 SPEC 7절을 네트워크 없이 확인한다."""

import subprocess

from rt_trading.v0 import (
    C,
    INVERSE,
    KODEX200,
    ROOT,
    DataError,
    Day,
    Result,
    gap,
    gap_reversal,
    merge,
    metrics,
    overnight,
    same_day_return,
    threshold,
    tick_up_ratio,
)

DATES = [f"2024-02-{d:02d}" for d in range(1, 26)]


def k200_fixture() -> list[Day]:
    """수정 종가는 늘 128이고 수정 시가로 갭을 만든다. 1~20번째 날 갭은 +6.25%와 -6.25%가 번갈아 나온다.

    21번째 날 갭 +6.25%(기준 크기와 같음), 22번째 -6.25%(같음), 23번째 0, 24번째 +50%.
    """
    adj_opens = [128] + [136 if i % 2 else 120 for i in range(1, 21)] + [136, 120, 128, 192]
    days = [Day(date, 30000, 30000, adj_open, 128) for date, adj_open in zip(DATES, adj_opens)]
    days[22] = Day(DATES[22], 30000, 30300, 120, 128)
    return days


def inverse_fixture(first: int) -> list[Day]:
    """first번째 날 상장. 21번째 날은 원가격 1,000원에 열어 1,100원에 닫고, 24번째 날 일봉은 없다."""
    days = {date: Day(date, 1000, 1000, 500, 500) for date in DATES[first:24]}
    if DATES[21] in days:
        days[DATES[21]] = Day(DATES[21], 1000, 1100, 500, 600)
    return list(days.values())


def near(actual: float, expected: float) -> None:
    assert abs(actual - expected) < 1e-12, f"{actual!r} != {expected!r}"


def raises(error: type[Exception], fn, *args) -> None:
    try:
        fn(*args)
    except error:
        return
    raise AssertionError(f"{error.__name__}가 나지 않았다")


def check_overnight() -> None:
    k200 = [
        Day("2024-01-02", 300, 300, 100, 100),
        Day("2024-01-03", 330, 310, 102, 101),
        Day("2024-01-04", 290, 280, 99, 98),
    ]
    results = overnight(k200)
    assert [(r.date, r.buy) for r in results] == [
        ("2024-01-02", None),
        ("2024-01-03", KODEX200),
        ("2024-01-04", KODEX200),
    ]
    near(results[1].net, (102 / 100) * (1 - C) / (1 + C) - 1)
    near(results[2].net, (99 / 101) * (1 - C) / (1 + C) - 1)
    near(results[2].gross, 99 / 101 - 1)


def check_gap_threshold() -> None:
    k200 = k200_fixture()
    near(gap(k200, 1), 0.0625)
    near(gap(k200, 2), -0.0625)
    near(threshold(k200, 21), 0.0625)
    # 24번째 날 기준 크기에는 23번째 날 갭 0이 들어가고 24번째 날 갭 0.5는 빠진다
    near(threshold(k200, 24), 19 * 0.0625 / 20)


def check_entries() -> None:
    results = gap_reversal(k200_fixture(), inverse_fixture(0))
    # 이상이면 인버스, 이하면 KODEX 200, 사이면 쉼, 살 종목 일봉이 없으면 쉼
    assert [(r.date, r.buy) for r in results] == list(zip(DATES[21:], [INVERSE, KODEX200, None, None]))
    g = 1100 / (1000 * (1 + 1 / 1000))
    near(results[0].net, (g * (1 - C) - 0.154 * (g - 1)) / (1 + C) - 1)
    near(results[0].gross, 1100 / 1000 - 1)
    g = 30300 / (30000 * (1 + 5 / 30000))
    near(results[1].net, g * (1 - C) / (1 + C) - 1)
    near(results[1].gross, 30300 / 30000 - 1)


def check_start() -> None:
    assert gap_reversal(k200_fixture(), inverse_fixture(0))[0].date == DATES[21]
    assert gap_reversal(k200_fixture(), inverse_fixture(22))[0].date == DATES[22]


def check_tick() -> None:
    near(tick_up_ratio(1999, "2023-12-11"), 1 / 1999)
    near(tick_up_ratio(2000, "2023-12-11"), 5 / 2000)
    near(tick_up_ratio(1995, "2023-12-08"), 5 / 1995)
    near(tick_up_ratio(2000, "2023-12-08"), 5 / 2000)


def check_costs() -> None:
    assert C == 0.000188
    net, gross = same_day_return(Day("2024-02-21", 2000, 2100, 1, 1), inverse=False)
    near(net, (2100 / 2005) * (1 - C) / (1 + C) - 1)
    near(gross, 2100 / 2000 - 1)
    net, _ = same_day_return(Day("2024-02-21", 1000, 1100, 1, 1), inverse=True)
    near(net, ((1100 / 1001) * (1 - C) - 0.154 * (1100 / 1001 - 1)) / (1 + C) - 1)
    net, _ = same_day_return(Day("2024-02-21", 1000, 900, 1, 1), inverse=True)
    near(net, (900 / 1001) * (1 - C) / (1 + C) - 1)


def check_no_lookahead() -> None:
    k200, inverse, cut = k200_fixture(), inverse_fixture(0), DATES[23]

    def changed(days: list[Day]) -> list[Day]:
        return [
            day if day.date < cut else Day(day.date, day.open * 1.7, day.close * 0.6, day.adj_open * 1.3, day.adj_close * 0.5)
            for day in days
        ]

    for before, after in [
        (overnight(k200), overnight(changed(k200))),
        (gap_reversal(k200, inverse), gap_reversal(changed(k200), changed(inverse))),
    ]:
        assert [r for r in before if r.date < cut] == [r for r in after if r.date < cut]
        assert before != after, "바꾼 미래 일봉이 결과에 닿지 않아 확인이 되지 않는다"


def check_metrics() -> None:
    m = metrics([
        Result("2023-12-27", None, None, None),
        Result("2023-12-28", KODEX200, 0.10, 0.11),
        Result("2024-01-02", KODEX200, -0.05, -0.04),
        Result("2024-01-03", None, None, None),
        Result("2024-01-04", INVERSE, 0.02, 0.03),
        Result("2024-01-05", KODEX200, 0.01, 0.015),
    ])
    final = 1.1 * 0.95 * 1.02 * 1.01
    assert (m["days"], m["trades"]) == (6, 4)
    near(m["total"], final - 1)
    near(m["annualized"], final ** (252 / 6) - 1)
    near(m["max_drawdown"], 0.05)
    near(m["first_half"], 1.1 * 0.95 - 1)
    near(m["second_half"], 1.02 * 1.01 - 1)
    assert list(m["yearly"]) == ["2023", "2024"]
    near(m["yearly"]["2023"], 0.1)
    near(m["yearly"]["2024"], 0.95 * 1.02 * 1.01 - 1)
    near(m["win_rate"], 0.75)
    near(m["avg_trade"], 0.02)
    near(m["gross_total"], 1.11 * 0.96 * 1.03 * 1.015 - 1)
    assert m["passed"]

    def series(*nets: float | None) -> dict:
        return metrics([
            Result(f"2024-03-0{i + 1}", None if net is None else KODEX200, net, net) for i, net in enumerate(nets)
        ])

    m = series(None, 0.5, -0.25, 0.4)  # 최대 낙폭 25%만 기준에 걸린다
    near(m["max_drawdown"], 0.25)
    assert m["total"] > 0 and m["first_half"] > 0 and m["second_half"] > 0 and not m["passed"]
    m = series(None, -0.01, 0.1, 0.01)  # 앞 기간 수익 -1%만 기준에 걸린다
    near(m["first_half"], -0.01)
    assert m["total"] > 0 and m["max_drawdown"] < 0.2 and m["second_half"] > 0 and not m["passed"]


def candle(date: str, open_: int, close: int) -> dict:
    return {"timestamp": f"{date}T00:00:00.000+09:00", "openPrice": str(open_), "closePrice": str(close)}


def check_merge() -> None:
    raw = [candle("2024-01-04", 102, 103), candle("2024-01-03", 100, 101), candle("2024-01-02", 99, 100)]
    adjusted = [candle("2024-01-04", 51, 52), candle("2024-01-03", 50, 51), candle("2024-01-02", 49, 50)]
    # 실행일(2024-01-04) 봉은 버리고 날짜 오름차순으로 합친다
    assert merge(KODEX200, raw, adjusted, "2024-01-04") == [
        Day("2024-01-02", 99, 100, 49, 50),
        Day("2024-01-03", 100, 101, 50, 51),
    ]
    raises(DataError, merge, KODEX200, raw, adjusted[:2], "2024-01-05")
    raises(DataError, merge, KODEX200, raw + raw[:1], adjusted, "2024-01-05")
    raises(DataError, merge, KODEX200, [candle("2024-01-02", 0, 100)], adjusted[2:], "2024-01-05")


def check_gitignore() -> None:
    paths = ["data/v0.sqlite3", "data/reports/v0.json"]
    done = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True)
    assert done.stdout.split() == paths, "data/ 아래 파일이 git에서 제외되지 않는다"


CHECKS = [
    ("종가 베팅 수익률", check_overnight),
    ("갭과 기준 크기(t 제외)", check_gap_threshold),
    ("진입 조건(이상·이하·사이)과 같은 날 수익률", check_entries),
    ("갭 되돌림 시험 시작일", check_start),
    ("호가 한 단계(2023-12-11 전후, 2,000원 경계)", check_tick),
    ("수수료와 인버스 세금", check_costs),
    ("미래 일봉을 바꿔도 전날까지 그대로", check_no_lookahead),
    ("지표와 통과 판정", check_metrics),
    ("원가격·수정 가격 날짜 불일치, 중복, 0원은 실패", check_merge),
    ("data/는 git에서 제외", check_gitignore),
]


def run() -> int:
    failed = 0
    for name, check in CHECKS:
        try:
            check()
            print(f"통과  {name}")
        except Exception as error:
            failed += 1
            print(f"실패  {name}: {type(error).__name__}: {error}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} 통과")
    return 1 if failed else 0
