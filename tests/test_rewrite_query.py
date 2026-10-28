"""改写后的查询清洗：多行、去编号、去引号、限长。"""
from app.agents.classifier import parse_query_list, parse_rewritten_query

def test_multi_line_with_numbering():
    raw = "1. fried chicken calories at night\n2) late night eating weight gain\n- 夜间进食 代谢"
    assert parse_query_list(raw, ["x"], 3) == [
        "fried chicken calories at night",
        "late night eating weight gain",
        "夜间进食 代谢",
    ]

def test_quotes_and_prefix_stripped():
    raw = '"overnight eating weight gain"\nQuery: metabolic rate at night'
    assert parse_query_list(raw, ["x"], 2) == [
        "overnight eating weight gain",
        "metabolic rate at night",
    ]

def test_empty_falls_back():
    assert parse_query_list("", ["fallback"], 2) == ["fallback"]
    assert parse_query_list(None, ["fallback"], 2) == ["fallback"]
    assert parse_query_list("   \n  ", ["fallback"], 2) == ["fallback"]

def test_limit_respected_and_deduped():
    raw = "a b c\nsame query\nsame query\nd e f"
    assert parse_query_list(raw, ["x"], 2) == ["a b c", "same query"]

def test_single_wrapper_still_works():
    assert parse_rewritten_query("only one", "x") == "only one"
