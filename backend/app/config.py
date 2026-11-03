"""
Environment-driven configuration and Google Cloud Secret Manager integration.
Enforces fail-fast startup validation so missing or placeholder secrets are rejected
in staging and production environments.
"""

from __future__ import annotations

import os
import urllib.request
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

FORBIDDEN_SECRET_VALUES = {
    "",
    "secretpassword",
    "orderbridge_webhook_verify_2026",
    "CHANGE_ME_LOCAL_DEV_PASSWORD",
    "CHANGE_ME_32_CHAR_MINIMUM_JWT_SIGNING_KEY",
    "CHANGE_ME_32_BYTE_PII_ENCRYPTION_KEY",
    "CHANGE_ME_META_WEBHOOK_VERIFY_TOKEN",
    "CHANGE_ME_META_APP_HMAC_SHA256_SECRET",
    "CHANGE_ME_SYSTEM_USER_ACCESS_TOKEN",
}


def fetch_gcp_secret(project_id: str, secret_id: str, version: str = "latest") -> str | None:
    """
    Fetches a secret payload from Google Cloud Secret Manager using the Compute Metadata
    Server OAuth token when running on Cloud Run, avoiding heavy gRPC startup latency.
    Returns None if unavailable.
    """
    if not project_id or not secret_id:
        return None
    try:
        meta_req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(meta_req, timeout=2.0) as resp:  # noqa: S310
            import json

            token_data = json.loads(resp.read().decode("utf-8"))
            access_token = token_data.get("access_token")
        if not access_token:
            return None

        secret_url = (
            f"https://secretmanager.googleapis.com/v1/projects/{project_id}"
            f"/secrets/{secret_id}/versions/{version}:access"
        )
        sec_req = urllib.request.Request(  # noqa: S310
            secret_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(sec_req, timeout=3.0) as sec_resp:  # noqa: S310
            import base64
            import json

            payload = json.loads(sec_resp.read().decode("utf-8"))
            b64_data = payload.get("payload", {}).get("data", "")
            if b64_data:
                return base64.b64decode(b64_data).decode("utf-8").strip()
    except Exception:
        return None
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    port: int = Field(default=8000, alias="PORT")
    app_url: str = Field(default="http://localhost:3000", alias="APP_URL")
    cors_allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000",
        alias="CORS_ALLOWED_ORIGINS",
    )

    database_url: str = Field(
        default="sqlite:///./orderbridge_dev.db",
        alias="DATABASE_URL",
    )

    jwt_secret: str = Field(default="dev_only_jwt_secret_not_for_production_use_32b", alias="JWT_SECRET")
    phone_encryption_key: str = Field(
        default="dev_only_phone_key_not_for_production_use_32",
        alias="PHONE_ENCRYPTION_KEY",
    )

    meta_verify_token: str = Field(default="", alias="META_VERIFY_TOKEN")
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_graph_api_version: str = Field(default="v21.0", alias="META_GRAPH_API_VERSION")
    meta_access_token: str = Field(default="", alias="META_ACCESS_TOKEN")

    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    llm_provider: str = Field(default="gemini", alias="LLM_PROVIDER")
    gemini_fallback_model: str = Field(default="gemini-3.8-flash", alias="GEMINI_FALLBACK_MODEL")
    gemini_vision_model: str = Field(default="gemini-3.8-flash", alias="GEMINI_VISION_MODEL")
    gemini_transcribe_model: str = Field(default="gemini-3.5-transcribe", alias="GEMINI_TRANSCRIBE_MODEL")
    default_tenant_monthly_llm_cap_paise: int = Field(
        default=500_000,
        alias="DEFAULT_TENANT_MONTHLY_LLM_CAP_PAISE",
    )

    gcp_project_id: str = Field(default="", alias="GCP_PROJECT_ID")
    gcp_region: str = Field(default="asia-south1", alias="GCP_REGION")
    gcs_media_bucket: str = Field(default="", alias="GCS_MEDIA_BUCKET")
    use_gcp_secret_manager: bool = Field(default=False, alias="USE_GCP_SECRET_MANAGER")

    @property
    def allowed_origins_list(self) -> list[str]:
        origins = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        return origins or ["http://localhost:3000"]

    def resolve_secret_manager_overrides(self) -> None:
        if not self.use_gcp_secret_manager or not self.gcp_project_id:
            return
        secret_mappings = {
            "database_url": "ORDERBRIDGE_DATABASE_URL",
            "jwt_secret": "ORDERBRIDGE_JWT_SECRET",
            "meta_verify_token": "ORDERBRIDGE_META_VERIFY_TOKEN",
            "meta_app_secret": "ORDERBRIDGE_META_APP_SECRET",
            "meta_access_token": "ORDERBRIDGE_META_ACCESS_TOKEN",
            "gemini_api_key": "ORDERBRIDGE_GEMINI_API_KEY",
        }
        for attr, secret_name in secret_mappings.items():
            val = fetch_gcp_secret(self.gcp_project_id, secret_name)
            if val:
                setattr(self, attr, val)

    def validate_production_readiness(self) -> None:
        """
        Fail fast in staging or production if critical secrets or PostgreSQL DSN are missing
        or left at placeholder values.
        """
        env_lower = self.app_env.lower().strip()
        if env_lower not in {"staging", "production", "prod"}:
            return

        if not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://", "postgresql+asyncpg://")
        ):
            raise RuntimeError(
                f"FATAL: APP_ENV={self.app_env} requires a PostgreSQL DATABASE_URL (got scheme: "
                f"{self.database_url.split(':', 1)[0]})."
            )

        required_secrets = {
            "JWT_SECRET": self.jwt_secret,
            "META_VERIFY_TOKEN": self.meta_verify_token,
            "META_APP_SECRET": self.meta_app_secret,
            "PHONE_ENCRYPTION_KEY": self.phone_encryption_key,
        }
        for name, val in required_secrets.items():
            cleaned = (val or "").strip()
            if cleaned in FORBIDDEN_SECRET_VALUES or cleaned.startswith("dev_only_") or len(cleaned) < 16:
                raise RuntimeError(
                    f"FATAL: Secret {name} is missing, uses a forbidden placeholder, or is shorter than "
                    f"16 characters in APP_ENV={self.app_env}."
                )

        if "*" in self.allowed_origins_list:
            raise RuntimeError(
                f"FATAL: Wildcard CORS_ALLOWED_ORIGINS='*' is prohibited in APP_ENV={self.app_env}."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    # Allow explicit environment variable overrides at call time
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.alias or field_name.upper()
        if alias in os.environ:
            raw_val = os.environ[alias]
            if field_name == "port" or field_name == "default_tenant_monthly_llm_cap_paise":
                setattr(settings, field_name, int(raw_val))
            elif field_name == "use_gcp_secret_manager":
                setattr(settings, field_name, raw_val.lower() in {"1", "true", "yes"})
            else:
                setattr(settings, field_name, raw_val)
    settings.resolve_secret_manager_overrides()
    settings.validate_production_readiness()
    return settings
