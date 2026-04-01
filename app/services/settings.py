# app/services/settings.py

from typing import Optional

from sqlalchemy.orm import Session

# Adjust this import to wherever you define your models
from app.models import SystemSetting  # e.g. app.models, app.db_models, etc.


def get_setting(db: Session, key: str, default: bool = False) -> bool:
    """
    Read a boolean setting from the SystemSetting table.
    Does NOT create/close the session – it uses the one passed in.
    """
    row: Optional[SystemSetting] = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == key)
        .first()
    )

    if not row:
        return default

    return str(row.value).lower() == "true"


def set_setting(db: Session, key: str, value: bool) -> None:
    """
    Write a boolean setting to the SystemSetting table.
    Does NOT create/close the session – it uses the one passed in.
    """
    row: Optional[SystemSetting] = (
        db.query(SystemSetting)
        .filter(SystemSetting.key == key)
        .first()
    )

    if row is None:
        row = SystemSetting(
            key=key,
            value="true" if value else "false",
        )
        db.add(row)
    else:
        row.value = "true" if value else "false"

    db.commit()
