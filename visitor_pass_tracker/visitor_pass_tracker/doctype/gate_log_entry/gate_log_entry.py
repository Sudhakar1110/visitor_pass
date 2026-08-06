import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class GateLogEntry(Document):
	def validate(self):
		self.validate_scan()
		self.set_linked_fields()
		if not self.source:
			self.source = "Desk"

	def validate_scan(self):
		if not self.scan_time:
			self.scan_time = now_datetime()
		if self.scan_type not in ("Entry", "Exit"):
			frappe.throw(_("Scan Type must be Entry or Exit"))
		if not self.gate:
			frappe.throw(_("Gate is mandatory"))

		self.is_authorized = self.check_pass_authorization() if self.entry_pass else 0
		if self.is_authorized:
			self.remarks = ""
		else:
			self.remarks = _("Unauthorized scan - no matching valid Entry Pass")

	def check_pass_authorization(self):
		"""A scan is authorized when:
		- Entry scan: pass is Active and scan_time falls inside the validity window
		- Exit scan: pass exists and is not revoked (always recorded for tracking)
		"""
		pass_doc = frappe.get_doc("Entry Pass", self.entry_pass)
		if pass_doc.status == "Revoked":
			return False
		if self.scan_type == "Entry":
			return (
				pass_doc.status == "Active"
				and pass_doc.valid_from <= self.scan_time <= pass_doc.valid_till
			)
		return True

	def set_linked_fields(self):
		if self.entry_pass:
			values = frappe.db.get_value(
				"Entry Pass",
				self.entry_pass,
				["visitor", "visitor_request", "visitor_name", "host_user"],
			)
			if values:
				self.visitor, self.visitor_request, self.visitor_name, self.host_user = values


# ---------------------------------------------------------------------------
# Gate scanner API - RFID / QR readers POST scan events here
# ---------------------------------------------------------------------------


@frappe.whitelist()
def submit_scan(
	entry_pass=None,
	gate=None,
	scan_type="Entry",
	scan_time=None,
	scanned_by_device=None,
	token=None,
):
	"""Accept a scan event from gate scanner hardware (QR/RFID reader/turnstile).

	Endpoint (POST, requires a logged-in service user / API key):
	    /api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.submit_scan

	Optional hardening: set `visitor_pass_api_token` in site_config.json and pass
	it as the `token` parameter - requests without the matching token are rejected.

	`gate` may be the Gate name or the Gate's device_id.
	`entry_pass` may be the Entry Pass name or the scanned QR content (a
	scannable portal URL; the legacy JSON payload is also accepted).
	"""
	_validate_api_token(token)

	scan_type = (scan_type or "Entry").strip()
	if scan_type not in ("Entry", "Exit"):
		frappe.throw(_("scan_type must be Entry or Exit"))

	gate_name = _resolve_gate(gate)
	pass_name = _resolve_entry_pass(entry_pass)
	scan_datetime = get_datetime(scan_time) if scan_time else now_datetime()

	# Duplicate-scan guard: a visitor already inside (Entry scan, no Exit scan)
	# must not generate endless Entry logs - answer "duplicate" instead.
	# Only applies while the pass is still valid: a Revoked / Expired pass is
	# reported as "unauthorized" so the gate keeps denying entry.
	if scan_type == "Entry" and pass_name:
		pass_status = frappe.db.get_value("Entry Pass", pass_name, "status")
		has_entry = frappe.db.exists(
			"Gate Log Entry",
			{"entry_pass": pass_name, "scan_type": "Entry", "docstatus": ("<", 2)},
		)
		if has_entry and pass_status == "Active":
			has_exit = frappe.db.exists(
				"Gate Log Entry",
				{"entry_pass": pass_name, "scan_type": "Exit", "docstatus": ("<", 2)},
			)
			if not has_exit:
				return {
					"status": "duplicate",
					"entry_pass": pass_name,
					"gate": gate_name,
					"scan_type": scan_type,
					"scan_time": str(scan_datetime),
					"message": _("Visitor already inside - duplicate Entry scan ignored"),
				}

	log = frappe.get_doc(
		{
			"doctype": "Gate Log Entry",
			"entry_pass": pass_name or None,
			"gate": gate_name,
			"scan_type": scan_type,
			"scan_time": scan_datetime,
			"scanned_by_device": scanned_by_device,
			"source": "Gate Device",
		}
	)
	log.insert(ignore_permissions=True)

	# an authorized exit completes the visit
	if log.is_authorized and scan_type == "Exit" and pass_name:
		frappe.db.set_value("Entry Pass", pass_name, "status", "Used")

	# an authorized entry notifies the host - in-app/email via the native
	# Notification fixture, plus an SMS to the host's mobile (best-effort)
	if log.is_authorized and scan_type == "Entry" and pass_name:
		_notify_host_sms(log)
		_notify_vip_arrival(log)

	frappe.db.commit()

	return {
		"status": "authorized" if log.is_authorized else "unauthorized",
		"entry_pass": pass_name,
		"gate": gate_name,
		"scan_type": scan_type,
		"scan_time": str(log.scan_time),
	}


