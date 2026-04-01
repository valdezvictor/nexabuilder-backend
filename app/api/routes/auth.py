# app/api/routes/auth.py
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    service = AuthService(db)
    user = service.authenticate(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    tokens = service.create_tokens_for_user(user)
    return TokenResponse(**tokens)

@router.post("/magic-link/email/request")
def request_email_magic_link(
    payload: MagicLinkEmailRequest,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        # optionally create lead user or return 404
        raise HTTPException(status_code=404, detail="User not found")
    ml_service = MagicLinkService(db)
    token = ml_service.create_email_magic_link(user)
    email_provider = get_email_provider(db)
    link = f"{settings.FRONTEND_URL}/magic-login?token={token}"
    email_provider.send_email(
        to=user.email,
        subject="Your NexaBuilder login link",
        body=f"Click to log in: {link}",
    )
    return {"status": "ok"}

@router.get("/magic-link/email/verify", response_model=TokenResponse)
def verify_email_magic_link(token: str, db: Session = Depends(get_db)):
    ml_service = MagicLinkService(db)
    user = ml_service.verify_email_magic_link(token)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    service = AuthService(db)
    tokens = service.create_tokens_for_user(user)
    return TokenResponse(**tokens)
