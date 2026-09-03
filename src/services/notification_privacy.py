"""Public notification projection; authenticated portfolio data stays unchanged."""

import re

PRIVATE_PORTFOLIO_MARKERS = (
    "成本", "浮盈", "浮亏", "盈亏", "净资产", "净值", "现金", "仓位", "资金",
    "持仓数量", "最多减", "条件加", "可用数量", "防守位", "risk_budget",
)


def contains_portfolio_details(content: str) -> bool:
    """Recognize legacy account details before a pending digest is delivered."""
    return any(marker in content for marker in PRIVATE_PORTFOLIO_MARKERS) or bool(
        re.search(r"回撤\s*[:：]?\s*[+\-\d]", content)
    )
