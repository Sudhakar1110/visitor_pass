import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.workflow import apply_workflow

from visitor_pass_tracker.utils import (
	_normalize_phone,
	check_blacklist,
	create_entry_pass_for_request,
)


class VisitorRequest(Document):
	def validate(self):
		self.match_existing_visitor()
		self.validate_blacklist()
		self.validate_visit_window()
		self.validate_no_overlapping_visit()

	def before_submit(self):
		self.validate_visit_date()

	def match_existing_visitor(self):
		"""Duplicate detection - when no Visitor is linked but a phone number is
		available, auto-link the existing Visitor master with the same phone
		instead of letting a duplicate slip through."""
		if self.visitor:
			return
		phone = _normalize_phone(self.visitor_phone)
		if not phone:
			return
		# fast path - exact phone match first (stored value equals the raw query)
		exact = frappe.get_all("Visitor", filters={"phone": phone}, fields=["name"], limit=1)
		if exact:
			self.visitor = exact[0].name
			return
		visitors = frappe.get_all("Visitor", fields=["name", "phone"], order_by="modified desc")
		for v in visitors:
			if _normalize_phone(v.phone) == phone:
				self.visitor = v.name
				break

	def validate_visit_date(self):
		if self.visit_date and self.visit_date < frappe.utils.today():
			frappe.throw(_("Visit Date cannot be in the past"))

	def validate_no_overlapping_visit(self):
		"""A visitor must not hold two Active visits with overlapping windows on
		the same day - prevents duplicate passes for the same person/gate."""
		if not self.visitor or not self.visit_date:
			return
		if not (self.expected_in_time and self.expected_out_time):
			return
		conflict = self._find_overlapping_visit()
		if conflict:
			frappe.throw(
				_("Visitor {0} already has {1} with an overlapping visit window on {2}. "
				  "Choose a non-overlapping time or use a single request.").format(
					self.visitor_name or self.visitor, conflict, self.visit_date
				)
			)

	def _find_overlapping_visit(self):
		# 1) overlapping submitted requests (excluding self)
		req_filters = {
			"visitor": self.visitor,
			"visit_date": self.visit_date,
			"docstatus": 1,
			"workflow_state": ["not in", ["Rejected"]],
		}
		if self.name:
			req_filters["name"] = ["!=", self.name]
		# ignore_permissions: this is a data-integrity guard and must consider
		# requests raised by other hosts, not only the current user's own
		for other in frappe.get_all(
			"Visitor Request",
			filters=req_filters,
			fields=["name", "expected_in_time", "expected_out_time"],
			ignore_permissions=True,
		):
			if self._time_windows_overlap(
				other.get("expected_in_time"), other.get("expected_out_time")
			):
				return "Visitor Request {0}".format(other.name)

		# 2) existing non-revoked Entry Passes for the same visitor/day
		#    (excluding this request's own pass)
		pass_filters = {
			"visitor": self.visitor,
			"status": ["in", ["Active", "Expired"]],
			"valid_from": [
				"between",
				[f"{self.visit_date} 00:00:00", f"{self.visit_date} 23:59:59"],
			],
		}
		if self.name:
			pass_filters["visitor_request"] = ["!=", self.name]
		for ep in frappe.get_all(
			"Entry Pass",
			filters=pass_filters,
			fields=["name", "valid_from", "valid_till"],
			ignore_permissions=True,
		):
			o_in = str(ep.valid_from)[11:19] if ep.valid_from else None
			o_out = str(ep.valid_till)[11:19] if ep.valid_till else None
			if self._time_windows_overlap(o_in, o_out):
				return "Entry Pass {0}".format(ep.name)
		return None

	def _time_windows_overlap(self, o_in, o_out):
		if not o_in or not o_out:
			return False
		return self.expected_in_time < o_out and o_in < self.expected_out_time

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

		- Manual rejections require a rejection reason (the automatic
		  blacklist rejection is exempt - the blacklist record is the reason).
		- Delivery visits skip the Department Head step automatically.
		- On the final "Approved" state, the Entry Pass + QR is auto-created.
		"""
		if (
			self.workflow_state == "Rejected"
			and self.blacklist_status != "Flagged"
			and not self.rejection_reason
		):
			frappe.throw(_("Please provide a Rejection Reason before rejecting the request"))

		if self.workflow_state == "Pending Department Approval" and self.purpose == "Delivery":
			apply_workflow(self, "Skip for Delivery")

		if self.workflow_state == "Approved":
			create_entry_pass_for_request(self)

	def on_cancel(self):
		self.revoke_entry_pass_on_cancel()

	def revoke_entry_pass_on_cancel(self):
		"""Cancelling a submitted request must not leave an Active (scannable)
		Entry Pass behind - revoke it so the gate stops accepting it."""
		pass_name = frappe.db.get_value("Entry Pass", {"visitor_request": self.name}, "name")
		if not pass_name:
			return
		pass_doc = frappe.get_doc("Entry Pass", pass_name)
		if pass_doc.status in ("Active", "Expired"):
			pass_doc.status = "Revoked"
			pass_doc.save(ignore_permissions=True)


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
