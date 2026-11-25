"""AI Lead Qualification and Voice AI Buying Potential Engine for Flow Chain.

Scores small business HVAC and Plumbing leads (0-100) across 7 objective criteria:
1. Small independent business: up to 20 pts
2. High phone-dependence: up to 20 pts
3. Emergency or after-hours service: up to 15 pts
4. Limited visible customer service infrastructure: up to 15 pts
5. Strong public business presence: up to 10 pts
6. Valid business phone number: up to 10 pts
7. Clear owner/operator contact opportunity: up to 10 pts

Evaluates AI Receptionist Opportunity:
- High potential (80-100 score or emergency + small team indicator)
- Medium potential (60-79 score)
- Low potential (40-59 score or very few positive signals)
- Insufficient information (missing critical business data)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


KNOWN_CHAINS_AND_FRANCHISES = re.compile(
    r"\b(roto-?rooter|mr\.?\s*rooter|benjamin\s*franklin|one\s*hour\s*heating|"
    r"aire\s*serv|service\s*experts|rescue\s*rooter|sears|home\s*depot|lowe'?s|"
    r"ferguson|johnstone|winsupply|horizon\s*services|goettl|nexstar|ars\s*rescue)\b",
    re.IGNORECASE,
)

SUPPLY_OR_NON_CONTRACTOR = re.compile(
    r"\b(supply|distributor|wholesale|parts|manufacturing|manufacturer|rental|"
    r"school|college|academy|union|local\s*\d+)\b",
    re.IGNORECASE,
)

EMERGENCY_KEYWORDS = re.compile(
    r"\b(24\s*/?\s*7|24\s*hours?|emergency|same\s*day|urgent|after[- ]hours?|on[- ]call|anytime)\b",
    re.IGNORECASE,
)

HIGH_PHONE_DEPENDENCE_TRADES = {
    "emergency plumber",
    "emergency plumbing",
    "drain cleaning",
    "water heater repair",
    "air conditioning repair",
    "heating repair",
    "hvac repair",
    "hvac contractor",
    "plumber",
    "plumbing contractor",
    "plumbing",
    "hvac",
}


def qualify_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate qualification score (0-100) and AI Receptionist Opportunity."""
    name = (lead.get("name") or "").strip()
    category = (lead.get("category") or "").strip()
    phone = (lead.get("phone") or "").strip()
    website = (lead.get("website") or "").strip()
    notes = (lead.get("notes") or "").strip()
    owner_name = (lead.get("owner_name") or "").strip()
    services = (lead.get("services_offered") or "").strip()
    employee_estimate = (lead.get("employee_estimate") or "").strip()

    try:
        review_count = int(lead.get("review_count") or 0)
    except (ValueError, TypeError):
        review_count = 0

    try:
        google_rating = float(lead.get("google_rating") or 0.0)
    except (ValueError, TypeError):
        google_rating = 0.0

    is_emergency_flag = bool(lead.get("is_emergency"))
    is_after_hours_flag = bool(lead.get("is_after_hours"))

    combined_text = f"{name} {category} {notes} {services}".lower()

    # Disqualification / exclusion check for large chains or distributors
    if KNOWN_CHAINS_AND_FRANCHISES.search(name):
        return {
            "lead_score": 0,
            "score_category": "Do not prioritise",
            "confidence_level": "High",
            "ai_opportunity": "Low potential",
            "ai_reasoning": "National franchise or large chain identified. Typically has enterprise dispatch or centralized call centers.",
            "qualification_details": {
                "criteria": {},
                "disqualified": True,
                "reason": "National franchise or large enterprise chain.",
            },
            "missing_information": [],
            "employee_estimate": employee_estimate or "50+",
            "is_emergency": 0,
            "is_after_hours": 0,
        }

    if SUPPLY_OR_NON_CONTRACTOR.search(name):
        return {
            "lead_score": 0,
            "score_category": "Do not prioritise",
            "confidence_level": "High",
            "ai_opportunity": "Low potential",
            "ai_reasoning": "Supplier, equipment distributor, or trade union rather than an active service contractor.",
            "qualification_details": {
                "criteria": {},
                "disqualified": True,
                "reason": "Equipment distributor, supply store, or non-contractor listing.",
            },
            "missing_information": [],
            "employee_estimate": employee_estimate or "Unknown",
            "is_emergency": 0,
            "is_after_hours": 0,
        }

    criteria: Dict[str, Dict[str, Any]] = {}
    missing_info: List[str] = []
    total_score = 0

    # 1. Small independent business (max 20 pts)
    # Check indicators of 1-10 employees, local contractor, owner-operated
    is_small_indep = False
    small_indep_evidence = []
    small_indep_pts = 0

    if employee_estimate in ("1-5", "1-10", "1-3", "4-7", "under 10"):
        small_indep_pts = 20
        small_indep_evidence.append(f"Employee estimate {employee_estimate} matches ideal target size (1–10).")
        is_small_indep = True
    elif "family" in combined_text or "owner-operated" in combined_text or "independent" in combined_text:
        small_indep_pts = 18
        small_indep_evidence.append("Described as family-owned or independent local contractor.")
        is_small_indep = True
    elif review_count > 0 and review_count <= 400:
        small_indep_pts = 15
        small_indep_evidence.append(f"Review count ({review_count}) is typical of an active local independent shop.")
        is_small_indep = True
    elif review_count > 1500:
        small_indep_pts = 5
        small_indep_evidence.append(f"High review count ({review_count}) suggests a larger regional operation.")
    else:
        small_indep_pts = 10
        small_indep_evidence.append("Local business listing without signs of enterprise scale.")
        missing_info.append("Verified employee count / team size")

    criteria["small_independent_business"] = {
        "awarded": small_indep_pts,
        "max": 20,
        "evidence": "; ".join(small_indep_evidence),
        "status": "inferred" if not employee_estimate else "estimated",
    }
    total_score += small_indep_pts

    # 2. High phone-dependence (max 20 pts)
    # Service trades where inbound callers need immediate quotes or repair dispatch
    phone_dep_pts = 0
    phone_dep_evidence = []
    cat_lower = category.lower()
    matched_trade = next((t for t in HIGH_PHONE_DEPENDENCE_TRADES if t in cat_lower or t in combined_text), None)

    if matched_trade:
        phone_dep_pts = 20
        phone_dep_evidence.append(f"Core industry is '{matched_trade}', heavily reliant on immediate inbound call handling.")
    elif "hvac" in combined_text or "plumb" in combined_text:
        phone_dep_pts = 18
        phone_dep_evidence.append("HVAC/Plumbing contractor service with direct customer call workflows.")
    else:
        phone_dep_pts = 10
        phone_dep_evidence.append("Trade category identified with standard inbound phone dependence.")

    criteria["high_phone_dependence"] = {
        "awarded": phone_dep_pts,
        "max": 20,
        "evidence": "; ".join(phone_dep_evidence),
        "status": "verified" if matched_trade else "inferred",
    }
    total_score += phone_dep_pts

    # 3. Emergency or after-hours service (max 15 pts)
    # Advertises 24/7, emergency plumbing, urgent AC repair, weekend calls
    emergency_pts = 0
    emergency_evidence = []
    has_emergency_keyword = bool(EMERGENCY_KEYWORDS.search(combined_text))

    if is_emergency_flag or is_after_hours_flag or has_emergency_keyword:
        emergency_pts = 15
        emergency_evidence.append("Publicly offers 24/7, emergency, or same-day repair dispatch.")
    elif "repair" in combined_text:
        emergency_pts = 8
        emergency_evidence.append("Offers repair services which often generate unplanned urgent calls.")
        missing_info.append("Explicit 24/7 or after-hours service confirmation")
    else:
        emergency_pts = 4
        emergency_evidence.append("No explicit emergency or after-hours service advertised.")
        missing_info.append("Emergency / after-hours operating hours")

    criteria["emergency_or_after_hours"] = {
        "awarded": emergency_pts,
        "max": 15,
        "evidence": "; ".join(emergency_evidence),
        "status": "verified" if (is_emergency_flag or has_emergency_keyword) else "unknown",
    }
    total_score += emergency_pts

    # 4. Limited visible customer service infrastructure (max 15 pts)
    # Small local operation without multi-tier phone menus or dedicated call centers
    cs_infra_pts = 0
    cs_infra_evidence = []

    if review_count > 0 and review_count < 300:
        cs_infra_pts = 15
        cs_infra_evidence.append("Local single-location profile; likely owner or field staff answering inbound calls.")
    elif review_count <= 800:
        cs_infra_pts = 12
        cs_infra_evidence.append("Moderate review count; likely limited office support juggling multiple responsibilities.")
    elif review_count > 1200:
        cs_infra_pts = 5
        cs_infra_evidence.append("Review volume suggests dedicated office dispatch team or call center.")
    else:
        cs_infra_pts = 10
        cs_infra_evidence.append("No obvious large customer service department detected in public profile.")
        missing_info.append("In-office receptionist / answering service status")

    criteria["limited_cs_infrastructure"] = {
        "awarded": cs_infra_pts,
        "max": 15,
        "evidence": "; ".join(cs_infra_evidence),
        "status": "inferred",
    }
    total_score += cs_infra_pts

    # 5. Strong public business presence (max 10 pts)
    # Website, Google reviews, active business rating
    presence_pts = 0
    presence_evidence = []

    if website and (review_count >= 10 or google_rating >= 4.0):
        presence_pts = 10
        presence_evidence.append(f"Official website present with active Google presence ({google_rating}★ from {review_count} reviews).")
    elif website or (review_count >= 5):
        presence_pts = 7
        presence_evidence.append("Web presence or active review history verified.")
        if not website:
            missing_info.append("Official company website URL")
    else:
        presence_pts = 3
        presence_evidence.append("Minimal public web presence; basic directory listing only.")
        missing_info.append("Official website and verified Google reviews")

    criteria["strong_public_presence"] = {
        "awarded": presence_pts,
        "max": 10,
        "evidence": "; ".join(presence_evidence),
        "status": "verified" if website and review_count > 0 else "partial",
    }
    total_score += presence_pts

    # 6. Valid US business phone number (max 10 pts)
    phone_pts = 0
    phone_evidence = []

    if phone and (phone.startswith("+1") and len(phone) == 12):
        phone_pts = 10
        phone_evidence.append(f"Standard US E.164 phone number ({phone}) verified.")
    elif phone:
        phone_pts = 6
        phone_evidence.append("Phone number provided but requires normalization.")
    else:
        phone_pts = 0
        phone_evidence.append("No valid business phone number found.")
        missing_info.append("Valid US business phone number")

    criteria["valid_business_phone"] = {
        "awarded": phone_pts,
        "max": 10,
        "evidence": "; ".join(phone_evidence),
        "status": "verified" if phone_pts == 10 else "invalid",
    }
    total_score += phone_pts

    # 7. Clear owner/operator contact opportunity (max 10 pts)
    owner_pts = 0
    owner_evidence = []

    if owner_name:
        owner_pts = 10
        owner_evidence.append(f"Public decision-maker or owner identified: {owner_name}.")
    elif "owner" in combined_text or "hart" in combined_text or "family" in combined_text:
        owner_pts = 7
        owner_evidence.append("Owner-operated business model noted in public business records.")
    else:
        owner_pts = 4
        owner_evidence.append("Owner name not listed on top-level listing; direct reachout to general line needed.")
        missing_info.append("Owner or decision-maker contact name")

    criteria["owner_contact_opportunity"] = {
        "awarded": owner_pts,
        "max": 10,
        "evidence": "; ".join(owner_evidence),
        "status": "verified" if owner_name else "unknown",
    }
    total_score += owner_pts

    # Cap total_score at 100
    total_score = max(0, min(100, total_score))

    # Priority category
    if total_score >= 80:
        score_category = "High priority"
    elif total_score >= 60:
        score_category = "Medium priority"
    elif total_score >= 40:
        score_category = "Low priority"
    else:
        score_category = "Do not prioritise"

    # Confidence calculation based on missing data
    if len(missing_info) <= 1 and phone_pts == 10:
        confidence = "High"
    elif len(missing_info) <= 3:
        confidence = "Medium"
    else:
        confidence = "Low"

    # AI Receptionist Opportunity Classification
    # Genuine indicators: emergency availability, after hours, small teams handling multiple jobs
    opportunity_reasons: List[str] = []

    if emergency_pts >= 12 or has_emergency_keyword or is_emergency_flag:
        opportunity_reasons.append(
            "Emergency & After-Hours Call Routing: Inbound emergencies outside 8am-5pm can be answered 24/7 and escalated to on-call technicians."
        )

    if is_small_indep:
        opportunity_reasons.append(
            "Missed-Call Recovery & Hands-Free Capture: Field technicians working on jobs (under sinks or on roofs) risk losing callers to competitors if calls ring out."
        )

    if phone_dep_pts >= 18:
        opportunity_reasons.append(
            "Appointment Scheduling & Lead Qualification: Collects customer address, urgent problem description, and preferred inspection time window."
        )

    if presence_pts >= 7:
        opportunity_reasons.append(
            "Customer Enquiry & FAQ Handling: Handles routine calls regarding service areas, licensing, and diagnostics while filtering solicitations."
        )

    if not opportunity_reasons:
        opportunity_reasons.append(
            "General Call Answering: Standard inbound voice answering during peak volume surges."
        )

    # Opportunity level
    if not phone or total_score < 40:
        ai_opportunity = "Insufficient information" if not phone else "Low potential"
    elif total_score >= 75 and (emergency_pts >= 12 or is_small_indep):
        ai_opportunity = "High potential"
    elif total_score >= 55:
        ai_opportunity = "Medium potential"
    else:
        ai_opportunity = "Low potential"

    if len(missing_info) >= 4 and not website:
        ai_opportunity = "Insufficient information"

    ai_reasoning_text = (
        f"Potential Use Cases (not verified business problems):\n"
        + "\n".join(f"• {r}" for r in opportunity_reasons)
    )

    estimated_emp = employee_estimate or ("1-10 (Estimated)" if is_small_indep else "Unknown (Estimated)")

    return {
        "lead_score": total_score,
        "score_category": score_category,
        "confidence_level": confidence,
        "ai_opportunity": ai_opportunity,
        "ai_reasoning": ai_reasoning_text,
        "qualification_details": {
            "criteria": criteria,
            "total_score": total_score,
            "disqualified": False,
        },
        "missing_information": missing_info,
        "employee_estimate": estimated_emp,
        "is_emergency": 1 if (emergency_pts >= 12 or is_emergency_flag or has_emergency_keyword) else 0,
        "is_after_hours": 1 if (is_after_hours_flag or "after" in combined_text or "24" in combined_text) else 0,
    }
