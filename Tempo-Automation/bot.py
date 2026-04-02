import os, logging, random, datetime, calendar, requests, zoneinfo, functools
import holidays
from flask import Flask, request as flask_request, Response
from twilio.rest import Client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# --- Config ---
TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM = os.environ["TWILIO_PHONE_NUMBER"]
MY_PHONE = os.environ["MY_PHONE_NUMBER"]

TEMPO_TOKEN = os.environ["TEMPO_API_TOKEN"]
TEMPO_BASE = os.environ.get("TEMPO_BASE_URL", "https://api.tempo.io")
JIRA_ACCOUNT_ID = os.environ["JIRA_ACCOUNT_ID"]
DAILY_SECONDS = int(float(os.environ.get("DAILY_HOURS", "7.5")) * 3600)  # 27000

JIRA_DOMAIN = os.environ["JIRA_DOMAIN"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
JIRA_BOARD_ID = os.environ["JIRA_BOARD_ID"]
TZ = zoneinfo.ZoneInfo(os.environ.get("TZ", "America/Toronto"))

AUTH_USER = os.environ["AUTH_USERNAME"]
AUTH_PASS = os.environ["AUTH_PASSWORD"]

twilio = Client(TWILIO_SID, TWILIO_AUTH)
ca_holidays = holidays.Canada(prov="ON")


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = flask_request.authorization
        if not auth or auth.username != AUTH_USER or auth.password != AUTH_PASS:
            return Response("Unauthorized", 401,
                            {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated


# ── Helpers ──────────────────────────────────────────────────────────────

def is_workday(d: datetime.date) -> bool:
    """True if d is a weekday and not a Canadian/Ontario holiday."""
    return d.weekday() < 5 and d not in ca_holidays

def sms(body: str):
    twilio.messages.create(body=body, from_=TWILIO_FROM, to=MY_PHONE)
    log.info(f"SMS sent: {body[:80]}...")


def jira_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"https://{JIRA_DOMAIN}{path}", params=params,
                     auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    r.raise_for_status()
    return r.json()


def get_active_tickets() -> list[dict]:
    """Get issues assigned to you in active sprint (In Progress / In Review).
    Returns list of {"key": "SGPT-661", "id": "12345"} dicts."""
    sprints = jira_get(f"/rest/agile/1.0/board/{JIRA_BOARD_ID}/sprint",
                       {"state": "active"})
    active = sprints.get("values", [])
    if not active:
        return []
    sid = active[0]["id"]
    jql = (f'assignee="{JIRA_ACCOUNT_ID}" '
           f'AND status in ("In Progress","In Review")')
    data = jira_get(f"/rest/agile/1.0/sprint/{sid}/issue",
                    {"jql": jql, "fields": "key"})
    tickets = [{"key": i["key"], "id": i["id"]} for i in data.get("issues", [])]
    log.info(f"Active tickets: {tickets}")
    return tickets


def get_logged_seconds(date: str) -> int:
    """Get total seconds already logged for a date."""
    url = f"{TEMPO_BASE}/4/worklogs"
    params = {"from": date, "to": date, "authorAccountId": JIRA_ACCOUNT_ID, "limit": 1000}
    log.info(f"GET {url} params={params}")
    r = requests.get(url, headers={"Authorization": f"Bearer {TEMPO_TOKEN}"}, params=params)
    log.info(f"GET worklogs response: {r.status_code} body={r.text[:500]}")
    if r.status_code != 200:
        log.error(f"GET worklogs FAILED: {r.status_code} {r.text}")
        return 0
    results = r.json().get("results", [])
    total = sum(w.get("timeSpentSeconds", 0) for w in results)
    log.info(f"Worklogs for {date}: {len(results)} entries, total={total}s ({total/3600:.2f}h)")
    return total


def log_worklog(issue_id: str, issue_key: str, date: str, seconds: int) -> bool:
    url = f"{TEMPO_BASE}/4/worklogs"
    payload = {"issueId": int(issue_id), "timeSpentSeconds": seconds,
               "startDate": date, "startTime": "09:00:00",
               "description": f"Work on {issue_key}",
               "authorAccountId": JIRA_ACCOUNT_ID}
    log.info(f"POST {url} payload={payload}")
    r = requests.post(url, headers={"Authorization": f"Bearer {TEMPO_TOKEN}",
                                     "Content-Type": "application/json"}, json=payload)
    log.info(f"POST worklog response: {r.status_code} body={r.text[:500]}")
    ok = r.status_code in (200, 201)
    if not ok:
        log.error(f"POST worklog FAILED {issue_key}(id={issue_id}) {date}: {r.status_code} {r.text}")
    return ok


def workdays(start: datetime.date, count: int) -> list[datetime.date]:
    """Return `count` workdays (weekdays excl. holidays) starting from `start`."""
    days = []
    d = start
    while len(days) < count:
        if is_workday(d):
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def top_up(ticket: dict, date: str, desired: int) -> tuple[str, int]:
    """Log time but never exceed DAILY_SECONDS. Returns (issue_key, seconds_added).
    ticket is {"key": "SGPT-661", "id": "12345"}."""
    already = get_logged_seconds(date)
    remaining = DAILY_SECONDS - already
    log.info(f"top_up {ticket['key']} {date}: already={already}s ({already/3600:.2f}h), "
             f"remaining={remaining}s ({remaining/3600:.2f}h), desired={desired}s")
    if remaining <= 0:
        log.info(f"top_up {ticket['key']} {date}: day is full, skipping")
        return ticket["key"], 0
    to_log = min(desired, remaining)
    ok = log_worklog(ticket["id"], ticket["key"], date, to_log)
    return ticket["key"], to_log if ok else 0


def pick(tickets: list[dict], n: int = 1) -> list[dict]:
    """Pick n random unique tickets."""
    return random.sample(tickets, min(n, len(tickets)))


# ── Variations ───────────────────────────────────────────────────────────

def variation_1(tickets: list[dict], days: list[datetime.date]) -> list[str]:
    """One ticket, 7.5h every day all week."""
    t = pick(tickets, 1)[0]
    summary = []
    for d in days:
        _, added = top_up(t, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {t['key']} +{added/3600:.1f}h")
    return [f"V1: {t['key']} all week"] + summary


def variation_2(tickets: list[dict], days: list[datetime.date]) -> list[str]:
    """Ticket A for 3 days, ticket B for 2 days."""
    ts = pick(tickets, 2)
    a, b = ts[0], ts[-1]  # if only 1 ticket, a==b is fine
    summary = [f"V2: {a['key']} x3 days, {b['key']} x2 days"]
    for d in days[:3]:
        _, added = top_up(a, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {a['key']} +{added/3600:.1f}h")
    for d in days[3:]:
        _, added = top_up(b, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {b['key']} +{added/3600:.1f}h")
    return summary


def variation_3(tickets: list[dict], days: list[datetime.date]) -> list[str]:
    """Day1: split 2.5+5, Day2: split 2.5+5 (same pair), Day3-5: 7.5 one ticket."""
    ts = pick(tickets, 2)
    a, b = ts[0], ts[-1]
    c = pick(tickets, 1)[0]
    summary = [f"V3: {a['key']}/{b['key']} split x2 days, {c['key']} x3 days"]
    for d in days[:2]:
        _, a1 = top_up(a, d.isoformat(), 9000)   # 2.5h
        _, a2 = top_up(b, d.isoformat(), 18000)  # 5h
        summary.append(f"  {d} {a['key']} +{a1/3600:.1f}h, {b['key']} +{a2/3600:.1f}h")
    for d in days[2:]:
        _, added = top_up(c, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {c['key']} +{added/3600:.1f}h")
    return summary


# ── Scheduled jobs ───────────────────────────────────────────────────────

def monday_job():
    """Every Monday: pick a random variation and log the whole week."""
    tickets = get_active_tickets()
    if not tickets:
        sms("No active tickets found in sprint — nothing logged.")
        return

    today = datetime.datetime.now(TZ).date()
    days = workdays(today, 5)  # Mon-Fri

    variation = random.choice([variation_1, variation_2, variation_3])
    lines = variation(tickets, days)

    sms("Weekly timesheet filled!\n" + "\n".join(lines))


def month_end_job():
    """Runs daily; if exactly 7 days before end of month, fill remaining gaps."""
    today = datetime.datetime.now(TZ).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    days_left = last_day - today.day

    if days_left != 6:  # only fire when 7 days remain (today = last_day - 6)
        return

    tickets = get_active_tickets()
    if not tickets:
        sms("Month-end fill: no active tickets found.")
        return

    # Fill every weekday from today to end of month
    end = datetime.date(today.year, today.month, last_day)
    lines = ["Month-end gap fill:"]
    d = today
    while d <= end:
        if is_workday(d):
            already = get_logged_seconds(d.isoformat())
            if already < DAILY_SECONDS:
                t = random.choice(tickets)
                _, added = top_up(t, d.isoformat(), DAILY_SECONDS)
                if added > 0:
                    lines.append(f"  {d} {t['key']} +{added/3600:.1f}h")
        d += datetime.timedelta(days=1)

    if len(lines) == 1:
        lines.append("  All days already filled!")

    sms("\n".join(lines))


@app.route("/")
@require_auth
def index():
    return """<html><head><title>Larus Tools</title>
<style>
body{font-family:system-ui,sans-serif;max-width:600px;margin:40px auto;padding:0 20px;color:#333}
h1{margin-bottom:4px}p.sub{color:#888;margin-top:0}
table{width:100%;border-collapse:collapse;margin-top:20px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #eee}
th{background:#f7f7f7}
a{color:#0066cc;text-decoration:none}a:hover{text-decoration:underline}
code{background:#f0f0f0;padding:2px 6px;border-radius:3px;font-size:0.9em}
</style></head><body>
<h1>Larus Tools</h1>
<p class="sub">Tempo timesheet automation</p>
<table>
<tr><th>Endpoint</th><th>Description</th></tr>
<tr><td><a href="/health">/health</a></td><td>Ping check</td></tr>
<tr><td><a href="/test/sms">/test/sms</a></td><td>Send status SMS (active tickets + today's hours)</td></tr>
<tr><td><a href="/test/topup">/test/topup</a></td><td>Top up today to 7.5h on a random ticket (workdays only)</td></tr>
<tr><td><a href="/test/holiday">/test/holiday</a></td><td>List all Ontario holidays (red days) for this year</td></tr>
<tr><td><a href="/run/weekly">/run/weekly</a></td><td>Fill the week with a random variation (cron: Mon 9am)</td></tr>
<tr><td><a href="/run/monthend">/run/monthend</a></td><td>Fill month-end gaps (cron: daily 8am, acts 7 days before EOM)</td></tr>
</table>
</body></html>"""


@app.route("/health")
def health():
    return "ok"


@app.route("/test/sms")
@require_auth
def test_sms():
    """Health check that sends you an SMS with debug info."""
    try:
        tickets = get_active_tickets()
        ticket_keys = [t["key"] for t in tickets]
        today = datetime.datetime.now(TZ).date().isoformat()

        # Fetch worklogs with full debug
        url = f"{TEMPO_BASE}/4/worklogs"
        params = {"from": today, "to": today, "authorAccountId": JIRA_ACCOUNT_ID, "limit": 1000}
        r = requests.get(url, headers={"Authorization": f"Bearer {TEMPO_TOKEN}"}, params=params)
        log.info(f"/test/sms GET {url} params={params} -> {r.status_code} body={r.text[:500]}")

        if r.status_code == 200:
            results = r.json().get("results", [])
            total = sum(w.get("timeSpentSeconds", 0) for w in results)
            entries = [f"  {w.get('issue',{}).get('key','?')}: {w.get('timeSpentSeconds',0)/3600:.2f}h"
                       for w in results]
            entry_text = "\n".join(entries) if entries else "  (none)"
            msg = (f"Tempo bot alive!\n"
                   f"Active tickets: {len(tickets)} ({', '.join(ticket_keys[:5]) or 'none'})\n"
                   f"Today ({today}): {total/3600:.2f}h / {DAILY_SECONDS/3600:.1f}h\n"
                   f"Entries:\n{entry_text}")
        else:
            msg = (f"Tempo bot alive but GET worklogs failed!\n"
                   f"Status: {r.status_code}\n"
                   f"Response: {r.text[:200]}\n"
                   f"Active tickets: {len(tickets)}")

        sms(msg)
        return msg, 200
    except Exception as e:
        log.exception(f"test/sms failed: {e}")
        return f"Error: {e}", 500


@app.route("/test/topup")
@require_auth
def test_topup():
    """Top up today to 7.5h on a random active ticket (weekdays only)."""
    try:
        today_date = datetime.datetime.now(TZ).date()
        if not is_workday(today_date):
            reason = ca_holidays.get(today_date, "weekend")
            msg = f"Not a workday ({reason}) — no hours logged."
            sms(msg)
            return msg, 200

        tickets = get_active_tickets()
        if not tickets:
            msg = "Top-up failed: no active tickets in sprint."
            sms(msg)
            return msg, 200

        today = datetime.datetime.now(TZ).date().isoformat()
        already = get_logged_seconds(today)
        remaining = DAILY_SECONDS - already
        t = random.choice(tickets)

        log.info(f"/test/topup: today={today} ticket={t} already={already}s "
                 f"({already/3600:.2f}h) remaining={remaining}s ({remaining/3600:.2f}h)")

        if remaining <= 0:
            msg = (f"Today already full.\n"
                   f"Logged: {already/3600:.2f}h / {DAILY_SECONDS/3600:.1f}h\n"
                   f"Nothing added.")
            sms(msg)
            return msg, 200

        ok = log_worklog(t["id"], t["key"], today, remaining)
        if ok:
            msg = (f"Topped up today:\n"
                   f"  {today} {t['key']} +{remaining/3600:.2f}h\n"
                   f"  Was: {already/3600:.2f}h, now: {DAILY_SECONDS/3600:.1f}h")
        else:
            msg = (f"FAILED to log {t['key']} on {today}.\n"
                   f"  Was: {already/3600:.2f}h, tried to add: {remaining/3600:.2f}h\n"
                   f"  Check Render logs for details.")
        sms(msg)
        return msg, 200
    except Exception as e:
        log.exception(f"test/topup failed: {e}")
        return f"Error: {e}", 500


@app.route("/test/holiday")
@require_auth
def test_holiday():
    """List all holidays (red days) for the current year."""
    try:
        today = datetime.datetime.now(TZ).date()
        year = today.year
        year_holidays = sorted(ca_holidays[datetime.date(year, 1, 1):datetime.date(year, 12, 31)])
        lines = [f"Ontario holidays {year}:\n"]
        for d in year_holidays:
            past = " (past)" if d < today else ""
            lines.append(f"  {d} {d.strftime('%a')} - {ca_holidays.get(d)}{past}")
        msg = "\n".join(lines)
        return msg, 200
    except Exception as e:
        log.exception(f"test/holiday failed: {e}")
        return f"Error: {e}", 500


@app.route("/run/weekly")
@require_auth
def run_weekly():
    """Trigger the Monday weekly job on demand."""
    try:
        monday_job()
        return "Weekly job done", 200
    except Exception as e:
        log.error(f"run/weekly failed: {e}")
        return f"Error: {e}", 500


@app.route("/run/monthend")
@require_auth
def run_monthend():
    """Trigger the month-end gap fill on demand."""
    try:
        month_end_job()
        return "Month-end job done", 200
    except Exception as e:
        log.error(f"run/monthend failed: {e}")
        return f"Error: {e}", 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log.info("Server starting — use external cron to hit /run/weekly and /run/monthend")
    app.run(host="0.0.0.0", port=port)
