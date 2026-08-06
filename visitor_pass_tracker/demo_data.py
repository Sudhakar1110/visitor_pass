"""Demo data generator for the Visitor Pass Tracker app.

Seeds a realistic dataset exercising every doctype and nearly every field:

    Gate                  - 50 gates (company, address, device id, active flag)
    Visitor               - 68 visitors (phone, email, ID proof, company, VIP, notes)
    Visitor Request       - 68 requests across ALL workflow states (Draft,
                            Pending Host/Department/Security Approval, Approved,
                            Rejected - blacklist + manual, Cancelled) with
                            vehicle/escort/multi-day/host check-in data
    Entry Pass            - 50 passes (Active / Used / Expired / Revoked) with a
                            generated QR image attached to every pass
    Gate Log Entry        - ~70 scans (authorized entries/exits, on-site,
                            overstays, no-shows, unauthorized) for the reports
    Blacklisted Visitor   - 50 records (some linked to demo visitors, some
                            standalone, Active + Lifted)

Linked ERPNext masters needed by the app are created too (Company, Addresses,
Employees used as hosts) so the data works on a blank site. Demo records are
tagged with a "[DEMO DATA]" marker so they can be wiped safely.

Usage (run as Administrator):

    bench --site <sitename> execute visitor_pass_tracker.demo_data.create_demo_data
    bench --site <sitename> execute visitor_pass_tracker.demo_data.delete_demo_data

Re-running create_demo_data first deletes the previous demo dataset, so it is
always deterministic. It is intentionally NOT whitelisted - run it from the
console/bench, never from the website.
"""

import frappe
from frappe import _
from frappe.utils import add_days, get_datetime, getdate, now_datetime

from visitor_pass_tracker.utils import attach_qr_code

DEMO_MARK = "[DEMO DATA]"

VISITOR_NAMES = [
	"Aarav Sharma", "Priya Patel", "Vikram Singh", "Ananya Iyer", "Rahul Verma",
	"Neha Gupta", "Arjun Nair", "Sneha Reddy", "Karthik Kumar", "Divya Menon",
	"Rohan Desai", "Pooja Joshi", "Aditya Rao", "Meera Krishnan", "Siddharth Bose",
	"Ishita Banerjee", "Manish Agarwal", "Kavya Pillai", "Rajat Mehta", "Anjali Kulkarni",
	"Suresh Babu", "Lakshmi Narayanan", "Deepak Chawla", "Shruti Jain", "Nikhil Kapoor",
	"Ritika Sharma", "Harsha Vardhan", "Tanvi Shah", "Gaurav Malik", "Sonali Das",
	"Abhishek Tiwari", "Nandini Rao", "Vivek Anand", "Pallavi Bhat", "Kunal Suri",
	"Riya Kapoor", "Sandeep Yadav", "Jaya Lakshmi", "Mohit Goel", "Swati Patil",
	"Prashant Kulkarni", "Shalini Mishra", "Ankit Bansal", "Deepika Rana", "Varun Khanna",
	"Amrita Singh", "Ravi Shankar", "Nisha Agarwal", "Tarun Batra", "Pooja Nair",
	"Sameer Khan", "Geeta Rani", "Anmol Saxena", "Kiran Bedi", "Harish Chandra",
	"Madhu Sudan", "Prakash Jha", "Reena Thomas", "Yogesh Patil", "Sunita Devi",
	"Akash Srivastava", "Bhavna Joshi", "Chandan Roy", "Dolly Kapoor", "Esha Saxena",
	"Farhan Ali", "Gayatri Deshpande", "Himanshu Mittal",
]

COMPANIES = [
	"Tata Consultancy Services", "Infosys", "Wipro", "HCL Technologies",
	"Tech Mahindra", "Zoho Corporation", "Flipkart", "Paytm",
	"BYJU'S", "Biocon", "L&T Construction", "Ashok Leyland",
	"Cognizant", "Accenture", "Deloitte", "Amazon India",
]

PURPOSES = ["Meeting", "Interview", "Delivery", "Maintenance", "Other"]
ID_PROOF_TYPES = ["Aadhar", "Passport", "Driving License", "Other"]
IN_TIMES = ["09:00:00", "09:30:00", "10:00:00", "11:00:00", "14:00:00"]
OUT_TIMES = ["16:00:00", "17:00:00", "18:00:00", "19:00:00"]
BLACKLIST_REASONS = [
	"Overstayed beyond pass validity", "Unauthorized access attempt",
	"Repeated no-show for scheduled visits", "Reported misconduct on-site",
	"Fake ID proof presented at gate", "Violation of security protocol",
	"Harassment complaint by employee", "Theft attempt at office premises",
]


