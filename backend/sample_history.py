"""Adds past (approved) bills so anomaly detection has history to learn from on day one.

Nothing here knows about limits. The history only teaches what is *normal*:
every employee, for every bill type, has 11 past months of 4 bills each, centred on about
Rs 3,333 per bill, i.e. about Rs 13,333 per employee per type per month. The detector learns
from that that anything at least 1.5x the usual amount is unusual (its own MIN_RATIO rule), which
works out at roughly Rs 5,000 for a single bill and roughly Rs 20,000 for a month's total.
Change TYPICAL_BILL or BILLS_PER_MONTH below to teach it different limits.

The 4 bills of a month are two pairs spread symmetrically around TYPICAL_BILL, so months look
natural but the learned "usual" values stay stable. Real approved bills are added to this history over time.

Sample bills have is_sample = 1. They're hidden from both portals and only used as history.
Set SAMPLE_HISTORY=0 in backend/.env and restart to remove them.
"""
import calendar
import random
from datetime import date, datetime, timedelta

MONTHS = 11
BILLS_PER_MONTH = 4  # must stay even: bills are generated in symmetric pairs
TYPICAL_BILL = 3333.34
PAIR_SPREAD = ((0.01, 0.05), (0.02, 0.06))  # each pair deviates this fraction below/above the typical bill
BILL_TYPES = ("Fuel", "Repair", "Maintenance", "Other")


def _past_months(today):
    y, m = today.year, today.month
    for _ in range(MONTHS):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        yield y, m


def seed(conn):
    employees = [r["id"] for r in conn.execute("SELECT id FROM users WHERE role = 'employee' ORDER BY id")]
    hr_id = conn.execute("SELECT id FROM users WHERE role = 'hr' ORDER BY id LIMIT 1").fetchone()["id"]
    rng = random.Random(42)
    today = date.today()
    serial = 0
    rows = []
    for employee_id in employees:
        for bill_type in BILL_TYPES:
            for y, m in _past_months(today):
                days = rng.sample(range(1, calendar.monthrange(y, m)[1] + 1), BILLS_PER_MONTH)
                amounts = []
                for low, high in PAIR_SPREAD[: BILLS_PER_MONTH // 2]:
                    spread = rng.uniform(low, high)
                    amounts += [TYPICAL_BILL * (1 + spread), TYPICAL_BILL * (1 - spread)]
                rng.shuffle(amounts)
                for day, amount in zip(sorted(days), amounts):
                    serial += 1
                    bill_day = date(y, m, day)
                    sent = datetime.combine(bill_day + timedelta(days=rng.randint(0, 2)), datetime.min.time()) \
                        + timedelta(hours=rng.randint(9, 18))
                    decided = sent + timedelta(days=rng.randint(1, 3))
                    rows.append((employee_id, f"SMP-{serial:05d}", bill_day.isoformat(), round(amount, 2), bill_type,
                                 sent.isoformat(), sent.isoformat(), decided.isoformat(), hr_id))
    conn.executemany(
        "INSERT INTO bills (employee_id, file_name, original_name, bill_number, bill_date, amount, bill_type,"
        " extraction_source, status, uploaded_at, submitted_at, decided_at, decided_by, decision_mode, is_sample)"
        " VALUES (?, '', 'Sample history', ?, ?, ?, ?, 'sample', 'approved', ?, ?, ?, ?, 'manual', 1)",
        rows)
    return len(rows)


def remove(conn):
    return conn.execute("DELETE FROM bills WHERE is_sample = 1").rowcount
