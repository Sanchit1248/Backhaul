"""Fixed table of Indian city coordinates (approximate city centres, WGS84) for the map.

Display only: never used as a fact in an email or answer. Add a city here when a
kickoff route or client sits somewhere not listed; unknown cities are listed under
the map instead of being placed.
"""
from __future__ import annotations

CITIES: dict[str, tuple[float, float]] = {
    "mumbai": (19.076, 72.878), "pune": (18.520, 73.857), "nashik": (19.998, 73.790),
    "nagpur": (21.146, 79.088), "aurangabad": (19.876, 75.343), "kolhapur": (16.705, 74.243),
    "solapur": (17.660, 75.906), "thane": (19.218, 72.978), "navi mumbai": (19.033, 73.030),
    "bhiwandi": (19.296, 73.063), "chakan": (18.760, 73.863), "talegaon": (18.735, 73.676),
    "ranjangaon": (18.770, 74.248), "panvel": (18.989, 73.117), "ahmedabad": (23.023, 72.571),
    "surat": (21.170, 72.831), "vadodara": (22.307, 73.181), "delhi": (28.614, 77.209),
    "new delhi": (28.614, 77.209), "gurugram": (28.459, 77.027), "noida": (28.535, 77.391),
    "jaipur": (26.912, 75.787), "indore": (22.720, 75.858), "bhopal": (23.260, 77.413),
    "bengaluru": (12.972, 77.595), "bangalore": (12.972, 77.595), "chennai": (13.083, 80.271),
    "hyderabad": (17.385, 78.487), "kolkata": (22.573, 88.364), "lucknow": (26.847, 80.947),
    "kanpur": (26.450, 80.332), "goa": (15.300, 74.124), "coimbatore": (11.017, 76.956),
    "visakhapatnam": (17.687, 83.219), "ludhiana": (30.901, 75.857), "chandigarh": (30.733, 76.779),
    "hosur": (12.740, 77.825), "sinnar": (19.845, 74.000), "vapi": (20.372, 72.905),
}


def coords(city: str | None) -> tuple[float, float] | None:
    return CITIES.get((city or "").strip().lower())
