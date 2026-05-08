# NexaBuilder

Multi-tenant SaaS platform for AI-powered lead management and contractor matching.

## Live Portals

| Portal | URL | Purpose |
|---|---|---|
| Admin | admin.nexabuilder.com | Lead management, metrics |
| Contractor | contractor.nexabuilder.com | Assigned leads |
| Call Center | call.nexabuilder.com | Lead creation |
| Partner | partners.nexabuilder.com | Partner metrics |
| Member | member.nexabuilder.com | Project intake + status |

## Stack

- Backend: FastAPI + SQLAlchemy async + PostgreSQL (AWS RDS)
- Frontend: React + TypeScript + Vite (per-portal S3 + CloudFront)
- Auth: JWT (portals) + Magic Link / SMS token (member portal)
- Email: AWS SES (nexabuilder.com verified)
- SMS: AWS SNS (dev) → Twilio (production)
- CI/CD: GitHub Actions → SSM → EC2

See full documentation in docs/ARCHITECTURE.md
