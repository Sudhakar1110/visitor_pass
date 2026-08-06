frappe.query_reports["Visitor Reconciliation"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "gate",
			label: __("Gate"),
			fieldtype: "Link",
			options: "Gate",
		},
		{
			fieldname: "flag",
			label: __("Show"),
			fieldtype: "Select",
			options: ["All Flags", "Scheduled", "No-show", "Overstay", "Unauthorized", "On-site", "Completed"],
		},
	],
};
