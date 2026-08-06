frappe.query_reports["Expected Visitors"] = {
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
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: [
				"All Statuses",
				"Blacklist Check",
				"Pending Host Approval",
				"Pending Department Approval",
				"Pending Security Approval",
				"Approved",
			],
		},
	],
};
