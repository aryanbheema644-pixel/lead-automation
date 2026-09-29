"""Trim a full Apify LinkedIn scrape to the screening-input schema.

Vendored verbatim (behavior-for-behavior) from the team's canonical mapper at
/Users/aryanbheema/Apify/transform.py (to_pdf_view) so the screening console
receives EXACTLY the JSON it expects, and so it deploys with this app (that file
is not on the server). Pure stdlib, null-tolerant: missing fields become None/[]
rather than raising, and common alternate actor field names are tolerated.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _first_value(profile: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for key in keys:
        value = profile.get(key)
        if value is not None and value != "":
            return value
    return None


def _first_list(profile: Dict[str, Any], keys: List[str]) -> List[Any]:
    for key in keys:
        value = profile.get(key)
        if isinstance(value, list):
            return value
    return []


def _skill_name(skill: Any) -> Optional[str]:
    if isinstance(skill, str):
        return skill
    if isinstance(skill, dict):
        return _first_value(skill, ["name", "skill", "title"])
    return None


def _location_string(location: Any) -> Optional[str]:
    if isinstance(location, str):
        return location
    if isinstance(location, dict):
        text = location.get("linkedinText")
        if text:
            return text
        parsed = location.get("parsed")
        if isinstance(parsed, dict):
            return parsed.get("text")
    return None


def _top_skills(profile: Dict[str, Any]) -> List[str]:
    source = profile.get("topSkills") or profile.get("skills") or []
    names = []
    for entry in source:
        name = _skill_name(entry)
        if name is not None:
            names.append(name)
        if len(names) == 3:
            break
    return names


def _certifications(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for cert in _first_list(profile, ["certifications", "certificates"]):
        title = _first_value(cert, ["title", "name"]) if isinstance(cert, dict) else cert
        out.append({"title": title})
    return out


def _experience(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for item in _first_list(profile, ["experience", "experiences", "positions"]):
        if not isinstance(item, dict):
            continue
        out.append({
            "companyName": _first_value(item, ["companyName", "company"]),
            "position": _first_value(item, ["position", "title"]),
            "startDate": item.get("startDate"),
            "endDate": item.get("endDate"),
            "duration": item.get("duration"),
            "location": item.get("location"),
            "description": item.get("description"),
        })
    return out


def _education(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for item in _first_list(profile, ["education", "educations"]):
        if not isinstance(item, dict):
            continue
        out.append({
            "schoolName": _first_value(item, ["schoolName", "school"]),
            "degree": _first_value(item, ["degree", "degreeName"]),
            "fieldOfStudy": _first_value(item, ["fieldOfStudy", "field"]),
            "period": item.get("period"),
            "startDate": item.get("startDate"),
            "endDate": item.get("endDate"),
        })
    return out


def to_pdf_view(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Trim one scraped profile to the screening-input subset (values verbatim)."""
    if not isinstance(profile, dict):
        raise TypeError("to_pdf_view expects a dict profile")
    return {
        "firstName": profile.get("firstName"),
        "lastName": profile.get("lastName"),
        "headline": profile.get("headline"),
        "location": _location_string(profile.get("location")),
        "linkedinUrl": _first_value(
            profile, ["linkedinUrl", "linkedin_url", "url", "publicProfileUrl"]),
        "about": _first_value(profile, ["about", "summary"]),
        "topSkills": _top_skills(profile),
        "certifications": _certifications(profile),
        "experience": _experience(profile),
        "education": _education(profile),
    }


# Keys that mark a dict as a real scraped profile (vs an actor error/notice item).
_PROFILE_MARKERS = ("firstName", "lastName", "linkedinUrl", "headline",
                    "experience", "education", "publicIdentifier")


def is_actor_error(item: Dict[str, Any]) -> bool:
    """True if a dataset item is an actor error/notice rather than a profile
    (e.g. harvestapi free-plan '{"error": "Free users are limited..."}')."""
    if not isinstance(item, dict) or "error" not in item:
        return False
    return not any(item.get(marker) for marker in _PROFILE_MARKERS)
