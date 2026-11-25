# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Install and upgrade wiring.

Three things happen on every migrate, all of them idempotent: the indexes
below are created, the workflow index cache is dropped, and the **My Work**
workspace is put back if it is missing.

This app adds **no field and no property setter to any core doctype**. What it
does add is two database indexes, on columns core never indexed but this page
filters by on every load:

* `Workflow Action.completed_by` -- Tab 4's only filter, against 331,833 rows.
  Core indexes `(reference_name, reference_doctype, status)`, `user` and
  `modified`; not this.

* `ToDo.allocated_to` -- Tab 1's only filter, against 17,917 rows. Core indexes
  `(reference_type, reference_name)` and `modified`; not this.

An index changes no schema definition and no document -- nothing is exported to
a core app's JSON, and uninstalling this app leaves the tables working exactly
as before, just slower for these two queries. Both calls are idempotent, so
running them on every migrate is safe.
"""

import json
import os

import frappe

# The workspace this app ships, and the folder its definition lives in.
#
# One definition, not two. `WORKSPACE_JSON` is read at migrate time rather
# than the record being rebuilt from a dict in this file, so editing the
# shipped JSON is the only way to change what gets created and the two can
# never drift apart.
# Matches api.LOG_PREFIX, so one Error Log filter finds the whole app.
LOG_PREFIX = "Task Manager"

WORKSPACE = "My Work"
WORKSPACE_JSON = os.path.join("task_manager", "workspace", "my_work", "my_work.json")

INDEXES = (
	# Tab 4 filters on completed_by + status and sorts by creation. A
	# completed_by-only index served the filter and then left the planner to
	# sort 28,864 rows for the heaviest approver -- 1.05 s. The composite
	# covers filter and sort together.
	("Workflow Action", ["completed_by", "status", "creation"]),
	("ToDo", ["allocated_to"]),
)


def after_install():
	add_indexes()
	add_state_indexes()
	ensure_workspace()


def after_migrate():
	add_indexes()
	add_state_indexes()
	ensure_workspace()
	clear_caches()


def ensure_workspace():
	"""Put the My Work workspace back if it is not there.

	Frappe already imports it. `workspace` is one of the folders listed in
	`frappe.model.sync.IMPORTABLE_DOCTYPES`, so `bench migrate` walks
	`task_manager/task_manager/workspace/` and loads the JSON like any other
	module asset. On a healthy site this function finds the record present
	and does nothing.

	It exists for the cases where that is not enough, and they are ordinary
	rather than exotic:

	* `import_file_by_path` compares the file`s `modified` against the row`s
	  and **skips when the row is newer**. Anyone who opens the workspace in
	  the Desk editor and saves it bumps the row past the file permanently,
	  so a workspace deleted after that point is never restored by sync.
	* A failed or half-finished migrate can leave the record missing while
	  the app is installed.
	* `bench --force` flows and restores from backup do not always carry
	  Workspace rows.

	What it deliberately does NOT do is overwrite a workspace that already
	exists. Rearranging the shortcuts on it is a legitimate thing for an
	administrator to do, and a migrate that silently undoes that every time
	would be worse than the problem being fixed. Create if missing, leave
	alone otherwise.

	The row and the file go missing independently, so both directions are
	repaired:

	* row missing, file present -> insert from the file. The ordinary case,
	  and the one this function was asked for.
	* row present, file missing -> rewrite the file from the row. Only
	  happens with `developer_mode` on, where deleting a module-owned
	  document deletes its source file as well. That is a real trap: one
	  delete from the Desk UI removes the app`s own definition from the
	  working tree, and nothing puts it back.
	* both missing -> logged, and nothing else can be done. The definition
	  lives in version control; restore the file and migrate again.

	Why this rather than `hooks.fixtures`
	-------------------------------------

	`fixtures` is for records this app wants to carry that live in *another*
	app`s doctype and have no home of their own -- it exports on demand and
	imports wholesale. A Workspace belonging to this app`s own module already
	has a home, the module folder, and that folder is already synced. Adding
	it to `fixtures` would give the same record two import paths that can
	disagree about which is newer, and a `Workspace` fixture filter wide
	enough to catch this one would also drag in workspaces owned by other
	apps on export.
	"""
	path = os.path.join(frappe.get_app_path("task_manager"), WORKSPACE_JSON)
	on_disk = os.path.exists(path)

	if frappe.db.exists("Workspace", WORKSPACE):
		if not on_disk:
			_rewrite_definition(path)
		return

	if not on_disk:
		frappe.log_error(
			title=f"{LOG_PREFIX}: workspace definition missing"[:140],
			message=(
				"Neither the Workspace row nor its definition exists. "
				"Restore it from version control and migrate again.\n\n"
				f"Expected the shipped JSON at {path}"
			),
		)
		return

	try:
		with open(path, encoding="utf-8") as handle:
			definition = json.load(handle)

		# `modified` is dropped on purpose. Keeping the file`s timestamp
		# would insert a row that is already older than the file, which is
		# the state that makes the next sync want to re-import it. Letting
		# the insert stamp `now` leaves the row newer than the file, which is
		# what a normal import produces.
		definition.pop("modified", None)

		doc = frappe.get_doc(definition)
		doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
		frappe.db.commit()
	except Exception:
		# A missing workspace is a navigation inconvenience. It must never be
		# the thing that fails a migration.
		frappe.log_error(
			title=f"{LOG_PREFIX}: could not create the {WORKSPACE} workspace"[:140],
			message=frappe.get_traceback(),
		)


