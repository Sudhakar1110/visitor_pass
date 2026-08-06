"""Public Visitor Portal - guest-accessible APIs.

Backs the `www/visitor_portal` website page (see `www/visitor_portal.html`).
Everything here is `allow_guest=True` and therefore exposed on the public
website - keep the surface minimal and never leak data across visitors:

- `register_visitor`  - public pre-registration (creates / updates the Visitor
  master, deduplicated by phone).
- `track_visit`       - a visitor can look up only their OWN requests/passes by
  phone (and optionally a pass number).
- `get_pass_qr`       - returns the stored QR image of a pass the caller
  verifiably owns (private files are read server-side and returned as a data
  URL, so no file-permission exposure).
"""

import base64

import frappe
from frappe import _

from .utils import _merged_filter, _normalize_phone, ensure_draft_visitor_request


def _public_visitor(phone):
	"""Visitor master matching the given phone digits (or None). Merged-away
	duplicates (nightly merge job) are skipped."""
	phone = _normalize_phone(phone)
	if not phone:
		return None
	name = frappe.db.get_value(
		"Visitor", {"phone": phone, **_merged_filter()}, "name"
	)
	return frappe.get_cached_doc("Visitor", name) if name else None


def _pass_payload(entry_pass, include_qr=False):
	"""A visitor-safe summary of an Entry Pass. Never includes host emails or
	other internal fields - only what the visitor needs at the gate."""
	payload = {
		"name": entry_pass.name,
		"status": entry_pass.status,
		"valid_from": entry_pass.valid_from,
		"valid_till": entry_pass.valid_till,
		"gate": entry_pass.location_gate,
		"visitor_name": entry_pass.visitor_name,
		"has_qr": bool(entry_pass.qr_code),
	}
	if include_qr:
		payload["qr_data_url"] = _read_qr_data_url(entry_pass)
	return payload


def _read_qr_data_url(entry_pass):
	"""Return the stored QR image as a base64 data URL (read server-side, so
	private-file permissions never block the public portal)."""
	qr_code = entry_pass.qr_code
	if not qr_code:
		return None
	try:
		file_doc = frappe.get_doc("File", {"file_url": qr_code})
		content = file_doc.get_content()
	except Exception:
		# the file may have been deleted / moved - regenerate and retry once
		from .utils import attach_qr_code

		try:
			attach_qr_code(entry_pass)
			entry_pass.reload()
			file_doc = frappe.get_doc("File", {"file_url": entry_pass.qr_code})
			content = file_doc.get_content()
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: QR read failed"),
				message=frappe.get_traceback(),
			)
			return None

	if isinstance(content, str):
		content = content.encode("utf-8")
	extension = (file_doc.file_url or "").rsplit(".", 1)[-1].lower()
	mime = "image/svg+xml" if extension == "svg" else "image/png"
	return "data:{0};base64,{1}".format(mime, base64.b64encode(content).decode("ascii"))


