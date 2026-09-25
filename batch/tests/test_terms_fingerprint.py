"""利用規約の節ごとの指紋（詳細設計 4.3）。

**関門を厳しく保ったまま、確認を軽くするための仕組み**である。
全体のハッシュは関門として残し、節ごとのハッシュで「どこが変わったか」を指す。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from batch.parser.terms import (
    BEFORE_HEADINGS,
    FingerprintError,
    build_fingerprint,
    describe_change,
    report_terms_change,
    sections,
)

#: 構造だけを模した合成データ。**実サイトの文言を含めない**（要件 4.5.2）
PAGE = """<html><head><title>ダミー</title></head><body>
<nav>お知らせ ダミーの案内</nav>
<h1>見出しA</h1><p>本文A</p>
<h2>見出しB</h2><p>本文B</p>
<h2>見出しC</h2><p>本文C</p>
</body></html>"""


def test_sections_split_on_headings():
    got = sections(PAGE)
    assert [s.heading for s in got] == [BEFORE_HEADINGS, "見出しA", "見出しB", "見出しC"]
    assert len({s.sha256 for s in got}) == 4


def test_markup_changes_do_not_change_section_hashes():
    """可視テキストでハッシュする。**マークアップの変化で止めない**。

    全文ハッシュが30分で変わった原因がマークアップだった（RESULTS.md）。
    節ごとのハッシュで同じ問題を繰り返さない。
    """
    restyled = PAGE.replace("<p>本文B</p>", '<p class="x" data-y="1">本文B</p>')
    assert [s.sha256 for s in sections(PAGE)] == [s.sha256 for s in sections(restyled)]


def test_fingerprint_stores_no_site_text():
    """**リポジトリにサイトの文言を置かない**（要件 4.5.2）。

    見出しも保存しない。報告に使う見出しは実行時の生きたページから取る。
    """
    saved = build_fingerprint(PAGE, "approved-hash")
    dumped = json.dumps(saved, ensure_ascii=False)
    for text in ("見出しA", "見出しB", "見出しC", "本文A", "お知らせ", "ダミー"):
        assert text not in dumped, f"サイト由来の文言が保存されている: {text}"
    assert saved["visibleTextSha256"] == "approved-hash"


def _write(tmp_path: pathlib.Path, html: str, approved: str) -> pathlib.Path:
    path = tmp_path / "terms_fingerprint.json"
    path.write_text(json.dumps(build_fingerprint(html, approved)), encoding="utf-8")
    return path


def test_change_in_the_page_furniture_is_pinpointed(tmp_path):
    """**見出しの前（ナビゲーション等）の変更を、規約本文の変更と区別できる。**

    これが A案の狙いである。止まるたびに全文を読み直すのでは、関門が
    「形骸化した通知」になる。
    """
    path = _write(tmp_path, PAGE, "approved-hash")
    changed = PAGE.replace("お知らせ ダミーの案内", "お知らせ 別のダミー")
    lines = describe_change(changed, "approved-hash", path)
    assert lines == [f"第0節「{BEFORE_HEADINGS}」が変わった"]


def test_change_in_a_clause_is_pinpointed(tmp_path):
    path = _write(tmp_path, PAGE, "approved-hash")
    changed = PAGE.replace("<p>本文B</p>", "<p>本文Bに一文を足した</p>")
    lines = describe_change(changed, "approved-hash", path)
    assert lines == ["第2節「見出しB」が変わった"]


def test_added_and_removed_sections_are_reported(tmp_path):
    path = _write(tmp_path, PAGE, "approved-hash")
    added = PAGE.replace("</body>", "<h2>見出しD</h2><p>本文D</p></body>")
    lines = describe_change(added, "approved-hash", path)
    assert any("節の数が変わった" in line for line in lines)
    assert any("第4節「見出しD」が増えた" in line for line in lines)


def test_stale_fingerprint_refuses_to_compare(tmp_path):
    """**指紋が古いときは節の比較をしない。**

    古い指紋と比べると「変わっていない節が変わった」と出て、判断を誤らせる。
    """
    path = _write(tmp_path, PAGE, "old-approved-hash")
    lines = describe_change(PAGE, "new-approved-hash", path)
    assert len(lines) == 1
    assert "指紋が古い" in lines[0]


def test_missing_fingerprint_raises(tmp_path):
    with pytest.raises(FingerprintError):
        describe_change(PAGE, "approved-hash", tmp_path / "does-not-exist.json")


def test_broken_fingerprint_raises(tmp_path):
    path = tmp_path / "terms_fingerprint.json"
    path.write_text('{"visibleTextSha256": "x", "sections": [{"index": 0}]}', encoding="utf-8")
    with pytest.raises(FingerprintError):
        describe_change(PAGE, "x", path)


def test_reporter_does_not_raise_when_the_fingerprint_is_unusable(capsys, tmp_path, monkeypatch):
    """**報告で落とさない。** 関門はすでに止めている。報告が例外を投げると
    「規約が変わった」という本来の理由が見えなくなる。
    """
    monkeypatch.setattr("batch.parser.terms.FINGERPRINT_PATH", tmp_path / "none.json")
    report_terms_change("approved-hash")(PAGE)
    out = capsys.readouterr().out
    assert "利用規約が変わっている" in out
    assert "比較ができない" in out


def test_reporter_prints_the_changed_section(capsys, tmp_path, monkeypatch):
    path = _write(tmp_path, PAGE, "approved-hash")
    monkeypatch.setattr("batch.parser.terms.FINGERPRINT_PATH", path)
    changed = PAGE.replace("<p>本文C</p>", "<p>本文Cを直した</p>")
    report_terms_change("approved-hash")(changed)
    out = capsys.readouterr().out
    assert "第3節「見出しC」が変わった" in out
