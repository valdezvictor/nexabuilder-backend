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
    """Create a magic link token and send invite email via SES."""
    from app.core.security import create_access_token

    # Look up the user's UUID — the verify endpoint requires sub=user_id
    r = await db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})
    row = r.fetchone()
    if not row:
        # Fallback — create token with email only (won't verify but link is still returnable)
        print(f"[Invite] Warning: user {email} not found in DB when creating token")
        token = create_access_token(
            {"email": email, "type": "magic_link"},
            expires_minutes=1440
        )
    else:
        user_id = str(row[0])
        token = create_access_token(
            {"sub": user_id, "email": email, "type": "magic_link"},
            expires_minutes=1440  # 24 hours for invites
        )
    portal_url = PORTAL_URLS.get(role, "https://member.nexabuilder.com/auth/verify")
    magic_link = f"{portal_url}?token={token}"
    display_name = name or email.split("@")[0]

    PORTAL_LABELS = {
        "admin":      "Admin Console",
        "agent":      "Call Center",
        "partner":    "Partner Portal",
        "contractor": "Contractor Portal",
        "lead":       "Member Portal",
    }
    portal_label = PORTAL_LABELS.get(role, "NexaBuilder Portal")

    try:
        import boto3
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": f"You are invited to NexaBuilder — {portal_label}"},
                "Body": {"Html": {"Data": (
                    "<div style=\"font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px\">"
                    "<h2 style=\"color:#0d1b2e;margin-top:24px\">Welcome to NexaBuilder!</h2>"
                    f"<p>Hi {display_name}, you have been added as a <strong>{role}</strong>.</p>"
                    f"<p>Click below to sign in to your {portal_label}:</p>"
                    f"<a href=\"{magic_link}\" style=\"display:inline-block;padding:12px 28px;"
                    "background:#1d6fde;color:#fff;text-decoration:none;border-radius:8px;"
                    "font-weight:700;margin:16px 0\">Sign In to NexaBuilder &rarr;</a>"
                    "<p style=\"font-size:12px;color:#64748b;margin-top:24px\">"
                    "This link expires in 24 hours.</p>"
                    "</div>"
                )}},
            }
        )
        print(f"[Invite] Email sent to {email} ({role})")
    except Exception as e:
        print(f"[Invite] SES error for {email}: {e}")
        # Still return the link so admin can copy it manually

    return magic_link


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
        if hasattr(body, 'job_title') and body.job_title is not None:
            sets.append("job_title = :job_title"); params["job_title"] = body.job_title

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


# ── Job Titles & Permissions endpoints ────────────────────────────────────────

@router.get("/admin/job-titles")
async def list_job_titles(
    department: Optional[str] = None,
    admin: dict = Depends(require_admin)
):
    """Return all job titles, optionally filtered by department."""
    S = get_sessionmaker()
    async with S() as db:
        where = "WHERE is_active = TRUE"
        params = {}
        if department:
            where += " AND department = :dept"
            params["dept"] = department
        r = await db.execute(text(
            f"SELECT id, title, department, base_role, extra_permissions, description "
            f"FROM job_titles {where} ORDER BY department, title"
        ), params)
        rows = r.fetchall()

    titles = []
    for row in rows:
        id_, title, dept, role, extras, desc = row
        titles.append({
            "id":          id_,
            "title":       title,
            "department":  dept,
            "base_role":   role,
            "extra_permissions": extras or [],
            "description": desc,
        })

    # Group by department
    by_dept: dict = {}
    for t in titles:
        by_dept.setdefault(t["department"], []).append(t)

    return {"titles": titles, "by_department": by_dept, "total": len(titles)}


