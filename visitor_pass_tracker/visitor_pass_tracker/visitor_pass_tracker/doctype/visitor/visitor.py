import frappe
from frappe import _
from frappe.model.document import Document

from visitor_pass_tracker.visitor_pass_tracker.utils import _normalize_phone, check_blacklist


class Visitor(Document):
	def validate(self):
		self.validate_phone()
		self.update_blacklist_status()

	def validate_phone(self):
		if self.phone and len(_normalize_phone(self.phone)) < 10:
			frappe.throw(_("Phone number must contain at least 10 digits"))

	def update_blacklist_status(self):
		"""Keep the read-only flag in sync with the Active blacklist."""
		self.is_blacklisted = 1 if check_blacklist(self) else 0
