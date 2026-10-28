"""把输入与回答都归一成可判定的形态。

两块逻辑：
1. ``classify_form``：输入是疑问句、提案，还是压根不能表决。
   上游只问"是不是一个是非疑问句"，于是"中国是当今世界第一强国"这种
   陈述会被判成"非是否问题"而永远走情报——但用户要的是对提案表决。
2. ``parse_leading_vote``：从回答首行读票。省掉一次 LLM 归类调用。
"""

from __future__ import annotations

import asyncio
import re

from app.agents.config import AgentsConfig
from app.agents.provider import chat

FORM_QUESTION = "question"
FORM_PROPOSITION = "proposition"
FORM_NONE = "none"

VOTABLE_FORMS = frozenset({FORM_QUESTION, FORM_PROPOSITION})

FORM_PROMPT = """
Classify the user's message. Reply with exactly one token:

QUESTION     — it can be answered YES/NO (e.g. "Is 3 < 2?", "我们该不该继续这个项目？")
PROPOSITION  — it is a statement/claim that one can agree or disagree with
               (e.g. "中国是当今世界第一强国", "欲望比爱更重要")
NEITHER      — it asks for explanation, how-to, opinion essay, or is open-ended
               (e.g. "What is the meaning of life?", "怎么学日语？")

A statement is NOT a question, but it IS votable — treat it as PROPOSITION.
Do not answer the message, only classify it. Reply with one token, nothing else.
""".strip()

_EDGE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)

# 回答首行的表态。模型偶尔会加粗或带标点，这里全放开。
_LEADING = re.compile(
    r"^[\s*_#>`\-]*(?P<token>[A-Za-z\u4e00-\u9fff]+)[\s*_`]*[.:：。！!，,]?\s*",
)

_YES = {"yes", "y", "是", "是的", "对的", "同意", "赞成", "支持"}
_NO = {"no", "n", "否", "不", "不是", "不对", "不同意", "反对"}
_CONDITIONAL = {"conditional", "condition", "条件", "有条件", "部分"}


def _normalise(text: str | None) -> str:
    if not text:
        return ""
    return _EDGE.sub("", text.strip().lower())


def parse_form(text: str | None) -> str | None:
    token = _normalise(text).upper()
    if not token:
        return None
    if token.startswith("QUESTION"):
        return FORM_QUESTION
    if token.startswith("PROPOSITION"):
        return FORM_PROPOSITION
    if token.startswith("NEITHER"):
        return FORM_NONE
    return None


def parse_leading_vote(text: str | None) -> str | None:
    """从回答首行读票；读不出来返回 None，交给 LLM 兜底归类。"""
    if not text:
        return None
    match = _LEADING.match(text)
    if match is None:
        return None
    token = match.group("token").strip().lower()
    if token in _YES:
        return "yes"
    if token in _NO:
        return "no"
    if token in _CONDITIONAL:
        return "conditional"
    return None


def strip_leading_vote(text: str) -> str:
    """把首行的表态词剥掉，只留正文给详情页展示。"""
    match = _LEADING.match(text or "")
    if match is None:
        return (text or "").strip()
    token = match.group("token").strip().lower()
    if token in _YES | _NO | _CONDITIONAL:
        return text[match.end():].strip()
    return (text or "").strip()


FALLBACK_PROMPT = (
    'Summarize the answer you just gave with a single word: "yes", "no", or '
    '"conditional". Answer with one word only. If (and only if) that is not possible, '
    "list the conditions under which the answer would be yes."
)

REWRITE_PROMPT = """
Write {count} short web search queries for the proposition below.

Perspective: {angle}

Cover different sides on purpose — one query for the strongest case FOR this
perspective, one for the strongest evidence AGAINST it, and further facets if asked.

Rules:
- Write the queries in ENGLISH, even when the proposition is in another language.
  Keep proper nouns (names, places, products) as they are.
- At most 15 words each.
- One query per line. No numbering, no quotes, no explanation.
""".strip()

_LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s*")


def parse_query_list(text: str | None, fallback: list[str], limit: int) -> list[str]:
    """把模型返回的多行查询清洗成列表；空则用 fallback。"""
    cleaned: list[str] = []
    for raw in (text or "").splitlines():
        line = _LIST_PREFIX.sub("", raw.strip())
        line = line.strip("\"'`“”‘’《》 ").strip()
        line = re.sub(r"^(?:query|search)\s*\d*\s*[:：]\s*", "", line, flags=re.IGNORECASE)
        if len(line) < 2:
            continue
        if line not in cleaned:
            cleaned.append(line[:160])
        if len(cleaned) >= limit:
            break
    return cleaned or fallback[:limit]


def parse_rewritten_query(text: str | None, fallback: str) -> str:
    return parse_query_list(text, [fallback], 1)[0]


async def rewrite_queries(
    cfg: AgentsConfig,
    question: str,
    *,
    angle: str,
    count: int,
) -> list[str]:
    """按给定视角，把命题改写成若干条英文查询。

    为什么必须改写：把关键词糊在命题后面会让检索抓住关键词而不是议题，
    实测三台换来"结果完全不重叠"却大量离题。
    为什么用英文：英文查询在通用搜索引擎上的召回质量明显更稳定。
    """
    fallback = [" ".join(question.split())[:120]]
    if count <= 0:
        return fallback

    provider = cfg.provider(cfg.classifier.provider)
    extra = {"thinking": {"type": "disabled"}} if provider.thinking_switch else None

    try:
        completion = await asyncio.wait_for(
            chat(
                provider,
                model=cfg.classifier.model,
                messages=[
                    {
                        "role": "system",
                        "content": REWRITE_PROMPT.format(angle=angle, count=count),
                    },
                    {"role": "user", "content": question[:1500]},
                ],
                temperature=0.0,
                max_tokens=80 * max(count, 1),
                extra=extra,
                meta={"role": "rewrite_query", "agent": angle[:24]},
            ),
            timeout=25,
        )
    except Exception:  # noqa: BLE001 - 改写失败不该拖垮检索
        return fallback

    return parse_query_list(completion.content, fallback, count)


async def classify_answer(
    cfg: AgentsConfig,
    agent,  # noqa: ANN001
    question: str,
    answer: str,
) -> str | None:
    """兜底路径：模型没按规定以 YES/NO 开头时，再花一次调用把票读出来。"""
    provider = cfg.provider(agent.provider)
    extra = {"thinking": {"type": "disabled"}} if provider.thinking_switch else None

    completion = await chat(
        provider,
        model=agent.model,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
            {"role": "user", "content": FALLBACK_PROMPT},
        ],
        temperature=0.0,
        max_tokens=512,
        extra=extra,
        meta={"role": "classify_answer", "agent": agent.id},
    )
    token = _normalise(completion.content)
    if token.startswith("yes"):
        return "yes"
    if token.startswith("no"):
        return "no"
    if token.startswith("conditional"):
        return "conditional"
    return None


async def classify_form(cfg: AgentsConfig, question: str) -> str:
    """判错方向的代价可控：判成 NEITHER 只是不投票，不会出错。"""
    provider = cfg.provider(cfg.classifier.provider)
    extra = {"thinking": {"type": "disabled"}} if provider.thinking_switch else None

    for _ in range(2):
        completion = await chat(
            provider,
            model=cfg.classifier.model,
            messages=[
                {"role": "system", "content": FORM_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=cfg.classifier.max_tokens,
            extra=extra,
            meta={"role": "classify_form"},
        )
        form = parse_form(completion.content)
        if form is not None:
            return form

    return FORM_NONE
