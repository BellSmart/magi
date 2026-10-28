"""提示词。

三个 agent 的差异必须落在**判断**上，而不只是语气。上游只给了一句
"You are a scientist"，模型会把它当成口吻指令——三台说同样的话、用不同的腔调。
这里把每个人格写成"优化什么 / 怎么判 / 盲区在哪"，并要求以表态开头。

表态开头（YES / NO / CONDITIONAL）还有个工程上的好处：
票型可以直接从首行解析，不必再花一次 LLM 调用去"总结你的回答"。
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

# 这里刻意**不放**任何"通用认知规范"。
# 早期版本给三个人格共享了"证据纪律 / 可反驳性 / 代价不对称"这一整段，
# 结果是把科学家的判准强加给了母亲与女人：CASPER 被要求"不许断言未证实的东西"，
# 就再也说不出"我知道不健康，但我就是想吃"。
# 三个 fragment 的互斥性应当来自各自 persona 里的判准，而不是共享的理性规范。
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

# 只讲格式，不讲认识论：引用必须指向真实存在的编号，这是解析与展示的要求。
_SOURCE_HINT = (
    "Numbered reference results are provided below. Cite them as [1], [2] when you "
    "use them. Never cite a number that is not in the list."
)

# 注入 prompt 的摘要长度上限（存储仍保留完整摘要，详情页可看全）。
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

    # 長さの指示は user 側に置く：system を差し替えるとプレフィックスキャッシュが
    # 分断され、深度を切り替えるたびに全トークンが未命中になる。
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

    # 摘要截到 400 字：8 条 ×900 字会把 prompt 撑到 4000+ token，
    # 既贵又稀释稳定前缀（实测命中率被压到 10% 以下），噪声还更多。
    blocks = [
        f"[{index + 1}] {item.get('title') or item.get('url')}\n{item.get('url')}\n"
        f"{(item.get('snippet') or '').strip()[:PROMPT_SNIPPET_CHARS]}"
        for index, item in enumerate(sources)
    ]
    return f"{body}\n\n【参考：WEB 検索結果】\n" + "\n\n".join(blocks)
