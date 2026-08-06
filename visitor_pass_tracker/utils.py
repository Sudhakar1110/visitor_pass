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
	auto_revoke_overstays()


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


def auto_revoke_overstays():
	"""Auto-revoke passes whose visitor is still on-site long after the pass
	expired (Entry scan, no Exit scan, past the grace period).

	Grace period is configurable via `visitor_pass_overstay_grace_hours` in
	site_config.json (default 6 hours). The overstay alert must already have
	been sent before the pass is revoked.
	"""
	grace = (
		frappe.conf.get("visitor_pass_overstay_grace_hours")
		or frappe.flags.visitor_pass_overstay_grace_hours
		or 6
	)
	threshold = add_to_date(now_datetime(), hours=-int(grace))
	passes = frappe.get_all(
		"Entry Pass",
		filters={
			"status": "Expired",
			"valid_till": ("<", threshold),
			"overstay_alert_sent": 1,
		},
		fields=["name", "visitor", "visitor_name"],
		ignore_permissions=True,
	)

	revoked = []
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
			frappe.db.set_value(
				"Entry Pass",
				entry.name,
				{"status": "Revoked", "revoked_by": "Administrator", "revoked_on": now_datetime()},
			)
			revoked.append(entry.name)

	if revoked:
		frappe.log_error(
			title=_("Visitor Pass Tracker: auto-revoked overstays"),
			message="Auto-revoked after grace period: " + ", ".join(revoked),
		)


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


VISITOR_MATCH_FIELDS = [
	"name",
	"visitor_name",
	"phone",
	"email",
	"company_name",
	"id_proof_number",
	"modified",
]


@frappe.whitelist()
def find_matching_visitors(phone=None, id_proof_number=None):
	"""Return existing Visitors matching the given phone and/or ID proof number.

	Used by the desk / web forms / API to suggest (and auto-select) the
	existing visitor master instead of creating a duplicate. Returns a list
	of {name, visitor_name, phone, email, company_name, id_proof_number}.

	All lookups are DB-bounded (exact-index match first, then digit-prefix
	candidates, then ID-proof candidates) - the table is never fully scanned.
	"""
	phone = _normalize_phone(phone)
	id_proof = (id_proof_number or "").strip().lower()

	matches = {}

	# 1) exact phone match - uses the phone index, returns immediately
	if phone:
		for v in frappe.get_all(
			"Visitor",
			filters={"phone": phone},
			fields=VISITOR_MATCH_FIELDS,
			order_by="modified desc",
			limit_page_length=20,
		):
			matches[v.name] = v

	# 2) digit-normalized fallback: candidates whose stored phone ENDS with the
	#    digits (covers +91 / spaces / dashes), confirmed in Python on a small set
	if phone and not matches:
		for v in frappe.get_all(
			"Visitor",
			filters={"phone": ["like", f"%{phone}"]},
			fields=VISITOR_MATCH_FIELDS,
			order_by="modified desc",
			limit_page_length=200,
		):
			if _normalize_phone(v.phone) == phone:
				matches[v.name] = v

	# 3) ID proof number match
	if id_proof:
		for v in frappe.get_all(
			"Visitor",
			filters={"id_proof_number": ["like", f"%{id_proof}%"]},
			fields=VISITOR_MATCH_FIELDS,
			order_by="modified desc",
			limit_page_length=200,
		):
			if (v.id_proof_number or "").strip().lower() == id_proof:
				matches[v.name] = v

	result = sorted(matches.values(), key=lambda v: v.modified, reverse=True)[:20]
	return [
		{
			"name": v.name,
			"visitor_name": v.visitor_name,
			"phone": v.phone,
			"email": v.email,
			"company_name": v.company_name,
			"id_proof_number": v.id_proof_number,
		}
		for v in result
	]


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
	end_date = request.get("visit_end_date") or request.get("visit_date")
	valid_from = get_datetime(f"{request.get('visit_date')} {in_time}")
	valid_till = get_datetime(f"{end_date} {out_time}")

	visitor_email = company_name = None
	if request.get("visitor"):
		visitor_email, company_name = frappe.db.get_value(
			"Visitor", request.get("visitor"), ["email", "company_name"]
		) or (None, None)

	entry_pass = frappe.get_doc(
		{
			"doctype": "Entry Pass",
			"visitor_request": request.name,
			"visitor": request.get("visitor"),
			"visitor_email": visitor_email,
			"company_name": company_name,
			"host": request.get("host"),
			"host_user": request.get("host_user"),
			"location_gate": request.get("location_gate"),
			"vehicle_number": request.get("vehicle_number"),
			"is_escort_required": request.get("is_escort_required"),
			"valid_from": valid_from,
			"valid_till": valid_till,
			"status": "Active",
		}
	)
	entry_pass.insert(ignore_permissions=True)
	entry_pass.reload()
	attach_qr_code(entry_pass)

	# The pass emails (host + visitor, with the QR) are sent from here - the
	# native "New"-event notifications fire during insert(), before the QR
	# exists, so this guarantees the QR image is actually delivered.
	send_pass_notifications(entry_pass)

	# SMS the visitor the pass number (channel-ready; requires Frappe SMS
	# Settings to be configured, failures are logged silently)
	if request.get("visitor"):
		visitor_phone = frappe.db.get_value("Visitor", request.get("visitor"), "phone")
		if visitor_phone:
			_send_sms(
				visitor_phone,
				_("Your entry pass {0} for {1} is approved. Present this pass number "
				  "at gate {2}.").format(
					entry_pass.name,
					request.get("visit_date"),
					request.get("location_gate") or "the main gate",
				),
			)
	return entry_pass


