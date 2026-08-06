from . import __version__ as __version__

app_name = "visitor_pass_tracker"
app_title = "Visitor Pass Tracker"
app_publisher = "Sudhakar"
app_description = "Automated visitor entry passes across multiple gates/locations for Frappe 15 / ERPNext 15"
app_email = "sudhakar@example.com"
app_license = "MIT"
app_icon = "octicon octicon-person"
app_color = "#2490ef"
source_link = "https://github.com/Sudhakar1110/visitor_pass"

# This app reuses ERPNext's Employee / Department and Frappe's Contact doctypes
required_apps = ["erpnext"]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# Everything below is imported when the app is installed via
# `bench --site <sitename> install-app visitor_pass_tracker`.
#
# Import order matters (linked records must exist before the documents that
# reference them): Roles -> Workflow State / Workflow Action Master -> Workflow
# -> Notification -> Dashboard Chart Source -> Dashboard Chart -> Number Card
# -> Dashboard. The "Visitor Reconciliation" script report is *not* a fixture -
# it is synced from its report folder by frappe.model.sync during migrate
# (standard script-report pattern, same as ERPNext).
fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Security Officer", "Department Head", "Reception"]]]},
	{
		"dt": "Workflow State",
		"filters": [
			[
				"name",
				"in",
				[
					"Draft",
					"Blacklist Check",
					"Pending Host Approval",
					"Pending Department Approval",
					"Pending Security Approval",
					"Approved",
					"Rejected",
				],
			]
		],
	},
	{
		"dt": "Workflow Action Master",
		"filters": [
			[
				"name",
				"in",
				[
					"Send for Blacklist Check",
					"Blacklist Check Cleared",
					"Reject: Blacklisted",
					"Approve as Host",
					"Approve by Department Head",
					"Skip for Delivery",
					"Approve by Security Officer",
					"Reject Request",
				],
			]
		],
	},
	{"dt": "Workflow", "filters": [["name", "=", "Visitor Request Workflow"]]},
	{
		"dt": "Notification",
		"filters": [
			[
				"name",
				"in",
				[
					"Visitor Request Pending Host Approval",
					"Visitor Request Approved",
					"Visitor Request Rejected - Blacklist",
					"Visitor Request Rejected",
					"Visitor Arrived",
					"Unauthorized Scan Detected",
				],
			]
		],
	},
	{
		"dt": "Dashboard Chart Source",
		"filters": [
			["name", "=", "visitor_pass_tracker.utils.get_peak_visit_hours"]
		],
	},
	{
		"dt": "Dashboard Chart",
		"filters": [
			[
				"name",
				"in",
				[
					"Visits by Purpose",
					"Visits by Department",
					"Peak Visit Hours",
					"Reconciliation Summary",
					"Visits by Gate",
					"Visits by Month",
					"Visits per Host",
				],
			]
		],
	},
	{
		"dt": "Number Card",
		"filters": [
			[
				"name",
				"in",
				[
					"Visitors On-Site Now",
					"Passes Expiring in Next Hour",
					"Visitors Expected Today",
				],
			]
		],
	},
	{"dt": "Web Form", "filters": [["name", "in", ["request-a-visit", "visitor-pre-registration"]]]},
	{"dt": "Client Script", "filters": [["name", "=", "Entry Pass - Resend Pass"]]},
	{"dt": "Dashboard", "filters": [["name", "=", "Visitor Overview"]]},
	# Kept for completeness - no extra custom fields are required because every
	# field lives on an app-owned doctype. Ship an empty custom_field.json.
	{"dt": "Custom Field", "filters": [["name", "in", []]]},
]

# ---------------------------------------------------------------------------
# Permissions - module-level hooks (Frappe 15 style)
# ---------------------------------------------------------------------------
has_permission = {
	"Visitor Request": "visitor_pass_tracker.visitor_pass_tracker.doctype.visitor_request.visitor_request.has_permission",
	"Entry Pass": "visitor_pass_tracker.visitor_pass_tracker.doctype.entry_pass.entry_pass.has_permission",
	"Gate Log Entry": "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.has_permission",
}

permission_query_conditions = {
	"Visitor Request": "visitor_pass_tracker.visitor_pass_tracker.doctype.visitor_request.visitor_request.get_permission_query_conditions",
	"Entry Pass": "visitor_pass_tracker.visitor_pass_tracker.doctype.entry_pass.entry_pass.get_permission_query_conditions",
	"Gate Log Entry": "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.get_permission_query_conditions",
}

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# 1. Pass-expiry alert: every 15 minutes, notify hosts about passes expiring
#    within the next 30 minutes, auto-mark expired passes as "Expired", alert
#    Security about overstays and auto-revoke them after the grace period.
# 2. Hourly automations: approval reminders + escalation, stale-request
#    auto-rejection and repeat-offender auto-blacklisting.
# 3. Daily 9 AM: SMS/email visitors whose Approved visit is tomorrow.
# 4. Daily 5 PM: "Expected Visitors tomorrow" digest to Security/Reception.
# 5. Daily 7 PM: end-of-day reconciliation digest to Security/System Manager.
# 6. Daily 2 AM: merge duplicate Visitors sharing the same phone.
scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"visitor_pass_tracker.utils.run_pass_expiry_checks",
		],
		"0 * * * *": [
			"visitor_pass_tracker.utils.run_hourly_automations",
		],
		"0 9 * * *": [
			"visitor_pass_tracker.utils.send_day_before_visit_reminders",
		],
		"0 17 * * *": [
			"visitor_pass_tracker.utils.send_expected_tomorrow_digest",
		],
		"0 19 * * *": [
			"visitor_pass_tracker.utils.send_end_of_day_reconciliation_digest",
		],
		"0 2 * * *": [
			"visitor_pass_tracker.utils.merge_duplicate_visitors",
		],
	},
}
