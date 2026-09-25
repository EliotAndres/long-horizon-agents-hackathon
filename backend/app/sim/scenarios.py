"""The demo world: SF Bay Area -> LAX on Monday 2026-09-28.

Before the fare sale, every flight violates at least one hard constraint, so nothing qualifies.
Price noise is bounded (``band``) so that thousands of background changes never cross the budget line.
"""

TRAVEL_DATE = "2026-09-28"
SIM_START = "2026-09-25T12:00:00+00:00"
TIMELAPSE_HOURS = 48

DEMO_REQUEST = (
    "I need to fly from San Francisco to Los Angeles on Monday, September 28. "
    "Arrive before 9 AM. Do not depart before 5 AM. Prefer nonstop, ideally from SFO; Oakland is fine. "
    "Flight budget $180. Book automatically if the hard constraints are satisfied."
)

# flight_id, carrier, origin, departure, arrival, price, stops, available, (band_lo, band_hi)
FLIGHTS = [
    ("UA456", "United", "SFO", "06:20", "08:12", 211, 0, True, (204, 226)),
    ("AA123", "American", "OAK", "06:40", "08:05", 195, 0, True, (188, 209)),
    ("WN1402", "Southwest", "OAK", "07:10", "08:25", 198, 0, True, (191, 214)),
    ("UA1190", "United", "SFO", "05:45", "08:50", 187, 1, True, (183, 199)),
    ("B61712", "JetBlue", "SFO", "07:30", "08:58", 172, 0, False, (166, 178)),
    ("DL880", "Delta", "SFO", "04:30", "06:05", 139, 0, True, (129, 149)),
    ("AS331", "Alaska", "SFO", "09:15", "10:40", 129, 0, True, (119, 139)),
]

# External world event #1: an airline fare sale. The headline move is UA456 $211 -> $169.
FARE_SALE = {
    "UA456": {"price": 169, "band": (167, 172)},
    "WN1402": {"price": 176, "band": (174, 179)},
}

# External world event #2: the airline retimes the booked flight: 08:12 -> 10:05 (+113 min).
SCHEDULE_SLIP_MIN = 113