@router.get("/admin/permissions")
async def list_permissions(admin: dict = Depends(require_admin)):
    """Return all permissions grouped by category."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT code, description FROM permissions ORDER BY code"
        ))
        rows = r.fetchall()

        r2 = await db.execute(text(
            "SELECT role, array_agg(permission_code ORDER BY permission_code) "
            "FROM role_permissions GROUP BY role"
        ))
        role_rows = r2.fetchall()

    perms = [{"code": row[0], "description": row[1],
               "category": row[0].split(".")[0]} for row in rows]

    # Group by category
    by_cat: dict = {}
    for p in perms:
        by_cat.setdefault(p["category"], []).append(p)

    role_perms = {row[0]: row[1] for row in role_rows}

    return {
        "permissions":    perms,
        "by_category":    by_cat,
        "role_permissions": role_perms,
        "total":          len(perms),
    }


@router.get("/admin/users/{user_id}/permissions")
async def get_user_permissions(
    user_id: str,
    admin: dict = Depends(require_admin)
):
    """Return the effective permissions for a specific user."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT role::text, job_title FROM users WHERE id = CAST(:id AS uuid)"
        ), {"id": user_id})
        row = r.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        role, job_title = row[0], row[1] if len(row) > 1 else None

        # Base role permissions
        r2 = await db.execute(text(
            "SELECT array_agg(permission_code) FROM role_permissions WHERE role = :role"
        ), {"role": role})
        base_perms = r2.scalar() or []

        # Extra permissions from job title
        extra_perms = []
        if job_title:
            r3 = await db.execute(text(
                "SELECT extra_permissions FROM job_titles WHERE title = :title"
            ), {"title": job_title})
            jt_row = r3.fetchone()
            if jt_row:
                extra_perms = jt_row[0] or []

        all_perms = list(set(base_perms + extra_perms))
        all_perms.sort()

    return {
        "user_id":    user_id,
        "role":       role,
        "job_title":  job_title,
        "base_permissions":  base_perms,
        "extra_permissions": extra_perms,
        "all_permissions":   all_perms,
    }


# ── Lead profile update endpoint ──────────────────────────────────────────────
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional

class LeadProfileUpdate(_BaseModel):
    first_name:   _Optional[str] = None
    last_name:    _Optional[str] = None
    phone:        _Optional[str] = None
    email:        _Optional[str] = None
    address_line1: _Optional[str] = None
    city:         _Optional[str] = None
    state:        _Optional[str] = None
    postal_code:  _Optional[str] = None
    budget:       _Optional[str] = None
    timeline:     _Optional[str] = None
    project_description: _Optional[str] = None

@router.patch("/admin/leads/{lead_id}/profile")
async def update_lead_profile(
    lead_id: int,
    body: LeadProfileUpdate,
    admin: dict = Depends(require_admin)
):
    """Update lead contact info and address. Used by admin to fix missing data."""
    S = get_sessionmaker()
    async with S() as db:
        sets, params = [], {"id": lead_id}
        field_map = {
            "first_name": "first_name", "last_name": "last_name",
            "phone": "phone", "email": "email",
            "address_line1": "address_line1", "city": "city",
            "state": "state", "postal_code": "postal_code",
            "budget": "budget", "timeline": "timeline",
            "project_description": "project_description",
        }
        for field, col in field_map.items():
            val = getattr(body, field, None)
            if val is not None:
                sets.append(f"{col} = :{field}")
                params[field] = val

        if not sets:
            raise HTTPException(400, "No fields to update")

        sets.append("updated_at = NOW()")
        set_clause = ", ".join(sets)
        await db.execute(text(
            f"UPDATE leads SET {set_clause} WHERE id = :id"
        ), params)
        await db.commit()

    return {"success": True, "lead_id": lead_id}


@router.get("/admin/leads/{lead_id}")
async def get_lead_detail(
    lead_id: int,
    admin: dict = Depends(require_admin)
):
    """Get full lead details for admin editing."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT id, email, first_name, last_name, phone, "
            "address_line1, city, state, postal_code, vertical, "
            "budget, timeline, project_description, status, "
            "needs_financing, project_sqft, estimated_cost_low, estimated_cost_high, "
            "created_at "
            "FROM leads WHERE id = :id"
        ), {"id": lead_id})
        row = r.fetchone()
        if not row:
            raise HTTPException(404, "Lead not found")

        cols = ["id","email","first_name","last_name","phone",
                "address_line1","city","state","postal_code","vertical",
                "budget","timeline","project_description","status",
                "needs_financing","project_sqft","estimated_cost_low","estimated_cost_high",
                "created_at"]
        return dict(zip(cols, [str(v) if v is not None else None for v in row]))
