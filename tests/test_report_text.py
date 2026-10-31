"""Report wording: realism text follows the fill model, H2 ratio and UTC times are formatted right."""
from mm.backtest import report


def test_utc_formatting_keeps_milliseconds():
    ns = 1_761_246_384_874_000_000          # 2025-10-23 19:06:24.874 UTC
    assert report._utc(ns).endswith("19:06:24.874")
    assert report._utc(None) == "n/a"


def test_h2_ratio_uses_predicted_fills_from_the_strategy_summary():
    m = {"strategy_summary": {"h2_predicted_fills": 100.0},
         "fills": {"count": 20, "by_mechanism": {"through": {"count": 10}}}}
    text = report._h2_text({"in_sample": {"metrics": m}})
    assert "in_sample 0.200 (through-only 0.100" in text
    assert report._h2_text({"x": {"metrics": {"fills": {"count": 1}}}}) == ""   # no strategy counter: no line


def test_realism_text_differs_by_fill_model():
    assert "top-of-book" in report._REALISM_QUOTE["queue"] and "bookTicker" in report._REALISM_QUOTE["data"]
    assert "depth diffs" in report._REALISM_L2["queue"] and "bookTicker" not in report._REALISM_L2["data"]
