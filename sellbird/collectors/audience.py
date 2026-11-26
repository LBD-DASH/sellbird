"""Search phrases for the four 6 Human Needs audiences.

The labels (individuals and couples, team leaders, HR, coaches outside
South Africa) are not map categories. Each one is a real web search for a
kind of practice. Coaches are searched outside South Africa. A search that
comes back empty stays empty.
"""

from sellbird.cadence import is_south_africa

# (match text, concrete searches, drop South Africa)
AUDIENCES = (
    ("individuals and couples", ("couples therapist", "relationship counsellor"), False),
    ("team leaders", ("leadership coach", "team development consultant"), False),
    ("human resource", ("HR consultant", "human resources advisor"), False),
    ("hr", ("HR consultant", "human resources advisor"), False),
    ("coaches", ("life coach", "executive coach"), True),
)

COACH_TERMS = ("life coach", "executive coach", "business coach")
_OUTSIDE = ("London", "New York", "Toronto", "Sydney", "Dublin")


def match_audience(industry: str):
    """(search terms, exclude South Africa) or None."""
    low = (industry or "").strip().lower()
    if not low:
        return None
    for key, terms, exclude_sa in AUDIENCES:
        if key == "hr":
            if low == "hr" or low.startswith("hr ") or " hr " in f" {low} ":
                return terms, exclude_sa
            continue
        if key in low:
            if key == "coaches":
                exclude_sa = True
            return terms, exclude_sa
    return None


def is_coach_search(term: str) -> bool:
    low = (term or "").lower()
    return any(piece in low for piece in COACH_TERMS)


def search_phrase(term: str, location: str = "") -> str:
    """A web query. Coach searches add -site:.za. Vague labels become a real phrase."""
    raw = (term or "").strip()
    matched = match_audience(raw)
    if matched:
        raw = matched[0][0]
        exclude_sa = matched[1]
    else:
        exclude_sa = is_coach_search(raw)
    place = (location or "").strip()
    if exclude_sa and is_south_africa(place):
        place = ""
    parts = [f'"{raw}"']
    if place:
        parts.append(f'"{place}"')
    phrase = " ".join(parts)
    if exclude_sa:
        phrase += " -site:.za"
    return phrase


def scout_queries(config) -> list[str]:
    """One search per configured audience, in a place outside South Africa for coaches."""
    industries = list(getattr(getattr(config, "icp", None), "industries", None) or [])
    geos = list(getattr(getattr(config, "icp", None), "geography", None) or [])
    queries = []
    seen = set()
    for industry in industries:
        matched = match_audience(industry)
        if not matched:
            continue
        terms, exclude_sa = matched
        places = [g for g in geos if not (exclude_sa and is_south_africa(g))]
        if exclude_sa and not places:
            places = list(_OUTSIDE[:3])
        place = places[0] if places else ""
        phrase = search_phrase(terms[0], place)
        if phrase not in seen:
            seen.add(phrase)
            queries.append(phrase)
    return queries
