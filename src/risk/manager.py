"""
Risk Management System
Protects capital through position limits, drawdown controls, and exposure management
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from enum import Enum
import structlog

from ..strategy.kelly import BetRecommendation, BetDirection

logger = structlog.get_logger()


class AlertLevel(str, Enum):
    """Risk alert severity levels"""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    HALT = "HALT"  # Stop all trading


@dataclass
class RiskAlert:
    """Risk alert notification"""
    level: AlertLevel
    message: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    market_id: Optional[str] = None
    metric: Optional[str] = None
    value: Optional[Decimal] = None
    threshold: Optional[Decimal] = None


@dataclass
class RiskLimits:
    """Risk management limits"""
    # Position limits
    max_position_size: Decimal = Decimal("100")  # Max $ per position
    max_position_pct: Decimal = Decimal("0.10")  # Max 10% of capital per position
    max_total_exposure: Decimal = Decimal("0.30")  # Max 30% total exposure

    # Loss limits
    max_daily_loss: Decimal = Decimal("0.05")  # Max 5% daily loss
    max_weekly_loss: Decimal = Decimal("0.10")  # Max 10% weekly loss
    max_drawdown: Decimal = Decimal("0.15")  # Max 15% drawdown from peak

    # Trade limits
    max_daily_trades: int = 20
    max_concurrent_positions: int = 10
    min_time_between_trades: int = 60  # seconds

    # Market limits
    min_market_liquidity: Decimal = Decimal("5000")  # Min $5k liquidity
    max_market_exposure: Decimal = Decimal("0.05")  # Max 5% of market liquidity

    # Edge requirements
    min_edge: Decimal = Decimal("0.02")  # Minimum 2% edge
    min_confidence: Decimal = Decimal("0.30")  # Minimum 30% confidence


@dataclass
class PortfolioState:
    """Current portfolio state for risk calculations"""
    # Capital
    initial_capital: Decimal = Decimal("0")
    current_capital: Decimal = Decimal("0")
    peak_capital: Decimal = Decimal("0")

    # Positions
    positions: Dict[str, Decimal] = field(default_factory=dict)  # market_id -> size
    position_values: Dict[str, Decimal] = field(default_factory=dict)  # market_id -> current value

    # PnL tracking
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")

    # Activity tracking
    daily_trades: int = 0
    last_trade_time: Optional[datetime] = None
    trade_history: List[Tuple[datetime, str, Decimal]] = field(default_factory=list)

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def total_exposure(self) -> Decimal:
        return sum(self.position_values.values())

    @property
    def exposure_pct(self) -> Decimal:
        if self.current_capital == 0:
            return Decimal("0")
        return self.total_exposure / self.current_capital

    @property
    def drawdown(self) -> Decimal:
        if self.peak_capital == 0:
            return Decimal("0")
        return (self.peak_capital - self.current_capital) / self.peak_capital

    @property
    def daily_return(self) -> Decimal:
        if self.initial_capital == 0:
            return Decimal("0")
        return self.daily_pnl / self.initial_capital

    def update_peak(self):
        """Update peak capital if current is higher"""
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital


class RiskManager:
    """
    Risk Management System

    Responsibilities:
    - Validate trades against risk limits
    - Track portfolio exposure
    - Monitor drawdown and losses
    - Generate risk alerts
    - Halt trading when limits breached
    """

    def __init__(
        self,
        limits: Optional[RiskLimits] = None,
        initial_capital: Decimal = Decimal("1000"),
    ):
        self.limits = limits or RiskLimits()
        self.state = PortfolioState(
            initial_capital=initial_capital,
            current_capital=initial_capital,
            peak_capital=initial_capital,
        )
        self.alerts: List[RiskAlert] = []
        self.trading_halted: bool = False
        self.halt_reason: Optional[str] = None

    def validate_trade(
        self,
        recommendation: BetRecommendation,
        market_liquidity: Decimal = Decimal("0"),
    ) -> Tuple[bool, List[RiskAlert]]:
        """
        Validate a trade recommendation against risk limits

        Args:
            recommendation: The bet recommendation to validate
            market_liquidity: Current market liquidity in USD

        Returns:
            Tuple of (is_valid, list_of_alerts)
        """
        alerts = []

        # Check if trading is halted
        if self.trading_halted:
            alerts.append(RiskAlert(
                level=AlertLevel.HALT,
                message=f"Trading halted: {self.halt_reason}",
            ))
            return False, alerts

        # Check edge requirement
        if recommendation.edge < self.limits.min_edge:
            alerts.append(RiskAlert(
                level=AlertLevel.INFO,
                message=f"Insufficient edge: {recommendation.edge:.2%} < {self.limits.min_edge:.2%}",
                market_id=recommendation.market_id,
                metric="edge",
                value=recommendation.edge,
                threshold=self.limits.min_edge,
            ))
            return False, alerts

        # Check confidence requirement
        if recommendation.confidence < self.limits.min_confidence:
            alerts.append(RiskAlert(
                level=AlertLevel.INFO,
                message=f"Insufficient confidence: {recommendation.confidence:.2%}",
                market_id=recommendation.market_id,
                metric="confidence",
                value=recommendation.confidence,
                threshold=self.limits.min_confidence,
            ))
            return False, alerts

        # Check position size limits
        if recommendation.recommended_size > self.limits.max_position_size:
            original_size = recommendation.recommended_size
            recommendation.recommended_size = self.limits.max_position_size
            alerts.append(RiskAlert(
                level=AlertLevel.WARNING,
                message=f"Position size capped: ${original_size:.2f} -> ${self.limits.max_position_size:.2f}",
                market_id=recommendation.market_id,
                metric="position_size",
                value=original_size,
                threshold=self.limits.max_position_size,
            ))

        # Check position percentage limit
        max_size_by_pct = self.state.current_capital * self.limits.max_position_pct
        if recommendation.recommended_size > max_size_by_pct:
            original_size = recommendation.recommended_size
            recommendation.recommended_size = max_size_by_pct
            alerts.append(RiskAlert(
                level=AlertLevel.WARNING,
                message=f"Position % capped: ${original_size:.2f} -> ${max_size_by_pct:.2f}",
                market_id=recommendation.market_id,
                metric="position_pct",
                value=original_size / self.state.current_capital,
                threshold=self.limits.max_position_pct,
            ))

        # Check total exposure
        new_exposure = self.state.total_exposure + recommendation.recommended_size
        max_exposure = self.state.current_capital * self.limits.max_total_exposure
        if new_exposure > max_exposure:
            available = max_exposure - self.state.total_exposure
            if available <= 0:
                alerts.append(RiskAlert(
                    level=AlertLevel.WARNING,
                    message=f"Max exposure reached: {self.state.exposure_pct:.1%}",
                    metric="total_exposure",
                    value=self.state.exposure_pct,
                    threshold=self.limits.max_total_exposure,
                ))
                return False, alerts
            else:
                recommendation.recommended_size = available
                alerts.append(RiskAlert(
                    level=AlertLevel.WARNING,
                    message=f"Position reduced due to exposure limit: ${recommendation.recommended_size:.2f}",
                    market_id=recommendation.market_id,
                ))

        # Check concurrent positions limit
        if len(self.state.positions) >= self.limits.max_concurrent_positions:
            # Check if this is an existing position
            if recommendation.market_id not in self.state.positions:
                alerts.append(RiskAlert(
                    level=AlertLevel.WARNING,
                    message=f"Max concurrent positions reached: {len(self.state.positions)}",
                    metric="concurrent_positions",
                    value=Decimal(len(self.state.positions)),
                    threshold=Decimal(self.limits.max_concurrent_positions),
                ))
                return False, alerts

        # Check daily trade limit
        if self.state.daily_trades >= self.limits.max_daily_trades:
            alerts.append(RiskAlert(
                level=AlertLevel.WARNING,
                message=f"Daily trade limit reached: {self.state.daily_trades}",
                metric="daily_trades",
                value=Decimal(self.state.daily_trades),
                threshold=Decimal(self.limits.max_daily_trades),
            ))
            return False, alerts

        # Check time between trades
        if self.state.last_trade_time:
            time_since_last = (datetime.utcnow() - self.state.last_trade_time).total_seconds()
            if time_since_last < self.limits.min_time_between_trades:
                alerts.append(RiskAlert(
                    level=AlertLevel.INFO,
                    message=f"Too soon since last trade: {time_since_last:.0f}s",
                    metric="trade_interval",
                ))
                return False, alerts

        # Check market liquidity
        if market_liquidity > 0:
            if market_liquidity < self.limits.min_market_liquidity:
                alerts.append(RiskAlert(
                    level=AlertLevel.WARNING,
                    message=f"Insufficient market liquidity: ${market_liquidity:,.0f}",
                    market_id=recommendation.market_id,
                    metric="liquidity",
                    value=market_liquidity,
                    threshold=self.limits.min_market_liquidity,
                ))
                return False, alerts

            # Check our impact on market
            market_impact = recommendation.recommended_size / market_liquidity
            if market_impact > self.limits.max_market_exposure:
                max_size = market_liquidity * self.limits.max_market_exposure
                recommendation.recommended_size = max_size
                alerts.append(RiskAlert(
                    level=AlertLevel.WARNING,
                    message=f"Position reduced to limit market impact: ${max_size:.2f}",
                    market_id=recommendation.market_id,
                    metric="market_impact",
                ))

        # Final size check
        if recommendation.recommended_size < Decimal("1"):
            alerts.append(RiskAlert(
                level=AlertLevel.INFO,
                message="Position size too small after risk adjustments",
                market_id=recommendation.market_id,
            ))
            return False, alerts

        return True, alerts

    def check_portfolio_health(self) -> List[RiskAlert]:
        """
        Check overall portfolio health and generate alerts

        Returns:
            List of risk alerts
        """
        alerts = []

        # Check drawdown
        if self.state.drawdown >= self.limits.max_drawdown:
            alert = RiskAlert(
                level=AlertLevel.HALT,
                message=f"Maximum drawdown breached: {self.state.drawdown:.1%}",
                metric="drawdown",
                value=self.state.drawdown,
                threshold=self.limits.max_drawdown,
            )
            alerts.append(alert)
            self._halt_trading(f"Drawdown limit breached: {self.state.drawdown:.1%}")

        elif self.state.drawdown >= self.limits.max_drawdown * Decimal("0.75"):
            alerts.append(RiskAlert(
                level=AlertLevel.WARNING,
                message=f"Drawdown warning: {self.state.drawdown:.1%}",
                metric="drawdown",
                value=self.state.drawdown,
                threshold=self.limits.max_drawdown,
            ))

        # Check daily loss
        if abs(self.state.daily_return) >= self.limits.max_daily_loss and self.state.daily_return < 0:
            alert = RiskAlert(
                level=AlertLevel.HALT,
                message=f"Daily loss limit breached: {self.state.daily_return:.1%}",
                metric="daily_loss",
                value=abs(self.state.daily_return),
                threshold=self.limits.max_daily_loss,
            )
            alerts.append(alert)
            self._halt_trading(f"Daily loss limit: {self.state.daily_return:.1%}")

        # Check exposure
        if self.state.exposure_pct >= self.limits.max_total_exposure:
            alerts.append(RiskAlert(
                level=AlertLevel.WARNING,
                message=f"Maximum exposure reached: {self.state.exposure_pct:.1%}",
                metric="exposure",
                value=self.state.exposure_pct,
                threshold=self.limits.max_total_exposure,
            ))

        return alerts

    def _halt_trading(self, reason: str):
        """Halt all trading"""
        self.trading_halted = True
        self.halt_reason = reason
        logger.critical("trading_halted", reason=reason)

    def resume_trading(self) -> bool:
        """
        Attempt to resume trading

        Only allows resumption if conditions have improved
        """
        # Check if conditions allow resumption
        if self.state.drawdown >= self.limits.max_drawdown:
            return False

        if self.state.daily_return <= -self.limits.max_daily_loss:
            return False

        self.trading_halted = False
        self.halt_reason = None
        logger.info("trading_resumed")
        return True

    def record_trade(
        self,
        market_id: str,
        size: Decimal,
        direction: BetDirection,
    ):
        """Record a completed trade"""
        self.state.daily_trades += 1
        self.state.last_trade_time = datetime.utcnow()
        self.state.trade_history.append((datetime.utcnow(), market_id, size))

        # Update positions
        current_position = self.state.positions.get(market_id, Decimal("0"))
        if direction == BetDirection.YES:
            new_position = current_position + size
        else:
            new_position = current_position - size

        if new_position == 0:
            self.state.positions.pop(market_id, None)
            self.state.position_values.pop(market_id, None)
        else:
            self.state.positions[market_id] = new_position
            self.state.position_values[market_id] = abs(new_position)

    def update_position_value(self, market_id: str, current_value: Decimal):
        """Update current value of a position"""
        if market_id in self.state.positions:
            self.state.position_values[market_id] = current_value

    def update_capital(self, new_capital: Decimal):
        """Update current capital and track peak"""
        self.state.current_capital = new_capital
        self.state.update_peak()

    def reset_daily_counters(self):
        """Reset daily counters (call at start of each day)"""
        self.state.daily_trades = 0
        self.state.daily_pnl = Decimal("0")

        # Check if we can resume trading after daily reset
        if self.trading_halted and "daily" in (self.halt_reason or "").lower():
            self.resume_trading()

    def get_available_capital(self) -> Decimal:
        """Get capital available for new positions"""
        max_exposure = self.state.current_capital * self.limits.max_total_exposure
        available = max_exposure - self.state.total_exposure
        return max(Decimal("0"), available)

    def get_max_position_size(self, market_id: str) -> Decimal:
        """Get maximum allowed position size for a market"""
        # Start with absolute max
        max_size = self.limits.max_position_size

        # Limit by percentage of capital
        pct_limit = self.state.current_capital * self.limits.max_position_pct
        max_size = min(max_size, pct_limit)

        # Limit by available exposure
        available = self.get_available_capital()
        max_size = min(max_size, available)

        return max(Decimal("0"), max_size)

    def get_status_report(self) -> str:
        """Generate status report"""
        lines = [
            "=" * 50,
            "RISK MANAGEMENT STATUS",
            "=" * 50,
            "",
            f"Capital: ${self.state.current_capital:,.2f}",
            f"Peak: ${self.state.peak_capital:,.2f}",
            f"Available: ${self.get_available_capital():,.2f}",
            "",
            f"Total PnL: ${self.state.total_pnl:+,.2f}",
            f"Daily PnL: ${self.state.daily_pnl:+,.2f}",
            f"Drawdown: {self.state.drawdown:.1%}",
            "",
            f"Total Exposure: ${self.state.total_exposure:,.2f} ({self.state.exposure_pct:.1%})",
            f"Positions: {len(self.state.positions)}",
            f"Daily Trades: {self.state.daily_trades}",
            "",
        ]

        if self.trading_halted:
            lines.extend([
                "*** TRADING HALTED ***",
                f"Reason: {self.halt_reason}",
            ])
        else:
            lines.append("Status: ACTIVE")

        if self.alerts:
            lines.extend([
                "",
                "Recent Alerts:",
            ])
            for alert in self.alerts[-5:]:
                lines.append(f"  [{alert.level.value}] {alert.message}")

        return "\n".join(lines)
