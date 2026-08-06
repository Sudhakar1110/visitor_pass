import frappe
from frappe import _

AUTHORIZED_COLOR = "#2ecc71"
UNAUTHORIZED_COLOR = "#e74c3c"


def execute(filters=None):
	"""Script Report entry point - printable daily visitor register (logbook).

	One row per gate scan (entry/exit) with the visitor, company, host, gate,
	device and authorization status. Returns the standard 6-tuple so the
	report chart and summary render like the reconciliation report.
	"""
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
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 110},
		{"fieldname": "time", "label": _("Time"), "fieldtype": "Time", "width": 100},
		{
			"fieldname": "gate",
			"label": _("Gate"),
			"fieldtype": "Link",
			"options": "Gate",
			"width": 130,
		},
		{"fieldname": "scan_type", "label": _("Scan Type"), "fieldtype": "Data", "width": 90},
		{"fieldname": "authorized", "label": _("Authorized"), "fieldtype": "Data", "width": 110},
		{
			"fieldname": "entry_pass",
			"label": _("Entry Pass"),
			"fieldtype": "Link",
			"options": "Entry Pass",
			"width": 150,
		},
		{
			"fieldname": "visitor",
			"label": _("Visitor"),
			"fieldtype": "Link",
			"options": "Visitor",
			"width": 140,
		},
		{"fieldname": "company", "label": _("Company"), "fieldtype": "Data", "width": 150},
		{
			"fieldname": "host",
			"label": _("Host"),
			"fieldtype": "Link",
			"options": "Employee",
			"width": 140,
		},
		{"fieldname": "device", "label": _("Device"), "fieldtype": "Data", "width": 120},
		{"fieldname": "remarks", "label": _("Remarks"), "fieldtype": "Data", "width": 220},
	]


def get_data(filters):
	from_date = filters.get("from_date")
	to_date = filters.get("to_date")
	gate = filters.get("gate")
	scan_type = filters.get("scan_type")
	authorized = filters.get("authorized")  # All / Authorized / Unauthorized

	conditions = ["gle.docstatus < 2"]
	params = {}
	if from_date and to_date:
		conditions.append("gle.scan_time BETWEEN %(from_date)s AND %(to_date)s")
		params["from_date"] = f"{from_date} 00:00:00"
		params["to_date"] = f"{to_date} 23:59:59"
	elif from_date:
		conditions.append("gle.scan_time >= %(from_date)s")
		params["from_date"] = f"{from_date} 00:00:00"
	elif to_date:
		conditions.append("gle.scan_time <= %(to_date)s")
		params["to_date"] = f"{to_date} 23:59:59"
	if gate:
		conditions.append("gle.gate = %(gate)s")
		params["gate"] = gate
	if scan_type in ("Entry", "Exit"):
		conditions.append("gle.scan_type = %(scan_type)s")
		params["scan_type"] = scan_type
	if authorized == "Authorized":
		conditions.append("gle.is_authorized = 1")
	elif authorized == "Unauthorized":
		conditions.append("gle.is_authorized = 0")

	# Data-visibility: non-security users (Employee / Department Head) only see
	# scans of the visits they host / created / head - never the whole building.
	from visitor_pass_tracker.utils import get_pass_scope_condition

	scope_cond, scope_params = get_pass_scope_condition(alias="ep")
	if scope_cond:
		conditions.append(scope_cond)
		params.update(scope_params)

	rows = frappe.db.sql(
		f"""
		SELECT
			DATE(gle.scan_time) AS `date`,
			TIME(gle.scan_time) AS `time`,
			gle.gate,
			gle.scan_type,
			gle.is_authorized,
			gle.entry_pass,
			gle.visitor,
			vis.company_name AS company,
			ep.host,
			gle.scanned_by_device AS device,
			gle.remarks
		FROM `tabGate Log Entry` gle
		LEFT JOIN `tabVisitor` vis ON vis.name = gle.visitor
		LEFT JOIN `tabEntry Pass` ep ON ep.name = gle.entry_pass
		WHERE {" AND ".join(conditions)}
		ORDER BY gle.scan_time
		""",
		params,
		as_dict=True,
	)

	for row in rows:
		row["authorized"] = _("Yes") if row["is_authorized"] else _("No")
	return rows


def get_chart(data):
	"""Bar chart of scan counts per day (used by the report view)."""
	counts = {}
	for row in data:
		counts[row["date"]] = counts.get(row["date"], 0) + 1
	labels = sorted(counts.keys())
	values = [counts[label] for label in labels]
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": _("Scans"), "values": values}],
		},
		"type": "bar",
	}


def get_report_summary(data):
	entries = sum(1 for row in data if row["scan_type"] == "Entry")
	exits = sum(1 for row in data if row["scan_type"] == "Exit")
	authorized = sum(1 for row in data if row["is_authorized"])
	unauthorized = len(data) - authorized
	return [
		{"value": len(data), "label": _("Total Scans"), "indicator": "blue"},
		{"value": entries, "label": _("Entries"), "indicator": "green"},
		{"value": exits, "label": _("Exits"), "indicator": "green"},
		{"value": authorized, "label": _("Authorized"), "indicator": "green"},
		{"value": unauthorized, "label": _("Unauthorized"), "indicator": "red"},
	]
