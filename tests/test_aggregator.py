"""聚合规则：这是唯一"必须绝对正确"的部分，所以覆盖得最密。"""

from __future__ import annotations

import pytest

from app.agents.aggregator import aggregate


def test_all_yes_is_unanimous():
    verdict = aggregate(["yes", "yes", "yes"], is_yes_no=True)
    assert verdict.status == "yes"
    assert verdict.reason_code == "unanimous"
    assert verdict.degraded is False


def test_single_no_vetoes():
    verdict = aggregate(["yes", "no", "yes"], is_yes_no=True)
    assert verdict.status == "no"
    assert verdict.reason_code == "vetoed"
    assert verdict.tally == {"yes": 2, "no": 1}


def test_conditional_beats_yes_when_it_is_the_majority():
    verdict = aggregate(["conditional", "conditional", "yes"], is_yes_no=True)
    assert verdict.status == "conditional"
    assert verdict.reason_code == "conditional"


def test_single_conditional_does_not_block_a_clear_yes():
    """条件票是弱赞成：2 票明确赞成时，1 票条件票不应把结论拉成「状 態」。"""
    verdict = aggregate(["yes", "conditional", "yes"], is_yes_no=True)
    assert verdict.status == "yes"
    assert verdict.reason_code == "majority_yes"


def test_conditional_ties_go_to_conditional():
    verdict = aggregate(["yes", "conditional"], is_yes_no=True)
    assert verdict.status == "conditional"


def test_no_beats_conditional():
    verdict = aggregate(["conditional", "no", "yes"], is_yes_no=True)
    assert verdict.status == "no"


def test_abstain_does_not_veto_but_marks_degraded():
    verdict = aggregate(["yes", "yes", "abstain"], is_yes_no=True)
    assert verdict.status == "yes"
    assert verdict.degraded is True
    assert verdict.reason_code == "unanimous"


def test_two_abstentions_fall_below_quorum():
    verdict = aggregate(["yes", "abstain", "abstain"], is_yes_no=True)
    assert verdict.status == "error"
    assert verdict.reason_code == "quorum_not_met"


def test_three_errors_fall_below_quorum():
    verdict = aggregate(["error", "error", "error"], is_yes_no=True)
    assert verdict.status == "error"


def test_quorum_three_rejects_degraded_result():
    verdict = aggregate(["yes", "yes", "abstain"], is_yes_no=True, quorum=3)
    assert verdict.status == "error"


def test_non_yes_no_question_skips_voting():
    verdict = aggregate(["info", "info", "info"], is_yes_no=False)
    assert verdict.status == "info"
    assert verdict.reason_code == "not_yes_no"


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["yes", "yes", "yes"], "yes"),
        (["yes", "no", "no"], "no"),
        (["conditional", "conditional", "yes"], "conditional"),
        (["yes", "abstain", "yes"], "yes"),
        (["abstain", "abstain", "yes"], "error"),
    ],
)
def test_status_matrix(statuses, expected):
    assert aggregate(statuses, is_yes_no=True).status == expected
