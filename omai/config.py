"""Settings, read from the environment (or .env). Secrets never live in the repo."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./omai.db"
    timezone: str = "Asia/Tashkent"
    run_scheduler: bool = False  # the API process starts the schedule when true (one process only)

    # --- SAP Business One Service Layer (supported connector, ref [2]) ---
    sap_sl_url: str = ""          # e.g. https://sap-server:50000/b1s/v1
    sap_company_db: str = ""
    sap_read_user: str = ""       # read-only SAP user
    sap_read_password: SecretStr = SecretStr("")
    sap_post_user: str = ""       # separate SAP user, only for authorized postings
    sap_post_password: SecretStr = SecretStr("")
    sap_verify_tls: bool = True
    # SAP gateway push (as in mgmg-agents): the gateway machine POSTs to /sap/push
    sap_push_secret: SecretStr = SecretStr("")

    # --- 1C OData (ref [3]) ---
    onec_odata_url: str = ""
    onec_login: str = ""
    onec_password: SecretStr = SecretStr("")

    # --- Billz 2.0 ---
    billz_secret_token: SecretStr = SecretStr("")
    billz_base_url: str = "https://api-admin.billz.ai"

    # --- Telegram (the approved messaging channel) ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_webhook_secret: SecretStr = SecretStr("")

    # --- AIPRO / MIYA / CASH (other agents) ---
    aipro_webhook_url: str = ""
    miya_webhook_url: str = ""
    cash_webhook_secret: SecretStr = SecretStr("")

    # --- staff workspace login ---
    session_secret: SecretStr = SecretStr("change-me")

    # --- policy (starting values; the approved policy overrides them in the DB) ---
    sync_stale_minutes: int = 60          # a critical source older than this blocks digital release
    count_tolerance_units: float = 0.0    # any difference above this is "material"
    count_tolerance_value: float = 0.0
    adjustment_limit_manager: float = 5_000_000   # so'm; above → accountant/owner
    adjustment_limit_accountant: float = 50_000_000

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def sap_configured(self) -> bool:
        return bool(self.sap_sl_url and self.sap_company_db and self.sap_read_user)

    @property
    def onec_configured(self) -> bool:
        return bool(self.onec_odata_url and self.onec_login)

    @property
    def billz_configured(self) -> bool:
        return bool(self.billz_secret_token.get_secret_value())

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value())


settings = Settings()
