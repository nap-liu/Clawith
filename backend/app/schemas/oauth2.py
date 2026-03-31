"""OAuth2 provider schemas."""
from pydantic import BaseModel, Field, field_validator
from typing import Optional
import uuid


class OAuth2FieldMapping(BaseModel):
    """OAuth2 field name mapping."""
    user_id: str = Field(default='', description='User ID field name (empty = use "sub")')
    name: str = Field(default='', description='Name field name (empty = use "name")')
    email: str = Field(default='', description='Email field name (empty = use "email")')
    mobile: str = Field(default='', description='Mobile field name (empty = use "phone_number")')


class OAuth2Config(BaseModel):
    """OAuth2 provider configuration."""
    app_id: str = Field(..., min_length=1, description='OAuth2 Client ID')
    app_secret: str = Field(..., min_length=1, description='OAuth2 Client Secret')
    authorize_url: str = Field(..., description='OAuth2 Authorization Endpoint')
    token_url: str = Field(default='', description='OAuth2 Token Endpoint (optional)')
    user_info_url: str = Field(default='', description='OAuth2 UserInfo Endpoint (optional)')
    scope: str = Field(default='openid profile email', description='OAuth2 Scopes')
    field_mapping: Optional[OAuth2FieldMapping] = Field(default=None, description='Custom field name mapping')
    
    @field_validator('authorize_url')
    @classmethod
    def validate_authorize_url(cls, v: str) -> str:
        if not v or (not v.startswith('http://') and not v.startswith('https://')):
            raise ValueError('authorize_url must be a valid HTTP/HTTPS URL')
        return v


class OAuth2ProviderCreate(BaseModel):
    """OAuth2 provider create payload."""
    name: str = Field(..., min_length=1)
    config: OAuth2Config
    is_active: bool = True
    sso_login_enabled: bool = True
    tenant_id: Optional[uuid.UUID] = None


class OAuth2ProviderUpdate(BaseModel):
    """OAuth2 provider update payload - config format only."""
    name: Optional[str] = None
    is_active: Optional[bool] = None
    sso_login_enabled: Optional[bool] = None
    config: Optional[OAuth2Config] = None