def _validate_api_token(token):
	expected = frappe.conf.get("visitor_pass_api_token")
	if expected and token != expected:
		frappe.throw(_("Invalid or missing API token"), frappe.AuthenticationError)


def _notify_host_sms(log):
	"""Best-effort SMS to the host's mobile when their visitor arrives."""
	if not log.host_user:
		return
	employee_phone = frappe.db.get_value(
		"Employee", {"user_id": log.host_user}, "cell_number"
	)
	if not employee_phone:
		return
	from visitor_pass_tracker.utils import _send_sms

	_send_sms(
		employee_phone,
		_("Your visitor {0} ({1}) has arrived at gate {2} at {3}.").format(
			log.visitor_name or "-",
			log.entry_pass or "-",
			log.gate,
			frappe.utils.format_datetime(log.scan_time),
		),
	)


def _notify_vip_arrival(log):
	"""VIP visitors get an instant alert (Security role + host, in-app/email +
	SMS) the moment they scan in. Best-effort - never raises."""
	if not log.visitor or not frappe.db.exists("Visitor", log.visitor):
		return
	if not frappe.db.get_value("Visitor", log.visitor, "is_vip"):
		return
	try:
		from visitor_pass_tracker.utils import _notify_users, _role_users, _send_sms

		subject = _("VIP ARRIVED: {0}").format(log.visitor_name or log.visitor)
		message = _(
			"<p>VIP visitor <b>{0}</b> ({1}) has arrived at gate <b>{2}</b> "
			"at <b>{3}</b>.</p><p>Escort and reception please note.</p>"
		).format(
			log.visitor_name or "-",
			log.entry_pass or "-",
			log.gate,
			frappe.utils.format_datetime(log.scan_time),
		)
		_notify_users(
			_role_users(["Security Officer"]), subject, message, "Gate Log Entry", log.name
		)
		if log.host_user:
			_notify_users({log.host_user}, subject, message, "Gate Log Entry", log.name)
			employee_phone = frappe.db.get_value(
				"Employee", {"user_id": log.host_user}, "cell_number"
			)
			if employee_phone:
				_send_sms(
					employee_phone,
					_("VIP visitor {0} has arrived at gate {1}.").format(
						log.visitor_name or "-", log.gate
					),
				)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: VIP arrival alert failed"),
			message=frappe.get_traceback(),
		)


def _resolve_gate(gate):
	if not gate:
		frappe.throw(_("Gate is required"))
	name = frappe.db.get_value("Gate", gate, "name")
	if not name:
		name = frappe.db.get_value("Gate", {"device_id": gate}, "name")
	if not name:
		frappe.throw(_("Gate not found: {0}").format(gate))
	return name