def send_pass_notifications(entry_pass):
	"""Email the Entry Pass + QR to the host and the visitor after the QR code
	has been generated. Failures are logged, never raised."""
	qr_code = frappe.db.get_value("Entry Pass", entry_pass.name, "qr_code")
	subject = _("Entry Pass {0} generated for {1}").format(
		entry_pass.name, entry_pass.visitor_name
	)
	message = _(
		"<h3>Entry Pass {0} generated</h3>"
		"<p>Visitor <b>{1}</b> | Valid from <b>{2}</b> till <b>{3}</b></p>"
		"<p>Gate: <b>{4}</b></p>"
	).format(
		entry_pass.name,
		entry_pass.visitor_name or "-",
		frappe.utils.format_datetime(entry_pass.valid_from),
		frappe.utils.format_datetime(entry_pass.valid_till),
		entry_pass.location_gate or "-",
	)
	if qr_code:
		message += '<p><img src="{0}" style="width:160px;"></p>'.format(qr_code)
	attachments = [{"file_url": qr_code}] if qr_code else None

	# host - email + in-app notification
	host_user = entry_pass.host_user
	if host_user:
		try:
			host_email = frappe.db.get_value("User", host_user, "email")
			if host_email:
				frappe.sendmail(
					recipients=host_email,
					subject=subject,
					message=message,
					reference_doctype="Entry Pass",
					reference_name=entry_pass.name,
					attachments=attachments,
				)
		except frappe.OutgoingEmailError:
			frappe.log_error(
				title=_("Visitor Pass Tracker: pass email failed"),
				message=f"Entry Pass email to host {host_user} could not be sent.",
			)
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": host_user,
				"from_user": frappe.session.user or "Administrator",
				"subject": subject,
				"document_type": "Entry Pass",
				"document_name": entry_pass.name,
				"type": "Alert",
			}
		).insert(ignore_permissions=True)

	# visitor - email (no in-app log; the visitor is usually not a User)
	visitor_email = frappe.db.get_value("Visitor", entry_pass.visitor, "email") if entry_pass.visitor else None
	if visitor_email:
		try:
			frappe.sendmail(
				recipients=visitor_email,
				subject=subject,
				message=message,
				reference_doctype="Entry Pass",
				reference_name=entry_pass.name,
				attachments=attachments,
			)
		except frappe.OutgoingEmailError:
			frappe.log_error(
				title=_("Visitor Pass Tracker: visitor pass email failed"),
				message=f"Entry Pass email to visitor {entry_pass.visitor_name} could not be sent.",
			)


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
# SMS helper - uses Frappe's SMS Settings (Twilio / Exotel / MSG91 etc.)
# ---------------------------------------------------------------------------


def _send_sms(phone, message):
	"""Best-effort SMS via Frappe SMS Settings. Never raises - failures are
	logged so email/in-app notifications still cover the recipient."""
	if not phone or not message:
		return
	try:
		from frappe.core.doctype.sms_settings.sms_settings import send_sms

		send_sms(receiver_list=[phone], msg=message)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: SMS failed"),
			message=frappe.get_traceback(),
		)


def is_security_user(user=None):
	"""True for Administrator / System Manager / Security Officer / Reception."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(
		set(frappe.get_roles(user)) & {"System Manager", "Security Officer", "Reception"}
	)


@frappe.whitelist()
def send_sms_to_phone(phone=None, message=None):
	"""Whitelisted SMS sender for internal integrations (SMS Settings required).
	Restricted to security roles so the (paid) SMS gateway cannot be abused."""
	if not is_security_user():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	if not phone or not message:
		frappe.throw(_("phone and message are required"))
	_send_sms(phone, message)
	return {"status": "sent"}


# ---------------------------------------------------------------------------
# Dashboard - Number Cards (type = Custom)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_visitors_expected_today(**kwargs):
	"""Number card: Visitor Requests scheduled for today that are not yet done
	(any open/approved state, excluding Draft and Rejected)."""
	count = frappe.db.count(
		"Visitor Request",
		filters={
			"visit_date": frappe.utils.today(),
			"docstatus": 1,
			"workflow_state": ["not in", ["Rejected"]],
		},
	)
	return {"value": count, "fieldtype": "Int"}


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
