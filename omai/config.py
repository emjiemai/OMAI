"""Settings, read from the environment (or .env). Secrets never live in the repo.

Only variables that already exist in the company's shared environment are read (the same group mgmg-agents
uses). ``DATABASE_URL`` is set by Render from the attached database. Everything else is a constant below.
"""

from __future__ import annotations

import hashlib
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- policy starting values (the approved policy replaces them) ---
SYNC_STALE_MINUTES = 60          # a critical source older than this blocks digital release
COUNT_TOLERANCE_UNITS = 0.0      # any difference above this is "material"
COUNT_TOLERANCE_VALUE = 0.0
ADJUSTMENT_LIMIT_MANAGER = 5_000_000      # so'm; above → accountant / owner
ADJUSTMENT_LIMIT_ACCOUNTANT = 50_000_000

# --- 1C OData: which register and columns hold stock (finance confirms) ---
ONEC_BALANCE_ENTITY = "AccumulationRegister_ТоварыНаСкладах/Balance"
ONEC_ITEM_FIELD = "Номенклатура_Key"
ONEC_WAREHOUSE_FIELD = "Склад_Key"
ONEC_QTY_FIELD = "КоличествоBalance"

# --- Billz ---
BILLZ_BASE_URL = "https://api-admin.billz.ai"
BILLZ_STOCK_PATH = "/v2/products"   # verify with Billz for our plan


class Settings(BaseSettings):
    database_url: str = "sqlite:///./omai.db"
    environment: str = "development"   # ENVIRONMENT=production starts the daily schedule
    dry_run: bool = False               # DRY_RUN=true: nothing is sent out

    sap_push_webhook_secret: SecretStr = SecretStr("")   # the SAP gateway machine pushes to POST /sap/push

    onec_odata_url: str = ""
    onec_login: str = ""
    onec_password: SecretStr = SecretStr("")

    billz_secret_token: SecretStr = SecretStr("")

    telegram_primary_bot_token: SecretStr = SecretStr("")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    @property
    def tz(self) -> ZoneInfo:
        import os
        return ZoneInfo(os.getenv("TZ") or "Asia/Tashkent")

    @property
    def onec_configured(self) -> bool:
        return bool(self.onec_odata_url and self.onec_login)

    @property
    def billz_configured(self) -> bool:
        return bool(self.billz_secret_token.get_secret_value())

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_primary_bot_token.get_secret_value())

    @property
    def run_scheduler(self) -> bool:
        return self.environment == "production"

    def derived_secret(self, purpose: str) -> str:
        """A per-purpose secret derived from the existing bot token (no extra variable)."""
        base = self.telegram_primary_bot_token.get_secret_value() or self.database_url
        return hashlib.sha256(f"omai:{purpose}:{base}".encode()).hexdigest()


settings = Settings()
