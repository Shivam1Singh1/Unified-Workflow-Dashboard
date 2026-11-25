# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""The named-approver gate: everything in scope.py that needs a query.

Two jobs.

**Resolving the requester.** Tab 3 asks "which requests did I raise". For a
generic workflow that is `owner`, and for these three it is not: HR raises
leave and On Duty requests on behalf of other people, so `owner` is the HR
user while the request belongs to the employee named on it. The request should
sit in the employee's Tab 3, not HR's, which means matching on the employee
field instead. `employee_of` is that lookup.

**Enforcement, when it is switched on.** `enforce_named_approver` is a
validate hook that refuses a state change by anyone who is not the person the
request names. It is registered only when `scope.ENFORCE_NAMED_APPROVER` is
True, which it is not by default -- see the note on that flag for why the
decision is somebody else's to make.

The read path -- deciding which rows a user may see -- needs no queries at
all. `approvals._gather` already selects the approver fields alongside every
row, so `scope.may_act` answers from data that is in hand. That is the whole
reason the fields are selected there rather than looked up here: one extra
column on a query that already runs costs nothing, while a per-row lookup
would be 200 queries for a list that renders six.
"""

import frappe
from frappe import _

# `add_docshare`, not the whitelisted `add`, and imported explicitly.
#
# Two separate traps, both of which produced a silent no-op here before being
# found, because the call site logs and swallows:
#
# * `import frappe` does not load `frappe.share`, so `frappe.share.add(...)`
#   raises AttributeError in anything that has not already imported it -- a
#   scheduled job, a bench script, a test.
# * The whitelisted `add()` takes no `flags`, so passing one is a TypeError.
#   `flags={"ignore_share_permission": True}` is not optional: without it
#   `check_share_permission` runs as whoever saved the document, and an
#   employee saving their own leave request has no share permission on it.
#
# `add_docshare` is also idempotent -- it looks for an existing DocShare via
# `get_share_name` and updates it rather than inserting a second row. This is
# the same call hrms makes in `share_doc_with_approver`.
from frappe.share import add_docshare

from task_manager import scope

# Request-scoped only. A worker serves many users in sequence, so this is
# seeded and read inside one request and never allowed to outlive it -- the
# key is the user, so a stale entry cannot be attributed to the wrong person
# even if the dict somehow survived.
_EMPLOYEE_CACHE_KEY = "task_manager:employee_of"


def employee_of(user: str) -> str | None:
	"""The Employee record this user is, or None.

	`user_id` is not unique on Employee -- a person who has been rehired has
	two records -- so an Active one is preferred and the newest wins. Taking
	whichever row the database returned first would attach the user's requests
	to a Left employee record and empty their Tab 3.
	"""
	if not user:
		return None

	cache = frappe.local.cache.setdefault(_EMPLOYEE_CACHE_KEY, {}) if hasattr(
		frappe.local, "cache"
	) else {}
	if user in cache:
		return cache[user]

	found = frappe.get_all(
		"Employee",
		filters={"user_id": user},
		fields=["name", "status"],
		order_by="field(status, 'Active', 'Suspended', 'Inactive', 'Left'), modified desc",
		limit=1,
		ignore_permissions=True,
	)
	employee = found[0]["name"] if found else None
	cache[user] = employee
	return employee


def select_fields(entry: dict) -> list[str]:
	"""Extra columns a scoped list query must carry for the gate to decide.

	Empty for an ungated doctype: no Employee link and no approver field means
	there is nothing extra to select. Returning `[None] + []` instead would put
	a null in the field list and fail the query.
	"""
	fields = scope.all_approver_fields(entry)
	if entry.get("employee_field"):
		fields = [entry["employee_field"]] + fields
	return fields


def requester_filters(entry: dict, user: str) -> dict | None:
	"""Filters matching "requests belonging to me" for a scoped doctype.

	None means "fall back to owner": a user with no Employee record cannot own
	an employee request, so there is nothing to match on and the generic rule
	is the honest answer.
	"""
	if not entry.get("employee_field"):
		# An Accounts document names no employee -- an invoice belongs to
		# whoever raised it. None sends `_gather` to its `owner` fallback,
		# which is the "created by" side and the narrowing these four need:
		# `get_list` will not supply it, because 719 users hold `Employee`,
		# `Employee` reads Purchase Invoice, and there is no `if_owner` and no
		# User Permission row on any of the four.
		return None
	employee = employee_of(user)
	if not employee:
		return None
	return {entry["employee_field"]: employee}


def approver_names(rows: list) -> dict:
	"""Full names for every approver id about to be displayed, in one query.

	The ids themselves never reach the browser -- they are email addresses on
	this site. This returns a lookup the caller uses to replace them.
	"""
	ids = set()
	for row in rows:
		entry = scope.for_doctype(row["doctype"])
		if not entry:
			continue
		ids.update(scope.named_approvers(entry, row["state"], row))
	ids.discard("")

	if not ids:
		return {}

	users = frappe.get_all(
		"User",
		filters={"name": ["in", list(ids)]},
		fields=["name", "full_name"],
		ignore_permissions=True,
	)
	# Keyed on the normalised id, because the Data approver fields are not
	# guaranteed to match User.name in case.
	return {u["name"].strip().lower(): (u["full_name"] or u["name"]) for u in users}


def unroutable(rows: list) -> int:
	"""Scoped rows sitting at an approval state that names nobody.

	These are real and they are invisible: 9 of 9,192 Gate Pass rows have a
	blank approver, and 21 of 656 active employees have no
	`head_leave_approver`. A request whose approver field is empty can never
	pass the gate, so it would silently vanish from every tab. Counting it is
	what lets the page say so instead.
	"""
	count = 0
	for row in rows:
		entry = scope.for_doctype(row["doctype"])
		if not entry:
			continue
		if not scope.is_gated(entry):
			# An ungated row names nobody because nobody is named on it. That
			# is the model, not a hole, and the role route still carries it.
			continue
		if row["state"] not in entry["levels"]:
			continue
		if not scope.named_approvers(entry, row["state"], row):
			count += 1
	return count


# ---- the on_update hook ----------------------------------------------------


def on_request_update(doc, method=None):
	"""Everything this app does when an in-scope request is saved.

	One handler rather than two hooks, because the two halves need the same
	guards and the same "never fatal" promise, and because the order matters:
	share first, then drop the caches that would otherwise still describe the
	world before the share.
	"""
	share_with_named_approvers(doc, method)
	clear_participant_caches(doc, method)


def clear_participant_caches(doc, method=None):
	"""Drop the cached payload for everybody this request appears to.

	Without this an approved request keeps showing in its approver's list for
	up to the cache TTL -- they approve it, come back, and it is still sitting
	there as if nothing happened. The requester has the same problem in
	reverse: their request is approved and their own page still says it is
	waiting.

	The people affected are exactly the requester and the approvers named at
	the state the document was in *before* this save plus the one it is in
	now, so a level-1 approval clears both the person who just acted and the
	person it has moved to.
	"""
	entry = scope.for_doctype(doc.doctype)
	if not entry:
		return

	from task_manager.services import cache

	try:
		users = set()

		state_field = _state_field(entry)
		before = doc.get_doc_before_save()

		for source in (doc, before):
			if not source:
				continue
			state = source.get(state_field) if state_field else None
			if state:
				users.update(scope.named_approvers(entry, state, source))

		# The requester, by the employee named on the request rather than by
		# authorship -- HR raises these for other people. An Accounts document
		# names no employee, so its raiser is reached through `doc.owner` below.
		employee = doc.get(entry["employee_field"]) if entry.get("employee_field") else None
		if employee:
			user_id = frappe.db.get_value("Employee", employee, "user_id")
			if user_id:
				users.add(user_id)
		if doc.owner:
			users.add(doc.owner)

		for user in users:
			if user:
				cache.clear(user)
	except Exception:
		frappe.log_error(
			title=f"Task Manager: could not clear caches for {doc.doctype}"[:140],
			message=f"name: {doc.name}\n\n{frappe.get_traceback()}",
		)


# ---- sharing ---------------------------------------------------------------


def share_with_named_approvers(doc, method=None):
	"""Give the named approver access to the request that names them.

	Hooked on `on_update`, which is where HRMS does the same thing for
	Leave Application's level-1 approver. Without it the approver cannot see
	the request at all: 289 to 291 `User Permission` rows per doctype pin
	those users to their own employee record, so another employee's request is
	invisible to them in a list and on a form alike. See
	scope.SHARE_WITH_NAMED_APPROVER for the measured breakdown.

	Three properties this needs and has:

	* **Idempotent.** `add_docshare` reuses an existing DocShare row rather
	  than inserting a second one, so saving a request fifty times leaves
	  one share. It is also skipped entirely when the approver can already
	  submit, which most can.
	* **Additive only.** Nothing here removes a share. A request that moves
	  from level 1 to level 2 leaves the level-1 approver's access in place --
	  they have already acted on it and may need to look again -- and an
	  approver swapped out mid-request keeps read access to something they
	  were legitimately handling.
	* **Never fatal.** A failure to share must not block somebody saving a
	  leave request. It is logged and swallowed.
	"""
	if not scope.SHARE_WITH_NAMED_APPROVER:
		return

	entry = scope.for_doctype(doc.doctype)
	if not entry:
		return

	if not scope.is_gated(entry):
		# Nobody is named, so there is nobody to share with -- and nothing to
		# work around either: the Accounts doctypes carry zero User Permission
		# rows, which is the restriction this whole mechanism exists to undo.
		return

	state_field = _state_field(entry)
	if not state_field:
		return

	state = doc.get(state_field)
	if not state or state not in entry["levels"]:
		return  # a draft, or a finished request: nobody is waiting on it

	for approver in scope.named_approvers(entry, state, doc):
		if approver == (doc.owner or "").lower():
			continue  # already theirs
		try:
			if not frappe.db.exists("User", {"name": approver, "enabled": 1}):
				continue
			# Only share when they cannot already act. Most approvers are
			# unrestricted and need nothing, so this keeps the DocShare table
			# from growing a row per request for no reason. Same guard hrms
			# uses.
			if frappe.has_permission(doc=doc, ptype="submit", user=approver):
				continue
			add_docshare(
				doc.doctype,
				doc.name,
				approver,
				read=1,
				write=1,
				# All three are submittable, and approving means submitting.
				submit=1,
				flags={"ignore_share_permission": True},
			)
		except Exception:
			frappe.log_error(
				title=f"Task Manager: could not share {doc.doctype} with approver"[:140],
				message=(
					f"doctype  : {doc.doctype}\n"
					f"name     : {doc.name}\n"
					f"approver : {approver}\n\n{frappe.get_traceback()}"
				),
			)


def unshared(doctype: str) -> dict:
	"""How many pending requests their named approver cannot actually see.

	A diagnostic, not part of any request path. The number is the honest
	measure of whether this doctype's approval tab works: a request the
	approver cannot list is a request Task Manager cannot show them, because
	`get_list` is the security boundary the tabs are built on and is not
	bypassed for anybody.
	"""
	entry = scope.for_doctype(doctype)
	if not entry:
		return {}

	filters = {"workflow_state": ["in", list(entry["levels"])], "docstatus": ["<", 2]}
	filters.update(entry["row_filters"])

	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", "workflow_state"] + select_fields(entry),
		ignore_permissions=True,
	)

	visible = blocked = unnamed = 0
	original = frappe.session.user
	try:
		for row in rows:
			approvers = scope.named_approvers(entry, row["workflow_state"], row)
			if not approvers:
				unnamed += 1
				continue
			seen = False
			for approver in approvers:
				if not frappe.db.get_value("User", approver, "enabled"):
					continue
				frappe.set_user(approver)
				try:
					if frappe.get_list(
						doctype, filters={"name": row["name"]}, fields=["name"], ignore_ifnull=True
					):
						seen = True
						break
				except Exception:
					pass
				finally:
					frappe.set_user(original)
			if seen:
				visible += 1
			else:
				blocked += 1
	finally:
		frappe.set_user(original)

	return {
		"doctype": doctype,
		"pending": len(rows),
		"visible_to_approver": visible,
		"blocked": blocked,
		"no_approver_named": unnamed,
	}


def stale_shares(doctype: str | None = None, revoke: bool = False, limit: int = 0) -> dict:
	"""Shares this app granted that the holder no longer has a reason to hold.

	`share_with_named_approvers` is additive by design -- the docstring there
	says so and gives the reason: an approver who has already acted may need
	to look again, and an approver swapped out mid-request was legitimately
	handling it. That is a defensible default and this function does not
	change it.

	What it does is make the consequence visible. A DocShare is a grant of
	read, write and submit on one document to one named person, and it
	outlives everything about why it was made: the approval, the approver
	field being edited, the role being taken away, the account being
	disabled. Removing somebody`s role does not remove the shares this app
	wrote for them, so "they should only see what they are permitted to see"
	is not true of those documents until the rows go.

	Two populations are reported, and they are not the same question:

	  orphaned   the holder is disabled, or is no longer a System User. There
	             is no argument for keeping these -- the account cannot log
	             in to use them and the grant is pure residue.
	  reassigned the holder is no longer named as an approver on the document.
	             Keeping these is the documented intent, so they are reported
	             separately and are the ones to think about rather than
	             reflexively clear.

	Read-only unless `revoke=True`, and never called from install, migrate or
	any request path. Deleting a DocShare takes access away from somebody, so
	run the report first and read it:

	    bench --site <site> execute \\
	      task_manager.services.approver_gate.stale_shares

	    bench --site <site> execute \\
	      task_manager.services.approver_gate.stale_shares \\
	      --kwargs "{'revoke': True}"

	Note for anyone verifying this on a live site: DocShare writes commit
	internally, so `frappe.db.rollback()` does not undo a revoke. There is no
	safe "try it and roll back".
	"""
	doctypes = [doctype] if doctype else scope.doctypes()

	report = {
		"dry_run": not revoke,
		"orphaned": [],
		"reassigned": [],
		"checked": 0,
		"revoked": 0,
	}

	for dt in doctypes:
		entry = scope.for_doctype(dt)
		if not entry or not scope.is_gated(entry):
			# An ungated doctype names nobody, so this app never shared it and
			# any DocShare on it belongs to somebody else.
			continue

		fields = scope.all_approver_fields(entry)

		shares = frappe.get_all(
			"DocShare",
			filters={"share_doctype": dt},
			fields=["name", "user", "share_name"],
			limit_page_length=limit or 0,
			ignore_permissions=True,
		)
		if not shares:
			continue

		# Two batched lookups per doctype, not two per share. Written the
		# obvious way first -- a `get_value` for the document and another for
		# the holder, inside the loop -- this issued 2,076 round trips for
		# the 1,038 shares on this site and took long enough that a test
		# calling it three times timed out. Nothing about the question needs
		# to be asked one row at a time.
		named_by_name = _rows_by_name(dt, [s["share_name"] for s in shares], fields)
		holders = _holders({s["user"] for s in shares if s["user"]})

		for share in shares:
			named = named_by_name.get(share["share_name"])
			if named is None:
				# The document is gone. Core removes shares with it, so this
				# is a race rather than a finding.
				continue

			report["checked"] += 1

			holder = holders.get(share["user"])
			holds = (share["user"] or "").strip().lower() in named

			if not holder or not holder["enabled"] or holder["user_type"] != "System User":
				bucket = "orphaned"
			elif not holds:
				bucket = "reassigned"
			else:
				continue

			report[bucket].append(
				{"doctype": dt, "name": share["share_name"], "user": share["user"]}
			)

			# Only the indefensible half is ever removed, and only on request.
			# A reassigned share is the documented behaviour of this app, not
			# a defect, so clearing it has to be a separate decision taken by
			# a person rather than a side effect of running the report.
			if revoke and bucket == "orphaned":
				try:
					frappe.delete_doc(
						"DocShare", share["name"], force=True, ignore_permissions=True
					)
					report["revoked"] += 1
				except Exception:
					frappe.log_error(
						title="Task Manager: could not revoke a stale share"[:140],
						message=frappe.get_traceback(),
					)

	report["orphaned_total"] = len(report["orphaned"])
	report["reassigned_total"] = len(report["reassigned"])
	return report


# `in` clauses are chunked rather than handed the whole list. A site with tens
# of thousands of shares on one doctype would otherwise build a single query
# with that many bound parameters, which MariaDB accepts right up until it
# does not.
_CHUNK = 500


def _rows_by_name(doctype: str, names: list, fields: list) -> dict:
	"""{document name -> the set of approver ids named on it}, batched."""
	out = {}
	unique = list(dict.fromkeys(names))

	for start in range(0, len(unique), _CHUNK):
		chunk = unique[start : start + _CHUNK]
		for row in frappe.get_all(
			doctype,
			filters={"name": ["in", chunk]},
			fields=["name"] + fields,
			limit_page_length=0,
			ignore_permissions=True,
		):
			out[row["name"]] = {
				(row.get(field) or "").strip().lower()
				for field in fields
				if row.get(field)
			}

	return out


def _holders(users: set) -> dict:
	"""{user id -> enabled/user_type}, batched. Missing users are simply absent."""
	out = {}
	unique = list(users)

	for start in range(0, len(unique), _CHUNK):
		chunk = unique[start : start + _CHUNK]
		for row in frappe.get_all(
			"User",
			filters={"name": ["in", chunk]},
			fields=["name", "enabled", "user_type"],
			limit_page_length=0,
			ignore_permissions=True,
		):
			out[row["name"]] = row

	return out


def backfill_shares(doctype: str, dry_run: bool = True, limit: int = 0) -> dict:
	"""Share the *already pending* requests with their named approvers.

	The `on_update` hook only fires when a request is next saved, so the
	requests pending at the moment this app is installed stay invisible to
	their approvers until somebody touches them. This closes that gap.

	It writes, so it defaults to `dry_run=True` and is never called
	automatically -- not from install, not from migrate. Run it deliberately:

	    bench --site <site> execute \\
	      task_manager.services.approver_gate.backfill_shares \\
	      --kwargs "{'doctype': 'Gate Pass', 'dry_run': False}"
	"""
	entry = scope.for_doctype(doctype)
	if not entry:
		frappe.throw(_("{0} is not a Task Manager scoped doctype").format(doctype))

	filters = {"workflow_state": ["in", list(entry["levels"])], "docstatus": ["<", 2]}
	filters.update(entry["row_filters"])

	rows = frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", "owner", "workflow_state"] + select_fields(entry),
		limit_page_length=limit or 0,
		ignore_permissions=True,
	)

	planned = 0
	written = 0
	for row in rows:
		for approver in scope.named_approvers(entry, row["workflow_state"], row):
			if approver == (row["owner"] or "").lower():
				continue
			if not frappe.db.get_value("User", approver, "enabled"):
				continue
			# Same guard as the hook, and it has to be handed a *loaded
			# document*, not a name. `has_permission(doctype, "submit",
			# doc=<name>, ...)` answered True for all 534 pending requests,
			# including the 144 their approver provably cannot list -- so the
			# guard silently skipped every share that was actually needed.
			# With a Document it agrees with `get_list`.
			try:
				doc = frappe.get_doc(doctype, row["name"])
			except Exception:
				continue
			if frappe.has_permission(doc=doc, ptype="submit", user=approver):
				continue
			planned += 1
			if dry_run:
				continue
			try:
				add_docshare(
					doctype,
					row["name"],
					approver,
					read=1,
					write=1,
					submit=1,
					flags={"ignore_share_permission": True},
				)
				written += 1
			except Exception:
				frappe.log_error(
					title=f"Task Manager: backfill share failed for {doctype}"[:140],
					message=f"name: {row['name']}\napprover: {approver}\n\n{frappe.get_traceback()}",
				)

	if not dry_run:
		frappe.db.commit()

	return {
		"doctype": doctype,
		"requests": len(rows),
		"shares_planned": planned,
		"shares_written": written,
		"dry_run": dry_run,
	}


# ---- enforcement (only registered when scope.ENFORCE_NAMED_APPROVER) -------


def enforce_named_approver(doc, method=None):
	"""Refuse a state change by anybody the request does not name.

	Hooked on `validate`, so it covers the form, `apply_workflow` and any
	other path that saves the document -- there is no client-side route around
	it.

	The check is against the state being *left*, not the one being entered: an
	approval is permission to move a document out of where it currently sits.
	"""
	if not scope.ENFORCE_NAMED_APPROVER:
		return

	entry = scope.for_doctype(doc.doctype)
	if not entry:
		return

	before = doc.get_doc_before_save()
	if not before:
		return  # an insert; there is no state to be leaving

	state_field = _state_field(entry)
	if not state_field:
		return

	old_state = before.get(state_field)
	new_state = doc.get(state_field)
	if not old_state or old_state == new_state:
		return

	if not scope.is_gated(entry):
		return  # no named approver to enforce; the role route is the rule

	if old_state not in entry["levels"]:
		return  # leaving Draft, or a state this app does not govern

	user = frappe.session.user
	if user == "Administrator":
		return
	if set(scope.BYPASS_ROLES) & set(frappe.get_roles(user)):
		return
	if scope.may_act(entry, old_state, before, user):
		return

	# Named, so the person reading it knows who to chase -- this is a message
	# to a colleague who clicked a button they should not have been offered,
	# not a security boundary that has to stay silent.
	approvers = scope.named_approvers(entry, old_state, before)
	waiting_on = ", ".join(
		frappe.db.get_value("User", a, "full_name") or a for a in approvers
	) or _("nobody")

	frappe.throw(
		_("This request is waiting on {0}. Only they can approve it.").format(waiting_on),
		frappe.PermissionError,
		title=_("Not your approval"),
	)


def _state_field(entry: dict) -> str | None:
	try:
		return frappe.get_cached_value("Workflow", entry["workflow"], "workflow_state_field")
	except Exception:
		return None