@frappe.whitelist(allow_guest=True)
def register_visitor(
	visitor_name=None,
	phone=None,
	email=None,
	company_name=None,
	id_proof_type=None,
	id_proof_number=None,
):
	"""Public pre-registration used by the Visitor Portal.

	Creates the Visitor master (the doctype's `after_insert` auto-creates the
	linked ERPNext Contact). When the phone already exists, only missing contact
	details are filled in - the visitor name is never overwritten and no
	duplicate record is created.
	"""
	visitor_name = (visitor_name or "").strip()
	phone = _normalize_phone(phone)
	email = (email or "").strip().lower()
	company_name = (company_name or "").strip()
	id_proof_type = (id_proof_type or "").strip()
	id_proof_number = (id_proof_number or "").strip()

	if not visitor_name:
		frappe.throw(_("Please provide your full name."))
	if len(phone) < 10:
		frappe.throw(_("Please provide a valid 10-digit phone number."))

	# deduplicate on the phone index first, then fall back to email
	# (merged-away duplicates are skipped)
	merged = _merged_filter()
	existing = None
	if phone:
		existing = frappe.db.get_value("Visitor", {"phone": phone, **merged}, "name")
	if not existing and email:
		existing = frappe.db.get_value(
			"Visitor", {"email": email, **merged}, "name"
		)

	if existing:
		doc = frappe.get_doc("Visitor", existing)
		# make sure the desk has an open request for this visitor too
		request_name = ensure_draft_visitor_request(doc.name)
		changed = []
		if not doc.email and email:
			doc.email = email
			changed.append("email")
		if not doc.company_name and company_name:
			doc.company_name = company_name
			changed.append("company_name")
		if not doc.id_proof_type and id_proof_type:
			doc.id_proof_type = id_proof_type
			changed.append("id_proof_type")
		if not doc.id_proof_number and id_proof_number:
			doc.id_proof_number = id_proof_number
			changed.append("id_proof_number")
		if changed:
			doc.save(ignore_permissions=True)
		return {
			"status": "updated" if changed else "matched",
			"visitor": doc.name,
			"visitor_name": doc.visitor_name,
			"phone": doc.phone,
			"request": request_name,
			"message": _("Your details are registered. A visit request has been created "
						"and is being processed - you can track it with your phone number."),
		}

	doc = frappe.get_doc(
		{
			"doctype": "Visitor",
			"visitor_name": visitor_name,
			"phone": phone,
			"email": email or None,
			"company_name": company_name or None,
			"id_proof_type": id_proof_type or "Other",
			"id_proof_number": id_proof_number or None,
		}
	)
	doc.insert(ignore_permissions=True)
	# after_insert already created a Draft Visitor Request for the Guest session -
	# fetch it (or create one defensively) so the portal can report it
	request_name = ensure_draft_visitor_request(doc.name)

	return {
		"status": "created",
		"visitor": doc.name,
		"visitor_name": doc.visitor_name,
		"phone": doc.phone,
		"request": request_name,
		"message": _("You are registered! A visit request has been created for you and "
						"our team will complete it. Track it anytime with your phone number."),
	}


@frappe.whitelist(allow_guest=True)
def track_visit(phone=None, pass_number=None):
	"""Return ONLY the caller's own requests and passes.

	Verification: a Visitor is resolved from the phone number, and any optional
	`pass_number` must belong to that same Visitor - otherwise nothing is
	returned (a pass lookup never leaks another visitor's data).
	"""
	phone = _normalize_phone(phone)
	visitor = _public_visitor(phone)

	# pass_number given -> verify ownership against the phone before returning
	# (an empty phone never matches, so a guessed pass name cannot leak data)
	if pass_number:
		if not phone:
			frappe.throw(_("Please enter the phone number you registered with."))
		entry_pass = frappe.db.exists("Entry Pass", pass_number)
		if not entry_pass:
			frappe.throw(_("Pass not found."))
		entry_pass = frappe.get_doc("Entry Pass", pass_number)
		owner = frappe.db.get_value("Visitor", entry_pass.visitor, "phone") if entry_pass.visitor else None
		if not visitor or _normalize_phone(owner) != phone:
			frappe.throw(_("Pass not found for this phone number."))
		return {"visits": [], "passes": [_pass_payload(entry_pass, include_qr=True)]}

	if not visitor:
		return {"visits": [], "passes": []}

	visits = frappe.get_all(
		"Visitor Request",
		filters={"visitor": visitor.name},
		fields=[
			"name",
			"workflow_state",
			"visit_date",
			"visit_end_date",
			"purpose",
			"location_gate",
			"rejection_reason",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=20,
	)
	passes = frappe.get_all(
		"Entry Pass",
		filters={"visitor": visitor.name},
		fields=["name", "status", "valid_from", "valid_till", "location_gate", "visitor_name", "qr_code"],
		order_by="valid_from desc",
		limit_page_length=20,
	)
	return {"visits": visits, "passes": [_pass_payload(p) for p in passes]}


@frappe.whitelist(allow_guest=True)
def get_pass_qr(pass_number=None, phone=None):
	"""QR image (data URL) + gate details for a pass the caller verifiably owns."""
	pass_number = (pass_number or "").strip()
	if not pass_number:
		frappe.throw(_("Pass number is required."))
	if not _normalize_phone(phone):
		frappe.throw(_("Please enter the phone number you registered with."))
	if not frappe.db.exists("Entry Pass", pass_number):
		frappe.throw(_("Pass not found."))

	entry_pass = frappe.get_doc("Entry Pass", pass_number)
	owner = frappe.db.get_value("Visitor", entry_pass.visitor, "phone") if entry_pass.visitor else None
	if _normalize_phone(owner) != _normalize_phone(phone):
		frappe.throw(_("Pass not found for this phone number."))

	payload = _pass_payload(entry_pass, include_qr=True)
	if not payload["qr_data_url"]:
		frappe.throw(_("QR code is not available for this pass."))
	return payload
