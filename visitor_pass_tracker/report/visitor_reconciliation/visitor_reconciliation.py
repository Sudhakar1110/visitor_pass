import frappe
from frappe import _

FLAG_INDICATORS = {
	"No-show": "Yellow",
	"Overstay": "Orange",
	"Unauthorized": "Red",
	"On-site": "Green",
	"Completed": "Green",
}

FLAG_COLORS = {
	"No-show": "#f0c419",
	"Overstay": "#ffa00a",
	"Unauthorized": "#e74c3c",
	"On-site": "#2ecc71",
	"Completed": "#2ecc71",
}

ALL_FLAGS = ("No-show", "Overstay", "Unauthorized", "On-site", "Completed")


def execute(filters=None):
	"""Script Report entry point. Returns columns, data, message, chart,
	report summary and skip-total-row (Frappe 15 supports 6 return values)."""
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
		{"fieldname": "flag", "label": _("Flag"), "fieldtype": "Data", "width": 130},
		{"fieldname": "indicator", "label": _("Indicator"), "fieldtype": "Data", "width": 110},
		{"fieldname": "type", "label": _("Type"), "fieldtype": "Data", "width": 100},
		{
			"fieldname": "entry_pass",
			"label": _("Entry Pass"),
			"fieldtype": "Link",
			"options": "Entry Pass",
			"width": 140,
		},
		{
			"fieldname": "visitor",
			"label": _("Visitor"),
			"fieldtype": "Link",
			"options": "Visitor",
			"width": 140,
		},
		{"fieldname": "company", "label": _("Company"), "fieldtype": "Data", "width": 150},
		{"fieldname": "purpose", "label": _("Purpose"), "fieldtype": "Data", "width": 110},
		{
			"fieldname": "gate",
			"label": _("Gate"),
			"fieldtype": "Link",
			"options": "Gate",
			"width": 120,
		},
		{"fieldname": "valid_from", "label": _("Valid From"), "fieldtype": "Datetime", "width": 150},
		{"fieldname": "valid_till", "label": _("Valid Till"), "fieldtype": "Datetime", "width": 150},
		{"fieldname": "last_entry", "label": _("Last Entry Scan"), "fieldtype": "Datetime", "width": 150},
		{"fieldname": "last_exit", "label": _("Last Exit Scan"), "fieldtype": "Datetime", "width": 150},
		{"fieldname": "count", "label": _("Count"), "fieldtype": "Int", "width": 70},
	]


def get_data(filters):
	now = frappe.utils.now_datetime()
	from_date = filters.get("from_date")
	to_date = filters.get("to_date")
	gate = filters.get("gate")
	flag_filter = filters.get("flag")

	data = []
	passes = _get_passes(from_date, to_date, gate)
	for entry in passes:
		scans = frappe.get_all(
			"Gate Log Entry",
			filters={"entry_pass": entry.name, "docstatus": ("<", 2), "is_authorized": 1},
			fields=["scan_type", "scan_time"],
			order_by="scan_time",
		)
		entry_scans = [s for s in scans if s.scan_type == "Entry"]
		exit_scans = [s for s in scans if s.scan_type == "Exit"]

		if not scans:
			flag = "No-show"
		elif entry_scans and exit_scans:
			flag = "Completed"
		elif entry_scans and entry.valid_till and frappe.utils.get_datetime(entry.valid_till) < now:
			flag = "Overstay"
		else:
			flag = "On-site"

		data.append(
			{
				"flag": flag,
				"indicator": FLAG_INDICATORS[flag],
				"type": _("Pass"),
				"entry_pass": entry.name,
				"visitor": entry.visitor,
				"company": frappe.db.get_value("Visitor", entry.visitor, "company_name"),
				"purpose": frappe.db.get_value(
					"Visitor Request", entry.visitor_request, "purpose"
				),
				"gate": entry.location_gate,
				"valid_from": entry.valid_from,
				"valid_till": entry.valid_till,
				"last_entry": entry_scans[-1].scan_time if entry_scans else None,
				"last_exit": exit_scans[-1].scan_time if exit_scans else None,
				"count": 1,
			}
		)

	# Unauthorized scans: gate logs that could not be matched to a valid pass
	log_filters = {"is_authorized": 0, "docstatus": ("<", 2)}
	if from_date and to_date:
		log_filters["scan_time"] = ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]
	if gate:
		log_filters["gate"] = gate

	unauthorized_logs = frappe.get_all(
		"Gate Log Entry",
		filters=log_filters,
		fields=["entry_pass", "visitor", "gate", "scan_type", "scan_time"],
		order_by="scan_time desc",
	)
	for log in unauthorized_logs:
		data.append(
			{
				"flag": "Unauthorized",
				"indicator": FLAG_INDICATORS["Unauthorized"],
				"type": _("Gate Log"),
				"entry_pass": log.entry_pass,
				"visitor": log.visitor,
				"company": None,
				"purpose": None,
				"gate": log.gate,
				"valid_from": None,
				"valid_till": None,
				"last_entry": log.scan_time if log.scan_type == "Entry" else None,
				"last_exit": log.scan_time if log.scan_type == "Exit" else None,
				"count": 1,
			}
		)

	if flag_filter and flag_filter in ALL_FLAGS:
		data = [row for row in data if row["flag"] == flag_filter]

	return data


def _get_passes(from_date, to_date, gate):
	filters = {"docstatus": ("<", 2)}
	if from_date and to_date:
		filters["valid_from"] = ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]
	if gate:
		filters["location_gate"] = gate
	return frappe.get_all(
		"Entry Pass",
		filters=filters,
		fields=[
			"name",
			"visitor",
			"visitor_request",
			"location_gate",
			"valid_from",
			"valid_till",
		],
		order_by="valid_from desc",
	)


def get_chart(data):
	"""Bar chart of record counts per flag (used by the dashboard card)."""
	counts = {}
	for row in data:
		counts[row["flag"]] = counts.get(row["flag"], 0) + 1
	labels = list(counts.keys())
	values = [counts[label] for label in labels]
	colors = [FLAG_COLORS.get(label, "#d1d8dd") for label in labels]
	return {
		"data": {
			"labels": labels,
			"datasets": [{"name": _("Records"), "values": values}],
		},
		"type": "bar",
		"colors": colors,
	}


def get_report_summary(data):
	flags = [row["flag"] for row in data]
	return [
		{"value": len(data), "label": _("Total Records"), "indicator": "blue"},
		{"value": flags.count("On-site"), "label": _("On-site"), "indicator": "green"},
		{"value": flags.count("Overstay"), "label": _("Overstay"), "indicator": "orange"},
		{"value": flags.count("No-show"), "label": _("No-show"), "indicator": "yellow"},
		{"value": flags.count("Unauthorized"), "label": _("Unauthorized"), "indicator": "red"},
	]
