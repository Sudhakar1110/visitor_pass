import frappe
from frappe import _
from frappe.model.document import Document


class EntryPass(Document):
	def validate(self):
		self.validate_validity_window()

	def validate_validity_window(self):
		if self.valid_from and self.valid_till and self.valid_till <= self.valid_from:
			frappe.throw(_("Valid Till must be after Valid From"))
