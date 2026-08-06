# Visitor Pass Tracker

A **Frappe 15 / ERPNext 15** app that automates visitor entry passes across
multiple gates/locations. It reuses ERPNext's **Employee** and **Department**
doctypes for host/approval routing, Frappe's **Contact** doctype for known
external visitors, and Frappe's native **Workflow**, **Notification** and
**Notification Log** features instead of duplicating them.

```
Draft -> Blacklist Check -> Pending Host Approval -> Pending Department
Approval -> Pending Security Approval -> Approved | Rejected
```

---

## Installation

```bash
# 1. Get the app into your bench (Frappe 15 + ERPNext 15 required)
bench get-app https://github.com/Sudhakar1110/visitor_pass.git

# 2. Install it on your site - roles, workflow, notifications, report,
#    dashboard and charts are all shipped as fixtures and installed automatically
bench --site <sitename> install-app visitor_pass_tracker

# 3. Restart + migrate (scheduler picks up the 15-minute expiry job)
bench --site <sitename> migrate
bench restart
```

> **Scheduler**: the pass-expiry automation runs on a cron entry registered in
> `hooks.py` (`*/15 * * * *`). Enable the scheduler with
> `bench --site <sitename> scheduler enable`.

## Applying updates to an existing site

Every release here (the 13 gap fixes, the 12 features, and the performance
batch) is pushed to `main`. To bring an already-installed site up to date:

```bash
cd ~/frappe-bench

# 1. pull the latest app code from GitHub (the app folder is a git checkout)
cd apps/visitor_pass_tracker && git pull origin main && cd ../..

# 2. sync doctypes, reports, pages and create the new DB indexes
bench --site <sitename> migrate

# 3. import fixtures (web forms, notifications, number cards, roles, workflow)
bench --site <sitename> sync-fixtures

# 4. remove the old code-sent pass emails so they don't double-send with the
#    new in-code QR email (only needed once, for sites installed before the
#    QR rewrite)
bench --site <sitename> execute frappe.delete_doc --kwargs "{'doctype':'Notification','name':'Entry Pass Generated'}"
bench --site <sitename> execute frappe.delete_doc --kwargs "{'doctype':'Notification','name':'Entry Pass Generated - Visitor'}"

# 5. verify everything is present on the site
bench --site <sitename> execute visitor_pass_tracker.utils.check_installation

bench restart
```

The last command prints a per-component report (fields, workflow transitions,
fixtures, web forms, reports, page, DB indexes) - every line should read
**OK**, and `stale_notifications_to_delete` should show both as `false`.

> **Workflow emails**: the Workflow's native `send_email_alert` emails the *role*
> that can perform the next action (e.g. all Employees for the host step).
> Targeted alerts to the specific host / Security role are handled by the
> Notification fixtures. If role-wide emails are too noisy, uncheck
> `send_email_alert` on the Workflow and keep only the Notification-based alerts.

---

## Doctypes

| Doctype             | Purpose                                                              |
| ------------------- | -------------------------------------------------------------------- |
| Visitor             | External visitor master (name, phone, email, ID proof, photo, company, optional `linked_contact` to ERPNext Contact) |
| Blacklisted Visitor | Blacklist master - `status` Active/Lifted, blacklisted_by/on, reason |
| Gate                | Gate / turnstile master (`gate_name`, `location` → Address, optional `company`, `device_id`) |
| Visitor Request     | Submittable request: visitor, host (Employee), department (fetched), purpose, visit window, gate, `blacklist_status`, workflow-driven |
| Entry Pass          | Auto-created on final approval: validity window, QR code, status Active/Expired/Used/Revoked |
| Gate Log Entry      | Scan events from gate hardware; whitelisted `submit_scan` / `manual_exit` / `revoke_pass` APIs; carries `visitor_name` + `host_user` (fetched) for notifications |

---

## Approval Workflow (native Workflow doctype)