def _phone(i):
	"""Unique +91 number: 10 digits, deterministic per index."""
	return "+91 98{:08d}".format(i % 100000000)


def _id_proof_number(i):
	return "{:012d}".format(400000000000 + i * 7)


def _company(i):
	return COMPANIES[i % len(COMPANIES)]


# ---------------------------------------------------------------------------
# ERPNext masters the app links to (Company / Address / Employee / Employment
# Type). All best-effort - failures are logged, never raised.
# ---------------------------------------------------------------------------


def _ensure_company():
	existing = frappe.get_all("Company", pluck="name", limit=1)
	if existing:
		return existing[0]
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Demo Corp Ltd",
				"abbr": "DEMO",
				"default_currency": "INR",
				"country": "India",
				"create_chart_of_accounts_based_on": "Standard Template",
			}
		).insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: demo company creation failed"),
			message=frappe.get_traceback(),
		)
		return None


def _ensure_employment_type():
	name = frappe.db.get_value("Employment Type", {"employment_type_name": "Employment"})
	if name:
		return name
	try:
		return frappe.get_doc(
			{"doctype": "Employment Type", "employment_type_name": "Employment"}
		).insert(ignore_permissions=True).name
	except Exception:
		frappe.log_error(
			title=_("Visitor Pass Tracker: demo employment type failed"),
			message=frappe.get_traceback(),
		)
		return None


def _ensure_addresses():
	created = []
	for i in range(6):
		fields = {
			"doctype": "Address",
			"address_title": "DEMO Location {0}".format(i + 1),
			"address_type": "Office",
			"address_line1": "Plot {0}, Demo Industrial Estate".format(i + 1),
			"address_line2": "Phase II, Guindy",
			"city": "Chennai",
			"state": "Tamil Nadu",
			"country": "India",
			"pincode": "6000{0:02d}".format(i + 1),
		}
		try:
			doc = frappe.get_doc(fields)
			doc.insert(ignore_permissions=True)
			created.append(doc.name)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: demo address failed"),
				message="Address {0}\n{1}".format(i, frappe.get_traceback()),
			)
	return created


def _ensure_employees(company):
	"""Create dedicated demo Employees (marked via the bio field) so the demo
	never touches real host users or fires their notifications.

	Deliberately does NOT fall back to existing Employees - a real employee
	could carry a user_id, and the approval workflow emails hosts, so reusing
	real employees could notify real people. Returns a list of Employee names
	(may be empty - callers must handle that)."""
	emp_meta = frappe.get_meta("Employee")
	employment_type = (
		_ensure_employment_type() if emp_meta.get_field("employment_type") else None
	)
	has_gender = bool(emp_meta.get_field("gender"))
	has_bio = bool(emp_meta.get_field("bio"))
	created = []
	for i in range(8):
		fields = {
			"doctype": "Employee",
			"employee_name": "Demo Host {0}".format(i + 1),
			"status": "Active",
			"date_of_joining": "2020-01-01",
			"company": company,
		}
		if employment_type:
			fields["employment_type"] = employment_type
		if has_gender:
			fields["gender"] = "Male" if i % 2 == 0 else "Female"
		if has_bio:
			fields["bio"] = DEMO_MARK
		try:
			doc = frappe.get_doc(fields)
			doc.insert(ignore_permissions=True)
			created.append(doc.name)
		except Exception:
			frappe.log_error(
				title=_("Visitor Pass Tracker: demo employee failed"),
				message=frappe.get_traceback(),
			)
	return created


# ---------------------------------------------------------------------------
# App doctypes
# ---------------------------------------------------------------------------


def _create_gates(company, addresses):
	created = []
	for i in range(50):
		name = "DEMO-Gate-{0:03d}".format(i + 1)
		if frappe.db.exists("Gate", name):
			continue
		gate = frappe.get_doc(
			{
				"doctype": "Gate",
				"gate_name": name,
				"location": addresses[i % len(addresses)] if addresses else None,
				"company": company,
				"device_id": "DEV-{0:04d}".format(1000 + i),
				# every 10th gate is deactivated (switchover/maintenance demo)
				"is_active": i % 10 != 9,
			}
		)
		gate.insert(ignore_permissions=True)
		created.append(name)
	return created


