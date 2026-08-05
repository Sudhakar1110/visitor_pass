import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime


class GateLogEntry(Document):
	def validate(self):
		self.validate_scan()
		self.set_linked_fields()

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
	`entry_pass` may be the Entry Pass name or the full QR payload (JSON string).
	"""
	_validate_api_token(token)

	scan_type = (scan_type or "Entry").strip()
	if scan_type not in ("Entry", "Exit"):
		frappe.throw(_("scan_type must be Entry or Exit"))

	gate_name = _resolve_gate(gate)
	pass_name = _resolve_entry_pass(entry_pass)
	scan_datetime = get_datetime(scan_time) if scan_time else now_datetime()

	log = frappe.get_doc(
		{
			"doctype": "Gate Log Entry",
			"entry_pass": pass_name or None,
			"gate": gate_name,
			"scan_type": scan_type,
			"scan_time": scan_datetime,
			"scanned_by_device": scanned_by_device,
		}
	)
	log.insert(ignore_permissions=True)

	# an authorized exit completes the visit
	if log.is_authorized and scan_type == "Exit" and pass_name:
		frappe.db.set_value("Entry Pass", pass_name, "status", "Used")

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
	# scanners may post the whole QR payload back (it is a JSON string)
	try:
		payload = frappe.parse_json(entry_pass)
		if isinstance(payload, dict) and payload.get("entry_pass"):
			name = payload["entry_pass"]
			if frappe.db.exists("Entry Pass", name):
				return name		except Exception:
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
	pass_doc.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.log_error(
		title=_("Visitor Pass Tracker: pass revoked"),
		message="Entry Pass {0} revoked by {1}. Remarks: {2}".format(
			pass_name, frappe.session.user, remarks or "-"
		),
	)
	return {"status": "revoked", "entry_pass": pass_name, "remarks": remarks}


@frappe.whitelist()
def manual_exit(entry_pass=None, gate=None, remarks=None, token=None):
	"""Close a visit without a gate scan (lost pass / manual exit).

	Logs an Exit Gate Log Entry (remarks recorded) and marks the pass Used,
	matching what an authorized exit scan does.

	Endpoint (POST, requires a logged-in service user / API key):
	    /api/method/visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.manual_exit

	`gate` may be the Gate name or the Gate's device_id; defaults to the
	pass's own location_gate.
	"""
	_validate_api_token(token)
	pass_name = _resolve_entry_pass(entry_pass)
	if not pass_name:
		frappe.throw(_("Entry Pass not found: {0}").format(entry_pass))

	pass_doc = frappe.get_doc("Entry Pass", pass_name)
	if pass_doc.status == "Revoked":
		frappe.throw(_("Entry Pass {0} is revoked").format(pass_name))

	gate_name = _resolve_gate(gate) if gate else None
	gate_name = gate_name or pass_doc.location_gate
	if not gate_name:
		frappe.throw(_("Gate is required to close the visit (pass has no Location / Gate)"))

	log = frappe.get_doc(
		{
			"doctype": "Gate Log Entry",
			"entry_pass": pass_name,
			"gate": gate_name,
			"scan_type": "Exit",
			"scan_time": now_datetime(),
			"remarks": remarks or _("Manual exit - visit closed by Security"),
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
	}
