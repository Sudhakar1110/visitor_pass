import frappe
from frappe import _
from frappe.model.document import Document


class EntryPass(Document):
	def validate(self):
		self.validate_validity_window()

	def validate_validity_window(self):
		if self.valid_from and self.valid_till and self.valid_till <= self.valid_from:
			frappe.throw(_("Valid Till must be after Valid From"))


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
