import base64
from io import BytesIO

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, getdate, now_datetime

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
# Duplicate-merge awareness - merged Visitors (marked via the hidden
# `merged_into` link by the nightly merge job) are excluded from lookups.
# Guarded by frappe.db.has_column so pre-migrate sites never error.
# ---------------------------------------------------------------------------


def _merged_filter():
	if frappe.db.has_column("Visitor", "merged_into"):
		return {"merged_into": ["is", "not set"]}
	return {}


# ---------------------------------------------------------------------------
# Pre-registration - auto-create a Visitor Request for the desk / approval flow
# ---------------------------------------------------------------------------


def ensure_draft_visitor_request(visitor):
	"""Create a Visitor Request for a pre-registered visitor so it is picked up
	by the desk / approval flow immediately.

	Idempotent: while any open (non-Rejected) request exists for the visitor,
	the existing one is returned and nothing new is created.

	By default the request stays Draft for Reception to complete. When a
	default host is configured (`visitor_pass_portal_default_host` in
	site_config.json) the request is auto-completed with that host (and the
	optional `visitor_pass_portal_default_gate`) and **submitted**, so it flows
	straight into the normal approval workflow (Blacklist Check -> Pending Host
	Approval -> ...) instead of sitting as a Draft on the desk.

	Never raises - pre-registration must never fail because of this."""
	if not visitor or not frappe.db.exists("Visitor", visitor):
		return None
	try:
		existing = frappe.db.get_value(
			"Visitor Request",
			filters={
				"visitor": visitor,
				"docstatus": ["<", 2],
				"workflow_state": ["not in", ["Rejected"]],
			},
			pluck="name",
		)
		if existing:
			return existing

		# optional site_config defaults - the request is only auto-submitted
		# when a default host is configured (gate stays optional)
		default_host = frappe.conf.get("visitor_pass_portal_default_host") or ""
		default_gate = frappe.conf.get("visitor_pass_portal_default_gate") or ""
		if default_host and not frappe.db.exists("Employee", default_host):
			default_host = ""
		if default_gate and not frappe.db.exists("Gate", default_gate):
			default_gate = ""

		request = frappe.get_doc(
			{
				"doctype": "Visitor Request",
				"visitor": visitor,
				"host": default_host or None,
				"location_gate": default_gate or None,
				"purpose": "Meeting",
				"visit_date": getdate(),
				"expected_in_time": "09:00:00",
				"expected_out_time": "18:00:00",
				"notes": _("Auto-created from visitor pre-registration."),
			}
		)
		request.insert(ignore_permissions=True, ignore_mandatory=True)

		# With a configured default host the request is complete - submit it so
		# it enters the approval workflow (the auto transitions are allowed to
		# "All", so the Guest session can apply them once submit permission is
		# bypassed). On any failure the request simply stays Draft on the desk
		# and Reception completes it manually as before.
		if default_host:
			request.flags.ignore_permissions = True
			try:
				request.submit()
			except Exception:
				frappe.log_error(
					title=_("Visitor Pass Tracker: auto-submit of pre-registration failed"),
				message=frappe.get_traceback(),
			)
		return request.name
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: draft request creation failed"),
			message=frappe.get_traceback(),
		)
		return None


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

	merged = _merged_filter()

	# 1) exact phone match - uses the phone index, returns immediately
	if phone:
		for v in frappe.get_all(
			"Visitor",
			filters={"phone": phone, **merged},
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
			filters={"phone": ["like", f"%{phone}"], **merged},
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
			filters={"id_proof_number": ["like", f"%{id_proof}%"], **merged},
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

	# The visitor SMS is also sent inside send_pass_notifications() above -
	# a single code path covers both the initial send and the desk "Resend Pass"
	return entry_pass


def send_pass_notifications(entry_pass, sent=None, skipped=None):
	"""Email the Entry Pass + QR to the host and the visitor after the QR code
	has been generated, and SMS the visitor the pass number. Failures are
	logged, never raised.

	`sent` / `skipped` (optional lists) collect human-readable descriptions of
	what was delivered / what had no recipient - used by the desk "Resend Pass"
	action (entry_pass.resend_pass) to show a delivery summary.
	"""
	if sent is None:
		sent = []
	if skipped is None:
		skipped = []

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

	# attachments: the QR image + a calendar invite (.ics) so the visitor and
	# host can add the visit window to their calendars in one tap
	attachments = [{"file_url": qr_code}] if qr_code else []
	attachments.append(
		{
			"fname": "{0}.ics".format(entry_pass.name),
			"fcontent": _build_ics(
				entry_pass.name,
				entry_pass.valid_from,
				entry_pass.valid_till,
				_("Visit: {0}").format(entry_pass.visitor_name or "Visitor"),
				entry_pass.location_gate or "",
				_("Entry Pass {0} | Visitor {1}").format(
					entry_pass.name, entry_pass.visitor_name or ""
				),
			),
		}
	)

	# host - email + in-app notification
	host_user = entry_pass.host_user
	if host_user:
		host_email = frappe.db.get_value("User", host_user, "email")
		if host_email:
			try:
				frappe.sendmail(
					recipients=host_email,
					subject=subject,
					message=message,
					reference_doctype="Entry Pass",
					reference_name=entry_pass.name,
					attachments=attachments,
				)
				sent.append(_("host email ({0})").format(host_email))
			except frappe.OutgoingEmailError:
				frappe.log_error(
					title=_("Visitor Pass Tracker: pass email failed"),
					message=f"Entry Pass email to host {host_user} could not be sent.",
				)
				skipped.append(_("host email (sending failed)"))
		else:
			skipped.append(_("host email (no email on user {0})").format(host_user))
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
	else:
		skipped.append(_("host (no host user)"))

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
			sent.append(_("visitor email ({0})").format(visitor_email))
		except frappe.OutgoingEmailError:
			frappe.log_error(
				title=_("Visitor Pass Tracker: visitor pass email failed"),
				message=f"Entry Pass email to visitor {entry_pass.visitor_name} could not be sent.",
			)
			skipped.append(_("visitor email (sending failed)"))
	else:
		skipped.append(_("visitor email (no address on file)"))

	# visitor - SMS with the pass number (channel-ready; requires Frappe SMS
	# Settings, failures are logged silently - the emails above still deliver)
	visitor_phone = frappe.db.get_value("Visitor", entry_pass.visitor, "phone") if entry_pass.visitor else None
	if visitor_phone:
		ok = _send_sms(
			visitor_phone,
			_("Your entry pass {0} for {1} is approved. Present this pass number "
			  "at gate {2}.").format(
				entry_pass.name,
				frappe.utils.getdate(entry_pass.valid_from),
				entry_pass.location_gate or "the main gate",
			),
		)
		if ok:
			sent.append(_("visitor SMS ({0})").format(visitor_phone))
		else:
			skipped.append(_("visitor SMS (gateway failure)"))
	else:
		skipped.append(_("visitor SMS (no phone on file)"))


def attach_qr_code(entry_pass):
	"""Generate a QR image for the Entry Pass and store it in `qr_code`.

	The QR encodes a **scannable portal URL** (`/visitor_portal?pass=PASS-...`)
	so a phone camera opens the pass status page on the visitor portal instead
	of showing raw text. Gate hardware posts the scanned text back to the scan
	API, which resolves the pass number from the URL (see `_resolve_entry_pass`
	- the legacy JSON payload format is still accepted there).

	PNG is preferred (pyqrcode + pypng, both bundled with Frappe 15 /
	ERPNext 15); falls back to SVG if pypng is unavailable.
	"""
	try:
		import pyqrcode
	except ImportError:
		frappe.log_error(
			title=_("Visitor Pass Tracker: pyqrcode not available"),
			message=f"Could not generate QR for Entry Pass {entry_pass.name}",
		)
		return

	# Build the site URL for the portal. During a normal (workflow / desk)
	# call get_url() resolves the real request host; in background contexts it
	# falls back to host_name from site_config.json (or localhost as a last
	# resort - a misconfigured host only makes the QR open the wrong URL, it
	# never breaks scanning, which resolves the pass from the query string).
	try:
		base = frappe.utils.get_url("/visitor_portal")
	except Exception:
		base = (frappe.conf.get("host_name") or "http://localhost:8000") + "/visitor_portal"
	payload = "{0}?pass={1}".format(base, entry_pass.name)
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


def regenerate_all_pass_qrs():
	"""Regenerate the QR image for every Entry Pass that has one stored.

	The QR format changed from a raw JSON payload (gate-hardware only, shows
	as raw text on phone cameras) to a scannable portal URL. Existing passes
	keep their old QR image until regenerated - run this once after upgrading
	so already-issued passes scan nicely on phones too:

	    bench --site <sitename> execute visitor_pass_tracker.utils.regenerate_all_pass_qrs

	Returns the number of passes updated. Never raises - failures are logged
	and the loop continues.
	"""
	names = frappe.get_all(
		"Entry Pass",
		filters={"qr_code": ["is", "set"]},
		pluck="name",
		limit_page_length=10000,
		ignore_permissions=True,
	)
	updated = 0
	for name in names:
		try:
			doc = frappe.get_doc("Entry Pass", name)
			attach_qr_code(doc)
			updated += 1
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: QR regeneration failed"),
				message="Entry Pass {0}\n{1}".format(name, frappe.get_traceback()),
			)
	print(
		"Regenerated QR images for {0} of {1} Entry Passes".format(updated, len(names))
	)
	return updated