The state machine ships as the **"Visitor Request Workflow"** fixture - no state
logic is hardcoded in Python. Only the *automatic* transitions are triggered by
the controller (`visitor_request.py`):

| From | To | Trigger |
| ---- | -- | ------- |
| Draft | Blacklist Check | **auto on Submit** (state set by the framework) |
| Blacklist Check | Rejected | **auto** when `blacklist_status == "Flagged"` → native Notification alerts the **Security Officer** role |
| Blacklist Check | Pending Host Approval | **auto** when the blacklist check is clear |
| Pending Host Approval | Pending Department Approval | Role **Employee** (the host) - action `Approve as Host` |
| Pending Department Approval | Pending Security Approval | Role **Department Head** - action `Approve by Department Head` |
| Pending Department Approval | Pending Security Approval | **auto** for `purpose == "Delivery"` (transition `Skip for Delivery`, conditioned on `doc.purpose == "Delivery"`) |
| Pending Security Approval | Approved | Role **Security Officer** - action `Approve by Security Officer` |
| Pending Host / Dept / Security Approval | Rejected | Role **Employee / Department Head / Security Officer** - action `Reject Request` (a Rejection Reason is mandatory; the automatic blacklist rejection is exempt) |
| Approved | (Entry Pass) | **auto** - Entry Pass + QR code created from the visit window |
| (any, after approval) | - | **Cancelling** a submitted request auto-revokes its Entry Pass so the gate stops accepting it |

Blacklist matching runs on **every save** of the request (including before
insert) by comparing the linked Visitor's `phone` / `id_proof_number` against
Active `Blacklisted Visitor` records (phone is normalized to digits).

---

## Automations

1. **Pass-expiry + overstay alert (scheduler, every 15 min)** - `utils.run_pass_expiry_checks`
   - Auto-marks `Entry Pass.status = "Expired"` when `valid_till` passes.
   - Creates a **Notification Log** (standard notification bell) + **Email** to
     the host user for Active passes expiring within the next 30 minutes
     (deduplicated via the hidden `expiry_alert_sent` flag).
   - **Overstay alert** - when a pass is past `valid_till` but has an entry
     scan and no exit scan, all **Security Officer** users are notified
     (deduplicated via the hidden `overstay_alert_sent` flag).

1b. **Gate notifications (native Notification fixtures)**
   - **Visitor Arrived** - authorized `Entry` scan on `Gate Log Entry` →
     Notification Log + Email to the host (`host_user`, fetched onto the log).
   - **Unauthorized Scan Detected** - any `is_authorized == 0` scan → alerts
     the **Security Officer** role.
   - **Entry Pass Generated** - the QR code is attached to the host email
     (`attach_files = From Field` → `qr_code`).

2. **Visitor Reconciliation (Script Report)** - run it from the dashboard card
   or the report list. Flags with color-coded `indicator` styling:
   - 🟡 **No-show** - pass was never scanned at any gate
   - 🟠 **Overstay** - entry scan exists, no exit scan, and `valid_till` passed
   - 🔴 **Unauthorized** - gate log with no matching valid pass
   - 🟢 **On-site** / **Completed** - healthy records
   The report also returns a chart + summary cards (so it renders as a
   dashboard card).

3. **Blacklist auto-check** - on the `Visitor Request` controller
   (`validate` + auto-reject in `on_submit`).

4. **Auto-revoke overstays** - after the overstay alert, passes whose visitor
   is still on-site past the grace period (`visitor_pass_overstay_grace_hours`
   in site_config.json, default 6h) are auto-revoked with an audit trail.

5. **Approval reminders + escalation (hourly)** - `utils.run_hourly_automations`
   - a request stuck in **Pending Host / Department / Security Approval** for
     more than `visitor_pass_reminder_hours` (default **4h**) reminds the
     approver (host / department head / Security Officer) by email + in-app
     notification; after `visitor_pass_escalation_hours` (default **24h**) the
     **System Manager** is notified. Deduplicated via hidden flags.

