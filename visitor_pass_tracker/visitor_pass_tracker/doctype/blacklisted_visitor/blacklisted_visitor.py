import frappe
from frappe.model.document import Document


class BlacklistedVisitor(Document):
	def validate(self):
		self.set_blacklisted_by()
		self.sync_from_visitor()

	def set_blacklisted_by(self):
		if not self.blacklisted_by:
			self.blacklisted_by = frappe.session.user

	def sync_from_visitor(self):
		"""Pre-fill name/phone/id proof from the linked Visitor when available."""
		if self.visitor:
			visitor = frappe.db.get_value(
				"Visitor",
				self.visitor,
				["visitor_name", "phone", "id_proof_number"],
				as_dict=True,
			)
			if visitor:
				if not self.visitor_name:
					self.visitor_name = visitor.visitor_name
				if not self.phone:
					self.phone = visitor.phone
				if not self.id_proof_number:
					self.id_proof_number = visitor.id_proof_number