def _create_visitors():
	created = []
	for i, visitor_name in enumerate(VISITOR_NAMES):
		doc = frappe.get_doc(
			{
				"doctype": "Visitor",
				"visitor_name": visitor_name,
				"phone": _phone(i),
				"email": "visitor{0:02d}@example.com".format(i),
				"id_proof_type": ID_PROOF_TYPES[i % len(ID_PROOF_TYPES)],
				"id_proof_number": _id_proof_number(i),
				"id_proof_verified": "Verified" if i % 3 != 2 else "Not Verified",
				"company_name": _company(i),
				"is_vip": i % 11 == 0,
				"notes": DEMO_MARK,
			}
		)
		doc.insert(ignore_permissions=True)
		created.append(doc.name)
	return created


def _create_blacklisted(visitors):
	created = []
	for i in range(50):
		reason = "{0} - {1}".format(DEMO_MARK, BLACKLIST_REASONS[i % len(BLACKLIST_REASONS)])
		status = "Lifted" if i >= 40 else "Active"
		blacklisted_on = add_days(getdate(), -(i % 30) - 1)
		if i < 10 and i < len(visitors):
			# linked to demo visitors - their requests get flagged / auto-rejected
			doc = frappe.get_doc(
				{
					"doctype": "Blacklisted Visitor",
					"visitor": visitors[i],
					"reason": reason,
					"blacklisted_on": blacklisted_on,
					"status": status,
				}
			)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "Blacklisted Visitor",
					"visitor_name": "Blacklisted Person {0}".format(i),
					"phone": _phone(i + 200),
					"id_proof_number": "BL{0:06d}".format(i),
					"reason": reason,
					"blacklisted_on": blacklisted_on,
					"status": status,
				}
			)
		doc.insert(ignore_permissions=True)
		created.append(doc.name)
	return created


def _create_request(visitor, host, gate, state, visit_date, purpose, in_time, out_time,
					visit_end_date=None, vehicle_number=None, is_escort_required=False,
					rejection_reason=None, notes=DEMO_MARK):
	"""Create a Visitor Request and push it into the requested workflow state.

	- Draft: plain insert.
	- Future/today + not blacklisted: real `submit()` so the workflow runs
	  (Draft -> Blacklist Check -> Pending Host Approval) and the state is
	  then set to the target. Past visits bypass the workflow (validate_visit_date
	  forbids submitting past dates) via direct docstatus/workflow_state writes.
	- Blacklist-flagged visitors auto-reject on submit ("Reject: Blacklisted").
	"""
	doc = frappe.get_doc(
		{
			"doctype": "Visitor Request",
			"visitor": visitor,
			"host": host,
			"purpose": purpose,
			"visit_date": visit_date,
			"visit_end_date": visit_end_date or None,
			"expected_in_time": in_time,
			"expected_out_time": out_time,
			"location_gate": gate,
			"vehicle_number": vehicle_number or None,
			"is_escort_required": is_escort_required,
			"notes": notes,
		}
	)
	doc.insert(ignore_permissions=True)

	if state == "Draft":
		return doc

	is_flagged = frappe.db.get_value("Visitor", visitor, "is_blacklisted")
	# Blacklist-flagged visitors may still go through the real submit flow when
	# the target state is Rejected - on_submit auto-applies "Reject: Blacklisted".
	# For any other target state the submit would force-reject them, so those
	# requests are placed directly into the desired state instead.
	can_submit = (not is_flagged or state == "Rejected") and getdate(visit_date) >= getdate()
	if can_submit:
		doc.flags.ignore_permissions = True
		doc.submit()
		# on_submit auto-rejects flagged visitors; otherwise it lands on
		# "Pending Host Approval" and we move it to the requested state
		state = state if doc.workflow_state != "Rejected" else "Rejected"
	else:
		frappe.db.set_value("Visitor Request", doc.name, "docstatus", 1)

	frappe.db.set_value("Visitor Request", doc.name, "workflow_state", state)
	if rejection_reason:
		frappe.db.set_value("Visitor Request", doc.name, "rejection_reason", rejection_reason)
	doc.reload()
	return doc


