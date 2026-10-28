"""检索查询改写的清洗逻辑。

这一层不碰网络：模型返回什么都可能（带引号、带 "query:" 前缀、多行），
必须收敛成一行可用查询，否则检索会拿一句废话去搜。
"""

from __future__ import annotations

from app.agents.classifier import parse_rewritten_query


def test_plain_query_passes_through():
    assert parse_rewritten_query("炸鸡 减肥 夜间 热量", "x") == "炸鸡 减肥 夜间 热量"


def test_quotes_and_prefix_are_stripped():
    assert parse_rewritten_query('"overnight fried chicken calories"', "x") == (
        "overnight fried chicken calories"
    )
    assert parse_rewritten_query("query: 面试 生日 取舍", "x") == "面试 生日 取舍"
    assert parse_rewritten_query("Query：面试 生日 取舍", "x") == "面试 生日 取舍"


def test_only_first_line_is_kept():
    raw = "炸鸡 夜间代谢\n\n这题的关键在于……"
    assert parse_rewritten_query(raw, "x") == "炸鸡 夜间代谢"


def test_empty_or_tiny_output_falls_back():
    assert parse_rewritten_query("", "原文兜底") == "原文兜底"
    assert parse_rewritten_query(None, "原文兜底") == "原文兜底"
    assert parse_rewritten_query("  ", "原文兜底") == "原文兜底"
    assert parse_rewritten_query('""', "原文兜底") == "原文兜底"


def test_length_is_capped():
    long = "词" * 500
    assert len(parse_rewritten_query(long, "x")) == 120
