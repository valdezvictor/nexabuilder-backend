"""crm_router.py — CRM + Review Pipeline for NexaBuilder"""
import os, secrets, json
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text as sqlt
from sqlalchemy.orm import sessionmaker
import boto3

router = APIRouter(prefix="/api/crm", tags=["CRM"])

def _adm(k):
    key = os.getenv("CMS_ADMIN_KEY","")
    if not key or k != key: raise HTTPException(403,"Invalid admin key")

def _db():
    engine = create_engine(
        os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()

def _ses():
    return boto3.client("ses", region_name="us-east-1")

SURVEY_BASE = "https://api.nexabuilder.com/api/crm/survey"

# ── Models ─────────────────────────────────────────────────────────────────────

class CompleteJobPayload(BaseModel):
    lead_id:       int
    contractor_id: int
    job_amount:    Optional[float] = None
    job_notes:     Optional[str]   = None
    google_place_id: Optional[str] = None   # contractor's Google Business place ID

class SurveyResponse(BaseModel):
    token:   str
    rating:  int   # 1-5
    comment: Optional[str] = None

# ── POST /api/crm/complete-job ─────────────────────────────────────────────────

@router.post("/complete-job")
def complete_job(payload: CompleteJobPayload, x_admin_key: str = Header(...)):
    """Mark a lead as job-complete and trigger the review request pipeline."""
    _adm(x_admin_key)
    db = _db()

    # 1. Get lead + homeowner info
    lead = db.execute(sqlt("""
        SELECT l.id, l.first_name, l.last_name, l.email, l.phone,
               l.vertical, l.assigned_contractor_id,
               ca.company_name, ca.id AS ca_id,
               u.first_name AS c_first, u.last_name AS c_last
        FROM leads l
        LEFT JOIN contractor_accounts ca ON ca.id=:cid
        LEFT JOIN users u ON u.id=ca.user_id
        WHERE l.id=:lid
    """), {"lid": payload.lead_id, "cid": payload.contractor_id}).fetchone()

    if not lead:
        raise HTTPException(404, "Lead not found")
    if not lead.email:
        raise HTTPException(422, "Lead has no email address — cannot send review request")

    # 2. Mark lead as completed
    db.execute(sqlt("""
        UPDATE leads SET
          lead_status       = 'completed',
          job_completed_at  = NOW(),
          job_amount        = :amt,
          job_notes         = :notes
        WHERE id = :lid
    """), {"amt": payload.job_amount, "notes": payload.job_notes, "lid": payload.lead_id})
    # Fire CAPI Purchase event for completed job
    try:
        import threading as _ct; from app.capi_dispatcher import fire_lead_event as _cf
        _ct.Thread(target=_cf, args=(payload.lead_id,"Purchase",float(payload.job_amount or 0)), daemon=True).start()
    except Exception: pass

    # 3. Create review request row
    token = secrets.token_urlsafe(32)
    rr = db.execute(sqlt("""
        INSERT INTO review_requests
          (lead_id, contractor_id, homeowner_email, homeowner_name, homeowner_phone,
           contractor_name, contractor_company, vertical, job_amount, token,
           google_place_id, status)
        VALUES
          (:lid, :cid, :email, :name, :phone,
           :cname, :company, :vertical, :amt, :token,
           :gplace, 'pending')
        RETURNING id
    """), {
        "lid":     payload.lead_id,
        "cid":     payload.contractor_id,
        "email":   lead.email,
        "name":    f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
        "phone":   lead.phone,
        "cname":   f"{lead.c_first or ''} {lead.c_last or ''}".strip(),
        "company": lead.company_name,
        "vertical":lead.vertical,
        "amt":     payload.job_amount,
        "token":   token,
        "gplace":  payload.google_place_id,
    }).fetchone()
    rr_id = rr[0]

    # 4. Send NexaBuilder survey email (Step 1)
    survey_url = f"{SURVEY_BASE}/{token}"
    vertical_label = (lead.vertical or "home improvement").replace("-"," ").title()
    contractor_label = lead.company_name or "your contractor"

    email_sent = False
    try:
        _ses().send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [lead.email]},
            Message={
                "Subject": {"Data": f"How did your {vertical_label} project go?"},
                "Body": {"Html": {"Data": f"""
<div style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#0d1e35">
  <img src="https://www.nexabuilder.com/images/NexaBuilder_logo.png" height="36" alt="NexaBuilder"/>
  <h2 style="margin-top:24px;font-size:22px">Hi {lead.first_name or 'there'}!</h2>
  <p style="color:#4a5568;line-height:1.7">
    Your <strong>{vertical_label}</strong> project with
    <strong>{contractor_label}</strong> has been marked as complete.
    We'd love to hear how it went — it only takes 30 seconds.
  </p>

  <div style="background:#f7f9fc;border-radius:12px;padding:24px;margin:24px 0;text-align:center">
    <p style="font-weight:700;margin-bottom:16px;font-size:15px">How would you rate your experience?</p>
    <div style="display:inline-flex;gap:8px">
      {''.join(f'''<a href="{survey_url}?r={i}" style="display:inline-block;width:48px;height:48px;line-height:48px;
        border-radius:50%;background:{'#C8922A' if i>=4 else '#64748b'};color:#fff;
        text-decoration:none;font-size:20px;font-weight:700;text-align:center">{i}</a>''' for i in range(1,6))}
    </div>
    <p style="font-size:12px;color:#8a9aaa;margin-top:8px">1 = Poor · 5 = Excellent</p>
  </div>

  <p style="color:#8a9aaa;font-size:12px;margin-top:24px">
    NexaBuilder · CSLB #1127866 · Southern California<br>
    <a href="{survey_url}?opt_out=1" style="color:#8a9aaa">Unsubscribe</a>
  </p>
</div>
"""}}
            }
        )
        email_sent = True
    except Exception as e:
        print(f"WARNING: survey email failed {lead.email}: {e}")

    # 5. Update survey_sent_at + lead.review_requested_at
    if email_sent:
        db.execute(sqlt("""
            UPDATE review_requests SET survey_sent_at=NOW(), status='survey_sent'
            WHERE id=:id
        """), {"id": rr_id})
        db.execute(sqlt(
            "UPDATE leads SET review_requested_at=NOW() WHERE id=:id"
        ), {"id": payload.lead_id})

    db.commit()

    return {
        "success":      True,
        "review_request_id": rr_id,
        "survey_url":   survey_url,
        "email_sent":   email_sent,
        "homeowner":    lead.email,
        "step":         1,
        "message":      f"Job marked complete. Survey {'emailed' if email_sent else 'created (email failed)'} to {lead.email}",
    }