def _make_pass(req, valid_from, valid_till):
	"""Create an Entry Pass (all fields) + attach a QR image. Never emails."""
	existing = frappe.db.get_value("Entry Pass", {"visitor_request": req.name}, "name")
	if existing:
		return frappe.get_doc("Entry Pass", existing)
	visitor_email, company_name = frappe.db.get_value(
		"Visitor", req.visitor, ["email", "company_name"]
	) or (None, None)
	doc = frappe.get_doc(
		{
			"doctype": "Entry Pass",
			"visitor_request": req.name,
			"visitor": req.visitor,
			"visitor_email": visitor_email,
			"company_name": company_name,
			"host": req.host,
			"host_user": req.host_user,
			"location_gate": req.location_gate,
			"vehicle_number": req.vehicle_number,
			"is_escort_required": req.is_escort_required,
			"valid_from": valid_from,
			"valid_till": valid_till,
			"status": "Active",
		}
	)
	doc.insert(ignore_permissions=True)
	doc.reload()
	attach_qr_code(doc)
	return doc


def _db_log(pass_doc, scan_type, scan_time, device, authorized, remarks, source):
	"""Insert a Gate Log Entry via db_insert (no validation hooks) so the
	authorization flag reflects what happened at scan time, not the pass's
	current status. Every field is set explicitly."""
	host_user = None
	if pass_doc.get("visitor_request"):
		host_user = frappe.db.get_value("Visitor Request", pass_doc.visitor_request, "host_user")
	doc = frappe.get_doc(
		{
			"doctype": "Gate Log Entry",
			"entry_pass": pass_doc.name,
			"visitor_request": pass_doc.get("visitor_request"),
			"visitor": pass_doc.get("visitor"),
			"visitor_name": pass_doc.get("visitor_name"),
			"gate": pass_doc.get("location_gate"),
			"host_user": host_user or pass_doc.get("host_user"),
			"scan_type": scan_type,
			"scan_time": scan_time,
			"scanned_by_device": device,
			"is_authorized": authorized,
			"remarks": remarks,
			"source": source,
		}
	)
	doc.db_insert(ignore_mandatory=True)


def _create_passes_and_logs(requests):
	"""Create one Entry Pass per approved request (indices 10..59 of the request
	list) with realistic statuses + gate logs. Returns pass count."""
	pass_count = 0
	today = getdate()
	for idx in range(10, 60):
		req = requests[idx]
		# the request's own visit window drives the pass (single source of truth)
		visit_date = getdate(req.visit_date)
		end_date = getdate(req.visit_end_date) if req.get("visit_end_date") else visit_date
		valid_from = get_datetime("{0} 09:00:00".format(visit_date))
		valid_till = get_datetime("{0} 18:00:00".format(end_date))

		if idx <= 31:
			# ---- Used: completed past visits (entry + exit, host check-in/out)
			entry_time = get_datetime("{0} 10:{1:02d}:00".format(visit_date, idx % 50))
			exit_time = get_datetime("{0} 17:{1:02d}:00".format(visit_date, idx % 55))
			pass_doc = _make_pass(req, valid_from, valid_till)
			frappe.db.set_value("Entry Pass", pass_doc.name, "status", "Used")
			_db_log(pass_doc, "Entry", entry_time, "DEV-1{0:03d}".format(idx), 1, "", "Gate Device")
			_db_log(pass_doc, "Exit", exit_time, "DEV-1{0:03d}".format(idx), 1, "", "Gate Device")
			frappe.db.set_value(
				"Visitor Request", req.name,
				{"host_checkin_time": entry_time, "host_checkout_time": exit_time},
			)
		elif idx <= 46:
			# ---- Active: today / tomorrow; 10 already scanned in (on-site)
			pass_doc = _make_pass(req, valid_from, valid_till)
			if idx <= 41:
				scan_time = get_datetime("{0} {1}".format(
					today, "10:30:00" if idx % 2 else "11:15:00"))
				_db_log(pass_doc, "Entry", scan_time, "DEV-2{0:03d}".format(idx), 1, "", "Gate Device")
			else:
				frappe.db.set_value(
					"Visitor Request", req.name, "day_before_reminder_sent", 1
				)
		elif idx <= 54:
			# ---- Expired: past visits; 6 overstays (entry, no exit), 2 no-shows
			pass_doc = _make_pass(req, valid_from, valid_till)
			frappe.db.set_value(
				"Entry Pass", pass_doc.name,
				{"status": "Expired", "expiry_alert_sent": 1},
			)
			if idx <= 52:
				scan_time = get_datetime("{0} 10:00:00".format(visit_date))
				_db_log(pass_doc, "Entry", scan_time, "DEV-3{0:03d}".format(idx), 1, "", "Gate Device")
				frappe.db.set_value("Entry Pass", pass_doc.name, "overstay_alert_sent", 1)
		else:
			# ---- Cancelled: approved visits cancelled -> pass revoked
			pass_doc = _make_pass(req, valid_from, valid_till)
			frappe.db.set_value(
				"Entry Pass", pass_doc.name,
				{
					"status": "Revoked",
					"revoked_by": "Administrator",
					"revoked_on": now_datetime(),
				},
			)
			frappe.db.set_value("Visitor Request", req.name, "docstatus", 2)
			req.reload()  # refresh so the summary shows "Cancelled"
			if idx in (55, 56):
				scan_time = get_datetime("{0} 09:45:00".format(today))
				_db_log(pass_doc, "Entry", scan_time, "DEV-4{0:03d}".format(idx), 1, "", "Gate Device")
		pass_count += 1

	# a handful of unauthorized scan attempts (expired passes) for the
	# reconciliation report - scanning a pass that is no longer valid
	for i in range(5):
		pass_doc = frappe.get_doc("Entry Pass", requests[47 + i].name)
		scan_time = get_datetime("{0} {1}:00:00".format(today, 12 + i))
		_db_log(
			pass_doc, "Entry", scan_time, "DEV-9001",
			0, "Unauthorized scan - no matching valid Entry Pass", "Gate Device",
		)
	return pass_count


