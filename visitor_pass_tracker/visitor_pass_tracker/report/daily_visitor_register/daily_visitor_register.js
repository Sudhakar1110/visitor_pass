frappe.query_reports["Daily Visitor Register"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "gate",
			label: __("Gate"),
			fieldtype: "Link",
			options: "Gate",
		},
		{
			fieldname: "scan_type",
			label: __("Scan Type"),
			fieldtype: "Select",
			options: ["", "Entry", "Exit"],
		},
		{
			fieldname: "authorized",
			label: __("Status"),
			fieldtype: "Select",
			options: ["All", "Authorized", "Unauthorized"],
		},
	],
};
