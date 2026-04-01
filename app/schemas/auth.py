from pydantic import BaseModel, EmailStr

class LoginRequest(BaseModel):
    email: str
    password: str

class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"

class MagicLinkEmailRequest(BaseModel):
    email: EmailStr

class MagicLinkSMSRequest(BaseModel):
    phone: str

class SMSCodeVerifyRequest(BaseModel):
    phone: str
    code: str
