"""web acceptance. web SPEC 5절의 사례를 네트워크 없이 실행하고 결과를 JSON으로 출력한다."""

import csv
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from rt_trading import report, v3_daily, v4_acceptance, v4_daily, v4_head, v4_serve
from rt_trading.toss import KST

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "docs" / "specs" / "web" / "fixtures" / "page.json"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BANNED_TAGS = {"script", "link", "img", "iframe", "object", "embed"}
BANNED_ATTRIBUTES = {"src", "href", "style"}


class Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: "Node | None") -> None:
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def find(self, tag: str, cls: str | None = None) -> list["Node"]:
        """자손 중 tag(그리고 cls 구분)인 요소를 문서 순서로 돌려준다."""
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if child.tag == tag and (cls is None or cls in child.classes()):
                    found.append(child)
                found += child.find(tag, cls)
        return found


class Tree(HTMLParser):
    """페이지를 요소 나무로 읽고, 나온 모든 시작 태그와 속성을 함께 기록한다."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node("root", [], None)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node.parent and node.tag != tag:
            node = node.parent
        if node.parent:
            self.current = node.parent

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _render(fixture: dict, variant: str | None = None) -> Tree:
    """임시 디렉터리에 저장 상태를 만들고 v4 페이지를 만든다. variant면 최신 보고서의 report.json·trades-head.csv를 그 값으로 바꾼다."""
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        paths = v4_daily.Paths(tmp / "market.sqlite3", tmp / "v4" / "market.sqlite3", tmp / "v4" / "paper.sqlite3", tmp / "v4" / "head", tmp / "v4" / "news", tmp / "us.sqlite3")
        (tmp / "v4_period.json").write_text(json.dumps({"start": fixture["start"]}), encoding="utf-8")
        (tmp / "krx_holidays.csv").write_text("date,reason\n" + "".join(f"{day},{reason}\n" for day, reason in fixture["holidays"]), encoding="utf-8")
        period = v3_daily.load_period(tmp / "v4_period.json", tmp / "krx_holidays.csv", "V4_PERIOD_INVALID")
        for row in fixture["decisions"]:
            v4_head.insert(paths.paper, dict.fromkeys(v4_head.COLUMNS, "") | {"attempts": 0} | row)
        with closing(sqlite3.connect(paths.paper)) as db, db:
            db.execute(f"CREATE TABLE settled_days ({v3_daily.SETTLED})")
            db.executemany("INSERT INTO settled_days VALUES (?, ?)", [(day, "") for day in fixture["settled"]])
        reports = dict(fixture["reports"])
        if variant:
            changed = fixture["variants"][variant]
            latest = reports[fixture["latest"]]
            reports[fixture["latest"]] = {"report": changed["report"] or latest["report"], "trades_head": changed["trades_head"]}
        for report_name, content in reports.items():
            folder = tmp / "reports" / report_name
            folder.mkdir(parents=True)
            (folder / "report.json").write_text(json.dumps(content["report"], ensure_ascii=False), encoding="utf-8")
            if content["trades_head"] is not None:
                with (folder / "trades-head.csv").open("w", newline="", encoding="utf-8") as file:
                    writer = csv.writer(file)
                    writer.writerow(report.TRADE_COLUMNS)
                    writer.writerows(content["trades_head"])
        log_path = tmp / "v4" / "run.log"
        log_path.write_text("".join(f"줄 {number:02d}\n" for number in range(1, fixture["log_lines"] + 1)), encoding="utf-8")
        now = datetime.fromisoformat(fixture["now"]).replace(tzinfo=KST)
        return Tree(v4_serve.page(paths, period, tmp / "reports", log_path, fixture["status"], now))


def _card(tree: Tree, title: str) -> Node:
    return next(card for card in tree.root.find("div", "card") if card.find("h2")[0].text() == title)


def _ratio(bar: Node) -> float:
    return float(bar.attrs["value"]) / float(bar.attrs["max"])


def case_summary_and_map() -> None:
    fixture = _fixture()
    expected, tree = fixture["expected"], _render(fixture)
    titles = [card.find("h2")[0].text() for card in tree.root.find("div", "card")]
    assert titles == expected["cards"], titles
    (settle,) = _card(tree, "기간").find("progress")
    assert abs(_ratio(settle) - expected["settle_bar"][0] / expected["settle_bar"][1]) < 1e-9, settle.attrs
    decision = _card(tree, "판단")
    text = decision.text()
    for line in expected["decision_card"]:
        assert line in text, (line, text)
    (big,) = decision.find("div", "big")
    assert expected["decision_tone"] in big.classes() and big.text() == expected["decision_card"][0], (big.classes(), big.text())
    rows = []
    for day in tree.root.find("div", "day"):
        chips = []
        for chip in day.find("div", "slot"):
            time = chip.find("b")[0].text()
            tones = [name for name in chip.classes() if name != "slot"]
            chips.append([time, chip.text()[len(time) :], *tones])
        rows.append([day.find("div", "date")[0].text(), chips])
    assert rows == expected["map"], rows


def case_head_card() -> None:
    fixture = _fixture()
    expected = fixture["expected"]
    head = _card(_render(fixture), "헤드 계좌")
    text = head.text()
    for word in expected["head"]["present"]:
        assert word in text, (word, text)
    for word in expected["head"]["absent"]:
        assert word not in text, (word, text)
    tone, profit = expected["head"]["profit_tone"]
    spans = [span for span in head.find("span") if tone in span.classes()]
    assert [span.text() for span in spans] == [profit], [(span.classes(), span.text()) for span in head.find("span")]
    (bar,) = head.find("progress")
    assert abs(_ratio(bar) - expected["head"]["drawdown_bar"][0] / expected["head"]["drawdown_bar"][1]) < 1e-9, bar.attrs
    for variant in ("sold", "bare"):
        card = _card(_render(fixture, variant), "헤드 계좌")
        text = card.text()
        for word in expected[variant]["present"]:
            assert word in text, (variant, word, text)
        for word in expected[variant]["absent"]:
            assert word not in text, (variant, word, text)
    bare = _card(_render(fixture, "bare"), "헤드 계좌")
    assert not [span for span in bare.find("span") if {"up", "down"} & set(span.classes())], "수익 0원에 색이 칠해졌다"


def case_no_external() -> None:
    fixture = _fixture()
    for variant in (None, "sold", "bare"):
        tree = _render(fixture, variant)
        for tag, attrs in tree.tags:
            assert tag not in BANNED_TAGS, (variant, tag)
            for key, value in attrs:
                assert key not in BANNED_ATTRIBUTES and not key.startswith("on"), (variant, tag, key)
                assert "javascript:" not in (value or "").lower(), (variant, tag, key, value)
        styles = tree.root.find("style")
        assert len(styles) == 1, (variant, len(styles))
        css = styles[0].text().lower()
        assert "@import" not in css and "url(" not in css, variant


def case_v4_page() -> None:
    v4_acceptance.case_serve_page()


CASES = [
    (
        "요약 칸이 자동 실행·기간·헤드 계좌·판단 순, 정산 막대 1/5, 판단 실패 6번(late·invalid·failed·missing 행과 마감 지난 행 없는 차례, 마감 전에 기록된 실패 포함)·"
        "ok 10번·capped 2번·지난 차례 18개·기간 35차례, 판단 지도는 거래일마다 한 줄이고 수능일은 그날 장 일정의 차례로 칸마다 시각·상태·부하·구분",
        case_summary_and_map,
    ),
    (
        "헤드 계좌 칸: 가장 뒤 보고서의 head 최종 자산·수익(원·%, 마이너스는 파랑)·기준 날짜·낙폭 막대, 매수·매도·매수로 끝나면 마지막 매수 종목·수량, 매수·매도로 끝나면 현금,"
        " 체결 내역이 없으면 기록 없음, run이 없으면 기준 줄 없음, 수익 0이면 부호·색 없이 0원",
        case_head_card,
    ),
    ("서버 밖 파일·스크립트 없음: 금지 태그, src·href·style·on 속성, javascript: 값이 없고 style은 하나이며 @import·url(이 없음(이유 속 태그는 글자로)", case_no_external),
    ("v4-acceptance의 현황 페이지 사례가 바꾸지 않은 채로 통과", case_v4_page),
]


def run() -> int:
    results = []
    for name, case in CASES:
        try:
            case()
            results.append({"case": name, "passed": True})
        except Exception as error:
            results.append({"case": name, "passed": False, "error": f"{type(error).__name__}: {error}"})
    passed = sum(result["passed"] for result in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"results": results, "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1
