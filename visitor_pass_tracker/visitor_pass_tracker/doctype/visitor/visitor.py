import frappe
from frappe import _
from frappe.model.document import Document

from visitor_pass_tracker.utils import _normalize_phone, check_blacklist


class Visitor(Document):
	def validate(self):
		self.validate_phone()
		self.update_blacklist_status()

	def after_insert(self):
		self.create_linked_contact()
		self.create_draft_request_for_guest()

	def create_draft_request_for_guest(self):
		"""Pre-registration from the public website (portal Pre-Register tab or
		the visitor-pre-registration web form) auto-creates a Draft Visitor
		Request so the desk sees it immediately. Visitors created from the desk
		(by Security / Reception / System Manager sessions) are left untouched -
		this only fires for Guest sessions."""
		if frappe.session.user != "Guest":
			return
		from visitor_pass_tracker.utils import ensure_draft_visitor_request

		ensure_draft_visitor_request(self.name)

	def create_linked_contact(self):
		"""Auto-create an ERPNext Contact for known external visitors so they can
		be reused across the suite (linked via `linked_contact`).

		Skips visitors without an email/phone and never raises - failures are
		logged so Visitor creation is never blocked.
		"""
		if self.linked_contact or not (self.email or self.phone):
			return
		try:
			if self.email:
				existing = frappe.db.get_value("Contact", {"email_id": self.email}, "name")
				if existing:
					self.db_set("linked_contact", existing)
					return
			contact = frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": self.visitor_name,
					"email_id": self.email or None,
					"phone": self.phone or None,
					"company_name": self.company_name or None,
				}
			).insert(ignore_permissions=True)
			self.db_set("linked_contact", contact.name)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: Contact creation failed"),
				message=frappe.get_traceback(),
			)

	def validate_phone(self):
		if self.phone and len(_normalize_phone(self.phone)) < 10:
			frappe.throw(_("Phone number must contain at least 10 digits"))

	def update_blacklist_status(self):
		"""Keep the read-only flag in sync with the Active blacklist."""
		self.is_blacklisted = 1 if check_blacklist(self) else 0