# ---------------------------------------------------------------------------
# SMS helper - uses Frappe's SMS Settings (Twilio / Exotel / MSG91 etc.)
# ---------------------------------------------------------------------------


def _send_sms(phone, message):
	"""Best-effort SMS via Frappe SMS Settings. Returns True when the SMS was
	handed to the gateway, False otherwise. Never raises - failures are logged
	so email/in-app notifications still cover the recipient."""
	if not phone or not message:
		return False
	try:
		from frappe.core.doctype.sms_settings.sms_settings import send_sms

		# best-effort: reaching the gateway call without an exception is treated
		# as handed off (send_sms return values vary across Frappe versions)
		send_sms(receiver_list=[phone], msg=message)
		return True
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: SMS failed"),
			message=frappe.get_traceback(),
		)
		return False


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
# Installation health check - run after migrate / sync-fixtures to confirm
# every component of the app is present on the site:
#   bench --site <sitename> execute visitor_pass_tracker.utils.check_installation
# ---------------------------------------------------------------------------


def check_installation():
	"""Verify every app component (fields, workflow, fixtures, reports, page,
	indexes) is present on the current site. Prints a readable report and
	returns the full dict.

	Run with: bench --site <sitename> execute visitor_pass_tracker.utils.check_installation
	(not whitelisted - diagnostics only).
	"""
	report = {}

	def mark(check, ok, detail=""):
		report[check] = {"status": "ok" if ok else "missing", "detail": detail}

	# 1) doctype fields added across the releases
	fields = {
		"Visitor Request": [
			"visit_end_date",
			"host_checkin_time",
			"host_checkout_time",
			"rejection_reason",
		],
		"Entry Pass": [
			"visitor_email",
			"company_name",
			"vehicle_number",
			"is_escort_required",
			"revoked_by",
			"revoked_on",
		],
		"Visitor": ["id_proof_document", "id_proof_verified"],
		"Gate Log Entry": ["source"],
	}
	for doctype, names in fields.items():
		meta = frappe.get_meta(doctype)
		for fieldname in names:
			mark("field:{0}.{1}".format(doctype, fieldname), meta.has_field(fieldname))

	# 2) roles, workflow - Reject action + the 3 reject transitions
	for role in ["Security Officer", "Department Head", "Reception"]:
		mark("role:{0}".format(role), frappe.db.exists("Role", role))
	mark("workflow_action:Reject Request", frappe.db.exists("Workflow Action Master", "Reject Request"))
	if frappe.db.exists("Workflow", "Visitor Request Workflow"):
		wf = frappe.get_doc("Workflow", "Visitor Request Workflow")
		rejects = [t for t in wf.transitions if t.action == "Reject Request"]
		mark("workflow:Reject transitions", len(rejects) == 3, "found {0}".format(len(rejects)))
	else:
		mark("workflow:Visitor Request Workflow", False)

	# 3) notification fixtures (the two Entry Pass ones are sent from code now)
	for name in [
		"Visitor Request Pending Host Approval",
		"Visitor Request Approved",
		"Visitor Request Rejected - Blacklist",
		"Visitor Request Rejected",
		"Visitor Arrived",
		"Unauthorized Scan Detected",
	]:
		mark("notification:{0}".format(name), frappe.db.exists("Notification", name))

	# 4) stale notifications that should be deleted (would double-send)
	stale = {}
	for name in ["Entry Pass Generated", "Entry Pass Generated - Visitor"]:
		present = bool(frappe.db.exists("Notification", name))
		stale[name] = present
		report["stale_notification:{0}".format(name)] = {
			"status": "delete me" if present else "ok",
			"detail": "old fixture - remove to avoid double emails" if present else "",
		}
	report["stale_notifications_to_delete"] = stale

	# 5) web forms
	for name in ["request-a-visit", "visitor-pre-registration"]:
		mark("web_form:{0}".format(name), frappe.db.exists("Web Form", name))

	# 5b) client script fixtures
	mark(
		"client_script:Entry Pass - Resend Pass",
		frappe.db.exists("Client Script", "Entry Pass - Resend Pass"),
	)

	# 6) number cards + dashboard
	for name in ["Visitors On-Site Now", "Passes Expiring in Next Hour", "Visitors Expected Today"]:
		mark("number_card:{0}".format(name), frappe.db.exists("Number Card", name))
	mark("dashboard:Visitor Overview", frappe.db.exists("Dashboard", "Visitor Overview"))

	# 7) print format, reports + page
	mark("print_format:Entry Pass Badge", frappe.db.exists("Print Format", "Entry Pass Badge"))
	for name in ["Visitor Reconciliation", "Daily Visitor Register", "Expected Visitors"]:
		mark("report:{0}".format(name), frappe.db.exists("Report", name))
	mark("page:gate-scanner", frappe.db.exists("Page", "gate-scanner"))

	# 7b) scheduler crons registered in hooks
	expected_crons = {
		"*/15 * * * *": ["visitor_pass_tracker.utils.run_pass_expiry_checks"],
		"0 * * * *": ["visitor_pass_tracker.utils.run_hourly_automations"],
		"0 9 * * *": ["visitor_pass_tracker.utils.send_day_before_visit_reminders"],
		"0 17 * * *": ["visitor_pass_tracker.utils.send_expected_tomorrow_digest"],
		"0 19 * * *": ["visitor_pass_tracker.utils.send_end_of_day_reconciliation_digest"],
		"0 2 * * *": ["visitor_pass_tracker.utils.merge_duplicate_visitors"],
	}
	hooks_cron = frappe.get_hooks("scheduler_events") or {}
	registered_crons = hooks_cron.get("cron") or {}
	for cron_expr, funcs in expected_crons.items():
		mark(
			"scheduler:cron {0}".format(cron_expr),
			all(f in (registered_crons.get(cron_expr) or []) for f in funcs),
			", ".join(registered_crons.get(cron_expr) or []) or "none",
		)

	# 8) public Visitor Portal (www page + guest APIs - no DB records)
	import os

	module_dir = frappe.utils.get_module_path("visitor_pass_tracker")
	mark(
		"portal:www page",
		os.path.exists(os.path.join(module_dir, "www", "visitor_portal.html")),
	)
	try:
		guest_apis_ok = all(
			callable(frappe.get_attr("visitor_pass_tracker.portal.{0}".format(name)))
			for name in [
				"register_visitor",
				"track_visit",
				"get_pass_qr",
				"get_pass_status",
				"cancel_visit",
			]
		)
	except Exception:
		guest_apis_ok = False
	mark("portal:guest APIs", guest_apis_ok)

	# 9) DB indexes (created by bench migrate for search_index fields)
	for table, columns in {
		"tabGate Log Entry": ["scan_time", "entry_pass"],
		"tabEntry Pass": ["valid_till"],
	}.items():
		try:
			existing = {
				r["Column_name"]
				for r in frappe.db.sql("SHOW INDEX FROM `{0}`".format(table), as_dict=True)
			}
		except Exception:
			existing = set()
		for column in columns:
			mark("index:{0}.{1}".format(table, column), column in existing)

	# readable output
	lines = ["[Visitor Pass Tracker] installation check"]
	missing = []
	for key, value in sorted(report.items()):
		if isinstance(value, dict) and "status" in value:
			status = value["status"]
			detail = (" - " + value["detail"]) if value.get("detail") else ""
			lines.append("{0:55} {1}{2}".format(key, status.upper(), detail))
			if status != "ok":
				missing.append(key)
	lines.append("")
	if missing:
		lines.append("MISSING: " + ", ".join(missing))
	else:
		lines.append("ALL CHECKS PASSED")
	print("\n".join(lines))

	return report


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


