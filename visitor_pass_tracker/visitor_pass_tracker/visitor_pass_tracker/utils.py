import base64
import json
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

# ---------------------------------------------------------------------------
# Scheduler event (every 15 minutes) - Pass expiry alert + auto-expiry
# ---------------------------------------------------------------------------


def run_pass_expiry_checks():
	"""Scheduler entry point (cron: */15 * * * *).

	1. Auto-expire Entry Passes whose valid_till has passed.
	2. Alert hosts about Active passes expiring within the next 30 minutes
	   (Notification Log + Email to the host user).
	"""
	expire_passes()
	alert_hosts_of_upcoming_expiry()


def expire_passes():
	"""Mark Entry Pass as Expired once valid_till has passed."""
	now = now_datetime()
	expired = frappe.get_all(
		"Entry Pass",
		filters={"status": "Active", "valid_till": ("<", now)},
		pluck="name",
	)
	for name in expired:
		frappe.db.set_value("Entry Pass", name, "status", "Expired")

	if expired:
		frappe.log_error(
			title=_("Visitor Pass Tracker: auto-expired passes"),
			message="Auto-expired: " + ", ".join(expired),
		)


def alert_hosts_of_upcoming_expiry():
	"""Notify the host when an Active pass is due to expire within 30 minutes."""
	now = now_datetime()
	threshold = add_to_date(now, minutes=30)
	passes = frappe.get_all(
		"Entry Pass",
		filters={
			"status": "Active",
			"valid_till": ["between", [now, threshold]],
			"expiry_alert_sent": 0,
		},
		fields=["name", "visitor", "visitor_request", "valid_till", "host_user"],
	)

	for entry in passes:
		if not entry.host_user:
			continue

		subject = _("Visitor Pass {0} is expiring soon").format(entry.name)
		message = _(
			"<p>Visitor Pass <b>{0}</b> is valid only till <b>{1}</b> "
			"(about 30 minutes from now).</p>"
			"<p>Please make sure the visitor completes their visit and exits "
			"through the gate before the pass expires, or contact Security "
			"to have it extended.</p>"
		).format(entry.name, frappe.utils.format_datetime(entry.valid_till))

		# In-app notification (shows up in the standard notification bell)
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": entry.host_user,
				"from_user": frappe.session.user or "Administrator",
				"subject": subject,
				"document_type": "Entry Pass",
				"document_name": entry.name,
				"type": "Alert",
			}
		).insert(ignore_permissions=True)

		# Email to the host (route via existing Email integrations / SMS gateways)
		try:
			host_email = frappe.db.get_value("User", entry.host_user, "email")
			if host_email:
				frappe.sendmail(
					recipients=host_email,
					subject=subject,
					message=message,
					reference_doctype="Entry Pass",
					reference_name=entry.name,
				)
		except frappe.OutgoingEmailError:
			frappe.log_error(
				title=_("Visitor Pass Tracker: outgoing email failed"),
				message=f"Expiry alert email for Entry Pass {entry.name} could not be sent.",
			)

		frappe.db.set_value("Entry Pass", entry.name, "expiry_alert_sent", 1)


# ---------------------------------------------------------------------------
# Blacklist auto-check
# ---------------------------------------------------------------------------


def _normalize_phone(phone):
	"""Phone matching helper - compare only digits (ignores +, spaces, dashes)."""
	return "".join(ch for ch in (phone or "") if ch.isdigit())


def check_blacklist(visitor):
	"""Return True if the visitor matches an Active Blacklisted Visitor record.

	`visitor` can be a Visitor Document/dict with `name`, `phone` and
	`id_proof_number`. Matching is done on phone and id_proof_number
	(independently) plus a direct Visitor link.
	"""
	phone = _normalize_phone(visitor.get("phone"))
	id_proof = (visitor.get("id_proof_number") or "").strip().lower()
	visitor_name = visitor.get("name")

	records = frappe.get_all(
		"Blacklisted Visitor",
		filters={"status": "Active"},
		fields=["name", "visitor", "phone", "id_proof_number"],
	)
	for rec in records:
		if visitor_name and rec.get("visitor") == visitor_name:
			return True
		if phone and _normalize_phone(rec.get("phone")) == phone:
			return True
		if id_proof and (rec.get("id_proof_number") or "").strip().lower() == id_proof:
			return True
	return False


# ---------------------------------------------------------------------------
# Entry Pass creation (on final workflow approval) + QR code generation
# ---------------------------------------------------------------------------


