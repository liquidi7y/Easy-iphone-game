"""
Notification Service
Sends alerts via Telegram, Discord, etc.
"""

import asyncio
from typing import Optional
import httpx
import structlog

logger = structlog.get_logger()


class NotificationService:
    """
    Multi-channel notification service

    Supports:
    - Telegram
    - Discord webhooks
    - Console output (always enabled)
    """

    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook: Optional[str] = None,
    ):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook = discord_webhook
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def send(
        self,
        message: str,
        level: str = "INFO",
        title: Optional[str] = None,
    ):
        """
        Send notification to all configured channels

        Args:
            message: The notification message
            level: Severity level (INFO, WARNING, CRITICAL)
            title: Optional title for the notification
        """
        # Always log to console
        log_func = {
            "INFO": logger.info,
            "WARNING": logger.warning,
            "CRITICAL": logger.critical,
            "ERROR": logger.error,
        }.get(level, logger.info)

        log_func("notification", message=message[:100], level=level)

        # Format message with title
        full_message = f"**{title}**\n\n{message}" if title else message

        # Send to all channels concurrently
        tasks = []

        if self.telegram_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(full_message, level))

        if self.discord_webhook:
            tasks.append(self._send_discord(full_message, level))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_telegram(self, message: str, level: str):
        """Send message via Telegram"""
        if not self._client:
            return

        # Add emoji based on level
        emoji = {
            "INFO": "ℹ️",
            "WARNING": "⚠️",
            "CRITICAL": "🚨",
            "ERROR": "❌",
        }.get(level, "📢")

        formatted = f"{emoji} {message}"

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            response = await self._client.post(
                url,
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": formatted,
                    "parse_mode": "Markdown",
                }
            )
            if response.status_code != 200:
                logger.warning("telegram_send_failed", status=response.status_code)
        except Exception as e:
            logger.warning("telegram_error", error=str(e))

    async def _send_discord(self, message: str, level: str):
        """Send message via Discord webhook"""
        if not self._client or not self.discord_webhook:
            return

        # Color based on level
        color = {
            "INFO": 0x3498db,  # Blue
            "WARNING": 0xf39c12,  # Orange
            "CRITICAL": 0xe74c3c,  # Red
            "ERROR": 0xe74c3c,  # Red
        }.get(level, 0x95a5a6)  # Gray

        try:
            response = await self._client.post(
                self.discord_webhook,
                json={
                    "embeds": [{
                        "description": message[:2000],  # Discord limit
                        "color": color,
                    }]
                }
            )
            if response.status_code not in (200, 204):
                logger.warning("discord_send_failed", status=response.status_code)
        except Exception as e:
            logger.warning("discord_error", error=str(e))

    async def send_trade_alert(
        self,
        market: str,
        direction: str,
        size: float,
        price: float,
        expected_ev: float,
        dry_run: bool = False,
    ):
        """Send trade execution alert"""
        trade_type = "🧪 DRY RUN" if dry_run else "💰 TRADE EXECUTED"
        message = f"""
{trade_type}

Market: {market[:50]}...
Direction: {direction}
Size: ${size:,.2f}
Price: {price:.4f}
Expected Value: ${expected_ev:+,.2f}
"""
        await self.send(message, level="INFO", title="Trade Alert")

    async def send_risk_alert(
        self,
        alert_type: str,
        message: str,
        metric: Optional[str] = None,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
    ):
        """Send risk management alert"""
        details = []
        if metric:
            details.append(f"Metric: {metric}")
        if value is not None:
            details.append(f"Value: {value:.4f}")
        if threshold is not None:
            details.append(f"Threshold: {threshold:.4f}")

        full_message = f"{message}\n\n" + "\n".join(details) if details else message

        level = "WARNING" if "warning" in alert_type.lower() else "CRITICAL"
        await self.send(full_message, level=level, title=f"Risk Alert: {alert_type}")

    async def send_daily_summary(
        self,
        total_trades: int,
        total_pnl: float,
        win_rate: float,
        best_trade: str,
        worst_trade: str,
    ):
        """Send daily performance summary"""
        pnl_emoji = "📈" if total_pnl >= 0 else "📉"
        message = f"""
{pnl_emoji} Daily Summary

Trades: {total_trades}
P&L: ${total_pnl:+,.2f}
Win Rate: {win_rate:.1%}

Best: {best_trade}
Worst: {worst_trade}
"""
        await self.send(message, level="INFO", title="Daily Summary")
