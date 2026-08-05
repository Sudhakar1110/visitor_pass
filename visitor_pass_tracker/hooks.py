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
					"Entry Pass Generated",
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
				],
			]
		],
	},
	{"dt": "Number Card", "filters": [["name", "in", ["Visitors On-Site Now", "Passes Expiring in Next Hour"]]]},
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
}

permission_query_conditions = {
	"Visitor Request": "visitor_pass_tracker.visitor_pass_tracker.doctype.visitor_request.visitor_request.get_permission_query_conditions",
}

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
# 1. Pass-expiry alert: every 15 minutes, notify hosts about passes expiring
#    within the next 30 minutes and auto-mark expired passes as "Expired".
# 2. Gate log reconciliation is a Script Report ("Visitor Reconciliation") that
#    can also be run on demand or scheduled by the site administrator.
scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"visitor_pass_tracker.utils.run_pass_expiry_checks",
		],
	},
}