def create_entry_pass_for_request(request):
	"""Create the Entry Pass + QR code when a Visitor Request is Approved.

	Idempotent - a request can only ever have one Entry Pass.
	"""
	if frappe.db.exists("Entry Pass", {"visitor_request": request.name}):
		return None

	in_time = request.get("expected_in_time") or "09:00:00"
	out_time = request.get("expected_out_time") or "18:00:00"
	valid_from = get_datetime(f"{request.get('visit_date')} {in_time}")
	valid_till = get_datetime(f"{request.get('visit_date')} {out_time}")

	entry_pass = frappe.get_doc(
		{
			"doctype": "Entry Pass",
			"visitor_request": request.name,
			"visitor": request.get("visitor"),
			"host": request.get("host"),
			"host_user": request.get("host_user"),
			"location_gate": request.get("location_gate"),
			"valid_from": valid_from,
			"valid_till": valid_till,
			"status": "Active",
		}
	)
	entry_pass.insert(ignore_permissions=True)
	entry_pass.reload()
	attach_qr_code(entry_pass)
	return entry_pass


def attach_qr_code(entry_pass):
	"""Generate a QR image for the Entry Pass and store it in `qr_code`.

	The QR payload is a JSON string the gate scanner hardware can POST straight
	back to the scan API. PNG is preferred (pyqrcode + pypng, both bundled with
	Frappe 15 / ERPNext 15); falls back to SVG if pypng is unavailable.
	"""
	try:
		import pyqrcode
	except ImportError:
		frappe.log_error(
			title=_("Visitor Pass Tracker: pyqrcode not available"),
			message=f"Could not generate QR for Entry Pass {entry_pass.name}",
		)
		return

	payload = json.dumps(
		{
			"type": "entry_pass",
			"entry_pass": entry_pass.name,
			"visitor": entry_pass.visitor,
			"valid_till": str(entry_pass.valid_till),
		}
	)
	qr = pyqrcode.create(payload)

	buffer = BytesIO()
	try:
		# requires pypng (a dependency of this app and of ERPNext)
		qr.png(buffer, scale=6, module_color=[0, 0, 0, 255], background=[255, 255, 255, 255])
		extension = "png"
	except Exception:
		# graceful fallback to SVG (no extra dependency)
		buffer = BytesIO()
		qr.svg(buffer, scale=6, background="#ffffff", module_color="#000000")
		extension = "svg"

	content = base64.b64encode(buffer.getvalue()).decode("utf-8")

	from frappe.utils.file_manager import save_file

	file_doc = save_file(
		f"entry_pass_qr_{entry_pass.name}.{extension}",
		content,
		"Entry Pass",
		entry_pass.name,
		decode=True,
		is_private=1,
		df="qr_code",
	)
	frappe.db.set_value("Entry Pass", entry_pass.name, "qr_code", file_doc.file_url)


# ---------------------------------------------------------------------------
# Dashboard - Number Cards (type = Custom)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_visitors_on_site(**kwargs):
	"""Number card: Active Entry Passes that have an Entry scan but no Exit scan."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT gle.entry_pass
		FROM `tabGate Log Entry` gle
		WHERE gle.scan_type = 'Entry' AND gle.docstatus < 2
		  AND NOT EXISTS (
			SELECT 1 FROM `tabGate Log Entry` gle2
			WHERE gle2.entry_pass = gle.entry_pass
			  AND gle2.scan_type = 'Exit'
			  AND gle2.docstatus < 2
		  )
		""",
		as_list=True,
	)
	if not rows:
		return {"value": 0, "fieldtype": "Int"}

	pass_names = [row[0] for row in rows if row[0]]
	count = frappe.db.count(
		"Entry Pass", filters={"status": "Active", "name": ["in", pass_names]}
	)
	return {"value": count, "fieldtype": "Int"}


@frappe.whitelist()
def get_passes_expiring_in_next_hour(**kwargs):
	"""Number card: Active Entry Passes expiring within the next hour."""
	now = now_datetime()
	end = add_to_date(now, hours=1)
	count = frappe.db.count(
		"Entry Pass",
		filters={"status": "Active", "valid_till": ["between", [now, end]]},
	)
	return {"value": count, "fieldtype": "Int"}


# ---------------------------------------------------------------------------
# Dashboard - Charts (Dashboard Chart Source)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_peak_visit_hours(**kwargs):
	"""Line chart: number of gate scans (entries + exits) per hour of the day."""
	rows = frappe.db.sql(
		"""
		SELECT HOUR(scan_time) AS hour_of_day, COUNT(*) AS count
		FROM `tabGate Log Entry`
		WHERE scan_time IS NOT NULL AND docstatus < 2
		GROUP BY HOUR(scan_time)
		ORDER BY hour_of_day
		""",
		as_dict=True,
	)
	counts = {row["hour_of_day"]: row["count"] for row in rows}
	labels = [f"{hour:02d}:00" for hour in range(24)]
	values = [counts.get(hour, 0) for hour in range(24)]
	return {
		"labels": labels,
		"datasets": [{"name": _("Gate Scans"), "values": values}],
	}
