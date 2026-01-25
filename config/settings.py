"""
Configuration settings for Polymarket Sentinel Bot
Uses Pydantic for validation and type safety
"""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from decimal import Decimal


class PolymarketSettings(BaseSettings):
    """Polymarket API credentials"""
    api_key: str = Field(default="", alias="POLYMARKET_API_KEY")
    api_secret: SecretStr = Field(default=SecretStr(""), alias="POLYMARKET_API_SECRET")
    api_passphrase: SecretStr = Field(default=SecretStr(""), alias="POLYMARKET_API_PASSPHRASE")


class WalletSettings(BaseSettings):
    """Wallet configuration"""
    private_key: SecretStr = Field(alias="WALLET_PRIVATE_KEY")
    address: str = Field(alias="WALLET_ADDRESS")

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        if v and not v.startswith("0x"):
            raise ValueError("Wallet address must start with 0x")
        return v.lower() if v else v


class TwitterSettings(BaseSettings):
    """Twitter/X API credentials"""
    api_key: str = Field(default="", alias="TWITTER_API_KEY")
    api_secret: SecretStr = Field(default=SecretStr(""), alias="TWITTER_API_SECRET")
    bearer_token: SecretStr = Field(default=SecretStr(""), alias="TWITTER_BEARER_TOKEN")
    access_token: str = Field(default="", alias="TWITTER_ACCESS_TOKEN")
    access_secret: SecretStr = Field(default=SecretStr(""), alias="TWITTER_ACCESS_SECRET")


class BlockchainSettings(BaseSettings):
    """Blockchain RPC settings"""
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        alias="POLYGON_RPC_URL"
    )
    polygon_rpc_backup: str = Field(
        default="https://polygon-rpc.com",
        alias="POLYGON_RPC_BACKUP"
    )


class RiskSettings(BaseSettings):
    """Risk management parameters"""
    max_capital_usdc: Decimal = Field(default=Decimal("1000"), alias="MAX_CAPITAL_USDC")
    max_bet_percent: Decimal = Field(default=Decimal("0.03"), alias="MAX_BET_PERCENT")
    max_exposure_percent: Decimal = Field(default=Decimal("0.25"), alias="MAX_EXPOSURE_PERCENT")
    max_drawdown_percent: Decimal = Field(default=Decimal("0.15"), alias="MAX_DRAWDOWN_PERCENT")
    min_edge_threshold: Decimal = Field(default=Decimal("0.05"), alias="MIN_EDGE_THRESHOLD")

    @field_validator("max_bet_percent", "max_exposure_percent", "max_drawdown_percent", "min_edge_threshold")
    @classmethod
    def validate_percent(cls, v: Decimal) -> Decimal:
        if v < 0 or v > 1:
            raise ValueError("Percentage must be between 0 and 1")
        return v


class BotSettings(BaseSettings):
    """Bot operational settings"""
    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")
    manual_approval_threshold: Decimal = Field(default=Decimal("50"), alias="MANUAL_APPROVAL_THRESHOLD")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v.upper()


class NotificationSettings(BaseSettings):
    """Notification settings (optional)"""
    telegram_bot_token: Optional[str] = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, alias="TELEGRAM_CHAT_ID")
    discord_webhook_url: Optional[str] = Field(default=None, alias="DISCORD_WEBHOOK_URL")


class Settings(BaseSettings):
    """Main settings aggregator"""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    polymarket: PolymarketSettings = Field(default_factory=PolymarketSettings)
    wallet: WalletSettings = Field(default_factory=lambda: WalletSettings(
        WALLET_PRIVATE_KEY="0x0000000000000000000000000000000000000000000000000000000000000000",
        WALLET_ADDRESS="0x0000000000000000000000000000000000000000"
    ))
    twitter: TwitterSettings = Field(default_factory=TwitterSettings)
    blockchain: BlockchainSettings = Field(default_factory=BlockchainSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from environment"""
        return cls(
            polymarket=PolymarketSettings(),
            wallet=WalletSettings(),
            twitter=TwitterSettings(),
            blockchain=BlockchainSettings(),
            risk=RiskSettings(),
            bot=BotSettings(),
            notifications=NotificationSettings()
        )


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings singleton"""
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings
