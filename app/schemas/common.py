"""Common Pydantic schemas: envelopes, pagination, auth payloads."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PageMeta(BaseModel):
    page: int = 1
    pageSize: int = 25
    total: int = 0
    totalPages: int = 0


class ErrorItem(BaseModel):
    code: str
    field: str | None = None
    message: str


class Envelope(BaseModel):
    data: Any = None
    meta: PageMeta | None = None
    errors: list[ErrorItem] | None = None


class Token(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    employee_id: str | None = None
    role_codes: list[str] = Field(default_factory=list)


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class SwitchOrgRequest(BaseModel):
    org_id: str


class UpdateMeRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: EmailStr | None = None
    avatar_url: str | None = None
    locale: str | None = None
    theme: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Any
    email: str
    employee_id: Any | None = None
    is_active: bool
    role_codes: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    is_global_admin: bool = False


class BulkIds(BaseModel):
    ids: list[Any]


class BulkCreate(BaseModel):
    items: list[dict]


class BulkUpdate(BaseModel):
    ids: list[Any]
    data: dict


class ActionRequest(BaseModel):
    comments: str | None = None
    reason: str | None = None
    data: dict | None = None


class PreferencesIn(BaseModel):
    ui_prefs: dict | None = None
    locale: str | None = None
    theme: str | None = None


class DashboardLayoutIn(BaseModel):
    widgets: list[Any] = Field(default_factory=list)