# ---------------------------------------------------------------------------
# Automations (scheduler) - approval reminders & escalation, stale-request
# auto-rejection, repeat-offender auto-blacklisting, day-before visit
# reminders, daily digests (expected-tomorrow + end-of-day reconciliation)
# and nightly duplicate-visitor merging.
#
# Every threshold is configurable via site_config.json:
#   visitor_pass_reminder_hours                  (default 4)
#   visitor_pass_escalation_hours                (default 24)
#   visitor_pass_stale_days                      (default 3)
#   visitor_pass_trusted_visit_threshold         (default 3)
#   visitor_pass_overstay_blacklist_threshold    (default 2)
#   visitor_pass_unauthorized_blacklist_threshold(default 3)
#   visitor_pass_digest_roles                    (default ["Security Officer", "Reception"])
#   visitor_pass_recon_digest_roles              (default ["Security Officer", "System Manager"])
# ---------------------------------------------------------------------------

PENDING_WORKFLOW_STATES = [
	"Pending Host Approval",
	"Pending Department Approval",
	"Pending Security Approval",
]


def _config_int(key, default):
	value = frappe.conf.get(key)
	try:
		return int(value) if value not in (None, "") else default
	except (TypeError, ValueError):
		return default


def _config_list(key, default):
	value = frappe.conf.get(key)
	if not value:
		return list(default)
	if isinstance(value, str):
		return [item.strip() for item in value.split(",") if item.strip()]
	return list(value)


