"""
Trading Strategies for Polymarket Sentinel

Available strategies:
- SentimentStrategy: Twitter sentiment + whale tracking (default)
- BTCArbitrageStrategy: Latency arbitrage on BTC price markets
"""

from .btc_arbitrage import BTCArbitrageStrategy, BTCArbitrageConfig
from .base import BaseStrategy

__all__ = [
    "BaseStrategy",
    "BTCArbitrageStrategy",
    "BTCArbitrageConfig",
]
