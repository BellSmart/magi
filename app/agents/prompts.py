"""提示词组装。

注意：这里不提供任何跨人格共享的判断标准。互斥性必须来自各 persona
自己的判准，否则三台会向同一个中心收敛（踩过）。
"""

from __future__ import annotations

from app.agents.config import AgentConfig

_COUNCIL = """
You are one of three MAGI subsystems voting in council. The other two embody
different values and will ask different questions of the same material — that is
by design. Do not be balanced, and do not adopt the other fragments' standards in
order to look reasonable. Vote from your own fragment's judgment.
Reply in the same language as the user.
""".strip()

# 别在这里加"证据纪律""可反驳性"之类的东西，那会把科学家的判准塞给另外两台。
_VOTE_PROTOCOL = """
[Output contract]

Begin your reply with exactly one word on its own line: YES, NO, or CONDITIONAL.
- YES — you endorse it.
- NO — you reject it.
- CONDITIONAL — LAST RESORT ONLY. Use it only when the proposition genuinely cannot
  be settled without one specific, nameable fact, and name that fact in the first
  sentence. "It depends", "context matters", "both sides have a point" are NOT
  valid reasons. Hesitation about degree, values or circumstances still requires
  you to choose YES or NO.
Then justify, following the length requested with the question.

If the user's message is a statement rather than a question, treat it as a
proposition and say whether you agree with it.
""".strip()

_OPEN_PROTOCOL = """
[Output contract]

This input is not a yes/no matter, so do not vote. Answer it directly from your
fragment's perspective, following the length requested with the question.
""".strip()

# 只约束引用格式，不约束怎么判断。
_SOURCE_HINT = (
    "Numbered reference results are provided below. Cite them as [1], [2] when you "
    "use them. Never cite a number that is not in the list."
)

# 注入 prompt 的摘要长度；库里存的是完整摘要。
PROMPT_SNIPPET_CHARS = 400


def build_system_prompt(
    agent: AgentConfig, *, sources: bool = False, votable: bool = True
) -> str:
    parts = [
        agent.persona.strip(),
        _COUNCIL,
        _VOTE_PROTOCOL if votable else _OPEN_PROTOCOL,
    ]
    if sources:
        parts.append(_SOURCE_HINT)
    return "\n\n".join(parts)


def build_user_message(
    question: str,
    sources: list[dict] | None = None,
    *,
    form: str = "question",
    length_words: int | None = None,
) -> str:
    parts: list[str] = []

    # 长度放 user 侧：改 system 会打断前缀缓存，每次调深度都要重新命中。
    if length_words:
        parts.append(
            f"[Answer length: about {length_words} words. Use the room for concrete "
            "evidence and reasoning, not for restating the question or filler.]"
        )
    if form == "proposition":
        parts.append(
            "The following is a PROPOSITION, not a question. State whether you agree "
            "with it."
        )
    parts.append(question)
    body = "\n\n".join(parts)

    if not sources:
        return body

    # 摘要截断：8 条 ×900 字会撑到 4000+ token，命中率掉到 10% 以下。
    blocks = [
        f"[{index + 1}] {item.get('title') or item.get('url')}\n{item.get('url')}\n"
        f"{(item.get('snippet') or '').strip()[:PROMPT_SNIPPET_CHARS]}"
        for index, item in enumerate(sources)
    ]
    return f"{body}\n\n【参考：WEB 検索結果】\n" + "\n\n".join(blocks)
