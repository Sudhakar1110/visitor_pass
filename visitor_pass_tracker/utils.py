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
	3. Alert the Security Officer role about overstaying visitors (entry scan
	   present, no exit scan, past valid_till).
	"""
	expire_passes()
	alert_hosts_of_upcoming_expiry()
	alert_security_of_overstays()


def expire_passes():
	"""Mark Entry Pass as Expired once valid_till has passed."""
	now = now_datetime()
	expired = frappe.get_all(
		"Entry Pass",
		filters={"status": "Active", "valid_till": ("<", now)},
		pluck="name",
		# scheduler runs as Administrator, but stay robust regardless of the
		# session user - the entry-pass permission scoping must not block it
		ignore_permissions=True,
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
		ignore_permissions=True,
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


def alert_security_of_overstays():
	"""Alert the Security Officer role about visitors still on-site after their
	pass expired (entry scan exists, no exit scan, past valid_till).
	Deduplicated via the hidden `overstay_alert_sent` flag.
	"""
	now = now_datetime()
	# NOTE: run_pass_expiry_checks() calls expire_passes() first, which marks
	# every pass past its valid_till as "Expired" - so overstay detection must
	# look at BOTH Active and Expired passes, otherwise it would never fire.
	passes = frappe.get_all(
		"Entry Pass",
		filters={
			"status": ["in", ["Active", "Expired"]],
			"valid_till": ("<", now),
			"overstay_alert_sent": 0,
		},
		fields=["name", "visitor", "visitor_name", "location_gate", "valid_till"],
		ignore_permissions=True,
	)

	overstayers = []
	for entry in passes:
		has_entry = frappe.db.exists(
			"Gate Log Entry",
			{"entry_pass": entry.name, "scan_type": "Entry", "docstatus": ("<", 2)},
		)
		has_exit = frappe.db.exists(
			"Gate Log Entry",
			{"entry_pass": entry.name, "scan_type": "Exit", "docstatus": ("<", 2)},
		)
		if has_entry and not has_exit:
			overstayers.append(entry)

	if not overstayers:
		return

	security_users = frappe.get_all(
		"Has Role", filters={"role": "Security Officer"}, pluck="parent"
	)
	security_users = {
		u for u in security_users if u and frappe.db.get_value("User", u, "enabled")
	}

	for entry in overstayers:
		try:
			subject = _("Overstay: visitor {0} may still be on-site").format(
				entry.visitor_name or entry.visitor or entry.name
			)
			message = _(
				"<p>Entry Pass <b>{0}</b> (visitor <b>{1}</b>) expired at <b>{2}</b> but the "
				"visitor has an entry scan and no exit scan - they may still be on-site at "
				"<b>{3}</b>.</p><p>Please check the gate area and reconcile the visit.</p>"
			).format(
				entry.name,
				entry.visitor_name or entry.visitor or "-",
				frappe.utils.format_datetime(entry.valid_till),
				entry.location_gate or "-",
			)
			for user in security_users:
				frappe.get_doc(
					{
						"doctype": "Notification Log",
						"for_user": user,
						"from_user": "Administrator",
						"subject": subject,
						"document_type": "Entry Pass",
						"document_name": entry.name,
						"type": "Alert",
					}
				).insert(ignore_permissions=True)
				try:
					email = frappe.db.get_value("User", user, "email")
					if email:
						frappe.sendmail(
							recipients=email,
							subject=subject,
							message=message,
							reference_doctype="Entry Pass",
							reference_name=entry.name,
						)
				except frappe.OutgoingEmailError:
					frappe.log_error(
						title=_("Visitor Pass Tracker: overstay email failed"),
						message=f"Overstay alert email for Entry Pass {entry.name} could not be sent.",
					)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: overstay alert failed"),
				message=frappe.get_traceback(),
			)
		finally:
			frappe.db.set_value("Entry Pass", entry.name, "overstay_alert_sent", 1)


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
# Duplicate detection - find the existing Visitor master
# ---------------------------------------------------------------------------


@frappe.whitelist()
def find_matching_visitors(phone=None, id_proof_number=None):
	"""Return existing Visitors matching the given phone and/or ID proof number.

	Used by the desk / web forms / API to suggest (and auto-select) the
	existing visitor master instead of creating a duplicate. Returns a list
	of {name, visitor_name, phone, email, company_name, id_proof_number}.
	"""
	matches = []
	visitors = frappe.get_all(
		"Visitor",
		fields=[
			"name",
			"visitor_name",
			"phone",
			"email",
			"company_name",
			"id_proof_number",
		],
		order_by="modified desc",
	)
	phone = _normalize_phone(phone)
	id_proof = (id_proof_number or "").strip().lower()
	if phone:
		# fast path - exact phone match first (stored value equals the raw query)
		exact_matches = frappe.get_all(
			"Visitor",
			filters={"phone": phone},
			fields=["name", "visitor_name", "phone", "email", "company_name", "id_proof_number"],
		)
		if exact_matches:
			return exact_matches[:20]
	for v in visitors:
		if phone and _normalize_phone(v.phone) == phone:
			matches.append(v)
		elif id_proof and (v.id_proof_number or "").strip().lower() == id_proof:
			matches.append(v)
		if len(matches) >= 20:
			break
	return matches


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


# ---------------------------------------------------------------------------
# Data-visibility scoping - shared by the permission hooks (Entry Pass / Gate
# Log Entry) and the script reports. Non-security users are restricted to
# their own visits (Employee/host) or their department's visits (Department
# Head, including sub-departments).
# ---------------------------------------------------------------------------


def get_user_scope(user=None):
	"""Return a dict describing the visitor-data visibility of a user.

	- full_access: True for Administrator / System Manager / Security Officer / Reception
	- employee: the user's Employee record (or None)
	- departments: departments headed by the user, including sub-departments
	"""
	user = user or frappe.session.user
	scope = {"full_access": False, "employee": None, "departments": []}
	if user == "Administrator":
		scope["full_access"] = True
		return scope
	roles = frappe.get_roles(user)
	if any(role in roles for role in ("System Manager", "Security Officer", "Reception")):
		scope["full_access"] = True
		return scope

	scope["employee"] = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if "Department Head" in roles and scope["employee"]:
		headed = frappe.get_all(
			"Department",
			filters={"head_of_department": scope["employee"]},
			fields=["name", "lft", "rgt"],
		)
		if headed:
			tree = frappe.get_all("Department", fields=["name", "lft", "rgt"])
			scope["departments"] = [
				d.name for d in tree if any(d.lft >= h.lft and d.rgt <= h.rgt for h in headed)
			]
	return scope


def get_pass_scope_condition(alias="ep", user=None):
	"""SQL condition restricting an Entry-Pass-alias query to what the user may
	see. Returns (None, {}) for full access; otherwise a parenthesised SQL
	snippet plus its parameters (for use with frappe.db.sql).

	A user sees passes they host (host / host_user), passes they created
	(request owner) and - for Department Heads - passes in their departments.
	"""
	scope = get_user_scope(user)
	if scope["full_access"]:
		return None, {}

	user = user or frappe.session.user
	params = {}
	alternatives = []

	if scope["employee"]:
		alternatives.append(
			f"(`{alias}`.`host` = %(vpt_employee)s OR `{alias}`.`host_user` = %(vpt_user)s)"
		)
		params["vpt_employee"] = scope["employee"]
		params["vpt_user"] = user

	# the requester may not be the host (e.g. Reception raised it) - the owner
	# of the linked request may see the pass too
	alternatives.append(
		f"`{alias}`.`visitor_request` IN ("
		f"SELECT `name` FROM `tabVisitor Request` WHERE `owner` = %(vpt_owner)s)"
	)
	params["vpt_owner"] = user

	if "Department Head" in frappe.get_roles(user):
		if scope["departments"]:
			alternatives.append(
				f"`{alias}`.`visitor_request` IN ("
				f"SELECT `name` FROM `tabVisitor Request` "
				f"WHERE `department` IN %(vpt_departments)s)"
			)
			params["vpt_departments"] = scope["departments"]
		# no departments headed -> the department alternative simply does not
		# apply; other alternatives may still match

	if not alternatives:
		return "1=0", {}
	return "(" + " OR ".join(alternatives) + ")", params
