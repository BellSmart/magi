"""投票聚合。

纯函数，不碰数据库、不碰网络——这是整条链路上唯一必须"绝对正确"的部分。

两种判定规则（按用户的设置选择）：

**veto（一票否决，默认）** —— 原作语义
    有效票不足法定数 → error
    任一 no          → no
    没有 conditional → yes
    明确 yes 多于 conditional → yes（条件票是弱赞成）
    其余             → conditional

**majority（多数决）** —— 更接近常规议事规则
    有效票不足法定数 → error
    明确 yes 多于 no → yes
    明确 no 多于 yes → no
    持平（含大量条件票/弃权） → conditional

为什么条件票是弱赞成：
模型有强烈的"看情况"倾向。若任何一张条件票就阻断成「状 態」，
这个档位会被日常化，结论永远停在"要看具体条件"。
条件票应表达"我还需要某个具体前提"，而不是"我不表态"。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

VALID_VOTES = frozenset({"yes", "no", "conditional"})

POLICIES = ("veto", "majority")
DEFAULT_POLICY = "veto"


@dataclass(frozen=True)
class Verdict:
    status: str  # yes / no / conditional / info / error
    tally: dict[str, int] = field(default_factory=dict)
    degraded: bool = False
    reason_code: str = "unanimous"
    # 与多数不同的票，界面用来标「少数意見」
    dissent: tuple[str, ...] = ()

    @property
    def valid_votes(self) -> int:
        return sum(self.tally.get(key, 0) for key in VALID_VOTES)


def is_valid_policy(value: str | None) -> bool:
    return value in POLICIES


def aggregate(
    statuses: Sequence[str],
    *,
    is_yes_no: bool,
    quorum: int = 2,
    policy: str = DEFAULT_POLICY,
    veto_on_no: bool = True,
    conditional_policy: str = "weak_yes",
) -> Verdict:
    tally = dict(Counter(statuses))
    abstained = tally.get("abstain", 0)

    if not is_yes_no:
        # 非是否问题不投票，三台回答本身就是结果。
        return Verdict("info", tally, degraded=abstained > 0, reason_code="not_yes_no")

    valid = [status for status in statuses if status in VALID_VOTES]

    if len(valid) < quorum:
        return Verdict("error", tally, degraded=True, reason_code="quorum_not_met")

    yes_count = valid.count("yes")
    no_count = valid.count("no")
    conditional_count = valid.count("conditional")

    if policy == "majority":
        if yes_count > no_count:
            return Verdict(
                "yes",
                tally,
                degraded=abstained > 0,
                reason_code="majority_yes",
                dissent=tuple(status for status in valid if status == "no"),
            )
        if no_count > yes_count:
            return Verdict(
                "no",
                tally,
                degraded=abstained > 0,
                reason_code="majority_no",
                dissent=tuple(status for status in valid if status != "no"),
            )
        return Verdict(
            "conditional",
            tally,
            degraded=abstained > 0,
            reason_code="tie",
            dissent=("yes",) if yes_count else (),
        )

    # --- veto（一票否决） ---
    if veto_on_no and no_count:
        return Verdict(
            "no",
            tally,
            degraded=abstained > 0,
            reason_code="vetoed",
            dissent=tuple(status for status in valid if status != "no"),
        )

    if conditional_count == 0:
        return Verdict("yes", tally, degraded=abstained > 0, reason_code="unanimous")

    if conditional_policy == "blocking":
        return Verdict(
            "conditional",
            tally,
            degraded=abstained > 0,
            reason_code="conditional",
            dissent=("yes",) if yes_count else (),
        )

    # 条件票算弱赞成，只有压过明确赞成才降为「状 態」。
    if yes_count > conditional_count:
        return Verdict(
            "yes",
            tally,
            degraded=abstained > 0,
            reason_code="majority_yes",
            dissent=("conditional",),
        )

    return Verdict(
        "conditional",
        tally,
        degraded=abstained > 0,
        reason_code="conditional",
        dissent=("yes",) if yes_count else (),
    )


REASON_TEXT = {
    "vetoed": "一票否决",
    "conditional": "条件付きの同意が優勢",
    "majority_yes": "明確な賛成が優勢",
    "majority_no": "明確な反対が優勢",
    "tie": "賛成と反対が拮抗",
    "unanimous": "全員が無条件で賛成",
    "quorum_not_met": "有効票が法定数に満たない",
    "not_yes_no": "是非を問えない入力のため投票しない",
}

POLICY_LABELS = {
    "veto": "一票否決 — 反対が 1 票でもあれば否決",
    "majority": "多数決 — 賛成が反対を上回れば可決",
}
