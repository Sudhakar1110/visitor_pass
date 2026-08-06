import frappe
from frappe import _

OPEN_STATES = (
	"Blacklist Check",
	"Pending Host Approval",
	"Pending Department Approval",
	"Pending Security Approval",
	"Approved",
)


def execute(filters=None):
	"""Script Report entry point - operational view of visitors expected on a
	date range, with their arrival status (has an authorized Entry scan)."""
	filters = filters or {}
	data = get_data(filters)
	return (
		get_columns(),
		data,
		None,
		get_chart(data),
		get_report_summary(data),
		0,
	)


def get_columns():
	return [
		{"fieldname": "date", "label": _("Visit Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "end_date", "label": _("End Date"), "fieldtype": "Date", "width": 100},
		{
			"fieldname": "visitor",
			"label": _("Visitor"),
			"fieldtype": "Link",
			"options": "Visitor",
			"width": 150,
		},
		{"fieldname": "visitor_name", "label": _("Visitor Name"), "fieldtype": "Data", "width": 150},
		{"fieldname": "company", "label": _("Company"), "fieldtype": "Data", "width": 150},
		{
			"fieldname": "host",
			"label": _("Host"),
			"fieldtype": "Link",
			"options": "Employee",
			"width": 140,
		},
		{"fieldname": "department", "label": _("Department"), "fieldtype": "Data", "width": 130},
		{"fieldname": "purpose", "label": _("Purpose"), "fieldtype": "Data", "width": 110},
		{
			"fieldname": "gate",
			"label": _("Gate"),
			"fieldtype": "Link",
			"options": "Gate",
			"width": 120,
		},
		{"fieldname": "in_time", "label": _("In Time"), "fieldtype": "Time", "width": 90},
		{"fieldname": "out_time", "label": _("Out Time"), "fieldtype": "Time", "width": 90},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 150},
		{"fieldname": "arrived", "label": _("Arrived"), "fieldtype": "Data", "width": 80},
		{"fieldname": "host_checkin", "label": _("Host Check-in"), "fieldtype": "Datetime", "width": 150},
	]


def get_data(filters):
	from_date = filters.get("from_date")
	to_date = filters.get("to_date")
	gate = filters.get("gate")
	status = filters.get("status")

	req_filters = {
		"docstatus": 1,
		"workflow_state": ["in", list(OPEN_STATES)],
	}
	if from_date and to_date:
		req_filters["visit_date"] = ["between", [from_date, to_date]]
	elif from_date:
		req_filters["visit_date"] = [">=", from_date]
	elif to_date:
		req_filters["visit_date"] = ["<=", to_date]
	if gate:
		req_filters["location_gate"] = gate
	if status and status != "All Statuses":
		req_filters["workflow_state"] = status

	requests = frappe.get_all(
		"Visitor Request",
		filters=req_filters,
		fields=[
			"name",
			"visitor",
			"visitor_name",
			"host",
			"host_name",
			"department",
			"purpose",
			"visit_date",
			"visit_end_date",
			"expected_in_time",
			"expected_out_time",
			"location_gate",
			"workflow_state",
			"host_checkin_time",
		],
		order_by="visit_date, expected_in_time",
	)

	# arrival: an Entry Pass with an authorized Entry scan exists for the request
	arrived_requests = set()
	for r in frappe.get_all(
		"Gate Log Entry",
		filters={"scan_type": "Entry", "is_authorized": 1, "docstatus": ("<", 2)},
		fields=["visitor_request"],
		order_by="scan_time",
	):
		if r.visitor_request:
			arrived_requests.add(r.visitor_request)

	data = []
	for r in requests:
		company = (
			frappe.db.get_value("Visitor", r.visitor, "company_name") if r.visitor else None
		)
		data.append(
			{
				"date": r.visit_date,
				"end_date": r.visit_end_date or r.visit_date,
				"visitor": r.visitor,
				"visitor_name": r.visitor_name,
				"company": company,
				"host": r.host,
				"department": r.department,
				"purpose": r.purpose,
				"gate": r.location_gate,
				"in_time": r.expected_in_time,
				"out_time": r.expected_out_time,
				"status": r.workflow_state,
				"arrived": _("Yes") if r.name in arrived_requests else _("No"),
				"host_checkin": r.host_checkin_time,
			}
		)
	return data


def get_chart(data):
	"""Bar chart of requests by workflow state (arrival counts in the summary)."""
	by_state = {}
	for row in data:
		by_state[row["status"]] = by_state.get(row["status"], 0) + 1
	labels = list(by_state.keys())
	values = [by_state[label] for label in labels]
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": _("Requests"), "values": values}],
		},
		"type": "bar",
	}


def get_report_summary(data):
	arrived = sum(1 for row in data if row["arrived"] == _("Yes"))
	not_arrived = len(data) - arrived
	return [
		{"value": len(data), "label": _("Expected"), "indicator": "blue"},
		{"value": arrived, "label": _("Arrived"), "indicator": "green"},
		{"value": not_arrived, "label": _("Not Arrived"), "indicator": "orange"},
	]
