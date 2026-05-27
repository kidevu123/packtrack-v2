from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    PACKTRACK_SECRET_KEY: str = "dev-secret-change-me"
    DATABASE_URL: str = "postgresql+psycopg://packtrack:packtrack@127.0.0.1:5432/packtrack"
    APP_BASE_URL: str = "http://localhost:8000"

    UPLOAD_DIR: Path = Path("./uploads")
    LOG_DIR: Path = Path("./logs")

    SESSION_COOKIE_NAME: str = "packtrack_session"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 14  # 14 days

    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REFRESH_TOKEN: str = ""
    ZOHO_ORG_ID: str = ""
    ZOHO_TOKEN_URL: str = "https://accounts.zoho.com/oauth/v2/token"
    ZOHO_API_BASE: str = "https://www.zohoapis.com/inventory/v1"

    ZOHO_GATEWAY_URL: str = ""
    ZOHO_GATEWAY_TOKEN: str = ""
    ZOHO_GATEWAY_BRAND: str = ""

    LUMA_RECEIPT_WEBHOOK_URL: str = ""
    LUMA_PACKTRACK_SECRET: str = ""

    OIDC_CLIENT_ID: str = ""
    OIDC_CLIENT_SECRET: str = ""
    OIDC_ISSUER_URL: str = ""       # e.g. http://192.168.1.164:9000/application/o/packtrack
    OIDC_REDIRECT_URI: str = ""     # e.g. http://192.168.1.206/auth/callback

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""

    ZOHO_WEBHOOK_SECRET: str = ""

    SYNC_INTERVAL_MINUTES: int = 30
    PUSH_RETRY_INTERVAL_MINUTES: int = 5

    @property
    def oidc_configured(self) -> bool:
        return bool(self.OIDC_CLIENT_ID and self.OIDC_CLIENT_SECRET and self.OIDC_ISSUER_URL and self.OIDC_REDIRECT_URI)

    @property
    def zoho_configured(self) -> bool:
        return bool(
            self.ZOHO_CLIENT_ID
            and self.ZOHO_CLIENT_SECRET
            and self.ZOHO_REFRESH_TOKEN
            and self.ZOHO_ORG_ID
        )

    @property
    def gateway_configured(self) -> bool:
        return bool(
            self.ZOHO_GATEWAY_URL
            and self.ZOHO_GATEWAY_TOKEN
            and self.ZOHO_GATEWAY_BRAND
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN)


settings = Settings()
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