# ── GET /api/crm/survey/{token} — Render survey page ──────────────────────────

@router.get("/survey/{token}", response_class=HTMLResponse)
def survey_page(token: str, r: Optional[int] = None, opt_out: Optional[int] = None):
    """Render the survey page — homeowner clicks a star rating from email."""
    db = _db()
    rr = db.execute(sqlt("""
        SELECT id, homeowner_name, contractor_company, vertical,
               survey_completed_at, opted_out_at, status
        FROM review_requests WHERE token=:t
    """), {"t": token}).fetchone()

    if not rr:
        return HTMLResponse("<h2>Link not found or expired.</h2>", status_code=404)

    # Handle opt-out
    if opt_out:
        db.execute(sqlt(
            "UPDATE review_requests SET opted_out_at=NOW(), status='opted_out' WHERE token=:t"
        ), {"t": token})
        db.commit()
        return HTMLResponse(_survey_html(token, None, opted_out=True))

    # Pre-fill rating from email click
    return HTMLResponse(_survey_html(token, r,
        name=rr.homeowner_name,
        company=rr.contractor_company,
        vertical=rr.vertical,
        already_done=rr.survey_completed_at is not None))


# ── POST /api/crm/survey/submit ─────────────────────────────────────────────

@router.post("/survey/submit")
def submit_survey(payload: SurveyResponse):
    """Record the homeowner's rating and trigger Google review if >= 4 stars."""
    db = _db()
    rr = db.execute(sqlt("""
        SELECT id, contractor_id, homeowner_email, contractor_company,
               google_place_id, survey_completed_at, vertical, lead_id
        FROM review_requests WHERE token=:t
    """), {"t": payload.token}).fetchone()

    if not rr:
        raise HTTPException(404, "Survey token not found")
    if rr.survey_completed_at:
        return {"success": True, "already_submitted": True}
    if not 1 <= payload.rating <= 5:
        raise HTTPException(422, "Rating must be 1-5")

    # Record survey response
    db.execute(sqlt("""
        UPDATE review_requests
        SET survey_rating=:r, survey_comment=:c,
            survey_completed_at=NOW(), status='completed'
        WHERE token=:t
    """), {"r": payload.rating, "c": payload.comment, "t": payload.token})

    # Update contractor_ratings aggregate
    db.execute(sqlt("""
        INSERT INTO contractor_ratings
          (contractor_id, total_reviews, avg_rating,
           five_star, four_star, three_star, two_star, one_star, last_review_at)
        VALUES (:cid, 1, :r, :s5, :s4, :s3, :s2, :s1, NOW())
        ON CONFLICT (contractor_id) DO UPDATE SET
          total_reviews = contractor_ratings.total_reviews + 1,
          avg_rating    = ROUND(
            (contractor_ratings.avg_rating * contractor_ratings.total_reviews + :r)
            / (contractor_ratings.total_reviews + 1), 2),
          five_star  = contractor_ratings.five_star  + :s5,
          four_star  = contractor_ratings.four_star  + :s4,
          three_star = contractor_ratings.three_star + :s3,
          two_star   = contractor_ratings.two_star   + :s2,
          one_star   = contractor_ratings.one_star   + :s1,
          last_review_at = NOW(),
          updated_at     = NOW()
    """), {
        "cid": rr.contractor_id, "r": payload.rating,
        "s5": 1 if payload.rating==5 else 0,
        "s4": 1 if payload.rating==4 else 0,
        "s3": 1 if payload.rating==3 else 0,
        "s2": 1 if payload.rating==2 else 0,
        "s1": 1 if payload.rating==1 else 0,
    })

    # Mark lead review as complete
    db.execute(sqlt(
        "UPDATE leads SET review_completed=TRUE WHERE id=:id"
    ), {"id": rr.lead_id})

    google_url = None
    google_sent = False

    # Step 2: If rating >= 4, send Google review request
    if payload.rating >= 4 and rr.google_place_id:
        google_url = f"https://search.google.com/local/writereview?placeid={rr.google_place_id}"
        vertical_label = (rr.vertical or "home improvement").replace("-"," ").title()
        try:
            _ses().send_email(
                Source="NexaBuilder <noreply@nexabuilder.com>",
                Destination={"ToAddresses": [rr.homeowner_email]},
                Message={
                    "Subject": {"Data": f"Share your experience with {rr.contractor_company or 'your contractor'} on Google"},
                    "Body": {"Html": {"Data": f"""
<div style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#0d1e35">
  <img src="https://www.nexabuilder.com/images/NexaBuilder_logo.png" height="36" alt="NexaBuilder"/>
  <h2 style="margin-top:24px">Thank you for your {payload.rating}&#11088; rating!</h2>
  <p style="color:#4a5568;line-height:1.7">
    We're glad your {vertical_label} project went well.
    Would you be willing to share your experience on Google?
    It helps other Southern California homeowners find great contractors like
    <strong>{rr.contractor_company or 'your contractor'}</strong>.
  </p>
  <div style="text-align:center;margin:28px 0">
    <a href="{google_url}" style="display:inline-block;padding:14px 32px;
      background:#4285F4;color:#fff;text-decoration:none;border-radius:8px;
      font-weight:700;font-size:15px">
      &#11088; Write a Google Review
    </a>
  </div>
  <p style="color:#8a9aaa;font-size:12px">
    NexaBuilder · CSLB #1127866<br>
    This is a one-time message. No further emails will be sent.
  </p>
</div>
"""}}
                }
            )
            google_sent = True
            db.execute(sqlt(
                "UPDATE review_requests SET google_request_sent_at=NOW(), step=2 WHERE token=:t"
            ), {"t": payload.token})
        except Exception as e:
            print(f"WARNING: Google review email failed: {e}")

    db.commit()
    return {
        "success":      True,
        "rating":       payload.rating,
        "google_sent":  google_sent,
        "google_url":   google_url,
        "message":      "Thank you for your feedback!" + (
            " We've sent you a link to leave a Google review." if google_sent else ""),
    }