def _role_users(roles):
	"""Enabled user names holding any of the given roles."""
	if isinstance(roles, str):
		roles = [roles]
	users = set()
	for role in roles:
		users.update(frappe.get_all("Has Role", filters={"role": role}, pluck="parent"))
	return {u for u in users if u and frappe.db.get_value("User", u, "enabled")}


def _role_emails(roles):
	"""Enabled user emails for the given roles (for digests / bulk emails)."""
	return {
		e
		for e in (frappe.db.get_value("User", u, "email") for u in _role_users(roles))
		if e
	}


def _notify_users(users, subject, message, reference_doctype=None, reference_name=None):
	"""In-app Notification Log + Email to each user. Never raises - failures are
	logged so one bad address cannot stop the rest of the job."""
	for user in users:
		try:
			email = frappe.db.get_value("User", user, "email")
			frappe.get_doc(
				{
					"doctype": "Notification Log",
					"for_user": user,
					"from_user": "Administrator",
					"subject": subject,
					"document_type": reference_doctype,
					"document_name": reference_name,
					"type": "Alert",
				}
			).insert(ignore_permissions=True)
			if email:
				frappe.sendmail(
					recipients=email,
					subject=subject,
					message=message,
					reference_doctype=reference_doctype,
					reference_name=reference_name,
				)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: automation notification failed"),
				message=frappe.get_traceback(),
			)


def run_hourly_automations():
	"""Hourly scheduler job: approval reminders + escalation, stale-request
	auto-rejection and repeat-offender auto-blacklisting."""
	_approval_reminders_and_escalation()
	_auto_reject_stale_requests()
	_auto_blacklist_repeat_offenders()


def _approvers_for(req):
	"""Users who can act on a request in its current workflow state."""
	if req.workflow_state == "Pending Host Approval":
		host_user = req.host_user
		if not host_user and req.host:
			host_user = frappe.db.get_value("Employee", req.host, "user_id")
		return {host_user} if host_user else set()
	if req.workflow_state == "Pending Department Approval":
		if req.department:
			head = frappe.db.get_value("Department", req.department, "head_of_department")
			if head:
				user = frappe.db.get_value("Employee", head, "user_id")
				if user:
					return {user}
		return _role_users(["Department Head"])
	if req.workflow_state == "Pending Security Approval":
		return _role_users(["Security Officer"])
	return set()


