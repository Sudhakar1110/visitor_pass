import frappe
from frappe import _
from frappe.model.document import Document

from visitor_pass_tracker.utils import attach_qr_code, is_security_user, send_pass_notifications


class EntryPass(Document):
	def validate(self):
		self.validate_validity_window()

	def validate_validity_window(self):
		if self.valid_from and self.valid_till and self.valid_till <= self.valid_from:
			frappe.throw(_("Valid Till must be after Valid From"))


@frappe.whitelist()
def resend_pass(entry_pass):
	"""Re-send an Active Entry Pass to the host and visitor: email with the QR
	image + calendar invite, plus an SMS of the pass number (best-effort).

	Used from the Entry Pass form ("Resend Pass" button) when a visitor lost
	their QR code or never received the original email. Returns a summary of
	what was delivered (`sent`) and what had no recipient / failed (`skipped`).
	"""
	if not entry_pass or not frappe.db.exists("Entry Pass", entry_pass):
		frappe.throw(_("Entry Pass {0} not found").format(entry_pass))

	doc = frappe.get_doc("Entry Pass", entry_pass)
	if not doc.has_permission("read"):
		frappe.throw(_("Not permitted to view this Entry Pass"), frappe.PermissionError)

	# Re-sending fires paid emails/SMS - restrict to the security roles or the
	# pass's own host / request owner (mirrors the send_sms_to_phone gate).
	user = frappe.session.user
	request_owner = None
	if doc.visitor_request:
		request_owner = frappe.db.get_value("Visitor Request", doc.visitor_request, "owner")
	if not is_security_user(user) and not (
		doc.host_user == user or (request_owner and request_owner == user)
	):
		frappe.throw(_("Not permitted to re-send this pass"), frappe.PermissionError)

	if doc.status != "Active":
		frappe.throw(
			_("Only Active passes can be re-sent (this pass is {0}).").format(doc.status)
		)

	# make sure the QR image exists before it is attached to the email
	if not doc.qr_code:
		attach_qr_code(doc)
		doc.reload()

	sent, skipped = [], []
	send_pass_notifications(doc, sent=sent, skipped=skipped)
	return {"status": "ok", "entry_pass": doc.name, "sent": sent, "skipped": skipped}


# ---------------------------------------------------------------------------
# Permissions - scoped via the has_permission / permission_query_conditions
# hooks registered in hooks.py. Employees see only passes they host (or
# created); Department Heads see their department's passes.
# ---------------------------------------------------------------------------


def has_permission(doc, ptype, user=None, debug=False):
	from visitor_pass_tracker.utils import get_user_scope

	user = user or frappe.session.user
	scope = get_user_scope(user)
	if scope["full_access"]:
		return True
	if doc.get("host_user") == user:
		return True
	if scope["employee"] and doc.get("host") == scope["employee"]:
		return True
	if doc.get("visitor_request"):
		req = frappe.db.get_value(
			"Visitor Request", doc.visitor_request, ["owner", "department"], as_dict=True
		)
		if req:
			if req.owner == user:
				return True
			if "Department Head" in frappe.get_roles(user) and req.department in scope["departments"]:
				return True
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
			"(`tabEntry Pass`.`host` = {0} OR `tabEntry Pass`.`host_user` = {1})".format(
				frappe.db.escape(scope["employee"]), frappe.db.escape(user)
			)
		)

	alternatives.append(
		"`tabEntry Pass`.`visitor_request` IN (SELECT `name` FROM `tabVisitor Request` "
		"WHERE `owner` = {0})".format(frappe.db.escape(user))
	)

	if "Department Head" in frappe.get_roles(user) and scope["departments"]:
		alternatives.append(
			"`tabEntry Pass`.`visitor_request` IN (SELECT `name` FROM `tabVisitor Request` "
			"WHERE `department` IN ({0}))".format(
				", ".join(frappe.db.escape(d) for d in scope["departments"])
			)
		)

	if not alternatives:
		return "1=0"
	return "(" + " OR ".join(alternatives) + ")"
