import os, logging, random, datetime, calendar, requests
from flask import Flask
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

twilio = Client(TWILIO_SID, TWILIO_AUTH)


# ── Helpers ──────────────────────────────────────────────────────────────

def sms(body: str):
    twilio.messages.create(body=body, from_=TWILIO_FROM, to=MY_PHONE)
    log.info(f"SMS sent: {body[:80]}...")


def jira_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"https://{JIRA_DOMAIN}{path}", params=params,
                     auth=(JIRA_EMAIL, JIRA_API_TOKEN))
    r.raise_for_status()
    return r.json()


def get_active_tickets() -> list[str]:
    """Get issue keys assigned to you in active sprint (In Progress / In Review)."""
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
    return [i["key"] for i in data.get("issues", [])]


def get_logged_seconds(date: str) -> int:
    """Get total seconds already logged for a date."""
    r = requests.get(
        f"{TEMPO_BASE}/4/worklogs",
        headers={"Authorization": f"Bearer {TEMPO_TOKEN}"},
        params={"from": date, "to": date, "authorAccountId": JIRA_ACCOUNT_ID,
                "limit": 1000},
    )
    if r.status_code != 200:
        return 0
    return sum(w["timeSpentSeconds"] for w in r.json().get("results", []))


def log_worklog(issue_key: str, date: str, seconds: int) -> bool:
    r = requests.post(
        f"{TEMPO_BASE}/4/worklogs",
        headers={"Authorization": f"Bearer {TEMPO_TOKEN}",
                 "Content-Type": "application/json"},
        json={"issueKey": issue_key, "timeSpentSeconds": seconds,
              "startDate": date, "startTime": "09:00:00",
              "description": f"Work on {issue_key}",
              "authorAccountId": JIRA_ACCOUNT_ID},
    )
    ok = r.status_code in (200, 201)
    if not ok:
        log.error(f"FAIL {issue_key} {date}: {r.status_code} {r.text}")
    return ok


def weekdays(start: datetime.date, count: int) -> list[datetime.date]:
    """Return `count` weekdays starting from `start`."""
    days = []
    d = start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def top_up(issue_key: str, date: str, desired: int) -> tuple[str, int]:
    """Log time but never exceed DAILY_SECONDS. Returns (issue_key, seconds_added)."""
    already = get_logged_seconds(date)
    remaining = DAILY_SECONDS - already
    if remaining <= 0:
        return issue_key, 0
    to_log = min(desired, remaining)
    ok = log_worklog(issue_key, date, to_log)
    return issue_key, to_log if ok else 0


def pick(tickets: list[str], n: int = 1) -> list[str]:
    """Pick n random unique tickets."""
    return random.sample(tickets, min(n, len(tickets)))


# ── Variations ───────────────────────────────────────────────────────────

def variation_1(tickets: list[str], days: list[datetime.date]) -> list[str]:
    """One ticket, 7.5h every day all week."""
    t = pick(tickets, 1)[0]
    summary = []
    for d in days:
        _, added = top_up(t, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {t} +{added/3600:.1f}h")
    return [f"V1: {t} all week"] + summary


def variation_2(tickets: list[str], days: list[datetime.date]) -> list[str]:
    """Ticket A for 3 days, ticket B for 2 days."""
    ts = pick(tickets, 2)
    a, b = ts[0], ts[-1]  # if only 1 ticket, a==b is fine
    summary = [f"V2: {a} x3 days, {b} x2 days"]
    for d in days[:3]:
        _, added = top_up(a, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {a} +{added/3600:.1f}h")
    for d in days[3:]:
        _, added = top_up(b, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {b} +{added/3600:.1f}h")
    return summary


def variation_3(tickets: list[str], days: list[datetime.date]) -> list[str]:
    """Day1: split 2.5+5, Day2: split 2.5+5 (same pair), Day3-5: 7.5 one ticket."""
    ts = pick(tickets, 2)
    a, b = ts[0], ts[-1]
    c = pick(tickets, 1)[0]
    summary = [f"V3: {a}/{b} split x2 days, {c} x3 days"]
    for d in days[:2]:
        _, a1 = top_up(a, d.isoformat(), 9000)   # 2.5h
        _, a2 = top_up(b, d.isoformat(), 18000)  # 5h
        summary.append(f"  {d} {a} +{a1/3600:.1f}h, {b} +{a2/3600:.1f}h")
    for d in days[2:]:
        _, added = top_up(c, d.isoformat(), DAILY_SECONDS)
        summary.append(f"  {d} {c} +{added/3600:.1f}h")
    return summary


# ── Scheduled jobs ───────────────────────────────────────────────────────

def monday_job():
    """Every Monday: pick a random variation and log the whole week."""
    tickets = get_active_tickets()
    if not tickets:
        sms("No active tickets found in sprint — nothing logged.")
        return

    today = datetime.date.today()
    days = weekdays(today, 5)  # Mon-Fri

    variation = random.choice([variation_1, variation_2, variation_3])
    lines = variation(tickets, days)

    sms("Weekly timesheet filled!\n" + "\n".join(lines))


def month_end_job():
    """Runs daily; if exactly 7 days before end of month, fill remaining gaps."""
    today = datetime.date.today()
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
        if d.weekday() < 5:
            already = get_logged_seconds(d.isoformat())
            if already < DAILY_SECONDS:
                t = random.choice(tickets)
                _, added = top_up(t, d.isoformat(), DAILY_SECONDS)
                if added > 0:
                    lines.append(f"  {d} {t} +{added/3600:.1f}h")
        d += datetime.timedelta(days=1)

    if len(lines) == 1:
        lines.append("  All days already filled!")

    sms("\n".join(lines))


@app.route("/health")
def health():
    return "ok"


@app.route("/test/sms")
def test_sms():
    """Health check that sends you an SMS."""
    try:
        tickets = get_active_tickets()
        today = datetime.date.today().isoformat()
        logged = get_logged_seconds(today)
        sms(f"Tempo bot alive!\n"
            f"Active tickets: {len(tickets)} ({', '.join(tickets[:5]) or 'none'})\n"
            f"Today ({today}): {logged/3600:.1f}h / {DAILY_SECONDS/3600:.1f}h logged")
        return "SMS sent", 200
    except Exception as e:
        log.error(f"test/sms failed: {e}")
        return f"Error: {e}", 500


@app.route("/test/topup")
def test_topup():
    """Top up today to 7.5h on a random active ticket (weekdays only)."""
    try:
        if datetime.date.today().weekday() >= 5:
            msg = "It's the weekend — no hours logged."
            sms(msg)
            return msg, 200

        tickets = get_active_tickets()
        if not tickets:
            sms("Top-up failed: no active tickets in sprint.")
            return "No active tickets", 200

        today = datetime.date.today().isoformat()
        t = random.choice(tickets)
        _, added = top_up(t, today, DAILY_SECONDS)

        if added > 0:
            msg = f"Topped up today:\n  {today} {t} +{added/3600:.1f}h"
        else:
            msg = f"Today already full ({DAILY_SECONDS/3600:.1f}h). Nothing added."
        sms(msg)
        return msg, 200
    except Exception as e:
        log.error(f"test/topup failed: {e}")
        return f"Error: {e}", 500


@app.route("/run/weekly")
def run_weekly():
    """Trigger the Monday weekly job on demand."""
    try:
        monday_job()
        return "Weekly job done", 200
    except Exception as e:
        log.error(f"run/weekly failed: {e}")
        return f"Error: {e}", 500


@app.route("/run/monthend")
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
