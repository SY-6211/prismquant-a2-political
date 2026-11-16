"""Offline configuration readiness only. No account, tracking or network actions."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import PROJECT_ROOT


class AccessManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    provider: Literal['1322', 'truth_api'] | None = None
    expected_user_id: str | None = Field(default=None, pattern=r'^[1-9][0-9]{0,29}$')
    identity_evidence: str | None = Field(default=None, max_length=500)
    license_scope: Literal['unknown', 'personal', 'group_internal', 'redistribution'] = 'unknown'
    license_reference: str | None = Field(default=None, max_length=500)
    quote_monthly_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=10, decimal_places=2)
    budget_monthly_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=10, decimal_places=2)
    budget_reference: str | None = Field(default=None, max_length=500)
    protocol_reference: str | None = Field(default=None, max_length=500)


class DataCredentials(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT/'.env', env_prefix='PQ_TRUTH_1322_',
                                     extra='ignore', hide_input_in_errors=True)
    rest_key: SecretStr = SecretStr('')
    ws_key: SecretStr = SecretStr('')
    ws_path: SecretStr = SecretStr('')

    @field_validator('rest_key', 'ws_key')
    @classmethod
    def data_key_only(cls, value):
        raw = value.get_secret_value()
        if raw.startswith(('sk-or-', 'sk-proj-')) or any(c.isspace() for c in raw):
            raise ValueError('Expected a separate data-source credential')
        return value

    @field_validator('ws_path')
    @classmethod
    def opaque_path_only(cls, value):
        import re
        raw = value.get_secret_value()
        if raw and not re.fullmatch(r'/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/?', raw):
            raise ValueError('Provide the path only, without URL, query or credentials')
        if len(raw) > 200:
            raise ValueError('Path too long')
        return value


def readiness(manifest: AccessManifest, credentials: DataCredentials) -> dict:
    missing = []
    def require(condition, code):
        if not condition:
            missing.append(code)
    require(manifest.provider is not None, 'provider_not_selected')
    require(bool(manifest.expected_user_id and manifest.identity_evidence and manifest.identity_evidence.strip()), 'account_identity_not_documented')
    require(manifest.license_scope in ('group_internal', 'redistribution') and bool(manifest.license_reference and manifest.license_reference.strip()),
            'group_use_license_not_documented')
    require(manifest.budget_monthly_usd is not None and manifest.quote_monthly_usd is not None and
            manifest.budget_monthly_usd >= manifest.quote_monthly_usd and bool(manifest.budget_reference and manifest.budget_reference.strip()),
            'budget_or_quote_not_documented')
    if manifest.provider == '1322':
        require(bool(credentials.rest_key.get_secret_value()), 'rest_credential_missing')
        require(bool(credentials.ws_key.get_secret_value()), 'websocket_credential_missing')
        require(bool(credentials.ws_path.get_secret_value()), 'websocket_path_missing')
    elif manifest.provider == 'truth_api':
        require(bool(manifest.protocol_reference and manifest.protocol_reference.strip()), 'official_protocol_not_provided')
        missing.append('official_adapter_not_implemented')
    return {'mode': 'offline_fast_source_check', 'provider': manifest.provider,
            'status': 'incomplete' if missing else 'configuration_complete_unverified', 'missing': missing,
            'live_enabled': False, 'network_requested': False, 'database_changed': False,
            'configuration_is_authorization_proof': False, 'latency_verified': False,
            'credential_presence': {key: bool(getattr(credentials, key).get_secret_value()) for key in ('rest_key','ws_key','ws_path')},
            'needs_live_validation': ['account_identity','license_scope','budget','payload_contract','disconnect_coverage','publication_to_display_latency']}
