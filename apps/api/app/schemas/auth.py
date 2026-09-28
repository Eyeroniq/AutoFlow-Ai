from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, field_validator

# bcrypt only uses the first 72 bytes of a password; reject longer ones rather than
# silently truncating.
BCRYPT_MAX_BYTES = 72

NormalizedEmail = Annotated[EmailStr, AfterValidator(lambda value: value.strip().lower())]


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"email": "demo@flowforge.ai", "password": "demo1234"}]}
    )

    email: NormalizedEmail
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"email": "ada@example.com", "password": "s3cure-pass", "full_name": "Ada Lovelace"}
            ]
        }
    )

    email: NormalizedEmail
    password: str = Field(min_length=8, max_length=BCRYPT_MAX_BYTES)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        if value.strip() == "":
            raise ValueError("Password cannot be blank")
        if len(value.encode("utf-8")) > BCRYPT_MAX_BYTES:
            raise ValueError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes")
        return value

    @field_validator("full_name")
    @classmethod
    def full_name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Full name cannot be blank")
        return value


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=4096)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
