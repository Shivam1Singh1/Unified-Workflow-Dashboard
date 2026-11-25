# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Tab 1 -- the ToDos allocated to me.

ToDo needs its own hard filter. Its permission rule returns *no* restriction at
all for anybody holding a role on the doctype, System Manager included, so a
plain `get_list` would hand one user the whole company's assignments. Every
query here pins `allocated_to` to the session user in code; the permission
layer is not trusted to do it.

Volume is the other problem. One real user on this site holds 10,207 open
ToDos, and 12,754 of the 15,887 open ToDos site-wide point at `Item` -- bulk
assignment noise rather than human work. A flat cap would show that user 2% of
their list and say nothing about the rest, so the total travels with the rows
and the page states what it is hiding.

No state is held at module level anywhere in this file. A worker process
serves many users in sequence, and anything cached in a module global would
outlive the request that built it.
"""

import frappe
from frappe.utils import cint, date_diff, nowdate, strip_html

LIMIT = 200


def _scope_or_filters():
	"""Narrow Tab 1 to whatever the rest of the page covers.

	A ToDo with no reference at all is kept in every case: somebody typed it
	for themselves, and it points at nothing any scope could exclude it from.

	Three answers, in the order they are decided:

	1. Wide coverage -- every covered doctype, both families. A ToDo earns its
	   place in Tab 1 when it points at something the other tabs would also
	   show, which is the only definition of "relevant" that cannot drift from
	   what the page actually does.

	   This is still a filter, and the distinction matters: the 12,754 open
	   `Item` ToDos that swamped this tab stay out, because Item is neither
	   submittable nor workflow-driven and so is not covered by anything.
	   The 1,791 pointing at `Material Request` do come in -- that doctype is
	   submittable, those are real documents, and a user assigned one is
	   genuinely being asked to do something.

	2. Narrow coverage with the restriction on -- the declared seven.
	3. Narrow coverage with it off -- no filter at all, every assigned ToDo.
	"""
	from task_manager import scope
	from task_manager.services import workflow_index

	if scope.COVER_ALL_SUBMITTABLE:
		return [
			["reference_type", "in", workflow_index.covered_doctypes()],
			["reference_type", "is", "not set"],
		]

	if not scope.restrict_todos_to_scope():
		return None

	return [
		["reference_type", "in", scope.doctypes()],
		["reference_type", "is", "not set"],
	]


def get_my_todos(user: str, limit: int = LIMIT) -> dict:
	filters = {"allocated_to": user, "status": "Open"}
	or_filters = _scope_or_filters()

	# The count has to carry the same narrowing as the rows, or the page
	# reports a total it is not showing and calls the difference truncation.
	# `frappe.db.count` takes no or_filters, so the narrowed case counts
	# through get_all -- cheap here, because narrowing is what makes the set
	# small in the first place.
	if or_filters:
		total = len(
			frappe.get_all(
				"ToDo",
				filters=filters,
				or_filters=or_filters,
				pluck="name",
				limit_page_length=0,
				ignore_permissions=True,
			)
		)
	else:
		total = frappe.db.count("ToDo", filters)

	if not total:
		return {"rows": [], "total": 0, "shown": 0}

	todos = frappe.get_all(
		"ToDo",
		filters=filters,
		or_filters=or_filters,
		fields=[
			"name",
			"reference_type",
			"reference_name",
			"description",
			"date",
			"priority",
			"assigned_by",
			"creation",
		],
		order_by="field(priority, 'High', 'Medium', 'Low'), date asc, creation asc",
		limit_page_length=limit,
		ignore_permissions=True,
	)

	live, finished = _live_references(todos, user)
	today = nowdate()

	rows = []
	stale = 0
	for todo in todos:
		reference = (todo.get("reference_type"), todo.get("reference_name"))
		if todo.get("reference_type") and reference not in live:
			# Dropped whole, description included: a ToDo description routinely
			# repeats the document's title, so returning it would leak the one
			# thing the permission check just denied.
			#
			# Two reasons a reference is missing from `live` -- unreadable, or
			# the document is finished. Only the second is counted, because
			# only the second is a correction to `total`; the first is a
			# permission boundary and `total` was never entitled to it.
			if reference in finished:
				stale += 1
			continue

		rows.append(
			{
				"name": todo["name"],
				"reference_type": todo.get("reference_type") or "",
				"reference_name": todo.get("reference_name") or "",
				"description": _first_line(todo.get("description")),
				"date": todo.get("date"),
				"priority": todo.get("priority") or "Medium",
				"assigned_by_name": _full_name(todo.get("assigned_by")),
				"age_days": date_diff(today, todo["creation"]),
				"overdue": bool(todo.get("date") and date_diff(today, todo["date"]) > 0),
			}
		)

	# `total` was counted in SQL, before anything knew which documents had
	# finished, so it has to be corrected here or the page reports a backlog
	# it is deliberately not showing and calls the difference truncation.
	#
	# Exact while `total` fits inside `limit`, which is the normal case. Past
	# that only the fetched window was examined, so the correction is partial
	# -- it can understate how many are stale, never overstate. Erring
	# towards "you may have more to do" is the right direction for a to-do
	# list to be wrong in.
	return {"rows": rows, "total": max(total - stale, len(rows)), "shown": len(rows)}


def _live_references(todos: list, user: str) -> tuple[set, set]:
	"""Referenced documents this user may read, split into live and finished.

	Grouping is what keeps this cheap: 200 ToDos spread across four doctypes
	cost four permission-aware queries, not two hundred. The finished test
	rides along on those same queries as one extra column -- it costs no
	round trip of its own.

	Why a ToDo on a finished document is dropped
	--------------------------------------------

	Nothing ever closes it. Frappe opens a ToDo when a document is assigned
	and then relies on somebody pressing Close; approving the document does
	not touch the assignment. So an assignment made during approval outlives
	the approval indefinitely, and Tab 1 kept presenting it as outstanding
	work.

	Measured on this site: **320 of the 2,410 open ToDos** pointing at a
	covered doctype point at a document that is already finished. 272 of the
	285 on `Item Creation Form` are stale, and `Supplier 10947` -- approved
	and created on 5 January, and read-only on its own form -- still carried
	an open "Assignment for Supplier HEAVEN GIFT GALLERY" near the top of one
	reader`s queue.

	What counts as finished, in the order it is decided
	---------------------------------------------------

	1. The doctype has an active workflow -> its state is in `terminal`.
	   Judged on the workflow alone and never on docstatus as well: several
	   flows here approve *after* submitting, so a docstatus of 1 can still
	   have levels to go -- `Payment Request` has two of them past that
	   point. Reading docstatus here would hide live work.

	2. No workflow, and the doctype is submittable -> docstatus is not 0.
	   Submitted or cancelled, either way nobody is waiting to submit it.

	3. Anything else -> kept. A doctype with neither a workflow nor a
	   docstatus has no "finished" to test for, and guessing would be worse
	   than showing it.

	The full index is read rather than the reader`s slice of it. Whether a
	document is finished is a fact about the document and not about who is
	looking, and a ToDo can point at a doctype outside this reader`s coverage
	while still being perfectly readable by them.

	Returns two sets rather than one set and a module global. The caller has
	to tell "you may not see this" from "this is done" -- only the second
	corrects `total` -- and the no-module-state rule at the top of this file
	is not worth trading for the convenience: one worker serves many users in
	sequence, and a global would outlive the request that filled it.
	"""
	from task_manager.services import workflow_index

	grouped = {}
	for todo in todos:
		if todo.get("reference_type") and todo.get("reference_name"):
			grouped.setdefault(todo["reference_type"], set()).add(todo["reference_name"])

	if not grouped:
		return set(), set()

	workflows = workflow_index.get_workflow_index()["workflows"]

	live = set()
	finished = set()

	for doctype, names in grouped.items():
		if not frappe.db.exists("DocType", doctype):
			continue

		entry = workflows.get(doctype)
		fields = ["name"]
		submittable = False

		if entry:
			fields.append(entry["state_field"] + " as tm_state")
		else:
			try:
				submittable = bool(frappe.get_meta(doctype).is_submittable)
			except Exception:
				submittable = False
			if submittable:
				fields.append("docstatus")

		try:
			if not frappe.has_permission(doctype, "read", user=user):
				continue
			found = frappe.get_list(
				doctype,
				filters={"name": ["in", list(names)]},
				fields=fields,
				limit_page_length=0,
				ignore_ifnull=True,
			)
		except Exception:
			# A doctype can be readable in principle and still fail here, for
			# instance when a custom permission hook throws. Losing those rows
			# is the safe direction.
			continue

		terminal = set(entry["terminal"]) if entry else set()

		for row in found:
			reference = (doctype, row["name"])
			if entry:
				if row.get("tm_state") in terminal:
					finished.add(reference)
					continue
			elif submittable and cint(row.get("docstatus")) != 0:
				finished.add(reference)
				continue
			live.add(reference)

	return live, finished


def _full_name(user: str | None) -> str:
	if not user:
		return ""
	return frappe.db.get_value("User", user, "full_name") or user


def _first_line(description: str | None) -> str:
	if not description:
		return ""
	text = strip_html(description).strip()
	line = text.splitlines()[0] if text else ""
	return line[:180]
