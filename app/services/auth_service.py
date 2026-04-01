# app/services/auth_service.py
class AuthService:
    def __init__(self, db: Session):
        self.db = db

    def authenticate(self, email: str, password: str) -> User | None:
        user = self.db.query(User).filter(User.email == email).first()
        if not user or not user.password_hash:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def create_tokens_for_user(self, user: User) -> dict:
        access = create_access_token(
            {"sub": str(user.id), "role": user.role.value},
            expires_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
        )
        refresh = create_refresh_token(
            {"sub": str(user.id)},
            expires_days=settings.REFRESH_TOKEN_EXPIRE_DAYS,
        )
        # optionally persist refresh token
        return {"access_token": access, "refresh_token": refresh}