# ── GET /api/crm/reviews — Admin dashboard data ────────────────────────────────

@router.get("/reviews")
def get_reviews(x_admin_key: str = Header(...)):
    """Returns review summary for admin dashboard."""
    _adm(x_admin_key)
    db = _db()

    summary = db.execute(sqlt("""
        SELECT
          COUNT(*) FILTER (WHERE status='survey_sent')   AS pending_surveys,
          COUNT(*) FILTER (WHERE status='completed')     AS completed,
          COUNT(*) FILTER (WHERE status='opted_out')     AS opted_out,
          ROUND(AVG(survey_rating) FILTER (WHERE survey_rating IS NOT NULL), 2) AS avg_rating,
          COUNT(*) FILTER (WHERE survey_rating >= 4)     AS positive,
          COUNT(*) FILTER (WHERE google_request_sent_at IS NOT NULL) AS google_requests_sent
        FROM review_requests
    """)).fetchone()

    recent = db.execute(sqlt("""
        SELECT rr.id, rr.homeowner_name, rr.contractor_company,
               rr.vertical, rr.survey_rating, rr.survey_comment,
               rr.status, rr.created_at, rr.job_amount
        FROM review_requests rr
        ORDER BY rr.created_at DESC LIMIT 20
    """)).fetchall()

    contractors = db.execute(sqlt("""
        SELECT cr.contractor_id, ca.company_name,
               cr.total_reviews, cr.avg_rating,
               cr.five_star, cr.four_star
        FROM contractor_ratings cr
        LEFT JOIN contractor_accounts ca ON ca.id=cr.contractor_id
        ORDER BY cr.avg_rating DESC, cr.total_reviews DESC
        LIMIT 10
    """)).fetchall()

    return {
        "summary": dict(summary._mapping) if summary else {},
        "recent_reviews": [dict(r._mapping) for r in recent],
        "contractor_rankings": [dict(r._mapping) for r in contractors],
    }


