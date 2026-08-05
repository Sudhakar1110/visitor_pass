import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.workflow import apply_workflow

from visitor_pass_tracker.visitor_pass_tracker.utils import (
	check_blacklist,
	create_entry_pass_for_request,
)


class VisitorRequest(Document):
	def validate(self):
		self.validate_blacklist()
		self.validate_visit_window()

	# ------------------------------------------------------------------
	# Blacklist auto-check (Server Script equivalent)
	# ------------------------------------------------------------------
	def validate_blacklist(self):
		"""Set blacklist_status by matching the linked Visitor against the
		Active blacklist. The workflow rejection happens automatically in
		`on_submit` (see below)."""
		if not self.visitor:
			return
		visitor = frappe.get_doc("Visitor", self.visitor)
		self.blacklist_status = "Flagged" if check_blacklist(visitor) else "Clear"

	def validate_visit_window(self):
		if (
			self.expected_in_time
			and self.expected_out_time
			and self.expected_out_time <= self.expected_in_time
		):
			frappe.throw(_("Expected Out Time must be after Expected In Time"))

	# ------------------------------------------------------------------
	# Workflow automation - the state machine itself lives in the native
	# "Visitor Request Workflow" (Workflow doctype, shipped as a fixture).
	# Only the *automatic* transitions are triggered from code here.
	# ------------------------------------------------------------------
	def on_submit(self):
		"""Draft -> Blacklist Check (auto). The framework has already set the
		workflow state to "Blacklist Check" before this hook runs.

		- match found   -> "Reject: Blacklisted"  (notifies Security role via
		                   the native Notification fixture)
		- clear         -> "Blacklist Check Cleared" -> Pending Host Approval
		"""
		if self.workflow_state == "Blacklist Check":
			if self.blacklist_status == "Flagged":
				apply_workflow(self, "Reject: Blacklisted")
			else:
				apply_workflow(self, "Blacklist Check Cleared")

	def on_update_after_submit(self):
		"""Runs on every save of the submitted request (including the saves
		performed by workflow actions).

		- Delivery visits skip the Department Head step automatically.
		- On the final "Approved" state, the Entry Pass + QR is auto-created.
		"""
		if self.workflow_state == "Pending Department Approval" and self.purpose == "Delivery":
			apply_workflow(self, "Skip for Delivery")

		if self.workflow_state == "Approved":
			create_entry_pass_for_request(self)


# ------------------------------------------------------------------
# Permissions - scoped via the `has_permission` and
# `permission_query_conditions` hooks registered in hooks.py.
# Employees (hosts) and Department Heads only see their own requests.
# ------------------------------------------------------------------
def has_permission(doc, ptype, user=None, debug=False):
	if not user:
		user = frappe.session.user
	if user == "Administrator":
		return True
	roles = frappe.get_roles(user)
	if any(role in roles for role in ("System Manager", "Security Officer", "Reception")):
		return True

	if doc.get("host"):
		host_user = frappe.db.get_value("Employee", doc.host, "user_id")
		if user == host_user or doc.owner == user:
			return True

	if "Department Head" in roles and doc.get("department"):
		return doc.department in _departments_headed_by(user)

	# Users holding the scoped roles must explicitly match the document;
	# otherwise deny (prevents direct-URL access to other people's requests).
	if "Employee" in roles or "Department Head" in roles:
		return False

	return None


def get_permission_query_conditions(user, doctype=None):
	"""Restrict list views for Employee (own requests only) and Department Head
	(department's requests only).

	NOTE: the conditions are AND-ed with the standard role/user-permission
	conditions - they can only restrict, never grant access."""
	if not user:
		user = frappe.session.user
	if user == "Administrator":
		return ""
	roles = frappe.get_roles(user)
	if any(role in roles for role in ("System Manager", "Security Officer", "Reception")):
		return ""

	conditions = []

	if "Department Head" in roles:
		departments = _departments_headed_by(user)
		if departments:
			conditions.append(
				"`tabVisitor Request`.`department` in ({})".format(
					", ".join(frappe.db.escape(d) for d in departments)
				)
			)
		else:
			return "1=0"

	if "Employee" in roles:
		employee = _employee_for_user(user)
		if employee:
			conditions.append(
				"(`tabVisitor Request`.`host` = {0} OR `tabVisitor Request`.`owner` = {1})".format(
					frappe.db.escape(employee), frappe.db.escape(user)
				)
			)
		else:
			conditions.append("`tabVisitor Request`.`owner` = {0}".format(frappe.db.escape(user)))

	return " AND ".join(conditions) if conditions else "1=0"


def _employee_for_user(user):
	return frappe.db.get_value("Employee", {"user_id": user}, "name")


def _departments_headed_by(user):
	"""Departments headed by the user, including sub-departments (lft/rgt tree)."""
	employee = _employee_for_user(user)
	if not employee:
		return []
	headed = frappe.get_all(
		"Department",
		filters={"head_of_department": employee},
		fields=["name", "lft", "rgt"],
	)
	if not headed:
		return []
	tree = frappe.get_all("Department", fields=["name", "lft", "rgt"])
	return [
		d.name for d in tree if any(d.lft >= h.lft and d.rgt <= h.rgt for h in headed)
	]