def _approval_reminders_and_escalation():
	"""Requests stuck in a pending state:
	- after `visitor_pass_reminder_hours` (default 4h): remind the approver
	- after `visitor_pass_escalation_hours` (default 24h): notify System Manager
	Deduplicated via the hidden approval_reminder_sent / approval_escalated flags.
	"""
	reminder_hours = _config_int("visitor_pass_reminder_hours", 4)
	escalation_hours = _config_int("visitor_pass_escalation_hours", 24)
	if reminder_hours <= 0:
		return
	now = now_datetime()
	reminder_cutoff = add_to_date(now, hours=-reminder_hours)
	escalation_cutoff = add_to_date(now, hours=-escalation_hours)
	# requests already past the stale-reject cutoff will be auto-rejected -
	# don't waste reminder/escalation emails on them
	stale_days = _config_int("visitor_pass_stale_days", 3)
	stale_cutoff = add_to_date(now, days=-stale_days) if stale_days > 0 else None

	requests = frappe.get_all(
		"Visitor Request",
		filters={"docstatus": 1, "workflow_state": ["in", PENDING_WORKFLOW_STATES]},
		fields=[
			"name",
			"workflow_state",
			"modified",
			"visitor_name",
			"visit_date",
			"host",
			"host_user",
			"department",
			"approval_reminder_sent",
			"approval_escalated",
		],
		ignore_permissions=True,
	)

	for req in requests:
		# skip requests that are about to be auto-rejected as stale
		if stale_cutoff and req.modified and req.modified <= stale_cutoff:
			continue
		try:
			if (
				req.modified
				and req.modified <= reminder_cutoff
				and not req.approval_reminder_sent
			):
				approvers = _approvers_for(req)
				subject = _("Reminder: Visitor Request {0} awaiting approval").format(req.name)
				message = _(
					"<p>Visitor Request <b>{0}</b> for <b>{1}</b> ({2}) is still "
					"<b>{3}</b> and has been waiting for over {4} hour(s).</p>"
					"<p>Please open the request and approve or reject it.</p>"
				).format(
					req.name,
					req.visitor_name or "-",
					req.visit_date,
					req.workflow_state,
					reminder_hours,
				)
				_notify_users(approvers, subject, message, "Visitor Request", req.name)
				frappe.db.set_value(
					"Visitor Request", req.name, "approval_reminder_sent", 1
				)

			if (
				req.modified
				and req.modified <= escalation_cutoff
				and not req.approval_escalated
			):
				managers = _role_users(["System Manager"])
				subject = _("Escalation: Visitor Request {0} is blocked").format(req.name)
				message = _(
					"<p>Visitor Request <b>{0}</b> for <b>{1}</b> has been <b>{2}</b> "
					"for over {3} hour(s) with no action.</p>"
					"<p>It needs attention - please follow up with the approver.</p>"
				).format(
					req.name,
					req.visitor_name or "-",
					req.workflow_state,
					escalation_hours,
				)
				_notify_users(managers, subject, message, "Visitor Request", req.name)
				frappe.db.set_value(
					"Visitor Request", req.name, "approval_escalated", 1
				)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: approval automation failed"),
				message=frappe.get_traceback(),
			)


def _auto_reject_stale_requests():
	"""Auto-reject requests still pending after `visitor_pass_stale_days`
	(default 3). The host gets the standard rejection notification."""
	from frappe.model.workflow import apply_workflow

	stale_days = _config_int("visitor_pass_stale_days", 3)
	if stale_days <= 0:
		return
	cutoff = add_to_date(now_datetime(), days=-stale_days)
	requests = frappe.get_all(
		"Visitor Request",
		filters={"docstatus": 1, "workflow_state": ["in", PENDING_WORKFLOW_STATES]},
		fields=["name", "modified"],
		ignore_permissions=True,
	)
	rejected = []
	for req in requests:
		if not (req.modified and req.modified <= cutoff):
			continue
		try:
			doc = frappe.get_doc("Visitor Request", req.name)
			doc.rejection_reason = _(
				"Auto-rejected: no response within {0} day(s)"
			).format(stale_days)
			apply_workflow(doc, "Reject Request")
			rejected.append(req.name)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: stale request rejection failed"),
				message=frappe.get_traceback(),
			)
	if rejected:
		frappe.log_error(
			title=_("Visitor Pass Tracker: auto-rejected stale requests"),
			message="Auto-rejected: " + ", ".join(rejected),
		)


