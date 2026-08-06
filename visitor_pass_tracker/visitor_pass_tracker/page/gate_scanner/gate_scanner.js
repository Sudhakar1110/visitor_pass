frappe.pages["gate-scanner"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Gate Scanner"),
		single_column: true,
	});

	let page = wrapper.page;
	page.set_title(__("Gate Scanner"));

	$(wrapper).find(".page-content").html(`
		<div class="gate-scanner p-3" style="max-width: 600px;">
			<div class="form-group">
				<label>${__("Entry Pass / QR Payload")}</label>
				<input type="text" class="form-control" id="gs-pass"
					placeholder="${__("PASS-2026-00001 or paste the QR JSON")}">
			</div>
			<div class="form-group">
				<label>${__("Gate")}</label>
				<select class="form-control" id="gs-gate"></select>
			</div>
			<div class="form-group">
				<label class="checkbox">
					<input type="checkbox" id="gs-force"> ${__("Force (allow exit without an entry scan)")}
				</label>
			</div>
			<div class="btn-group">
				<button class="btn btn-primary" id="gs-entry">${__("Authorize Entry")}</button>
				<button class="btn btn-warning" id="gs-exit">${__("Record Exit")}</button>
				<button class="btn btn-secondary" id="gs-manual">${__("Manual Exit")}</button>
				<button class="btn btn-danger" id="gs-revoke">${__("Revoke")}</button>
				<button class="btn btn-info" id="gs-extend">${__("Extend")}</button>
			</div>
			<div id="gs-result" class="mt-4"></div>
		</div>
	`);

	let $pass = $("#gs-pass");
	let $gate = $("#gs-gate");
	let $force = $("#gs-force");
	let $result = $("#gs-result");

	// load the gates into the dropdown
	frappe.call({
		method: "frappe.client.get_list",
		args: { doctype: "Gate", fields: ["name"], limit_page_length: 0 },
		callback: function (r) {
			let opts = (r.message || [])
				.map((g) => `<option value="${g.name}">${g.name}</option>`)
				.join("");
			$gate.html(opts || `<option value="">${__("No gates configured")}</option>`);
		},
	});

	function scan(pass, gate) {
		frappe.call({
			method: "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.submit_scan",
			args: { entry_pass: pass, gate: gate, scan_type: "Entry" },
			callback: showScanResult,
		});
	}

	function exit(pass, gate, force) {
		frappe.call({
			method: "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.submit_scan",
			args: { entry_pass: pass, gate: gate, scan_type: "Exit" },
			callback: showScanResult,
		});
	}

	function manualExit(pass, gate, force) {
		frappe.call({
			method: "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.manual_exit",
			args: { entry_pass: pass, gate: gate, force: force },
			callback: showScanResult,
		});
	}

	function revoke(pass) {
		frappe.confirm(__("Revoke this Entry Pass? The gate will stop accepting it."), function () {
			frappe.call({
				method: "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.revoke_pass",
				args: { entry_pass: pass, remarks: __("Revoked from Gate Scanner console") },
				callback: showScanResult,
			});
		});
	}

	function extend(pass) {
		frappe.prompt(
			[
				{
					fieldname: "valid_till",
					label: __("New Valid Till"),
					fieldtype: "Datetime",
					reqd: 1,
				},
			],
			function (values) {
				frappe.call({
					method: "visitor_pass_tracker.visitor_pass_tracker.doctype.gate_log_entry.gate_log_entry.extend_pass",
					args: { entry_pass: pass, valid_till: values.valid_till },
					callback: showScanResult,
				});
			},
			__("Extend Entry Pass")
		);
	}

	function showScanResult(r) {
		if (r.exc) {
			$result.html(`<div class="alert alert-danger">${__("Error")}: ${r.exc}</div>`);
			return;
		}
		let m = r.message || {};
		let status = m.status || "unknown";
		let cls = "success",
			label = __("Authorized");
		if (status === "unauthorized") {
			cls = "danger";
			label = __("Unauthorized");
		} else if (status === "duplicate") {
			cls = "warning";
			label = __("Duplicate (already inside)");
		} else if (status === "revoked") {
			cls = "danger";
			label = __("Revoked");
		} else if (status === "used") {
			cls = "info";
			label = __("Visit closed");
		} else if (status === "extended") {
			cls = "info";
			label = __("Extended");
		}

		let detail = "";
		if (m.entry_pass) {
			detail += `<div><b>${__("Pass")}:</b> ${m.entry_pass}</div>`;
		}
		if (m.gate) {
			detail += `<div><b>${__("Gate")}:</b> ${m.gate}</div>`;
		}
		if (m.scan_type) {
			detail += `<div><b>${__("Scan")}:</b> ${m.scan_type}</div>`;
		}
		if (m.scan_time) {
			detail += `<div><b>${__("Time")}:</b> ${m.scan_time}</div>`;
		}
		if (m.valid_till) {
			detail += `<div><b>${__("Valid Till")}:</b> ${m.valid_till}</div>`;
		}

		$result.html(`
			<div class="alert alert-${cls}" style="font-size: 14px;">
				<b>${label}</b>
				${detail}
			</div>
		`);

		// show the visitor details for a resolved pass
		frappe.call({
			method: "visitor_pass_tracker.visitor_pass_tracker.page.gate_scanner.gate_scanner.resolve_pass",
			args: { entry_pass: m.entry_pass || $pass.val() },
			callback: function (dr) {
				let info = dr.message;
				if (info && info.found) {
					$result.append(`
						<div class="card mt-2" style="max-width: 420px;">
							<div class="card-body">
								${info.photo ? `<img src="${info.photo}" class="mb-2" style="width: 70px; height: 70px; object-fit: cover; border-radius: 50%;">` : ""}
								<h5>${info.visitor_name || ""}</h5>
								<div class="text-muted small">
									${info.company ? `<div>${info.company}</div>` : ""}
									${info.visitor_phone ? `<div>${info.visitor_phone}</div>` : ""}
									${info.vehicle_number ? `<div>${__("Vehicle")}: ${info.vehicle_number}</div>` : ""}
									${info.is_escort_required ? `<div class="text-danger">${__("Escort required")}</div>` : ""}
									<div>${__("Status")}: <b>${info.status}</b></div>
									<div>${__("Valid")}: ${info.valid_from} - ${info.valid_till}</div>
								</div>
							</div>
						</div>
					`);
				}
			},
		});
	}

	$("#gs-entry").on("click", function () {
		scan($pass.val(), $gate.val());
	});
	$("#gs-exit").on("click", function () {
		exit($pass.val(), $gate.val());
	});
	$("#gs-manual").on("click", function () {
		manualExit($pass.val(), $gate.val(), $force.is(":checked") ? 1 : 0);
	});
	$("#gs-revoke").on("click", function () {
		revoke($pass.val());
	});
	$("#gs-extend").on("click", function () {
		extend($pass.val());
	});

	$pass.on("keydown", function (e) {
		if (e.key === "Enter") {
			scan($pass.val(), $gate.val());
		}
	});
};
