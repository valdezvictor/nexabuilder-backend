"""
app/routers/api/user_management.py
====================================
Admin user management endpoints.
  GET    /admin/users              — list all users (admin only)
  POST   /admin/users/invite       — create user + send magic link invite
  PATCH  /admin/users/{id}         — update role, department, name, status
  DELETE /admin/users/{id}         — deactivate user (soft delete)
  POST   /admin/users/{id}/resend  — resend invite magic link
"""
import uuid as _uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy import text

from app.core.auth import get_current_user
from app.db import get_sessionmaker

router = APIRouter()

# ── Role → portal URL mapping ─────────────────────────────────────────────────
PORTAL_URLS = {
    "admin":      "https://admin.nexabuilder.com/auth/verify",
    "agent":      "https://call.nexabuilder.com/auth/verify",
    "partner":    "https://admin.nexabuilder.com/auth/verify",
    "contractor": "https://contractor.nexabuilder.com/auth/verify",
    "lead":       "https://member.nexabuilder.com/auth/verify",
}

DEPARTMENTS = [
    "Call Center", "Marketing", "Legal", "Accounting",
    "Operations", "Technology", "Executive", "Sales",
]

TENANT_ID = "a040fe36-dad0-4963-b8c2-fb0aa834e6f7"  # default tenant


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") not in ("admin",):
        raise HTTPException(403, "Admin access required")
    return user


# ── Models ─────────────────────────────────────────────────────────────────────
class InviteUser(BaseModel):
    email: str
    role: str
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    department: Optional[str] = None
    title:      Optional[str] = None
    phone:      Optional[str] = None
    notes:      Optional[str] = None
    send_invite: bool = True

class UpdateUser(BaseModel):
    role:       Optional[str] = None
    status:     Optional[str] = None
    first_name: Optional[str] = None
    last_name:  Optional[str] = None
    department: Optional[str] = None
    title:      Optional[str] = None
    phone:      Optional[str] = None
    notes:      Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────
async def _send_invite(email: str, role: str, name: str, db) -> str:
    """Create a magic link token and email the invite."""
    from app.core.security import create_access_token
    from datetime import timedelta
    import urllib.request, urllib.parse, json

    # Create magic link token (15 min)
    token = create_access_token({"email": email, "type": "magic_link"}, expires_delta=timedelta(minutes=60))
    portal_url = PORTAL_URLS.get(role, "https://member.nexabuilder.com/auth/verify")
    magic_link = f"{portal_url}?token={token}"

    # Send via SES (simple)
    try:
        import boto3
        ses = boto3.client("ses", region_name="us-east-1")
        display_name = name or email.split("@")[0]
        portal_label = {
            "admin":      "Admin Console",
            "agent":      "Call Center",
            "partner":    "Partner Portal",
            "contractor": "Contractor Portal",
            "lead":       "Member Portal",
        }.get(role, "NexaBuilder Portal")

        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": f"You\'re invited to NexaBuilder — {portal_label}"},
                "Body": {"Html": {"Data": f"""
<div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px">
  <img src="https://nexabuilder.com/images/logo.png" height="40" alt="NexaBuilder"/>
  <h2 style="color:#0d1b2e;margin-top:24px">Welcome to NexaBuilder, {display_name}!</h2>
  <p>You\'ve been added as a <strong>{role}</strong> on the NexaBuilder platform.</p>
  <p>Click the button below to sign in to your {portal_label}:</p>
  <a href="{magic_link}" style="display:inline-block;padding:12px 28px;background:#1d6fde;
     color:#fff;text-decoration:none;border-radius:8px;font-weight:700;margin:16px 0">
     Sign In →
  </a>
  <p style="font-size:12px;color:#64748b;margin-top:24px">
    This link expires in 60 minutes. If you didn\'t expect this invitation, you can ignore it.
  </p>
</div>
"""}},
            }
        )
        print(f"[Invite] Sent to {email} ({role})")
        return magic_link
    except Exception as e:
        print(f"[Invite] SES error: {e}")
        return magic_link  # Return link even if email fails


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.get("/admin/users")
async def list_users(
    role: Optional[str] = None,
    status: Optional[str] = None,
    department: Optional[str] = None,
    admin: dict = Depends(require_admin)
):
    """List all users with optional filters."""
    S = get_sessionmaker()
    async with S() as db:
        where_clauses = ["1=1"]
        params = {}
        if role:
            where_clauses.append("u.role::text = :role")
            params["role"] = role
        if status:
            where_clauses.append("u.status::text = :status")
            params["status"] = status
        if department:
            where_clauses.append("u.department ILIKE :dept")
            params["dept"] = f"%{department}%"

        where = " AND ".join(where_clauses)
        r = await db.execute(text(f"""
            SELECT
                u.id, u.email, u.role::text, u.status::text,
                u.first_name, u.last_name, u.department, u.title,
                u.phone, u.notes,
                u.created_at, u.last_login_at, u.is_email_verified
            FROM users u
            WHERE {where}
            ORDER BY u.role, u.created_at DESC
        """), params)
        rows = r.fetchall()

    users = []
    for row in rows:
        uid, email, role_, status_, fn, ln, dept, title, phone, notes, created, last_login, verified = row
        users.append({
            "id":           str(uid),
            "email":        email,
            "role":         role_,
            "status":       status_,
            "first_name":   fn,
            "last_name":    ln,
            "department":   dept,
            "title":        title,
            "phone":        phone,
            "notes":        notes,
            "full_name":    " ".join(filter(None, [fn, ln])) or email.split("@")[0],
            "created_at":   created.isoformat() if created else None,
            "last_login_at": last_login.isoformat() if last_login else None,
            "verified":     verified,
        })
    return {"users": users, "total": len(users)}


