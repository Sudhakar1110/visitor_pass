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
- `get_pass_status`    - minimal public status view backing the QR scan
  experience: the QR on a pass encodes a portal URL (?pass=PASS-...), so a
  phone camera opens the pass page instead of raw text. Returns only what
  the visitor needs at the gate (status, window, gate, name, QR image).
- `cancel_visit`      - a visitor cancels their OWN visit request by phone;
  submitted requests are cancelled through the normal document flow (which
  revokes any issued Entry Pass) and the host is notified.
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
		"visitor_request": entry_pass.visitor_request,
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

	# mirror ensure_draft_visitor_request's logic so the message only claims
	# auto-submission when a valid default host is actually configured
	default_host = frappe.conf.get("visitor_pass_portal_default_host") or ""
	auto_submitted = bool(default_host) and frappe.db.exists("Employee", default_host)
	if auto_submitted:
		created_message = _(
			"You are registered! Your visit request has been submitted for approval - "
			"you will receive your entry pass as soon as it is approved. "
			"Track it anytime with your phone number."
		)
		updated_message = _(
			"Your details are registered. Your visit request has been submitted for "
			"approval - track it anytime with your phone number."
		)
	else:
		created_message = _(
			"You are registered! A visit request has been created for you and "
			"our team will complete it. Track it anytime with your phone number."
		)
		updated_message = _(
			"Your details are registered. A visit request has been created "
			"and is being processed - you can track it with your phone number."
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
			"message": updated_message,
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
		"message": created_message,
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
			"docstatus",
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
		fields=["name", "status", "valid_from", "valid_till", "location_gate", "visitor_name", "visitor_request", "qr_code"],
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


@frappe.whitelist(allow_guest=True)
def get_pass_status(pass_number=None):
	"""Public, minimal status view for a QR-scanned pass URL.

	Backs the phone-scan experience: the QR on an Entry Pass now encodes a
	portal URL (`/visitor_portal?pass=PASS-...`), and opening it on a phone
	renders this readable status instead of raw text. Returns only the
	non-sensitive fields the visitor needs at the gate (status, validity
	window, gate, visitor name, QR image) - never phone numbers, emails,
	hosts or other internal data.

	The QR (and therefore the URL) is private - the pass file is
	permission-protected and the link only reaches the visitor and host -
	so this endpoint exposes no more than the badge print format already
	carries in public.
	"""
	pass_number = (pass_number or "").strip()
	if not pass_number or not frappe.db.exists("Entry Pass", pass_number):
		return {"found": False}
	entry_pass = frappe.get_doc("Entry Pass", pass_number)
	payload = _pass_payload(entry_pass, include_qr=True)
	payload["found"] = True
	return payload


@frappe.whitelist(allow_guest=True)
def cancel_visit(request=None, phone=None):
	"""Cancel a visit request from the portal (self-service).

	Ownership is verified exactly like `track_visit` - the phone number must
	resolve to the Visitor that owns the request, so a caller can never cancel
	someone else's visit.

	- Draft requests are simply deleted (nothing was submitted yet).
	- Submitted requests go through the normal `doc.cancel()` flow, whose
	  `on_cancel` revokes any issued Entry Pass so the gate stops accepting it.
	- The host (host user, or the host Employee's user) is notified in-app
	  + email (best-effort - failures are logged, never raised).
	- Already rejected / cancelled / completed requests are refused with a
	  clear message instead of silently failing.
	"""
	phone = _normalize_phone(phone)
	if not request:
		frappe.throw(_("Request is required."))
	if not phone:
		frappe.throw(_("Please enter the phone number you registered with."))

	visitor = _public_visitor(phone)
	if not visitor:
		frappe.throw(_("No registration found for this phone number."))

	if not frappe.db.exists("Visitor Request", request):
		frappe.throw(_("Request not found."))

	req = frappe.get_doc("Visitor Request", request)
	# ownership: the request must belong to the Visitor resolved from the phone
	if not req.visitor or req.visitor != visitor.name:
		frappe.throw(_("Request not found for this phone number."))

	if req.docstatus == 2:
		frappe.throw(_("This request has already been cancelled."))
	if req.workflow_state == "Rejected":
		frappe.throw(_("This request was already rejected and cannot be cancelled."))

	# an already-completed visit (pass Used) cannot be cancelled
	pass_name = frappe.db.get_value("Entry Pass", {"visitor_request": request}, "name")
	if pass_name:
		pass_status = frappe.db.get_value("Entry Pass", pass_name, "status")
		if pass_status == "Used":
			frappe.throw(_("This visit is already completed and cannot be cancelled."))

	if req.docstatus == 0:
		# never submitted - nothing to revoke, just remove the draft
		frappe.delete_doc("Visitor Request", request, ignore_permissions=True, force=1)
		frappe.db.commit()
		_notify_host_of_cancellation(req)
		return {
			"status": "cancelled",
			"request": request,
			"message": _("Your visit request has been cancelled."),
		}

	# submitted request -> cancel via the normal document flow (revokes the pass)
	req.flags.ignore_permissions = True
	req.cancel()
	frappe.db.commit()
	_notify_host_of_cancellation(req)

	return {
		"status": "cancelled",
		"request": request,
		"message": _("Your visit has been cancelled and any issued pass has been revoked."),
	}


def _notify_host_of_cancellation(req):
	"""Notify the host that their visitor cancelled (in-app + email, best-effort)."""
	host_user = req.host_user
	if not host_user and req.host:
		host_user = frappe.db.get_value("Employee", req.host, "user_id")
	if not host_user:
		return
	try:
		subject = _("Visit cancelled: {0} ({1})").format(
			req.visitor_name or req.visitor or "-", req.name
		)
		message = _(
			"<p>The visitor <b>{0}</b> has cancelled their visit request "
			"<b>{1}</b>.</p>"
			"<p>Any entry pass issued for this visit has been revoked.</p>"
		).format(req.visitor_name or req.visitor or "-", req.name)

		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": host_user,
				"from_user": frappe.session.user or "Administrator",
				"subject": subject,
				"document_type": "Visitor Request",
				"document_name": req.name,
				"type": "Alert",
			}
		).insert(ignore_permissions=True)

		email = frappe.db.get_value("User", host_user, "email")
		if email:
			frappe.sendmail(
				recipients=email,
				subject=subject,
				message=message,
				reference_doctype="Visitor Request",
				reference_name=req.name,
			)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: cancellation notification failed"),
			message=frappe.get_traceback(),
		)
