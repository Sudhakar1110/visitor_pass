import frappe
from frappe.model.document import Document


class BlacklistedVisitor(Document):
	def validate(self):
		self.set_blacklisted_by()
		self.sync_from_visitor()

	def on_update(self):
		"""Keep the linked Visitor flag and open requests in sync whenever a
		Blacklisted Visitor record is created or its status changes."""
		self.sync_visitor_blacklist_flag()
		self.sync_related_requests()

	def on_trash(self):
		"""Recompute the linked Visitor's flag when the record is deleted (it may
		have been the only active record keeping the visitor flagged)."""
		self.sync_visitor_blacklist_flag()

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

	def sync_visitor_blacklist_flag(self):
		"""Recompute the linked Visitor's read-only `is_blacklisted` flag so a
		"Lifted" (or deleted) blacklist record clears it immediately."""
		if not self.visitor or not frappe.db.exists("Visitor", self.visitor):
			return
		visitor = frappe.get_doc("Visitor", self.visitor)
		visitor.update_blacklist_status()
		frappe.db.set_value(
			"Visitor", self.visitor, "is_blacklisted", 1 if visitor.is_blacklisted else 0
		)

	def sync_related_requests(self):
		"""Re-flag open Visitor Requests for the same visitor so `blacklist_status`
		is current. Does not auto-apply workflow transitions - the flag is simply
		kept accurate for the next approval step."""
		if not self.visitor:
			return
		open_requests = frappe.get_all(
			"Visitor Request",
			filters={
				"visitor": self.visitor,
				"docstatus": ["<", 2],
				"workflow_state": ["not in", ["Approved", "Rejected"]],
			},
			pluck="name",
		)
		for name in open_requests:
			doc = frappe.get_doc("Visitor Request", name)
			doc.validate_blacklist()
			frappe.db.set_value(
				"Visitor Request", name, "blacklist_status", doc.blacklist_status
			)
