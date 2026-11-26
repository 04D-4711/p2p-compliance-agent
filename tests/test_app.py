"""Streamlit 界面冒烟测试：逐页渲染并走一遍巡检 → 看板 → 整改 → 复测。"""
import importlib
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")
# Windows 智能应用控制可能拦截 pyarrow 的 DLL；导入失败时跳过界面测试，而不是报错
pytest.importorskip("pyarrow.compute", reason="pyarrow 无法加载（可能被 Windows 应用程序控制策略拦截）")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("P2P_DB", str(tmp_path / "t.db"))
    from p2p import config
    importlib.reload(config)
    monkeypatch.setattr(config, "LABELS_PATH", tmp_path / "labels.csv")
    at = st_testing.AppTest.from_file(str(Path(__file__).parent.parent / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def _goto(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, at.exception


def test_pages_render(app):
    for p in ["录单（事前拦截）", "单据追溯", "效果评估"]:
        _goto(app, p)


def test_po_blocked_in_ui(app):
    _goto(app, "录单（事前拦截）")
    app.selectbox[0].set_value("U06")          # 申请人 = 审批人 U06
    app.button[0].click().run()                # 提交 PO
    assert any("已拦截" in e.value for e in app.error)


def test_scan_to_closed(app):
    app.sidebar.toggle[0].set_value(False).run()   # 不启用记忆：v2 命中大多自动确认，直接走整改
    _goto(app, "巡检 Agent")
    [b for b in app.button if b.label == "运行巡检"][0].click().run()
    assert app.dataframe
    [b for b in app.button if b.label == "写入风险看板"][0].click().run()
    assert any("新建案件" in s.value for s in app.success)
    _goto(app, "风险看板")
    assert not app.exception
    [b for b in app.button if b.label == "执行整改（演示）"][0].click().run()
    [b for b in app.button if b.label == "复测"][0].click().run()
    assert not app.exception


def test_review_writes_memory_page(app):
    _goto(app, "巡检 Agent")
    [s for s in app.selectbox if s.label == "规则版本"][0].set_value("v1").run()
    [b for b in app.button if b.label == "运行巡检"][0].click().run()
    [b for b in app.button if b.label == "写入风险看板"][0].click().run()
    _goto(app, "风险看板")
    app.multiselect[0].set_value(["待确认"]).run()
    [t for t in app.text_input if t.label.startswith("复核意见")][0].set_value("测试复核意见").run()
    [b for b in app.button if b.label == "判定误报"][0].click().run()
    assert not app.exception
    _goto(app, "记忆库")
    assert app.dataframe and not app.exception