6. **Auto-reject stale requests (hourly)** - still pending after
   `visitor_pass_stale_days` (default **3 days**) -> auto-rejected with reason
   "no response within N day(s)" and the host gets the standard rejection
   notification.

7. **Auto-blacklist repeat offenders (hourly)** - visitors with
   `visitor_pass_overstay_blacklist_threshold`+ overstays (default **2**) or
   `visitor_pass_unauthorized_blacklist_threshold`+ unauthorized scans
   (default **3**) get an Active **Blacklisted Visitor** record created
   automatically (reason auto-filled), the Visitor flag is synced, and their
   open pending requests are rejected.

8. **Auto-approve trusted repeat visitors** - a visitor with at least
   `visitor_pass_trusted_visit_threshold` completed (Used) passes (default
   **3**) and no blacklist record skips the **Department** and **Security**
   approval steps automatically and goes straight to **Approved** (the host
   step and blacklist check always stay).

9. **Day-before visit reminder (daily 9 AM)** - `utils.send_day_before_visit_reminders`
   emails + SMSes the visitor "your visit is tomorrow" with gate, time window
   and host (deduplicated per request).

10. **Expected-tomorrow digest (daily 5 PM)** - `utils.send_expected_tomorrow_digest`
    emails the full list of approved visits for tomorrow (visitor, purpose,
    window, gate, host, vehicle) to `visitor_pass_digest_roles` (default
    **Security Officer, Reception**).

11. **End-of-day reconciliation digest (daily 7 PM)** - `utils.send_end_of_day_reconciliation_digest`
    emails the Visitor Reconciliation summary for today (scheduled / on-site /
    overstay / no-show / unauthorized counts) plus a **CSV attachment** of all
    flagged records to `visitor_pass_recon_digest_roles` (default **Security
    Officer, System Manager**).

12. **Calendar invite (.ics)** - every pass email (host + visitor) now carries
    an `.ics` attachment so the visit window lands in their calendar.

13. **Duplicate visitor auto-merge (daily 2 AM)** - `utils.merge_duplicate_visitors`
    merges Visitors sharing the same phone: child documents (requests, passes,
    gate logs, blacklist records) are relinked to the most complete record,
    missing fields are absorbed, and duplicates are marked `merged_into`
    (hidden) - nothing is ever deleted, and merged records are excluded from
    lookups and the portal.

14. **VIP arrival alert** - visitors flagged **VIP** (Check on the Visitor
    doctype) trigger an instant email + in-app + SMS alert to the **Security
    Officer** role and the host the moment they scan in.

> **Configuration** (site_config.json) - all defaults are sensible; override
> any of: `visitor_pass_reminder_hours`, `visitor_pass_escalation_hours`,
> `visitor_pass_stale_days`, `visitor_pass_trusted_visit_threshold`,
> `visitor_pass_overstay_blacklist_threshold`,
> `visitor_pass_unauthorized_blacklist_threshold`, `visitor_pass_digest_roles`,
> `visitor_pass_recon_digest_roles`. Set a threshold to `0` to disable that
> automation. After pulling this release run `bench --site <sitename> migrate`
> (adds the new columns) and `bench restart` (picks up the new crons).

---

## Gate scanner API

Gate hardware (QR/RFID readers, turnstiles) POSTs scan events to a whitelisted
method (login required - use a service user + API keys):

```bash
curl -X POST https://your-site.com/api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.submit_scan \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "Content-Type: application/json" \
  -d '{
        "entry_pass": "PASS-2026-00001",   # or paste the QR payload (JSON string)
        "gate": "Main Gate",               # Gate name or device_id
        "scan_type": "Entry",              # Entry | Exit
        "scanned_by_device": "TURNSTILE-01"
      }'
```

Response: `{"status": "authorized" | "unauthorized", ...}`. An authorized Exit
scan automatically marks the pass as **Used**. To harden the endpoint, set
`visitor_pass_api_token` in `site_config.json`; every request must then pass the
matching `token` parameter.