def create_demo_data():
	"""Seed the full demo dataset (≥50 records per doctype, all fields)."""
	# wipe any previous demo dataset so re-runs are deterministic
	delete_demo_data(quiet=True)

	company = _ensure_company()
	addresses = _ensure_addresses()
	employees = _ensure_employees(company)
	if not employees:
		frappe.throw(
			_("No Employees available and demo Employee creation failed - "
			  "Visitor Request requires a Host (Employee). Create an Employee first.")
		)

	gates = _create_gates(company, addresses)
	visitors = _create_visitors()
	blacklisted = _create_blacklisted(visitors)

	requests = []
	for i, visitor in enumerate(visitors):
		host = employees[i % len(employees)]
		gate = gates[i % len(gates)]
		in_time = IN_TIMES[i % len(IN_TIMES)]
		out_time = OUT_TIMES[i % len(OUT_TIMES)]
		vehicle = "TN 01 AB {0:04d}".format(i + 10) if i % 5 == 0 else None
		escort = (i % 9 == 0)

		# the visit date drives everything downstream (pass window, scan times)
		# and is kept consistent between the request and its pass/logs
		if 10 <= i <= 31:
			visit_date = add_days(getdate(), -(i % 20) - 2)      # Used - completed past visits
		elif 32 <= i <= 46:
			visit_date = add_days(getdate(), 0 if i % 3 else 1)  # Active - today / tomorrow
		elif 47 <= i <= 54:
			visit_date = add_days(getdate(), -(i % 15) - 1)      # Expired - past visits
		else:
			visit_date = add_days(getdate(), 0 if i >= 10 else 1)
		end_date = add_days(visit_date, 2) if i % 13 == 0 else None

		if i < 2:
			# blacklist-linked -> real submit auto-rejects via the workflow
			state = "Rejected"
		elif i < 6:
			state = "Draft"
		elif i < 10:
			# blacklist-linked, still awaiting action in the approval flow
			state = "Pending Host Approval"
		elif i < 60:
			state = "Approved"
		elif i < 62:
			state = "Rejected"  # manual rejection with a reason
		elif i < 64:
			state = "Pending Department Approval"
		elif i < 66:
			state = "Pending Security Approval"
		else:
			state = "Pending Host Approval"

		rejection_reason = None
		if state == "Rejected" and i >= 60:
			rejection_reason = "Host unavailable on the requested date - please reschedule."

		requests.append(
			_create_request(
				visitor, host, gate, state,
				visit_date=visit_date,
				purpose=PURPOSES[i % len(PURPOSES)],
				in_time=in_time, out_time=out_time,
				visit_end_date=end_date,
				vehicle_number=vehicle,
				is_escort_required=escort,
				rejection_reason=rejection_reason,
			)
		)

	passes = _create_passes_and_logs(requests)
	frappe.db.commit()

	_show_summary(visitors, requests, passes, gates, blacklisted, employees)
	return {
		"visitors": len(visitors),
		"visitor_requests": len(requests),
		"entry_passes": passes,
		"gates": len(gates),
		"blacklisted_visitors": len(blacklisted),
		"employees": len(employees),
	}