def _auto_blacklist_repeat_offenders():
	"""Visitors with >= `visitor_pass_overstay_blacklist_threshold` overstays
	(auto-revoked passes) or >= `visitor_pass_unauthorized_blacklist_threshold`
	unauthorized scans are auto-blacklisted: an Active Blacklisted Visitor
	record is created (the doctype hooks sync the Visitor flag), and their open
	pending requests are auto-rejected."""
	from frappe.model.workflow import apply_workflow

	overstay_threshold = _config_int("visitor_pass_overstay_blacklist_threshold", 2)
	unauthorized_threshold = _config_int("visitor_pass_unauthorized_blacklist_threshold", 3)

	overstays = {}
	if overstay_threshold > 0:
		for r in frappe.db.sql(
			"""
			SELECT `visitor` AS visitor, COUNT(*) AS cnt
			FROM `tabEntry Pass`
			WHERE `status` = 'Revoked' AND `overstay_alert_sent` = 1
			  AND `visitor` IS NOT NULL AND `visitor` != ''
			GROUP BY `visitor`
			""",
			as_dict=True,
		):
			overstays[r["visitor"]] = r["cnt"]

	unauthorized = {}
	if unauthorized_threshold > 0:
		for r in frappe.db.sql(
			"""
			SELECT `visitor` AS visitor, COUNT(*) AS cnt
			FROM `tabGate Log Entry`
			WHERE `is_authorized` = 0 AND `docstatus` < 2
			  AND `visitor` IS NOT NULL AND `visitor` != ''
			GROUP BY `visitor`
			""",
			as_dict=True,
		):
			unauthorized[r["visitor"]] = r["cnt"]

	for visitor in set(overstays) | set(unauthorized):
		o_cnt = overstays.get(visitor, 0)
		u_cnt = unauthorized.get(visitor, 0)
		if o_cnt < overstay_threshold and u_cnt < unauthorized_threshold:
			continue
		if not frappe.db.exists("Visitor", visitor):
			continue
		if frappe.db.exists("Blacklisted Visitor", {"visitor": visitor, "status": "Active"}):
			continue
		try:
			visitor_data = frappe.db.get_value(
				"Visitor",
				visitor,
				["visitor_name", "phone", "id_proof_number"],
				as_dict=True,
			)
			reasons = []
			if o_cnt >= overstay_threshold:
				reasons.append(_("{0} overstays").format(o_cnt))
			if u_cnt >= unauthorized_threshold:
				reasons.append(_("{0} unauthorized scans").format(u_cnt))
			reason = _("Auto-blacklisted: {0}").format(", ".join(reasons))
			frappe.get_doc(
				{
					"doctype": "Blacklisted Visitor",
					"visitor": visitor,
					"visitor_name": visitor_data.visitor_name,
					"phone": visitor_data.phone,
					"id_proof_number": visitor_data.id_proof_number,
					"reason": reason,
					"blacklisted_by": "Administrator",
					"blacklisted_on": getdate(),
					"status": "Active",
				}
			).insert(ignore_permissions=True)

			# reject the visitor's open pending requests
			for name in frappe.get_all(
				"Visitor Request",
				filters={
					"visitor": visitor,
					"docstatus": 1,
					"workflow_state": ["in", PENDING_WORKFLOW_STATES],
				},
				pluck="name",
				ignore_permissions=True,
			):
				doc = frappe.get_doc("Visitor Request", name)
				doc.blacklist_status = "Flagged"
				doc.rejection_reason = reason
				apply_workflow(doc, "Reject Request")
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: auto-blacklist failed"),
				message=frappe.get_traceback(),
			)


def is_trusted_visitor(visitor_name):
	"""A visitor is 'trusted' when they have at least
	`visitor_pass_trusted_visit_threshold` completed (Used) passes, are not
	blacklisted and have no active blacklist record. Trusted visitors skip the
	Department and Security approval steps automatically."""
	if not visitor_name or not frappe.db.exists("Visitor", visitor_name):
		return False
	threshold = _config_int("visitor_pass_trusted_visit_threshold", 3)
	if threshold <= 0:
		return False
	if check_blacklist({"name": visitor_name}):
		return False
	completed = frappe.db.count(
		"Entry Pass", filters={"visitor": visitor_name, "status": "Used"}
	)
	return completed >= threshold


def send_day_before_visit_reminders():
	"""Daily job (9 AM): email + SMS visitors whose Approved visit is tomorrow."""
	tomorrow = add_to_date(getdate(), days=1)
	requests = frappe.get_all(
		"Visitor Request",
		filters={
			"docstatus": 1,
			"workflow_state": "Approved",
			"visit_date": str(tomorrow),
			"day_before_reminder_sent": 0,
		},
		fields=[
			"name",
			"visitor",
			"visitor_name",
			"visitor_phone",
			"visit_date",
			"expected_in_time",
			"expected_out_time",
			"location_gate",
			"host_name",
		],
		ignore_permissions=True,
	)
	for req in requests:
		try:
			email = (
				frappe.db.get_value("Visitor", req.visitor, "email")
				if req.visitor
				else None
			)
			subject = _("Reminder: your visit tomorrow at {0}").format(
				req.location_gate or "our facility"
			)
			message = _(
				"<p>Hi <b>{0}</b>,</p>"
				"<p>This is a reminder that your visit is scheduled for "
				"<b>tomorrow, {1}</b> ({2} to {3}) at <b>{4}</b>.</p>"
				"<p>Your host is <b>{5}</b>. Please carry a valid ID proof.</p>"
			).format(
				req.visitor_name or "-",
				req.visit_date,
				req.expected_in_time or "-",
				req.expected_out_time or "-",
				req.location_gate or "-",
				req.host_name or "-",
			)
			if email:
				frappe.sendmail(
					recipients=email,
					subject=subject,
					message=message,
					reference_doctype="Visitor Request",
					reference_name=req.name,
				)
			if req.visitor_phone:
				_send_sms(
					req.visitor_phone,
					_("Reminder: your visit is tomorrow ({0}) at {1}, {2} to {3}.").format(
						req.visit_date,
						req.location_gate or "the facility",
						req.expected_in_time or "-",
						req.expected_out_time or "-",
					),
				)
			frappe.db.set_value(
				"Visitor Request", req.name, "day_before_reminder_sent", 1
			)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: day-before reminder failed"),
				message=frappe.get_traceback(),
			)