@router.post("/admin/users/invite")
async def invite_user(
    body: InviteUser,
    admin: dict = Depends(require_admin)
):
    """Create a new user and optionally send an invite email."""
    valid_roles = ("admin","agent","partner","contractor","lead")
    if body.role not in valid_roles:
        raise HTTPException(400, f"Invalid role. Must be one of: {valid_roles}")

    S = get_sessionmaker()
    async with S() as db:
        # Check if user already exists
        r = await db.execute(text(
            "SELECT id, role::text, status::text FROM users WHERE email = :email"
        ), {"email": body.email.lower().strip()})
        existing = r.fetchone()

        if existing:
            # User exists — update their role/profile if needed
            user_id = existing[0]
            await db.execute(text("""
                UPDATE users SET
                    role       = CAST(:role AS userrole),
                    first_name = COALESCE(:fn, first_name),
                    last_name  = COALESCE(:ln, last_name),
                    department = COALESCE(:dept, department),
                    title      = COALESCE(:title, title),
                    phone      = COALESCE(:phone, phone),
                    status     = \'active\'
                WHERE id = :id
            """), {
                "id": user_id, "role": body.role,
                "fn": body.first_name, "ln": body.last_name,
                "dept": body.department, "title": body.title,
                "phone": body.phone,
            })
            await db.commit()
            action = "updated"
        else:
            # Create new user
            user_id = str(_uuid.uuid4())
            await db.execute(text("""
                INSERT INTO users (id, email, role, status, first_name, last_name,
                                   department, title, phone, notes, is_email_verified, is_phone_verified)
                VALUES (
                    CAST(:id AS uuid), :email, CAST(:role AS userrole), \'active\',
                    :fn, :ln, :dept, :title, :phone, :notes, false, false
                )
            """), {
                "id": user_id, "email": body.email.lower().strip(),
                "role": body.role, "fn": body.first_name, "ln": body.last_name,
                "dept": body.department, "title": body.title,
                "phone": body.phone, "notes": body.notes,
            })
            # Link to default tenant
            await db.execute(text("""
                INSERT INTO user_tenants (id, user_id, tenant_id)
                VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), CAST(:tid AS uuid))
                ON CONFLICT DO NOTHING
            """), {"id": str(_uuid.uuid4()), "uid": user_id, "tid": TENANT_ID})
            await db.commit()
            action = "created"

    # Send invite email
    magic_link = None
    if body.send_invite:
        S2 = get_sessionmaker()
        async with S2() as db2:
            display_name = f"{body.first_name or ''} {body.last_name or ''}".strip() or body.email
            magic_link = await _send_invite(body.email, body.role, display_name, db2)

    return {
        "success":    True,
        "action":     action,
        "user_id":    str(user_id),
        "email":      body.email,
        "role":       body.role,
        "invite_sent": body.send_invite,
        "magic_link":  magic_link,  # Return for admin to copy if needed
    }


@router.patch("/admin/users/{user_id}")
async def update_user(
    user_id: str,
    body: UpdateUser,
    admin: dict = Depends(require_admin)
):
    """Update user role, department, profile, or status."""
    S = get_sessionmaker()
    async with S() as db:
        # Build dynamic SET clause
        sets, params = [], {"id": user_id}

        if body.role is not None:
            sets.append("role = CAST(:role AS userrole)")
            params["role"] = body.role
        if body.status is not None:
            sets.append("status = CAST(:status AS userstatus)")
            params["status"] = body.status
        if body.first_name is not None:
            sets.append("first_name = :fn"); params["fn"] = body.first_name
        if body.last_name is not None:
            sets.append("last_name = :ln"); params["ln"] = body.last_name
        if body.department is not None:
            sets.append("department = :dept"); params["dept"] = body.department
        if body.title is not None:
            sets.append("title = :title"); params["title"] = body.title
        if body.phone is not None:
            sets.append("phone = :phone"); params["phone"] = body.phone
        if body.notes is not None:
            sets.append("notes = :notes"); params["notes"] = body.notes

        if not sets:
            raise HTTPException(400, "No fields to update")

        sets.append("updated_at = NOW()")
        set_clause = ", ".join(sets)
        await db.execute(text(
            f"UPDATE users SET {set_clause} WHERE id = CAST(:id AS uuid)"
        ), params)
        await db.commit()

    return {"success": True, "user_id": user_id}


@router.delete("/admin/users/{user_id}")
async def deactivate_user(
    user_id: str,
    admin: dict = Depends(require_admin)
):
    """Soft-deactivate a user (preserves data)."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT email FROM users WHERE id = CAST(:id AS uuid)"
        ), {"id": user_id})
        row = r.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        email = row[0]

        await db.execute(text(
            "UPDATE users SET status = \'inactive\', updated_at = NOW() "
            "WHERE id = CAST(:id AS uuid)"
        ), {"id": user_id})
        await db.commit()

    return {"success": True, "email": email, "status": "inactive"}


@router.post("/admin/users/{user_id}/resend")
async def resend_invite(
    user_id: str,
    admin: dict = Depends(require_admin)
):
    """Resend invite magic link to an existing user."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT email, role::text, first_name, last_name "
            "FROM users WHERE id = CAST(:id AS uuid)"
        ), {"id": user_id})
        row = r.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        email, role, fn, ln = row

    display_name = f"{fn or ''} {ln or ''}".strip() or email
    S2 = get_sessionmaker()
    async with S2() as db2:
        magic_link = await _send_invite(email, role, display_name, db2)

    return {"success": True, "email": email, "invite_sent": True, "magic_link": magic_link}