### Gate operations (incident / manual handling)

```bash
# Instantly revoke a pass (lost badge / security incident)
curl -X POST https://your-site.com/api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.revoke_pass \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"entry_pass": "PASS-2026-00001", "remarks": "Visitor left without exiting"}'

# Close a visit without a scan (lost pass / manual exit) - logs an Exit scan + marks pass Used.
# Refused unless the visitor has an Entry scan - pass \"force\": 1 to close anyway.
curl -X POST https://your-site.com/api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.manual_exit \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"entry_pass": "PASS-2026-00001", "gate": "Main Gate", "remarks": "Manual exit at gate"}'

# Extend a late-running visit (new valid_till; an Expired pass is reactivated)
curl -X POST https://your-site.com/api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.extend_pass \
  -H "Authorization: token <api_key>:<api_secret>" \
  -H "Content-Type: application/json" \
  -d '{"entry_pass": "PASS-2026-00001", "valid_till": "2026-08-06 20:30:00"}'
```

Duplicate detection: `visitor_pass_tracker.utils.find_matching_visitors` (whitelisted)
returns existing Visitors matching a phone / ID proof, and `Visitor Request`
auto-links the existing Visitor master when a phone number is entered without
selecting a Visitor. A visitor can no longer hold two **overlapping** visit
windows on the same day (duplicate-pass guard), and a repeated Entry scan on a
pass whose holder is already inside is answered with `"status": "duplicate"`
instead of creating another log.

Revoking records an audit trail: `revoke_pass` sets `revoked_by` / `revoked_on`
on the Entry Pass, `manual_exit` tags the log with the acting user, and every
Gate Log Entry carries a `source` (Gate Device / Desk / Manual Exit / API) so
manual desk entries are distinguishable from hardware scans.

---

## Self-service & operations

- **Public Visitor Portal** (`/visitor_portal`) - a guest-accessible website page
  backed by `allow_guest=True` APIs (`visitor_pass_tracker.portal`):
  - **Pre-Register** - creates/updates the Visitor master (deduplicated by
    phone; the visitor's linked ERPNext Contact is auto-created) **and
    auto-creates a Draft Visitor Request in the desk** so Reception/Security
    can complete the host, gate and window and submit it through the normal
    workflow (idempotent - no second request while one is already open). The
    same applies to the public `visitor-pre-registration` web form; visitors
    created from the desk itself are never auto-requested. Note: since `Host`
    is a mandatory field, the auto-created draft sits in the desk as **Draft**
    and can only be submitted after Reception assigns the host (and adjusts
    the gate/window if needed).
  - **Track My Visit** - a visitor can look up only their own requests/passes
    by phone, with live workflow status (approved / pending / rejected).
  - **My Pass (QR)** - downloads / prints the QR badge for any pass the phone
    number verifiably owns. Private QR files are read server-side and returned
    as a data URL, so no file permissions are exposed.
  The page works with the standard Frappe website chrome (no login, no extra
  setup - CSRF is handled by `frappe.call`); a **Visitor Portal** shortcut was
  added to the desk workspace.
- **Web forms** (shipped as fixtures): **Request a Visit** (`/request-a-visit`, login
  required, raises a Draft Visitor Request - the logged-in employee becomes the
  default host) and **Visitor Pre-Registration** (`/visitor-pre-registration`,
  public, creates/updates the Visitor master).
- **Gate Scanner** desk page (`/app/gate-scanner`) - a console for security to
  scan/paste a pass and run Entry / Exit / Manual Exit / Revoke / Extend without
  REST calls (camera scanning can be added on top; the console works with the
  pass number or the full QR payload).
- **Expected Visitors** script report - operational list for a date range with
  arrival status (arrived / not arrived) and host check-in, per gate/status.
- **Visitors Expected Today** number card on the dashboard.
- **Multi-day / overnight visits** - set `Visit End Date` on the request; the
  pass validity spans the range.
- **Host check-in / check-out** - `host_checkin` / `host_checkout` whitelisted
  APIs (or the allow-on-submit fields) record when the host received the
  visitor and when the meeting ended.
- **ID proof verification** - Visitors carry an `id_proof_document` attachment
  and an `id_proof_verified` flag.
- **SMS channel** - best-effort SMS via Frappe's **SMS Settings** (Twilio /
  Exotel / MSG91...): the visitor gets the pass number by SMS on approval, and
  the host gets an SMS when the visitor arrives. Configure SMS Settings in
  Frappe; failures are logged, never raised. WhatsApp can be routed through a
  WhatsApp-capable gateway in SMS Settings.