def _resolve_entry_pass(entry_pass):
	if not entry_pass:
		return None
	if frappe.db.exists("Entry Pass", entry_pass):
		return entry_pass
	# legacy format: scanners may post the whole QR payload back (a JSON string)
	try:
		payload = frappe.parse_json(entry_pass)
		if isinstance(payload, dict) and payload.get("entry_pass"):
			name = payload["entry_pass"]
			if frappe.db.exists("Entry Pass", name):
				return name
	except Exception:
		pass
	# current format: the QR encodes a scannable portal URL
	# (/visitor_portal?pass=PASS-...) so phone cameras open the pass page
	# instead of raw text - gate hardware posts the URL back as-is
	try:
		from urllib.parse import parse_qs, urlparse

		parsed = urlparse(entry_pass)
		if parsed.scheme in ("http", "https") or parsed.path == "/visitor_portal":
			names = parse_qs(parsed.query).get("pass")
			if names:
				name = names[0]
				if frappe.db.exists("Entry Pass", name):
					return name
	except Exception:
		pass
	return None


@frappe.whitelist()
def revoke_pass(entry_pass=None, remarks=None, token=None):
	"""Instantly revoke an Entry Pass (incident response).

	Endpoint (POST, requires a logged-in service user / API key):
	    /api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.revoke_pass

	Same optional hardening as submit_scan - set `visitor_pass_api_token` in
	site_config.json and pass it as the `token` parameter.
	"""
	_validate_api_token(token)
	pass_name = _resolve_entry_pass(entry_pass)
	if not pass_name:
		frappe.throw(_("Entry Pass not found: {0}").format(entry_pass))

	pass_doc = frappe.get_doc("Entry Pass", pass_name)
	pass_doc.status = "Revoked"
	pass_doc.revoked_by = frappe.session.user
	pass_doc.revoked_on = now_datetime()
	pass_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "revoked",
		"entry_pass": pass_name,
		"remarks": remarks,
		"revoked_by": frappe.session.user,
		"revoked_on": str(pass_doc.revoked_on),
	}


@frappe.whitelist()
def manual_exit(entry_pass=None, gate=None, remarks=None, token=None, force=0):
	"""Close a visit without a gate scan (lost pass / manual exit).

	Logs an Exit Gate Log Entry (remarks recorded) and marks the pass Used,
	matching what an authorized exit scan does.

	Endpoint (POST, requires a logged-in service user / API key):
	    /api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.manual_exit

	`gate` may be the Gate name or the Gate's device_id; defaults to the
	pass's own location_gate. Pass `force=1` to close a visit that has no
	Entry scan (visitor never entered) - by default this is rejected.
	"""
	_validate_api_token(token)
	pass_name = _resolve_entry_pass(entry_pass)
	if not pass_name:
		frappe.throw(_("Entry Pass not found: {0}").format(entry_pass))

	force = int(force or 0)
	pass_doc = frappe.get_doc("Entry Pass", pass_name)
	if pass_doc.status == "Revoked":
		frappe.throw(_("Entry Pass {0} is revoked").format(pass_name))
	if pass_doc.status == "Used":
		frappe.throw(_("Entry Pass {0} is already used - visit already closed").format(pass_name))

	# Guard: don't close a visit that never started (no Entry scan) unless
	# Security explicitly forces it.
	if not force:
		has_entry = frappe.db.exists(
			"Gate Log Entry",
			{"entry_pass": pass_name, "scan_type": "Entry", "docstatus": ("<", 2)},
		)
		if not has_entry:
			frappe.throw(
				_("No Entry scan found for {0} - this visitor never entered the premises. "
				  "Pass force=1 to close the visit anyway.").format(pass_name)
			)

	gate_name = _resolve_gate(gate) if gate else None
	gate_name = gate_name or pass_doc.location_gate
	if not gate_name:
		frappe.throw(_("Gate is required to close the visit (pass has no Location / Gate)"))

	actor = frappe.session.user
	final_remarks = remarks or _("Manual exit - visit closed by Security")
	final_remarks = "{0} [by {1}]".format(final_remarks, actor)

	log = frappe.get_doc(
		{
			"doctype": "Gate Log Entry",
			"entry_pass": pass_name,
			"gate": gate_name,
			"scan_type": "Exit",
			"scan_time": now_datetime(),
			"remarks": final_remarks,
			"source": "Manual Exit",
		}
	)
	log.insert(ignore_permissions=True)
	frappe.db.set_value("Entry Pass", pass_name, "status", "Used")
	frappe.db.commit()

	return {
		"status": "used",
		"entry_pass": pass_name,
		"gate": gate_name,
		"scan_time": str(log.scan_time),
		"by": actor,
	}


