# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Reminder guards.

Phase 1 implements the guards only. Sending, throttling and the Comment trail
land in Phase 3; `send` deliberately refuses until then rather than half-doing
it, so nobody wires the button up to a no-op by accident.

The guards are the part worth getting right first. `send_reminder` is a
mutating call that takes a doctype and a name straight from the browser, which
makes it the one place a client-supplied identifier reaches the server. Every
guard below re-derives its answer from the database; nothing the client sent is
believed except the two identifiers themselves, and those are validated before
use.
"""

import frappe
from frappe import _

from task_manager import scope
from task_manager.services import approver_gate, workflow_index

THROTTLE_HOURS = 24
COMMENT_PREFIX = "Approval reminder sent"


class NotPermitted(frappe.PermissionError):
	pass


def guard(doctype: str, name: str, user: str) -> "frappe.model.document.Document":
	"""Return the document, or raise the same generic error for every refusal.

	The message is identical whether the document does not exist, is not this
	user's, or is not in a state a reminder makes sense for. Distinguishing
	them would turn this endpoint into an oracle: call it with guessed names
	and the error text tells you which ones are real.
	"""
	if not doctype or not name:
		raise NotPermitted(_("Not permitted"))

	# A doctype string from the browser must never reach a query untested.
	if not frappe.db.exists("DocType", doctype):
		raise NotPermitted(_("Not permitted"))

	index = workflow_index.for_user(user)
	entry = index.get(doctype)
	if not entry:
		raise NotPermitted(_("Not permitted"))

	try:
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		raise NotPermitted(_("Not permitted"))

	if not doc.has_permission("read"):
		raise NotPermitted(_("Not permitted"))

	scope_entry = scope.for_doctype(doctype)

	if not _is_requester(doc, scope_entry, user):
		# A reminder is a thing the requester sends about their own document.
		raise NotPermitted(_("Not permitted"))

	if entry["is_submittable"] and doc.docstatus == 2:
		raise NotPermitted(_("Not permitted"))

	state = doc.get(entry["state_field"])
	if not state or state in entry["terminal"]:
		raise NotPermitted(_("Not permitted"))

	if scope_entry:
		# Both checks have to follow the same rule Tab 3 does, or the button
		# it renders refuses when pressed.
		if state not in scope_entry["levels"]:
			# Still a draft: waiting on the requester to send it.
			raise NotPermitted(_("Not permitted"))
		if scope.is_gated(scope_entry):
			if scope.may_act(scope_entry, state, doc, user):
				# Waiting on the requester themselves, who happens to be its
				# approver too. There is nobody to remind.
				raise NotPermitted(_("Not permitted"))
		elif state in entry["my_states"]:
			# Same thing by role, for a doctype that names nobody: an Accounts
			# Manager reminding on their own journal entry is reminding
			# themselves.
			raise NotPermitted(_("Not permitted"))
	elif state in entry["my_states"]:
		# Waiting on the requester themselves; there is nobody to remind.
		raise NotPermitted(_("Not permitted"))

	return doc


def _is_requester(doc, scope_entry: dict | None, user: str) -> bool:
	"""Whose request this is.

	For a generic workflow that is authorship. For the three HRMS request
	types it is the employee named on the request, because HR raises these on
	behalf of other people -- 183 On Duty requests are pending right now and
	some were typed by somebody other than the person who needs them. Tab 3
	shows such a request to the employee, so the employee is who may remind on
	it; the HR user who typed it may not.
	"""
	if not scope_entry:
		return doc.owner == user

	if not scope_entry.get("employee_field"):
		# No employee is named on an Accounts document; it belongs to whoever
		# raised it, which is the same rule Tab 3 lists it under.
		return doc.owner == user

	employee = approver_gate.employee_of(user)
	if employee and doc.get(scope_entry["employee_field"]) == employee:
		return True
	# An employee record is not required to have raised something -- fall back
	# to authorship rather than refusing outright.
	return doc.owner == user


def last_reminder(doc) -> str | None:
	"""Timestamp of the most recent reminder Comment on this document."""
	rows = frappe.get_all(
		"Comment",
		filters={
			"comment_type": "Info",
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
			"content": ["like", COMMENT_PREFIX + "%"],
		},
		fields=["creation"],
		order_by="creation desc",
		limit=1,
		ignore_permissions=True,
	)
	return rows[0]["creation"] if rows else None


def last_reminders(rows: list) -> dict:
	"""When each listed document was last reminded, in one query per doctype.

	The dashboard shows "Remind" or "Reminded today" per row, so this has to
	be batched -- one Comment query per row would be 200 queries for a list
	that renders six.
	"""
	grouped = {}
	for row in rows:
		grouped.setdefault(row["doctype"], []).append(row["name"])

	out = {}
	for doctype, names in grouped.items():
		comments = frappe.get_all(
			"Comment",
			filters={
				"comment_type": "Info",
				"reference_doctype": doctype,
				"reference_name": ["in", names],
				"content": ["like", COMMENT_PREFIX + "%"],
			},
			fields=["reference_name", "creation"],
			order_by="creation desc",
			ignore_permissions=True,
		)
		for comment in comments:
			key = (doctype, comment["reference_name"])
			if key not in out:
				out[key] = comment["creation"]

	return out


def send(doctype: str, name: str, user: str) -> dict:
	"""Phase 3. Guards already hold; mail, throttle and Comment are pending."""
	guard(doctype, name, user)
	frappe.throw(
		_("Reminders are not enabled yet."),
		title=_("Coming in Phase 3"),
	)
