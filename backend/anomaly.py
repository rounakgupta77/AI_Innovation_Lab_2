"""Flags bills whose amount looks unusual, at the moment an employee sends them to HR.

Both detectors learn only from this company's own bill history in the local database (offline):

  1. Statistical rules, using median and MAD (a "robust z-score" that a few odd bills can't skew):
       a. amount vs this employee's past bills of the same type
       b. amount vs all employees' past bills of the same type
       c. this employee's monthly total for the type vs their past monthly totals
  2. Isolation Forest (scikit-learn), retrained on every check once there is enough history.

History = approved bills plus pending bills that weren't flagged; rejected bills are left out.
Only unusually HIGH amounts are flagged.
"""
import logging
import math
from collections import defaultdict
from datetime import date
from statistics import median

log = logging.getLogger("anomaly")

try:
    from sklearn.ensemble import IsolationForest
except ImportError:
    IsolationForest = None

TYPES = ("Fuel", "Repair", "Maintenance", "Other")
MIN_OWN_BILLS = 5        # employee bills of a type needed before comparing with their own history
MIN_COMPANY_BILLS = 8    # bills of a type (all employees) needed for the company-wide comparison
MIN_MONTHS = 3           # past months needed for the monthly-total check
MIN_ML_BILLS = 30        # history size before the Isolation Forest is used
MAD_SCALE = 1.4826       # makes MAD comparable to a standard deviation
Z_LIMIT = 3.5            # robust z-score above which an amount counts as unusual
MIN_RATIO = 1.5          # ...and it must also be at least this many times the median
ML_CONTAMINATION = 0.01  # share of history the forest treats as outliers


def ml_available():
    return IsolationForest is not None


def _rs(value):
    return f"₹{value:,.0f}"


def _month_name(key):
    try:
        return date(int(key[:4]), int(key[5:7]), 1).strftime("%b %Y")
    except ValueError:
        return key


def _med_scale(samples):
    med = median(samples)
    mad = median(abs(s - med) for s in samples)
    return med, max(MAD_SCALE * mad, 0.1 * med, 1.0)


def _robust_high(value, samples):
    """Returns (is_unusually_high, median, ratio_to_median)."""
    med, scale = _med_scale(samples)
    ratio = value / med if med > 0 else math.inf
    return (value - med) / scale > Z_LIMIT and ratio >= MIN_RATIO, med, ratio


def cutoff(samples):
    """The amount above which _robust_high starts flagging, learned from these samples."""
    med, scale = _med_scale(samples)
    return max(med + Z_LIMIT * scale, MIN_RATIO * med)