def delete_demo_data(quiet=False):
	"""Delete every record tagged with the demo marker (dependency order)."""
	req_names = frappe.get_all(
		"Visitor Request", filters={"notes": DEMO_MARK}, pluck="name"
	)
	pass_names = (
		frappe.get_all(
			"Entry Pass", filters={"visitor_request": ["in", req_names]}, pluck="name"
		)
		if req_names else []
	)
	log_names = (
		frappe.get_all(
			"Gate Log Entry", filters={"entry_pass": ["in", pass_names]}, pluck="name"
		)
		if pass_names else []
	)
	for name in log_names:
		frappe.delete_doc("Gate Log Entry", name, force=1)
	for name in pass_names:
		frappe.delete_doc("Entry Pass", name, force=1)
	for name in req_names:
		frappe.delete_doc("Visitor Request", name, force=1)

	visitor_names = frappe.get_all(
		"Visitor", filters={"notes": DEMO_MARK}, pluck="name"
	)
	contacts = []
	for name in visitor_names:
		linked = frappe.db.get_value("Visitor", name, "linked_contact")
		if linked:
			contacts.append(linked)
		frappe.delete_doc("Visitor", name, force=1)
	for name in contacts:
		frappe.delete_doc("Contact", name, force=1)

	blacklist_names = frappe.get_all(
		"Blacklisted Visitor", filters={"reason": ["like", "{0}%".format(DEMO_MARK)]},
		pluck="name",
	)
	for name in blacklist_names:
		frappe.delete_doc("Blacklisted Visitor", name, force=1)

	for name in frappe.get_all("Gate", filters={"gate_name": ["like", "DEMO-Gate-%"]}, pluck="name"):
		frappe.delete_doc("Gate", name, force=1)
	for name in frappe.get_all("Address", filters={"address_title": ["like", "DEMO Location%"]}, pluck="name"):
		frappe.delete_doc("Address", name, force=1)
	for name in frappe.get_all("Employee", filters={"bio": DEMO_MARK}, pluck="name"):
		frappe.delete_doc("Employee", name, force=1)

	frappe.db.commit()
	if not quiet:
		print("[Visitor Pass Tracker] demo data deleted: {0} requests, {1} passes, "
			  "{2} logs, {3} visitors, {4} blacklist records".format(
				  len(req_names), len(pass_names), len(log_names),
				  len(visitor_names), len(blacklist_names)))


def _show_summary(visitors, requests, passes, gates, blacklisted, employees):
	states = {}
	for req in requests:
		state = str(req.workflow_state or "Draft")
		if req.docstatus == 2:
			state = "Cancelled"
		states[state] = states.get(state, 0) + 1
	# count only demo gate logs (linked to demo passes) - not the site's total
	req_names = frappe.get_all(
		"Visitor Request", filters={"notes": DEMO_MARK}, pluck="name"
	)
	pass_names = frappe.get_all(
		"Entry Pass", filters={"visitor_request": ["in", req_names]}, pluck="name"
	)
	logs = (
		frappe.db.count("Gate Log Entry", filters={"entry_pass": ["in", pass_names]})
		if pass_names else 0
	)
	print("\n[Visitor Pass Tracker] demo data created")
	print("  Gates                 : {0}".format(len(gates)))
	print("  Visitors              : {0}".format(len(visitors)))
	print("  Visitor Requests      : {0}".format(len(requests)))
	for state, count in sorted(states.items()):
		print("      - {0:32} {1}".format(state, count))
	print("  Entry Passes (QR set) : {0}".format(passes))
	print("  Gate Log Entries      : {0}".format(logs))
	print("  Blacklisted Visitors  : {0}".format(len(blacklisted)))
	print("  Demo Employees (hosts): {0}".format(len(employees)))
	print("Run delete_demo_data() to remove everything.")
