# Fixtures

Everything in this folder is packaged as a **fixture** and imported automatically
when the app is installed:

```bash
bench --site <sitename> install-app visitor_pass_tracker
```

In Frappe 15 `sync_fixtures` imports every `*.json` in this folder directly
(alphabetically, with link & mandatory validation relaxed during import), so
file order does not matter. The `fixtures` list in `hooks.py` is used by
`bench export-fixtures` to regenerate these files:

1. `role.json` — Security Officer, Department Head, Reception roles
2. `workflow_state.json` — the 7 workflow states (Frappe 15 `Workflow State` records)
3. `workflow_action_master.json` — the 7 transition actions (Frappe 15 `Workflow Action Master` records)
4. `workflow.json` — the native **Visitor Request Workflow** state machine
5. `notification.json` — native Notification records (host approvals, blacklist rejection, entry pass generated)
6. `dashboard_chart_source.json` — custom chart source for "Peak Visit Hours"
   (`source_name` is the dotted path to `utils.get_peak_visit_hours`)
7. `dashboard_chart.json` — the 4 dashboard charts
8. `number_card.json` — the 2 dashboard number cards
9. `dashboard.json` — the "Visitor Overview" dashboard
10. `custom_field.json` — intentionally empty: every field lives on an
    app-owned doctype, so no custom fields are required (kept for convention).

Note: the "Visitor Reconciliation" script report is *not* a fixture. Its record is
synced automatically from `report/visitor_reconciliation/visitor_reconciliation.json`
by `frappe.model.sync` during `bench migrate` (the standard script-report pattern,
same as ERPNext's `accounts_receivable`).

To re-export any of these after editing them in the UI, run:

```bash
bench --site <sitename> export-fixtures
```
