from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.schemas.trade import TradeOut
from app.db import get_db

router = APIRouter(prefix="/api", tags=["Trades"])

@router.get("/trades", response_model=list[TradeOut])
def get_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.name.asc()).all()
    return trades
