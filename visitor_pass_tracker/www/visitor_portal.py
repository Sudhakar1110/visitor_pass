import frappe

no_cache = 1


def get_context(context):
	"""Visitor Portal page controller.

	The page itself is static (all dynamic work happens through the
	guest-whitelisted APIs in `visitor_pass_tracker.portal`), so this only
	sets the title and the list of pre-registration fields for the form.
	"""
	context.title = "Visitor Pass Portal"
	context.no_cache = 1
	context.website_route = "/visitor_portal"

	field = frappe.get_meta("Visitor").get_field("id_proof_type")
	options = (field.options if field else "") or ""
	context.id_proof_options = options.splitlines() or ["Aadhar", "Passport", "Driving License", "Other"]
	return context