@frappe.whitelist()
def extend_pass(entry_pass=None, valid_till=None, remarks=None, token=None):
	"""Extend the validity of an Entry Pass (late-running visit).

	Endpoint (POST, requires a logged-in service user / API key):
	    /api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.extend_pass

	Pass the new `valid_till` datetime. An already-expired pass is reactivated
	if the new validity is in the future. Same optional hardening as
	submit_scan - set `visitor_pass_api_token` in site_config.json and pass it
	as the `token` parameter.
	"""
	_validate_api_token(token)
	pass_name = _resolve_entry_pass(entry_pass)
	if not pass_name:
		frappe.throw(_("Entry Pass not found: {0}").format(entry_pass))

	pass_doc = frappe.get_doc("Entry Pass", pass_name)
	if pass_doc.status == "Revoked":
		frappe.throw(_("Entry Pass {0} is revoked and cannot be extended").format(pass_name))
	if pass_doc.status == "Used":
		frappe.throw(_("Entry Pass {0} is already used - visit already closed").format(pass_name))

	if not valid_till:
		frappe.throw(_("valid_till is required to extend the pass"))
	new_till = get_datetime(valid_till)
	if new_till <= pass_doc.valid_from:
		frappe.throw(_("New Valid Till must be after Valid From"))

	was_expired = pass_doc.status == "Expired"
	pass_doc.valid_till = new_till
	if was_expired and new_till > now_datetime():
		pass_doc.status = "Active"
	pass_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "extended",
		"entry_pass": pass_name,
		"valid_till": str(pass_doc.valid_till),
		"remarks": remarks,
	}


# ---------------------------------------------------------------------------
# Permissions - scoped via the has_permission / permission_query_conditions
# hooks registered in hooks.py. Employees see the logs of their own visits
# only; Department Heads see their department's visits.
# ---------------------------------------------------------------------------


def has_permission(doc, ptype, user=None, debug=False):
	from visitor_pass_tracker.utils import get_user_scope

	user = user or frappe.session.user
	scope = get_user_scope(user)
	if scope["full_access"]:
		return True
	if doc.get("host_user") == user:
		return True
	if doc.get("visitor_request") and frappe.db.get_value(
		"Visitor Request", doc.visitor_request, "owner"
	) == user:
		return True
	if doc.get("entry_pass") and frappe.db.exists("Entry Pass", doc.entry_pass):
		from visitor_pass_tracker.visitor_pass_tracker.doctype.entry_pass.entry_pass import (
			has_permission as entry_pass_has_permission,
		)

		return entry_pass_has_permission(frappe.get_doc("Entry Pass", doc.entry_pass), ptype, user=user)
	return False


def get_permission_query_conditions(user, doctype=None):
	from visitor_pass_tracker.utils import get_user_scope

	scope = get_user_scope(user)
	if scope["full_access"]:
		return ""
	user = user or frappe.session.user
	alternatives = []

	if scope["employee"]:
		alternatives.append(
			"`tabGate Log Entry`.`host_user` = {0}".format(frappe.db.escape(user))
		)
		alternatives.append(
			"`tabGate Log Entry`.`entry_pass` IN (SELECT `name` FROM `tabEntry Pass` "
			"WHERE `host` = {0} OR `host_user` = {1})".format(
				frappe.db.escape(scope["employee"]), frappe.db.escape(user)
			)
		)

	alternatives.append(
		"`tabGate Log Entry`.`visitor_request` IN (SELECT `name` FROM `tabVisitor Request` "
		"WHERE `owner` = {0})".format(frappe.db.escape(user))
	)

	if "Department Head" in frappe.get_roles(user) and scope["departments"]:
		alternatives.append(
			"`tabGate Log Entry`.`visitor_request` IN (SELECT `name` FROM `tabVisitor Request` "
			"WHERE `department` IN ({0}))".format(
				", ".join(frappe.db.escape(d) for d in scope["departments"])
			)
		)

	if not alternatives:
		return "1=0"
	return "(" + " OR ".join(alternatives) + ")"