def load_history(conn, exclude_id=None):
    rows = conn.execute(
        "SELECT id, employee_id, bill_type, bill_date, amount FROM bills"
        " WHERE (status = 'approved' OR (status = 'pending' AND anomaly = 0))"
        " AND amount > 0 AND bill_date != '' AND bill_type != '' AND id != ?",
        (exclude_id if exclude_id is not None else -1,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Isolation Forest
# --------------------------------------------------------------------------- #

def _features(rows):
    """Per bill: log amount, log ratio to the employee's usual amount for the type,
    the employee's running monthly total and bill count for the type (up to and including
    this bill, so a bill is judged the same way when it is new as when it is history),
    and the bill type (one-hot)."""
    by_emp_type = defaultdict(list)
    for r in rows:
        by_emp_type[(r["employee_id"], r["bill_type"])].append(r["amount"])
    medians = {k: median(v) for k, v in by_emp_type.items()}

    running_total = defaultdict(float)
    running_count = defaultdict(int)
    out = [None] * len(rows)
    for i in sorted(range(len(rows)), key=lambda k: (rows[k]["bill_date"], k)):
        r = rows[i]
        key = (r["employee_id"], r["bill_type"])
        mkey = key + (r["bill_date"][:7],)
        running_total[mkey] += r["amount"]
        running_count[mkey] += 1
        out[i] = [
            math.log1p(r["amount"]),
            math.log(r["amount"] / medians[key]),
            math.log1p(running_total[mkey]),
            float(running_count[mkey]),
            *(1.0 if r["bill_type"] == t else 0.0 for t in TYPES),
        ]
    return out


def _isolation_forest_flags(history, bill):
    if not ml_available() or len(history) < MIN_ML_BILLS:
        return False
    features = _features(history + [bill])
    model = IsolationForest(n_estimators=200, contamination=ML_CONTAMINATION, random_state=42)
    model.fit(features[:-1])
    return model.predict(features[-1:])[0] == -1


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def assess(conn, bill):
    """bill: row with id, employee_id, bill_type, bill_date, amount.
    Returns {"anomaly": bool, "reasons": [str], "history": int}."""
    amount = float(bill["amount"])
    btype, emp, month = bill["bill_type"], bill["employee_id"], bill["bill_date"][:7]
    history = load_history(conn, bill["id"])
    same_type = [h for h in history if h["bill_type"] == btype]
    reasons = []

    own = [h["amount"] for h in same_type if h["employee_id"] == emp]
    if len(own) >= MIN_OWN_BILLS:
        high, med, ratio = _robust_high(amount, own)
        if high:
            reasons.append(f"{_rs(amount)} is {ratio:.1f}× this employee's usual {btype} bill "
                           f"(median {_rs(med)} over {len(own)} bills)")

    company = [h["amount"] for h in same_type]
    if len(company) >= MIN_COMPANY_BILLS:
        high, med, ratio = _robust_high(amount, company)
        if high:
            reasons.append(f"{_rs(amount)} is {ratio:.1f}× the company-wide usual {btype} bill "
                           f"(median {_rs(med)} over {len(company)} bills)")

    monthly = defaultdict(float)
    others_this_month = 0
    for h in same_type:
        if h["employee_id"] == emp:
            monthly[h["bill_date"][:7]] += h["amount"]
            others_this_month += h["bill_date"][:7] == month
    month_total = monthly.pop(month, 0.0) + amount
    past = list(monthly.values())
    if others_this_month and len(past) >= MIN_MONTHS:
        high, med, ratio = _robust_high(month_total, past)
        if high:
            reasons.append(f"{_month_name(month)} {btype} total reaches {_rs(month_total)} with this bill "
                           f"({others_this_month + 1} bills), {ratio:.1f}× this employee's usual monthly total "
                           f"(median {_rs(med)} over {len(past)} months)")

    try:
        candidate = {"employee_id": emp, "bill_type": btype, "bill_date": bill["bill_date"], "amount": amount}
        # Only unusually high amounts matter, so the ML flag needs the same "at least MIN_RATIO x usual" as the rules.
        if (company and amount >= MIN_RATIO * median(company)
                and _isolation_forest_flags(history, candidate)):
            reasons.append(f"ML model (Isolation Forest) rates this bill as unusual "
                           f"compared with {len(history)} past bills")
    except Exception:
        log.exception("Isolation Forest check failed")

    return {"anomaly": bool(reasons), "reasons": reasons, "history": len(history)}


def learned_limits(conn):
    """The cut-offs the statistical rules have learned from the current history.
    Per bill: the lower of the employee's own and the company-wide cut-off for each employee and type.
    Per month: the monthly-total cut-off for each employee and type. Each is returned as a min/max range."""
    this_month = date.today().strftime("%Y-%m")
    by_series, by_type = defaultdict(list), defaultdict(list)
    months = defaultdict(lambda: defaultdict(float))
    for h in load_history(conn):
        by_type[h["bill_type"]].append(h["amount"])
        series = (h["employee_id"], h["bill_type"])
        by_series[series].append(h["amount"])
        if h["bill_date"][:7] != this_month:
            months[series][h["bill_date"][:7]] += h["amount"]

    per_bill = []
    for (_, btype), amounts in by_series.items():
        if len(amounts) < MIN_OWN_BILLS:
            continue
        limit = cutoff(amounts)
        if len(by_type[btype]) >= MIN_COMPANY_BILLS:
            limit = min(limit, cutoff(by_type[btype]))
        per_bill.append(limit)
    per_month = [cutoff(list(m.values())) for m in months.values() if len(m) >= MIN_MONTHS]

    def span(values):
        return {"min": round(min(values)), "max": round(max(values)), "series": len(values)} if values else None

    return {"per_bill": span(per_bill), "per_month": span(per_month)}


def info(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(is_sample), 0) AS sample FROM bills"
        " WHERE (status = 'approved' OR (status = 'pending' AND anomaly = 0))"
        " AND amount > 0 AND bill_date != '' AND bill_type != ''").fetchone()
    return {
        "history_bills": row["total"],
        "sample_bills": row["sample"],
        "ml_installed": ml_available(),
        "ml_active": ml_available() and row["total"] >= MIN_ML_BILLS,
        "ml_min_bills": MIN_ML_BILLS,
        "limits": learned_limits(conn),
    }