def send_expected_tomorrow_digest():
	"""Daily job (5 PM): email the list of expected visitors for tomorrow to the
	configured roles (default Security Officer + Reception)."""
	roles = _config_list("visitor_pass_digest_roles", ["Security Officer", "Reception"])
	recipients = _role_emails(roles)
	if not recipients:
		return
	tomorrow = add_to_date(getdate(), days=1)
	requests = frappe.get_all(
		"Visitor Request",
		filters={"docstatus": 1, "workflow_state": "Approved", "visit_date": str(tomorrow)},
		fields=[
			"name",
			"visitor_name",
			"expected_in_time",
			"expected_out_time",
			"location_gate",
			"host_name",
			"purpose",
			"vehicle_number",
		],
		order_by="location_gate, expected_in_time",
		ignore_permissions=True,
	)
	if not requests:
		return
	rows = "".join(
		"<tr><td>{0}</td><td>{1}</td><td>{2} - {3}</td><td>{4}</td><td>{5}</td><td>{6}</td></tr>".format(
			frappe.utils.escape_html(r.visitor_name or "-"),
			frappe.utils.escape_html(r.purpose or "-"),
			r.expected_in_time or "-",
			r.expected_out_time or "-",
			frappe.utils.escape_html(r.location_gate or "-"),
			frappe.utils.escape_html(r.host_name or "-"),
			frappe.utils.escape_html(r.vehicle_number or "-"),
		)
		for r in requests
	)
	html = _(
		"<h3>Expected Visitors - {0}</h3>"
		"<p>{1} approved visit(s) scheduled for tomorrow.</p>"
		"<table border='1' cellpadding='6' style='border-collapse:collapse'>"
		"<tr><th>Visitor</th><th>Purpose</th><th>Window</th><th>Gate</th><th>Host</th><th>Vehicle</th></tr>"
		"{2}</table>"
	).format(tomorrow, len(requests), rows)
	try:
		frappe.sendmail(
			recipients=sorted(recipients),
			subject=_("Expected Visitors tomorrow ({0}) - {1} visit(s)").format(
				tomorrow, len(requests)
			),
			message=html,
		)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: expected-tomorrow digest failed"),
			message=frappe.get_traceback(),
		)


def send_end_of_day_reconciliation_digest():
	"""Daily job (7 PM): email the Visitor Reconciliation summary for today to
	Security + System Manager, with a CSV attachment of flagged records."""
	roles = _config_list(
		"visitor_pass_recon_digest_roles", ["Security Officer", "System Manager"]
	)
	recipients = _role_emails(roles)
	if not recipients:
		return
	today = getdate()
	try:
		from visitor_pass_tracker.visitor_pass_tracker.report.visitor_reconciliation.visitor_reconciliation import (  # noqa
			execute as reconciliation_execute,
		)

		_columns, data, _message, _chart, summary, _skip = reconciliation_execute(
			{"from_date": str(today), "to_date": str(today)}
		)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: reconciliation digest failed"),
			message=frappe.get_traceback(),
		)
		return

	summary_html = "".join(
		"<tr><td>{0}</td><td style='text-align:center'><b>{1}</b></td></tr>".format(
			frappe.utils.escape_html(item["label"]), item["value"]
		)
		for item in summary
	)
	anomalies = [row for row in data if row["flag"] in ("No-show", "Overstay", "Unauthorized")]
	preview_html = ""
	if anomalies:
		preview = anomalies[:20]
		preview_html = _(
			"<h4>Flags needing attention (first {0})</h4>"
			"<table border='1' cellpadding='6' style='border-collapse:collapse'>"
			"<tr><th>Flag</th><th>Pass</th><th>Visitor</th><th>Gate</th><th>Valid Till</th></tr>"
		).format(len(preview))
		for row in preview:
			visitor_name = (
				frappe.db.get_value("Visitor", row["visitor"], "visitor_name")
				if row.get("visitor")
				else None
			) or "-"
			preview_html += "<tr><td>{0}</td><td>{1}</td><td>{2}</td><td>{3}</td><td>{4}</td></tr>".format(
				frappe.utils.escape_html(row["flag"]),
				frappe.utils.escape_html(row.get("entry_pass") or "-"),
				frappe.utils.escape_html(visitor_name),
				frappe.utils.escape_html(row.get("gate") or "-"),
				frappe.utils.escape_html(str(row.get("valid_till") or "-")),
			)
		preview_html += "</table>"

	html = _(
		"<h3>End-of-day reconciliation - {0}</h3>"
		"<table border='1' cellpadding='6' style='border-collapse:collapse'>{1}</table>"
	).format(today, summary_html) + preview_html

	try:
		frappe.sendmail(
			recipients=sorted(recipients),
			subject=_("End-of-day reconciliation ({0})").format(today),
			message=html,
			attachments=[
				{
					"fname": "reconciliation_{0}.csv".format(today),
					"fcontent": _recon_csv(anomalies),
				}
			],
		)
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: reconciliation digest email failed"),
			message=frappe.get_traceback(),
		)


def _recon_csv(rows):
	"""CSV of flagged reconciliation records (attachment for the digest)."""
	import csv
	import io

	output = io.StringIO()
	writer = csv.writer(output)
	writer.writerow(
		["flag", "type", "entry_pass", "visitor", "gate", "valid_till", "last_entry", "last_exit"]
	)
	for row in rows:
		writer.writerow(
			[
				row.get("flag", ""),
				row.get("type", ""),
				row.get("entry_pass", ""),
				row.get("visitor", ""),
				row.get("gate", ""),
				row.get("valid_till", ""),
				row.get("last_entry", ""),
				row.get("last_exit", ""),
			]
		)
	return output.getvalue()


