# app/services/magic_link_service.py
class MagicLinkService:
    def __init__(self, db: Session):
        self.db = db

    def create_email_magic_link(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        self.db.add(
            AuthToken(
                user_id=user.id,
                token=token,
                type=AuthTokenType.email_magic_link,
                expires_at=expires_at,
            )
        )
        self.db.commit()
        return token

    def verify_email_magic_link(self, token: str) -> User | None:
        record = (
            self.db.query(AuthToken)
            .filter_by(token=token, type=AuthTokenType.email_magic_link, used=False)
            .first()
        )
        if not record or record.expires_at < datetime.utcnow():
            return None
        record.used = True
        self.db.commit()
        return self.db.query(User).get(record.user_id)