# ── GET /api/crm/lead/{lead_id}/status ────────────────────────────────────────

@router.get("/lead/{lead_id}/status")
def lead_crm_status(lead_id: int, x_admin_key: str = Header(...)):
    """CRM status for a specific lead."""
    _adm(x_admin_key)
    db = _db()
    row = db.execute(sqlt("""
        SELECT l.lead_status, l.job_completed_at, l.job_amount,
               l.review_requested_at, l.review_completed,
               rr.survey_rating, rr.survey_comment, rr.status AS review_status,
               rr.google_request_sent_at, rr.survey_url
        FROM leads l
        LEFT JOIN review_requests rr ON rr.lead_id=l.id
        WHERE l.id=:id
        ORDER BY rr.created_at DESC LIMIT 1
    """), {"id": lead_id}).fetchone()
    if not row: raise HTTPException(404, "Lead not found")
    return dict(row._mapping)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _survey_html(token, prefill_rating=None, name="", company="",
                 vertical="", already_done=False, opted_out=False):
    if opted_out:
        return """<div style="font-family:sans-serif;text-align:center;padding:48px">
            <h2>You've been unsubscribed.</h2>
            <p>You won't receive any more review requests from NexaBuilder.</p></div>"""
    if already_done:
        return """<div style="font-family:sans-serif;text-align:center;padding:48px">
            <h2>&#10003; Already submitted</h2>
            <p>Thank you — we already have your feedback!</p></div>"""

    vertical_label = (vertical or "home improvement").replace("-"," ").title()
    stars_js = f"document.querySelector('[data-r=\"{prefill_rating}\"]')?.click();" if prefill_rating else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Rate your experience — NexaBuilder</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#f7f9fc;margin:0;display:flex;
       align-items:center;justify-content:center;min-height:100vh}}
  .card{{background:#fff;border-radius:16px;padding:40px 32px;max-width:480px;
         width:100%;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}}
  img{{height:32px;margin-bottom:24px}}
  h2{{color:#0d1e35;font-size:22px;margin-bottom:8px}}
  p{{color:#4a5568;line-height:1.6;margin-bottom:24px}}
  .stars{{display:flex;justify-content:center;gap:10px;margin:20px 0}}
  .star{{width:52px;height:52px;border-radius:50%;border:2px solid #dde3ea;
         background:#fff;font-size:22px;cursor:pointer;display:flex;
         align-items:center;justify-content:center;transition:all .15s;font-weight:700;color:#64748b}}
  .star.active,.star:hover{{background:#C8922A;border-color:#C8922A;color:#fff;transform:scale(1.1)}}
  textarea{{width:100%;border:1.5px solid #dde3ea;border-radius:10px;padding:12px;
            font-size:14px;font-family:inherit;resize:vertical;min-height:80px;
            box-sizing:border-box;margin:12px 0}}
  textarea:focus{{outline:none;border-color:#C8922A}}
  button{{width:100%;padding:14px;background:#C8922A;color:#fff;border:none;
          border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;
          font-family:inherit;margin-top:8px}}
  button:disabled{{opacity:.5;cursor:not-allowed}}
  .msg{{margin-top:20px;padding:14px;border-radius:10px;font-weight:600;display:none}}
  .ok{{background:#f0fdf4;color:#16a34a;border:1px solid #86efac}}
  .label{{font-size:12px;color:#8a9aaa;margin-top:6px}}
</style>
</head>
<body>
<div class="card">
  <img src="https://www.nexabuilder.com/images/NexaBuilder_logo.png" alt="NexaBuilder"/>
  <h2>How was your {vertical_label} project?</h2>
  <p>{'With ' + company if company else ''} Rate your experience below.</p>
  <div class="stars">
    {''.join(f'<button class="star" data-r="{i}" onclick="pick({i})">{i}</button>' for i in range(1,6))}
  </div>
  <div class="label" id="rLabel">Tap a number to rate</div>
  <textarea id="comment" placeholder="Tell us more (optional)..." style="display:none"></textarea>
  <button id="subBtn" onclick="submit()" disabled>Submit Review</button>
  <div class="msg ok" id="okMsg">Thank you! Your feedback means a lot. &#127881;</div>
</div>
<script>
var selected = 0;
var labels = ['','Poor','Below Average','Average','Good','Excellent'];
function pick(n){{
  selected=n;
  document.querySelectorAll('.star').forEach(function(s){{
    s.classList.toggle('active', parseInt(s.dataset.r)===n);
  }});
  document.getElementById('rLabel').textContent = labels[n] || '';
  document.getElementById('comment').style.display='block';
  document.getElementById('subBtn').disabled=false;
}}
async function submit(){{
  if(!selected) return;
  document.getElementById('subBtn').disabled=true;
  document.getElementById('subBtn').textContent='Submitting...';
  var r = await fetch('/api/crm/survey/submit',{{
    method:'POST', headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{
      token:'{token}', rating:selected,
      comment: document.getElementById('comment').value||null
    }})
  }});
  var d = await r.json();
  document.getElementById('okMsg').style.display='block';
  document.getElementById('subBtn').style.display='none';
  if(d.google_url){{
    var btn=document.createElement('a');
    btn.href=d.google_url; btn.target='_blank';
    btn.style.cssText='display:block;margin-top:16px;padding:13px;background:#4285F4;color:#fff;border-radius:10px;text-decoration:none;font-weight:700;font-size:14px;text-align:center';
    btn.textContent='⭐ Leave us a Google Review';
    document.querySelector('.card').appendChild(btn);
  }}
}}
window.onload=function(){{ {stars_js} }};
</script>
</body>
</html>"""

@router.get("/leads/pipeline")
def leads_pipeline(x_admin_key: str = Header(...)):
    _adm(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            'SELECT l.id, l.first_name, l.last_name, l.email, l.phone, '
            'l.vertical, l.lead_status, l.ai_score, l.source_domain, '
            'l.city, l.state, l.estimated_cost_low, l.estimated_cost_high, '
            'l.project_type, l.assigned_contractor_id, l.created_at, '
            'l.job_completed_at, l.job_amount, l.needs_financing, '
            'ca.company_name AS contractor_company '
            'FROM leads l '
            'LEFT JOIN contractor_accounts ca ON ca.id = l.assigned_contractor_id::integer '
            'ORDER BY l.created_at DESC LIMIT 200'
        )).fetchall()
        status_order = ['submitted','matched','review','contacted','bid_sent','accepted','in_progress','completed','cancelled']
        pipeline = {s: [] for s in status_order}
        for r in rows:
            d = dict(r._mapping)
            d['created_at'] = d['created_at'].isoformat() if d['created_at'] else None
            d['job_completed_at'] = d['job_completed_at'].isoformat() if d['job_completed_at'] else None
            s = d.get('lead_status') or 'submitted'
            pipeline.setdefault(s, []).append(d)
        return {'pipeline': pipeline, 'total': len(rows), 'status_order': status_order}
    finally:
        db.close()


@router.patch("/leads/{lead_id}/status")
def update_lead_status(lead_id: int, payload: dict, x_admin_key: str = Header(...)):
    _adm(x_admin_key)
    db = _db()
    try:
        new_status = payload.get('status')
        if not new_status: raise HTTPException(400, 'status required')
        db.execute(sqlt('UPDATE leads SET lead_status=:s, status_updated_at=NOW(), internal_notes=COALESCE(:notes,internal_notes) WHERE id=:id'),
                   {'s': new_status, 'notes': payload.get('notes'), 'id': lead_id})
        db.commit()
        return {'success': True, 'lead_id': lead_id, 'status': new_status}
    finally:
        db.close()


@router.patch("/leads/{lead_id}/financing")
def toggle_financing(lead_id: int, payload: dict, x_admin_key: str = Header(...)):
    """Toggle needs_financing and update financing fields on a lead."""
    _adm(x_admin_key)
    db = _db()
    try:
        needs = payload.get("needs_financing")
        status = payload.get("financing_status")
        amount = payload.get("financing_amount")
        term = payload.get("financing_term_months")
        lender = payload.get("lender_ref")
        sets = []
        params = {"id": lead_id}
        if needs is not None:
            sets.append("needs_financing=:needs")
            params["needs"] = needs
        if status is not None:
            sets.append("financing_status=:status")
            params["status"] = status
        if amount is not None:
            sets.append("financing_amount=:amount")
            params["amount"] = amount
        if term is not None:
            sets.append("financing_term_months=:term")
            params["term"] = term
        if lender is not None:
            sets.append("lender_ref=:lender")
            params["lender"] = lender
        if not sets:
            raise HTTPException(400, "No fields to update")
        db.execute(sqlt(
            f"UPDATE leads SET {','.join(sets)}, status_updated_at=NOW() WHERE id=:id"
        ), params)
        db.commit()
        row = db.execute(sqlt(
            "SELECT needs_financing, financing_status, financing_amount, "
            "financing_term_months, lender_ref FROM leads WHERE id=:id"
        ), {"id": lead_id}).fetchone()
        return {"success": True, "lead_id": lead_id,
                "needs_financing": row.needs_financing,
                "financing_status": row.financing_status,
                "financing_amount": float(row.financing_amount) if row.financing_amount else None,
                "lender_ref": row.lender_ref}
    finally:
        db.close()

@router.get("/milestones")
def get_milestones(x_admin_key: str = Header(...), status: str = None, disputed_only: bool = False):
    _adm(x_admin_key)
    db = _db()
    try:
        where = 'WHERE 1=1'
        params = {}
        if status: where += ' AND pm.status=:status'; params['status'] = status
        if disputed_only: where += " AND (pm.status='disputed' OR pm.dispute_reason IS NOT NULL)"
        rows = db.execute(sqlt(
            'SELECT pm.id, pm.lead_id, pm.milestone_number, pm.title, pm.description, '
            'pm.phase_amount, pm.nexabuilder_fee, pm.status, pm.dispute_reason, '
            'pm.contractor_confirmed_at, pm.homeowner_confirmed_at, '
            'pm.contractor_notes, pm.homeowner_notes, pm.created_at, pm.updated_at, '
            'l.first_name, l.last_name, l.vertical, l.city, '
            'ca.company_name AS contractor_company '
            'FROM project_milestones pm '
            'JOIN leads l ON l.id=pm.lead_id '
            'LEFT JOIN contractor_accounts ca ON ca.id=l.assigned_contractor_id::integer '
            + where + ' ORDER BY pm.created_at DESC LIMIT 100'
        ), params).fetchall()
        result = []
        for r in rows:
            d = dict(r._mapping)
            for k in ['contractor_confirmed_at','homeowner_confirmed_at','created_at','updated_at']:
                if d.get(k): d[k] = d[k].isoformat()
            result.append(d)
        return {'milestones': result, 'total': len(result)}
    finally:
        db.close()


@router.post("/milestones/{milestone_id}/dispute")
def file_dispute(milestone_id: int, payload: dict, x_admin_key: str = Header(...)):
    _adm(x_admin_key)
    db = _db()
    try:
        reason = payload.get('reason', '')
        if not reason: raise HTTPException(400, 'reason required')
        row = db.execute(sqlt("UPDATE project_milestones SET status='disputed', dispute_reason=:r, updated_at=NOW() WHERE id=:id RETURNING id, lead_id, title, phase_amount"), {'r': reason, 'id': milestone_id}).fetchone()
        if not row: raise HTTPException(404, 'Milestone not found')
        db.execute(sqlt('INSERT INTO escrow_transactions (lead_id, milestone_id, transaction_type, amount, status, notes) VALUES (:lid,:mid,:ttype,:amt,:st,:note)'), {'lid': row.lead_id, 'mid': milestone_id, 'ttype': 'hold', 'amt': row.phase_amount, 'st': 'frozen', 'note': 'Automated hold: milestone dispute filed'})
        db.commit()
        return {'success': True, 'milestone_id': milestone_id, 'status': 'disputed', 'escrow': 'frozen', 'title': row.title, 'amount': float(row.phase_amount)}
    finally:
        db.close()


@router.patch("/milestones/{milestone_id}/resolve")
def resolve_dispute(milestone_id: int, payload: dict, x_admin_key: str = Header(...)):
    _adm(x_admin_key)
    db = _db()
    try:
        resolution = payload.get('resolution', 'approved'); notes = payload.get('notes', '')
        new_status = 'completed' if resolution == 'approved' else 'rejected'
        db.execute(sqlt('UPDATE project_milestones SET status=:s, dispute_reason=NULL, homeowner_notes=:n, updated_at=NOW() WHERE id=:id'), {'s': new_status, 'n': notes, 'id': milestone_id})
        db.execute(sqlt('UPDATE escrow_transactions SET status=:s, updated_at=NOW() WHERE milestone_id=:mid AND status=:frozen_val'), {'s': 'released' if resolution == 'approved' else 'forfeited', 'mid': milestone_id})
        db.commit()
        return {'success': True, 'milestone_id': milestone_id, 'resolution': resolution, 'status': new_status}
    finally:
        db.close()


@router.get("/escrow")
def get_escrow(x_admin_key: str = Header(...)):
    _adm(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt('SELECT et.id, et.lead_id, et.milestone_id, et.transaction_type, et.amount, et.fee_amount, et.status, et.escrow_ref, et.notes, et.created_at, et.funded_at, et.disbursed_at, l.first_name, l.last_name, l.vertical FROM escrow_transactions et LEFT JOIN leads l ON l.id=et.lead_id ORDER BY et.created_at DESC LIMIT 100')).fetchall()
        summary = db.execute(sqlt('SELECT status, COUNT(*) cnt, COALESCE(SUM(amount),0) total FROM escrow_transactions GROUP BY status')).fetchall()
        result = []
        for r in rows:
            d = dict(r._mapping)
            for k in ['created_at','funded_at','disbursed_at']:
                if d.get(k): d[k] = d[k].isoformat()
            result.append(d)
        return {'transactions': result, 'total': len(result), 'summary': {r.status: {'count': r.cnt, 'total': float(r.total)} for r in summary}}
    finally:
        db.close()
