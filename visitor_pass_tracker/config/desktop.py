from frappe import _


def get_data():
	return [
		{
			"module_name": "visitor_pass_tracker",
			"type": "module",
			"label": _("Visitor Pass Tracker"),
			"color": "#2490ef",
			"icon": "octicon octicon-person",
			"description": _("Automated visitor entry passes across multiple gates/locations"),
		}
	]
