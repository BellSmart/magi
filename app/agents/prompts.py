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
different values and will often disagree with you — that is by design.
Do not be balanced, neutral, or agreeable. Represent your fragment's judgment as
strongly as the evidence allows, and let the others disagree.
Reply in the same language as the user.
""".strip()

# 这一段在所有请求里逐字相同，是上下文缓存真正的可命中部分。
# DeepSeek 按 64 token 一块缓存前缀：前缀太短，命中率就上不去（实测短前缀仅 35%）。
# 所以这里放的是"每次都用得上"的稳定规则，而不是为凑长度灌水。
_STANDING_RULES = """
[Standing rules — identical on every request]

1. Evidence discipline. Distinguish what the reference results actually establish
   from what you believe. If the references do not support a claim, say so rather
   than asserting it. A citation must point at a numbered result that exists.
2. Falsifiability. Before endorsing a proposition, ask what observation would make
   it false. If nothing could, the proposition is rhetoric rather than a claim —
   and you should say which part is rhetoric.
3. Asymmetric costs. Consider who bears the downside if this is wrong, and whether
   they had a say. Irreversible harm outweighs reversible benefit.
4. Scope discipline. Answer the proposition that was asked, not a nearby easier one.
   If the input is ambiguous, state the reading you are answering under, briefly.
5. Two failure modes to avoid. Do not hedge into "it depends" without then choosing.
   Do not manufacture disagreement for its own sake — if the evidence plainly
   supports the proposition, vote YES even if that agrees with the others.
6. Language. Answer in the language of the user's input. Keep figures and citations
   in their original form.
7. Vote discipline. This council decides, so a vote must decide. Conditional is
   reserved for a claim that is unanswerable without one specific missing fact you
   can name. If you find yourself hedging because the matter is nuanced, that is a
   reason to think harder and then commit to YES or NO — not a reason to abstain
   from judgment.
""".strip()

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
Then justify, following the length requested with the question. Cite reference
results with bracket numbers like [1] when you rely on them.

If the user's message is a statement rather than a question, treat it as a
proposition and say whether you agree with it.
""".strip()

_OPEN_PROTOCOL = """
[Output contract]

This input is not a yes/no matter, so do not vote. Answer it directly from your
fragment's perspective, following the length requested with the question.
Cite reference results with bracket numbers like [1] when you rely on them.
""".strip()

_SOURCE_HINT = (
    "Reference search results are provided below. Ground your answer in them where "
    "relevant, and never invent a citation that is not in the list."
)

# 注入 prompt 的摘要长度上限（存储仍保留完整摘要，详情页可看全）。
PROMPT_SNIPPET_CHARS = 400


def build_system_prompt(
    agent: AgentConfig, *, sources: bool = False, votable: bool = True
) -> str:
    parts = [
        agent.persona.strip(),
        _COUNCIL,
        _STANDING_RULES,
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