def _rewrite_definition(path: str):
	"""Put the shipped JSON back from the row that is still in the database.

	`export_doc` is what `developer_mode` already calls on every save of a
	module-owned document, so this writes the same file the Desk would have
	written -- it is not a second, hand-rolled serialiser that could drift
	from the first.
	"""
	try:
		from frappe.modules.export_file import export_doc

		os.makedirs(os.path.dirname(path), exist_ok=True)
		export_doc(frappe.get_doc("Workspace", WORKSPACE))
	except Exception:
		frappe.log_error(
			title=f"{LOG_PREFIX}: could not rewrite the workspace definition"[:140],
			message=frappe.get_traceback(),
		)


def add_indexes():
	for doctype, fields in INDEXES:
		_index(doctype, fields)


def add_state_indexes():
	"""Index `owner` and the workflow state field on every workflow doctype.

	`owner` is the one that matters, and it was measured, not assumed. Tab 3
	asks each doctype "which documents did I raise that are still pending",
	and on Purchase Invoice that query took 3.0 seconds against 25,197 rows
	for a user who owns none of them. EXPLAIN showed why:

	* The state filter is not selective. Tab 3 asks for every non-terminal
	  state, which on Purchase Invoice is all four of them -- 10,197 of 25,197
	  rows. An index on `workflow_state` cannot help a predicate that matches
	  40% of the table.
	* `owner` is selective -- zero rows here -- and was not indexed, so the
	  planner fell back to walking the `modified` index in order, looking for
	  200 matches that did not exist, i.e. a full scan.

	Indexing `owner` turns that into a zero-row index lookup. The state index
	is kept because it does help Tab 2, where `my_states` is usually a narrow
	set, but on its own it fixed nothing.

	Deriving the list from the active workflows rather than hardcoding it
	means a workflow added later gets its indexes on the next migrate.
	"""
	workflows = frappe.get_all(
		"Workflow",
		filters={"is_active": 1},
		fields=["document_type", "workflow_state_field"],
		ignore_permissions=True,
	)

	for workflow in workflows:
		doctype = workflow.document_type
		field = workflow.workflow_state_field
		if not (doctype and field):
			continue
		# Four active workflows on this site point at deleted doctypes.
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.db.has_column(doctype, field):
			continue
		_index(doctype, ["owner"])
		_index(doctype, [field])


def _index(doctype, fields):
	try:
		frappe.db.add_index(doctype, fields)
	except Exception:
		# A missing table or a permission-starved database user should slow
		# the page down, not break the migration.
		frappe.log_error(
			title=f"{LOG_PREFIX}: could not index {doctype}.{fields[0]}"[:140],
			message=frappe.get_traceback(),
		)


def clear_caches():
	from task_manager.services import workflow_index

	workflow_index.clear_index()
