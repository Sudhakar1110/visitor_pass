import frappe


from frappe import _

from visitor_pass_tracker.utils import is_security_user


@frappe.whitelist()
def resolve_pass(entry_pass=None):
	"""Resolve an Entry Pass (name, scanned QR URL or legacy JSON payload) for
	the Gate Scanner page.

	Returns the pass details so the console can show who is scanning in, plus
	enough context for a security decision before calling submit_scan / etc.
	"""
	if not is_security_user():
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	from visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry import (
		_resolve_entry_pass,
	)

	pass_name = _resolve_entry_pass(entry_pass)
	if not pass_name or not frappe.db.exists("Entry Pass", pass_name):
		return {"found": False, "entry_pass": entry_pass or ""}

	doc = frappe.get_doc("Entry Pass", pass_name)
	visitor = None
	if doc.visitor:
		visitor = frappe.db.get_value(
			"Visitor",
			doc.visitor,
			["visitor_name", "company_name", "photo", "phone"],
			as_dict=True,
		)
	return {
		"found": True,
		"entry_pass": doc.name,
		"visitor_name": doc.visitor_name,
		"status": doc.status,
		"valid_from": str(doc.valid_from),
		"valid_till": str(doc.valid_till),
		"location_gate": doc.location_gate,
		"vehicle_number": doc.vehicle_number,
		"is_escort_required": doc.is_escort_required,
		"company": visitor.company_name if visitor else None,
		"visitor_phone": visitor.phone if visitor else None,
		"photo": visitor.photo if visitor else None,
	}
