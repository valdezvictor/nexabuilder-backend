# NexaBuilder — Full Architecture & Roadmap

> Connecting homeowners, contractors, financial services, and legal providers through AI-powered lead management, smart matching, and drone-assisted project assessment.

## Platform Overview

NexaBuilder is a full-stack SaaS platform managing the complete lifecycle of a home improvement or construction project — from lead capture through contractor matching, AI site assessment, permit generation, and financial/insurance coordination.

**Target Markets:** US residential homeowners with a focus on the Hispanic market via SMS-first, phone-friendly workflows and local TV/radio marketing channels.

---

## Live Portals

| Portal | URL | Users | Purpose |
|---|---|---|---|
| Admin Console | admin.nexabuilder.com | Internal team | Lead management, metrics, routing |
| Contractor Portal | contractor.nexabuilder.com | Licensed contractors | Assigned leads, project status |
| Call Center | call.nexabuilder.com | Agents | Inbound/outbound lead creation |
| Partner Portal | partners.nexabuilder.com | Finance/legal partners | Lead metrics, conversions |
| Member Portal | member.nexabuilder.com | Homeowners/leads | Project intake, status timeline |

---

## Authentication

| Portal | Method | Token |
|---|---|---|
| Admin / Contractor / Call Center / Partner | JWT (email + password) | 8hr access token |
| Member (email lead) | Magic link via AWS SES | 15min magic link → 8hr JWT |
| Member (phone-only lead) | SMS token via AWS SNS | 30-day direct JWT |

### Phone-only lead flow (Hispanic market)
```
Call center agent creates lead (phone only)
→ System generates 30-day access token
→ Token URL sent via SNS SMS
→ Lead clicks link → member.nexabuilder.com/auth/verify?token=...
→ Lands on project status dashboard
```

---

## AWS Infrastructure

| Resource | Value |
|---|---|
| EC2 Instance | i-085556772bfeba172 (port 8000) |
| RDS | nexabuilder-prod-db.cyfiieky5gzb.us-west-1.rds.amazonaws.com |
| ALB | nexabuilder-alb-1099236684.us-west-1.elb.amazonaws.com |
| API CloudFront | E13XGZOR47ZU71 → api.nexabuilder.com |
| Admin CloudFront | E1P759DDN8G6OT → admin.nexabuilder.com |
| Contractor CloudFront | EEKLDCQB5FY6G → contractor.nexabuilder.com |
| Call Center CloudFront | E3N01CYWA0II81 → call.nexabuilder.com |
| Partner CloudFront | EXVFFQBQO2B87 → partners.nexabuilder.com |
| Member CloudFront | E1G1JNEJ8RRKBJ → member.nexabuilder.com |

---

## Lead Intake Flow

```
ENTRY POINTS                    SOURCE TAGS
member.nexabuilder.com    →     web_form
call.nexabuilder.com      →     call_center_inbound / call_center_outbound
TV ad response            →     tv_ad
Radio ad response         →     radio_ad
Referral                  →     referral

         ↓

POST /api/leads/intake
  → Creates Lead record
  → Creates User account (or finds existing)
  → If phone-only: generates 30-day token URL
  → If email: sends magic link (SES)
  → Auto-sends SMS token via SNS (phone-only)

         ↓

member.nexabuilder.com/auth/verify?token=...
  → Validates token
  → Issues JWT
  → Redirects to /dashboard

         ↓

member.nexabuilder.com/dashboard
  → 7-step project status timeline
```

---

## Product Roadmap

### Phase 1 — Foundation (COMPLETE)
- 5 portals live (Admin, Contractor, Call Center, Partner, Member)
- JWT + Magic Link + Phone-only SMS token auth
- Lead intake form (multi-vertical)
- Call center lead creation with source tracking
- Admin lead management table
- AWS SES domain verified (nexabuilder.com)
- AWS SNS SMS service
- CI/CD GitHub Actions → EC2
- RDS PostgreSQL + snapshots

### Phase 2 — Intelligence Layer
- AI Intake Assessment
  - Vertical auto-classification
  - Project complexity scoring
  - Permit requirements by zip/city
  - Financial/insurance alert triggers
- Smart Contractor Matching
  - Match by: vertical + license + proximity + capacity
  - CSLB data: 241k+ California contractors in DB
  - Multi-state contractor data staged
- Vendor Integration Hub
  - Plug-and-play service onboarding (SMS, email, CRM)
  - Credentials in AWS SSM Parameter Store
  - Switch providers without code changes
- Sendy.co Email Marketing
  - Self-hosted on EC2, connected to SES
  - Newsletter campaigns, DTC marketing
  - Hispanic market outreach

### Phase 3 — Drone + AI Blueprint
- Drone Dispatch Workflow
  - Schedule site visit from member portal
  - Video upload to S3
  - AI processes footage
- AI Blueprint Generation
  - Site dimensions extracted from drone footage
  - Structural requirements flagged
  - Blueprint PDF generated
  - Permit application pre-filled
- AI Project Advisor
  - Pool location → retaining wall requirements
  - Addition → permit type determination
  - Cross-sell: pool → financing, addition → insurance

### Phase 4 — Financial & Legal Verticals
- Personal loan + home equity matching
- Insurance quote triggers
- Mortgage pre-qualification
- Permit assistance legal services
- Quote comparison
- Document upload (S3 signed URLs)
- Appointment scheduling

### Phase 5 — Scale
- Twilio migration for production SMS
- Multi-state expansion
- Spanish-language portal
- Mobile app (React Native)

---

## CSLB Contractor Data

241,000+ California licensed contractors imported from cslb.ca.gov.
Fields: License #, Business Name, License Type, Classifications, Status, Address, Phone, Expiration.
CSV backup maintained. Additional states scraped and staged.

---

## Tenants

| Domain | Role |
|---|---|
| admin.nexabuilder.com | admin |
| contractor.nexabuilder.com | contractor |
| call.nexabuilder.com | agent |
| partners.nexabuilder.com | partner |
| member.nexabuilder.com | lead |

---

## Architecture Decisions

| Decision | Choice | Reason |
|---|---|---|
| Auth | JWT (stateless) | Works across CloudFront CDN |
| SMS dev | AWS SNS | Same IAM, free tier |
| SMS prod | Twilio | Delivery receipts, two-way, scale |
| Email | AWS SES + Sendy | Cost-effective + campaigns |
| Frontend | React + Vite + S3 | Fast deploys, portal isolation |
| DB | PostgreSQL async | asyncpg for high throughput |
| CI/CD | GitHub Actions + SSM | No SSH keys needed |

