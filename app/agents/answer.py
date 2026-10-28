"""答申の生成パラメータ。

出力上限（max_tokens）と思考強度（reasoning_effort）は独立した二つのノブ。
以前は「簡潔/標準/詳細」という一つの档位に束ねていたが、利用者から
「強度も別々に選びたい」という要望が出たため分離した。

長さの指示は user メッセージ側に置く（system を差し替えると
プレフィックスキャッシュが分断されるため）。
"""

from __future__ import annotations

MIN_OUTPUT_TOKENS = 256
DEFAULT_MAX_TOKENS = 2000
# 概算係数。中国語はほぼ 1 文字 1 token。
TOKENS_PER_WORD = 4

# DeepSeek は low / high / max の三段（medium は high 扱い）。
EFFORT_LABELS: dict[str, str] = {
    "low": "低（速い・安い）",
    "high": "高（標準的な推論）",
    "max": "最高（最も深く考える・遅い）",
}
EFFORTS = tuple(EFFORT_LABELS)
DEFAULT_EFFORT = "low"


def is_valid_effort(value: str | None) -> bool:
    return value in EFFORT_LABELS


def normalise_effort(value: str | None) -> str:
    return value if value in EFFORT_LABELS else DEFAULT_EFFORT


def derive_words(max_tokens: int) -> int:
    """出力上限から、モデルに伝える目標語数を導く。"""
    return max(64, int(max_tokens / TOKENS_PER_WORD))


def resolve_max_tokens(requested: int | None, hard_cap: int) -> int:
    base = requested if requested is not None else DEFAULT_MAX_TOKENS
    return max(MIN_OUTPUT_TOKENS, min(int(base), hard_cap))
