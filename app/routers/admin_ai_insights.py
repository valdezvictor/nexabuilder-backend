from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.db import get_db

router = APIRouter(prefix="/admin-console/ai", tags=["Admin Console – AI Insights"])


# ---------------------------------------------------------
# 1. Lead Quality Distribution
# ---------------------------------------------------------
@router.get("/lead-quality-distribution")
def lead_quality_distribution(db: Session = Depends(get_db)):
    logs = db.query(RoutingDecisionLog).filter(RoutingDecisionLog.ai_label != None).all()

    distribution = {}
    vertical_scores = {}

    for log in logs:
        label = log.ai_label
        distribution[label] = distribution.get(label, 0) + 1

        if log.lead_vertical_code:
            vertical_scores.setdefault(log.lead_vertical_code, []).append(log.ai_score)

    vertical_avg = {
        v: sum(scores) / len(scores)
        for v, scores in vertical_scores.items()
    }

    return {
        "distribution": distribution,
        "vertical_avg_scores": vertical_avg,
        "total": len(logs),
    }


# ---------------------------------------------------------
# 2. Routing Accuracy (AI vs Contractor Behavior)
# ---------------------------------------------------------
@router.get("/routing-accuracy")
def routing_accuracy(db: Session = Depends(get_db)):
    offers = db.query(LeadOffer).all()
    logs = db.query(RoutingDecisionLog).filter(RoutingDecisionLog.ai_label != None).all()

    stats = {}

    for log in logs:
        label = log.ai_label
        stats.setdefault(label, {"accepted": 0, "declined": 0, "total": 0})

        offer = next(
            (o for o in offers if o.lead_id == log.lead_id and o.contractor_id == log.contractor_id),
            None
        )
        if not offer:
            continue

        stats[label]["total"] += 1
        if offer.accepted:
            stats[label]["accepted"] += 1
        elif offer.declined:
            stats[label]["declined"] += 1

    # Compute accuracy
    for label, s in stats.items():
        if s["total"] > 0:
            s["acceptance_rate"] = s["accepted"] / s["total"]
            s["decline_rate"] = s["declined"] / s["total"]
        else:
            s["acceptance_rate"] = 0
            s["decline_rate"] = 0

    return stats


# ---------------------------------------------------------
# 3. Contractor AI Insights
# ---------------------------------------------------------
@router.get("/contractor-insights")
def contractor_ai_insights(db: Session = Depends(get_db)):
    contractors = db.query(Contractor).all()
    logs = db.query(RoutingDecisionLog).filter(RoutingDecisionLog.ai_score != None).all()

    insights = {}

    for c in contractors:
        c_logs = [l for l in logs if l.contractor_id == c.id]

        if not c_logs:
            continue

        ai_scores = [l.ai_score for l in c_logs]
        labels = [l.ai_label for l in c_logs]

        insights[c.id] = {
            "contractor_name": c.business_name,
            "avg_ai_score": sum(ai_scores) / len(ai_scores),
            "label_distribution": {
                label: labels.count(label) for label in set(labels)
            },
            "total_routed": len(c_logs),
            "acceptance_rate": c.acceptance_rate,
            "capacity_today": c.leads_today,
            "capacity_week": c.leads_this_week,
        }

    return insights


# ---------------------------------------------------------
# 4. Vertical AI Insights
# ---------------------------------------------------------
@router.get("/vertical-insights")
def vertical_ai_insights(db: Session = Depends(get_db)):
    logs = db.query(RoutingDecisionLog).filter(RoutingDecisionLog.ai_score != None).all()

    verticals = {}

    for log in logs:
        v = log.lead_vertical_code
        if not v:
            continue

        verticals.setdefault(v, {"scores": [], "labels": []})
        verticals[v]["scores"].append(log.ai_score)
        verticals[v]["labels"].append(log.ai_label)

    result = {}
    for v, data in verticals.items():
        result[v] = {
            "avg_ai_score": sum(data["scores"]) / len(data["scores"]),
            "label_distribution": {
                label: data["labels"].count(label)
                for label in set(data["labels"])
            },
            "total": len(data["scores"]),
        }

    return result


# ---------------------------------------------------------
# 5. AI Anomaly Detection
# ---------------------------------------------------------
@router.get("/anomalies")
def ai_anomalies(db: Session = Depends(get_db)):
    logs = db.query(RoutingDecisionLog).filter(RoutingDecisionLog.ai_score != None).all()

    if not logs:
        return {"anomalies": []}

    scores = [l.ai_score for l in logs]
    avg = sum(scores) / len(scores)

    anomalies = []

    # Detect sudden drops in AI score
    recent = scores[-50:] if len(scores) > 50 else scores
    recent_avg = sum(recent) / len(recent)

    if recent_avg < avg * 0.75:
        anomalies.append("Recent AI scores dropped significantly.")

    # Detect contractor capacity issues
    contractors = db.query(Contractor).all()
    for c in contractors:
        if c.daily_capacity and c.leads_today >= c.daily_capacity:
            anomalies.append(f"{c.business_name} is at daily capacity.")

    return {
        "avg_ai_score": avg,
        "recent_avg_ai_score": recent_avg,
        "anomalies": anomalies,
    }