## Dashboard: "Visitor Overview"

- **Number cards**: Visitors On-Site Now (Active pass + entry scan, no exit
  scan) · Passes Expiring in Next Hour
- **Charts**: Visits by Purpose (pie) · Visits by Department (bar) ·
  Peak Visit Hours (line, from Gate Log Entry scan times) · Reconciliation
  Summary (report card) · Visits by Gate (bar) · Visits by Month (timeseries)
  · Visits per Host (bar)

## Printing

- **Entry Pass Badge** print format (standard, Jinja) ships with the app -
  print it from any Entry Pass (photo, host, gate, validity window, QR code).
- New **Visitors** get an ERPNext **Contact** auto-created and linked via
  `linked_contact` (skipped when no email/phone; failures are logged).
- **Visitor Request** carries `vehicle_number` + `is_escort_required` for
  contractor / escorted visits; **Gate** carries an optional `company`.
- **Daily Visitor Register** script report - printable security logbook of all
  gate scans (per day / gate / status).
- **Entry Pass Badge** now also prints the vehicle number, escort requirement
  and company name.
- **Visitor history** - all doctypes declare `links`, so every Visitor shows a
  "Linked With" history (requests, passes, gate logs, blacklist records) and
  deletion is blocked while linked documents exist.

---

## Permissions

| Role | Access |
| ---- | ------ |
| **Security Officer** | Full access to Visitor Request, Gate Log Entry, Blacklisted Visitor, Gate; approves the security step |
| **Employee** (host) | Creates Visitor Requests, approves their own step; `has_permission` scopes them to requests where they are the host (or owner) |
| **Department Head** | Approves the department step for requests in their department (incl. sub-departments) |
| **Reception** | Creates Visitor + Visitor Request, views Entry Pass QR for printing/display |
| **System Manager** | Full administrative access |

> **Data visibility**: Entry Pass and Gate Log Entry are scoped for
> **Employee** (only passes/logs of visits they host or created) and
> **Department Head** (their department, incl. sub-departments) - including
> the Daily Visitor Register and Visitor Reconciliation reports. Security
> Officer / Reception / System Manager always see everything.

---

## Development notes

- **DB indexes**: `Entry Pass.valid_till` and `Gate Log Entry.scan_time` /
  `entry_pass` carry `search_index` (created by `bench migrate`) so the
  15-minute scheduler, expiry checks and reports stay fast as data grows.
- **Reconciliation is bounded**: the report scans at most
  `visitor_pass_reconciliation_limit` records (default 1000, most recent
  first) - raise it in `site_config.json` or use date filters for full runs.
- `frappe.utils.fixtures.sync_fixtures` imports everything in
  `visitor_pass_tracker/fixtures/` (order defined in
  `hooks.py`). Re-export after UI changes: `bench --site <site> export-fixtures`.
- QR codes are generated with `pyqrcode` (bundled with Frappe 15) as private
  PNG files (SVG fallback if `pypng` is missing).
- The `Visitor` doctype uses hash naming; `Visitor Request` uses `VREQ-.YYYY.-`
  and `Entry Pass` uses `PASS-.YYYY.-`.
- Requires ERPNext 15 (Employee / Department / Address doctypes).

## License

MIT
