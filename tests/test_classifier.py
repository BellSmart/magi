"""输入形态判定与首行读票——都是纯函数，不碰网络。"""

from __future__ import annotations

import pytest

from app.agents.classifier import (
    FORM_NONE,
    FORM_PROPOSITION,
    FORM_QUESTION,
    parse_form,
    parse_leading_vote,
    strip_leading_vote,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("QUESTION", FORM_QUESTION),
        ("question.", FORM_QUESTION),
        ("PROPOSITION", FORM_PROPOSITION),
        ("**PROPOSITION**", FORM_PROPOSITION),
        ("NEITHER", FORM_NONE),
        ("Neither.", FORM_NONE),
        ("", None),
        ("我不确定", None),
    ],
)
def test_parse_form(raw, expected):
    assert parse_form(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("YES\n\n因为……", "yes"),
        ("yes", "yes"),
        ("**NO**\n\n理由", "no"),
        ("No. The evidence...", "no"),
        ("CONDITIONAL\n\n条件：……", "conditional"),
        ("Conditional: only if X", "conditional"),
        ("是的，我认为……", "yes"),
        ("不是，理由如下", "no"),
        ("（mock melchior）Yes.", None),
        ("", None),
    ],
)
def test_parse_leading_vote(raw, expected):
    assert parse_leading_vote(raw) == expected


def test_strip_leading_vote_keeps_body():
    assert strip_leading_vote("YES\n\n因为证据支持。") == "因为证据支持。"
    assert strip_leading_vote("**NO** 理由如下") == "理由如下"
    # 没有表态词时原样返回
    assert strip_leading_vote("（mock melchior）Yes.") == "（mock melchior）Yes."