def merge_duplicate_visitors():
	"""Nightly job: merge Visitor masters sharing the same phone number. The
	most complete record becomes primary; child documents (requests, passes,
	gate logs, blacklist records) are relinked, missing fields are absorbed,
	and the duplicates are marked via the hidden `merged_into` link. Records
	are never deleted."""
	visitors = frappe.get_all(
		"Visitor",
		fields=[
			"name",
			"visitor_name",
			"phone",
			"email",
			"company_name",
			"id_proof_type",
			"id_proof_number",
			"photo",
			"modified",
			"is_blacklisted",
			"merged_into",
			"notes",
		],
		order_by="modified desc",
		limit_page_length=5000,
		ignore_permissions=True,
	)
	by_phone = {}
	for v in visitors:
		if v.merged_into:
			continue
		phone = _normalize_phone(v.phone)
		if len(phone) < 7:
			continue
		by_phone.setdefault(phone, []).append(v)

	merged_total = 0
	for group in by_phone.values():
		if len(group) < 2:
			continue
		try:
			merged_total += _merge_visitor_group(group)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: visitor merge failed"),
				message=frappe.get_traceback(),
			)
	if merged_total:
		frappe.log_error(
			title=_("Visitor Pass Tracker: merged duplicate visitors"),
			message=_("Merged {0} duplicate visitor record(s)").format(merged_total),
		)


def _merge_visitor_group(group):
	"""Merge one phone-group of visitors into the most complete record.
	Returns the number of records merged away."""
	def _completeness(v):
		score = 0
		if v.email:
			score += 2
		if v.company_name:
			score += 1
		if v.id_proof_number:
			score += 1
		if v.photo:
			score += 1
		return score

	primary = max(
		group, key=lambda v: (_completeness(v), v.modified or get_datetime("2000-01-01"))
	)
	primary_name = frappe.db.get_value(
		"Visitor", primary.name, ["visitor_name", "phone"], as_dict=True
	)
	merged_count = 0
	for dup in group:
		if dup.name == primary.name:
			continue
		_relink_child_documents(dup.name, primary.name, primary_name)
		_absorb_missing_fields(primary, dup)
		frappe.db.set_value("Visitor", dup.name, "merged_into", primary.name)
		merged_count += 1
	# re-save the primary so the blacklist flag recomputes after relinking
	try:
		frappe.get_doc("Visitor", primary.name).save(ignore_permissions=True)
	except Exception:
		# a legacy short phone can trip validate_phone - the relinks already
		# happened, so just log and continue
		frappe.log_error(
			title=_("Visitor Pass Tracker: primary visitor re-save failed"),
			message=frappe.get_traceback(),
		)
	return merged_count


def _relink_child_documents(from_visitor, to_visitor, primary_name=None):
	"""Re-point every child document from the duplicate to the primary visitor,
	refreshing fetched name/phone fields so logs and requests stay accurate."""
	for doctype, fieldname in {
		"Visitor Request": "visitor",
		"Entry Pass": "visitor",
		"Gate Log Entry": "visitor",
		"Blacklisted Visitor": "visitor",
	}.items():
		for name in frappe.get_all(
			doctype, filters={fieldname: from_visitor}, pluck="name"
		):
			frappe.db.set_value(doctype, name, fieldname, to_visitor)
			if primary_name and doctype == "Gate Log Entry":
				frappe.db.set_value(doctype, name, "visitor_name", primary_name.get("visitor_name"))
			elif primary_name and doctype == "Visitor Request":
				frappe.db.set_value(
					doctype, name, "visitor_name", primary_name.get("visitor_name")
				)
				frappe.db.set_value(doctype, name, "visitor_phone", primary_name.get("phone"))


def _absorb_missing_fields(primary, dup):
	"""Fill empty fields on the primary from the duplicate (never overwrite)."""
	for fieldname in [
		"email",
		"company_name",
		"id_proof_type",
		"id_proof_number",
		"linked_contact",
	]:
		current = frappe.db.get_value("Visitor", primary.name, fieldname)
		incoming = dup.get(fieldname)
		if not current and incoming:
			frappe.db.set_value("Visitor", primary.name, fieldname, incoming)


def _build_ics(pass_name, valid_from, valid_till, summary, location, description=""):
	"""Minimal iCalendar (RFC 5545) VEVENT string for email attachments."""
	def _fmt(dt):
		return get_datetime(dt).strftime("%Y%m%dT%H%M%S")

	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		"PRODID:-//Visitor Pass Tracker//EN",
		"BEGIN:VEVENT",
		"UID:{0}@visitor-pass-tracker".format(pass_name),
		"DTSTAMP:{0}".format(_fmt(now_datetime())),
		"DTSTART:{0}".format(_fmt(valid_from)),
		"DTEND:{0}".format(_fmt(valid_till)),
		"SUMMARY:{0}".format(summary),
		"LOCATION:{0}".format(location or ""),
		"DESCRIPTION:{0}".format(description),
		"END:VEVENT",
		"END:VCALENDAR",
	]
	return "\r\n".join(lines)
