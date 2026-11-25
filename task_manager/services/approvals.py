# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Tabs 2, 3 and 4, and the level trail they share.

The shape of this module is dictated by two measured costs.

**Document loads are the expensive part.** A transition condition cannot be
evaluated in SQL: core's `is_transition_condition_satisfied` calls
`doc.as_dict()`, so the whole document has to be loaded. That was measured at
17.9 ms per document on Purchase Order. The only defence is to decide *which*
documents matter before loading any of them -- so candidates are gathered
cheaply, merged, sorted and cut to the display window first, and conditions are
evaluated last, on the survivors alone. Doing it the other way round cost 6.8
seconds for one real user.

**Caps have to be honest.** The heaviest approver on this site has 28,864
completed approvals, and 182 of their 200 most recent are still sitting at a
non-terminal state. A silent 200-row cut would present a firehose as a
complete list, so every tab reports whether it was truncated and the page says
so.

Everything returned is name-and-date only. `completed_by` is a user id, which
is an email address on this site, and it never leaves this module.
"""

import frappe
from frappe import _
from frappe.model.workflow import (
	get_workflow_safe_globals,
	is_transition_condition_satisfied,
)
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import now_datetime, time_diff_in_hours

from task_manager import scope
from task_manager.services import approver_gate

LIMIT = 200

# Hard ceiling on full document loads per tab. The measured worst case needed
# 238; this leaves headroom without letting a pathological user spend minutes.
CONDITION_BUDGET = 250


def needs_my_action(user: str, index: dict) -> dict:
	"""Tab 2 -- documents whose current state I can move out of."""
	candidates, truncated = _gather(
		index,
		lambda entry: entry["my_states"],
		approval_only=True,
	)

	candidates.sort(key=lambda row: row["modified"])
	window = candidates[:LIMIT]

	rows = []
	loads = 0
	for row in window:
		entry = index[row["doctype"]]
		state = row["state"]

		# The named-approver gate, for the three HRMS request types only.
		#
		# This runs before the condition check because it is free -- the
		# approver fields came back with the row -- and because it is the
		# check that actually holds. Four transitions on these workflows are
		# granted to a role with no condition at all, and
		# `_condition_passes` correctly returns True for those, which was
		# offering 47 Head Leave Approvers every pending On Duty request and
		# 76 Department HODs every pending Gate Pass. See scope.py.
		#
		# The condition check below is deliberately still applied on top. The
		# gate is stronger than every condition these three workflows carry
		# today, but a condition edited later might test something else
		# entirely, and this page should not quietly stop honouring it.
		#
		# It runs for the gated doctypes only. On the four Accounts types no
		# approver is named, so `may_act` would be False for everybody and the
		# document would reach no tab at all. There the role-and-condition
		# engine below is the whole answer, and the correct one: 17 Accounts
		# Managers share that queue by design.
		scope_entry = scope.for_doctype(row["doctype"])
		if scope_entry:
			# A state this entry does not call a level is the raiser's own --
			# a draft to send, or a rejection to resend. It is never an
			# approval, whoever holds the role that moves it.
			#
			# The gated types got this free: a draft names no approver, so
			# `may_act` was already False. The ungated ones do not, and the
			# workflows grant exactly these routes to a broad role -- `Draft ->
			# Send For Approval` and `Rejected by Account Manager -> Resend For
			# Approval` are both `Accounts User`, 42 holders. Without this line
			# every one of them was offered all 325 draft and 331 rejected
			# Purchase Invoices as their own approvals. Measured: 193 rows of
			# other people's drafts in one Accounts User's Tab 2.
			#
			# Tab 3 still shows these to whoever raised them, by `owner`.
			if state not in scope_entry["levels"]:
				continue
			if scope.is_gated(scope_entry) and not scope.may_act(
				scope_entry, state, row, user
			):
				continue

		if row["owner"] == user and not _self_approval_allowed(entry, state, user):
			# Core lets Administrator approve their own document regardless.
			# This page applies the stricter rule to everybody, Administrator
			# included: your own document is not yours to approve unless the
			# transition explicitly says so.
			continue

		if _needs_condition_check(entry, state, user):
			if loads >= CONDITION_BUDGET:
				truncated = True
				continue
			loads += 1
			if not _condition_passes(entry, row, user):
				continue

		rows.append(row)

	# Counted before _decorate, which strips the approver fields off the rows
	# it keeps -- and `rows` holds the same dict objects as `window`.
	stranded = approver_gate.unroutable(window)

	_decorate(rows, index, user)
	return {
		"rows": rows,
		"shown": len(rows),
		"total": len(candidates),
		"truncated": truncated or len(candidates) > LIMIT,
		"documents_loaded": loads,
		"unroutable": stranded,
	}


def awaiting_others(user: str, index: dict) -> dict:
	"""Tab 3 -- documents I raised that are parked on somebody else."""
	candidates, truncated = _gather(
		index,
		# For a scoped doctype the state set is deliberately widened to every
		# non-terminal state. `others_states` is derived from roles, and for
		# these three that is the wrong question: whether a request is waiting
		# on somebody else depends on whether *I* am the person it names, not
		# on which roles I hold. An approver who raises their own leave holds
		# a role covering its state, so the role-derived set would drop their
		# own request from this tab -- and the gate in Tab 2 would then
		# correctly refuse it too, leaving it in neither.
		lambda entry: entry["non_terminal"] if entry.get("in_scope") else entry["others_states"],
		owner=user,
	)

	kept = []
	for row in candidates:
		scope_entry = scope.for_doctype(row["doctype"])
		if not scope_entry:
			kept.append(row)
			continue
		if row["state"] not in scope_entry["levels"]:
			# A draft I have not sent yet. It is waiting on me to send it, not
			# on anybody else, and 187 of these exist across On Duty and Gate
			# Pass -- enough to swamp the tab with work nobody is blocked on.
			continue
		if scope.is_gated(scope_entry):
			if scope.may_act(scope_entry, row["state"], row, user):
				continue  # waiting on me; Tab 2 owns it
		elif row["state"] in index[row["doctype"]]["my_states"]:
			# Ungated equivalent of the line above. Nobody is named, so "is it
			# waiting on me" is a question about roles again: if I hold one that
			# moves this state on, it is mine to act on and Tab 2 has it. An
			# Accounts Manager who raises their own journal entry would
			# otherwise see it in both tabs.
			continue
		kept.append(row)
	candidates = kept

	candidates.sort(key=lambda row: row["modified"])
	rows = candidates[:LIMIT]

	stranded = approver_gate.unroutable(rows)
	_decorate(rows, index, user)

	from task_manager.services import reminders

	reminded = reminders.last_reminders(rows)
	today = frappe.utils.nowdate()
	for row in rows:
		# Two kinds of row share this card now. Stamped on every row rather
		# than inferred from a missing key in the template, because "no level"
		# and "no workflow" are different states of the world and the page
		# should not have to guess which one it is looking at.
		row["kind"] = "workflow"
		row["waiting_on_roles"] = _roles_out_of(index[row["doctype"]], row["state"])
		last = reminded.get((row["doctype"], row["name"]))
		row["last_reminder"] = last
		row["reminded_today"] = bool(last and str(last)[:10] == today)

	# ---- the other family ------------------------------------------------
	#
	# Submittable doctypes with no workflow contribute here and nowhere else.
	# See services/drafts.py for why an unsent draft is the only thing they
	# can honestly contribute, and why none of it reaches Tab 2.
	#
	# The merge happens after the workflow rows are decorated, not before:
	# `_decorate`, `pending_since` and `level_trail` all key off `index`, and
	# a doctype with no workflow is by definition not in it. drafts.py returns
	# rows already carrying every field this card reads, so what is left here
	# is a sort and a cut.
	#
	# Sorted on `pending_hours` rather than `modified`, because that is the
	# one ordering key both kinds are guaranteed to carry; descending on it is
	# the same oldest-first the rest of the page promises.
	draft_total = 0
	if scope.COVER_ALL_SUBMITTABLE:
		from task_manager.services import drafts as draft_service
		from task_manager.services import workflow_index

		mine = draft_service.my_drafts(
			user,
			workflow_index.draft_doctypes_for_user(user),
			workflow_index.held_back_for_user(user),
		)
		draft_total = mine["total"]
		truncated = truncated or mine["truncated"]
		rows = rows + mine["rows"]

	# Sorted here and only here, whether or not anything was merged in.
	#
	# The sort above this is on `modified` and runs before `_decorate`, which
	# is the only order available at that point -- `pending_since` does not
	# exist until the Workflow Action rows have been read. It is also not the
	# order this card claims: every row shows how long it has waited, and
	# `modified` is not that. Two rows one minute apart in `modified` can be
	# months apart in waiting time.
	#
	# Re-sorting on `pending_hours` costs nothing on a list already cut to
	# LIMIT, is the number actually printed on each row, and is the one key
	# both kinds of row carry. Doing it unconditionally matters: sorting only
	# when drafts happened to be merged would have made the order of this
	# card depend on whether the reader had any unsent drafts.
	rows.sort(key=lambda row: row["pending_hours"], reverse=True)
	rows = rows[:LIMIT]

	total = len(candidates) + draft_total

	return {
		"rows": rows,
		"shown": len(rows),
		"total": total,
		# How many of the shown rows are unsent drafts rather than requests
		# somebody else is holding. The card says so, because "12 requests"
		# and "12 things, 9 of which you never sent" are different reports.
		"drafts": sum(1 for row in rows if row.get("kind") == "draft"),
		"truncated": truncated or total > LIMIT,
		"unroutable": stranded,
	}


def approved_by_me(user: str, index: dict) -> dict:
	"""Tab 4 -- documents I cleared that a later level has not acted on.

	Completed Workflow Action rows are the only record of who cleared which
	level, and they can only be read with `get_all`: the doctype's own
	permission query appends `status = 'Open'`, which makes every Completed row
	invisible to `get_list` for everybody except Administrator. So the rows are
	fetched unfiltered by permission and then every referenced document is
	re-fetched through `get_list`, which is what actually enforces access.

	That second pass is not a formality. Half the Workflow Action table points
	at documents that no longer exist -- 51% of the newest Open rows, sampled.
	"""
	actions = frappe.get_all(
		"Workflow Action",
		filters={"completed_by": user, "status": "Completed"},
		fields=["reference_doctype", "reference_name", "workflow_state", "creation"],
		order_by="creation desc",
		limit_page_length=LIMIT,
		ignore_permissions=True,
	)
	total_approvals = frappe.db.count(
		"Workflow Action", {"completed_by": user, "status": "Completed"}
	)

	my_level_at = {}
	my_date_at = {}
	grouped = {}
	for action in actions:
		doctype = action["reference_doctype"]
		if doctype not in index:
			# Unreadable, module-blocked, ghost doctype, or no active workflow.
			continue

		# Submitting your own document is not approving it.
		#
		# Core writes a Completed Workflow Action for *every* transition a
		# user performs, including `Draft -> Submit` on a document they
		# raised themselves. This tab reads that table as "levels I cleared",
		# so without this line a requester who has never approved anything
		# gets their own requests back under "Stuck after you", captioned
		# "You approved step 1".
		#
		# Found on a real document: HR-EXP-25-26-00419, an Expense Claim
		# raised by anuj.desai and still pending with Accounts. His only
		# Workflow Action on it is the Draft one -- his own submission -- and
		# it was the whole of his Stuck-after-you card. His own request was
		# being presented to him as somebody else`s approval that he had
		# handled.
		#
		# Same rule `_gather` applies to Tab 2, read off the same
		# `start_states`. A document where the user *also* cleared a genuine
		# level still appears, with that level, because only the start-state
		# rows are skipped and not the document.
		if action["workflow_state"] in set(index[doctype].get("start_states") or []):
			continue

		grouped.setdefault(doctype, []).append(action["reference_name"])
		key = (doctype, action["reference_name"])
		my_level_at[key] = index[doctype]["level_of"].get(action["workflow_state"])
		# When the reader cleared it. The card says "You approved on ...",
		# and the only record of that is the Workflow Action row this loop
		# is already holding -- re-deriving it later would mean a second
		# pass over the same table.
		my_date_at[key] = action["creation"]

	rows = []
	for doctype, names in grouped.items():
		entry = index[doctype]
		state_field = entry["state_field"]
		scope_entry = scope.for_doctype(doctype)

		fields = ["name", "owner", state_field + " as state", "modified"]
		if scope_entry:
			fields += approver_gate.select_fields(scope_entry)

		try:
			found = frappe.get_list(
				doctype,
				filters={"name": ["in", names]},
				fields=fields,
				limit_page_length=0,
				ignore_ifnull=True,
			)
		except Exception:
			continue

		for doc in found:
			state = doc["state"]
			if not state or state in entry["terminal"]:
				continue  # already finished; nothing is stuck

			if scope_entry:
				# Same correction as Tab 3: for these three, "is it back on my
				# desk" is answered by the name on the request, not by the
				# roles I hold.
				if state not in scope_entry["levels"]:
					continue
				if scope.is_gated(scope_entry):
					if scope.may_act(scope_entry, state, doc, user):
						continue
				elif state in entry["my_states"]:
					continue  # back on my desk by role; Tab 2 owns it
			elif state in entry["my_states"]:
				continue  # back on my desk, so it belongs in Tab 2 instead

			row = {
				"doctype": doctype,
				"name": doc["name"],
				"owner": doc["owner"],
				"state": state,
				"modified": doc["modified"],
				"my_level": my_level_at.get((doctype, doc["name"])),
				"approved_on": my_date_at.get((doctype, doc["name"])),
				"stuck_level": entry["level_of"].get(state),
				"total_levels": entry["total_levels"],
				"waiting_on_roles": _roles_out_of(entry, state),
			}
			if scope_entry:
				for field in approver_gate.select_fields(scope_entry):
					row[field] = doc.get(field)
			rows.append(row)

	rows.sort(key=lambda row: row["modified"])

	# Counted before the display cut, like the other three tabs count theirs.
	stuck_found = len(rows)

	rows = rows[:LIMIT]
	_decorate(rows, index, user)

	return {
		"rows": rows,
		"shown": len(rows),
		# `total` is the key every other tab returns and the only one the page
		# reads -- the KPI tile prints "of N" from `block.total`. This tab
		# returned `total_approvals` and nothing else, so its tile silently
		# never showed the sub-line while the other three did.
		#
		# It means what it means on the other tabs: how many rows were found
		# before the cut to LIMIT. That is not the same number as
		# `total_approvals`, which counts every workflow action this user has
		# ever completed and is a far larger figure -- printing that as "of N"
		# next to a handful of stuck documents would be nonsense.
		"total": stuck_found,
		"total_approvals": total_approvals,
		"truncated": total_approvals > LIMIT,
	}


def _gather(index: dict, states_for, owner: str | None = None, approval_only=False):
	"""Run one permission-aware list query per doctype and merge the results.

	`get_list` is the security boundary here. The state filter and the owner
	filter narrow the work; they are not what keeps another user's documents
	out.

	For a scoped doctype the query carries three extra things, all of them
	columns on a query that was going to run anyway:

	* the approver fields, so the named-approver gate can decide without a
	  per-row lookup -- 200 rows would otherwise be 200 queries;
	* the `row_filters` that separate this request type from its neighbours,
	  which is what keeps 82 Work From Home requests out of the On Duty tabs;
	* `employee` instead of `owner` as the "mine" filter, because HR raises
	  these requests on behalf of other people and the request belongs to the
	  employee named on it, not to whoever typed it.

	`approval_only` drops any state the entry does not call a level -- a draft
	to send, a rejection to resend. It has to be done here rather than in the
	caller's loop, because the loop runs on a window that was already cut at
	LIMIT. Measured on a real Accounts Manager: their oldest 200 candidates
	were 198 drafts and 2 journal entries, so filtering afterwards left a
	two-row queue while 800 genuine approvals sat behind the cut.
	"""
	candidates = []
	truncated = False

	for doctype, entry in index.items():
		states = states_for(entry)
		scope_entry = scope.for_doctype(doctype)

		if approval_only:
			if scope_entry:
				states = [s for s in states if s in scope_entry["levels"]]
			else:
				# The generic half of the same rule, and the thing that makes
				# wide coverage survivable. A start state is where a document
				# is raised, before anybody has been asked for anything, so
				# whoever holds the role that moves it on is not approving --
				# the author has not sent it yet.
				#
				# Without this, widening to all 47 workflows repeats a
				# measured regression exactly: `Draft -> Send For Approval`
				# on Purchase Invoice is granted to `Accounts User`, 42
				# holders, and one of them had 193 rows of other people`s
				# drafts in Tab 2. Purchase Invoice is declared in scope.py
				# and so was fixed by the branch above; the 40 workflows that
				# declare nothing need this one.
				#
				# `.get` rather than `[...]`: a cached index built before
				# start_states existed is still a valid index, and losing the
				# narrowing for up to INDEX_TTL is better than a KeyError
				# that empties the tab.
				starts = set(entry.get("start_states") or [])
				states = [s for s in states if s not in starts]

		if not states:
			continue

		state_field = entry["state_field"]

		fields = ["name", "owner", state_field + " as state", "modified"]
		filters = {state_field: ["in", states]}
		if entry["is_submittable"]:
			filters["docstatus"] = ["<", 2]

		if scope_entry:
			fields += approver_gate.select_fields(scope_entry)
			filters.update(scope_entry["row_filters"])

		if owner:
			mine = approver_gate.requester_filters(scope_entry, owner) if scope_entry else None
			if mine:
				filters.update(mine)
			else:
				# No Employee record, so there is nothing to match on and
				# authorship is the honest answer.
				filters["owner"] = owner

		try:
			found = frappe.get_list(
				doctype,
				filters=filters,
				fields=fields,
				order_by="modified asc",
				limit_page_length=LIMIT,
				ignore_ifnull=True,
			)
		except Exception:
			# A single broken doctype must not empty the tab.
			frappe.log_error(
				title=f"Task Manager: list failed for {doctype}"[:140],
				message=frappe.get_traceback(),
			)
			continue

		if len(found) >= LIMIT:
			truncated = True

		for row in found:
			candidate = dict(row)
			candidate["doctype"] = doctype
			candidates.append(candidate)

	return candidates, truncated


def _self_approval_allowed(entry: dict, state: str, user: str) -> bool:
	allowed_roles = set(entry["self_approval_states"].get(state) or [])
	return bool(allowed_roles & set(frappe.get_roles(user)))


def _needs_condition_check(entry: dict, state: str, user: str) -> bool:
	roles = set(entry["conditional_states"].get(state) or [])
	return bool(roles & set(frappe.get_roles(user)))


def _condition_passes(entry: dict, row: dict, user: str) -> bool:
	"""Evaluate the transition condition against the real document.

	Conditions are allowed to call `frappe.db.get_value` and
	`frappe.db.get_list`, which run as the session user -- the same identity
	this whole request belongs to -- so the evaluation cannot be used to read
	past anybody's permissions.

	The document is loaded as a plain row, not through `frappe.get_doc`, and
	that is the difference between a fast page and a slow one: measured on
	Purchase Order, `get_doc` costs 19.1 ms because it loads every child table,
	while the parent row alone costs 1.4 ms -- 13x. All 348 workflow
	conditions on this site are simple field comparisons, so the child tables
	are pure waste.

	If a condition does need something the row lacks -- a child table, a
	virtual field -- the cheap evaluation raises and the full document is
	loaded for that one check. So the optimisation can only cost time, never
	correctness.
	"""
	roles = set(frappe.get_roles(user))

	candidates = [
		t
		for t in entry["transitions"]
		if t["state"] == row["state"] and t["allowed"] in roles
	]
	if not candidates:
		return False
	if any(not t["condition"] for t in candidates):
		return True  # an unconditional route out is enough; load nothing

	try:
		light = frappe.db.get_value(row["doctype"], row["name"], "*", as_dict=True)
	except Exception:
		light = None
	light = frappe._dict(light) if light else None

	full = None
	for transition in candidates:
		verdict = None

		if light is not None:
			try:
				# Mirrors core's is_transition_condition_satisfied, which is a
				# single safe_eval over doc.as_dict().
				verdict = frappe.safe_eval(
					transition["condition"], get_workflow_safe_globals(), {"doc": light}
				)
			except Exception:
				verdict = None  # fall through to the full document

		if verdict is None:
			if full is None:
				try:
					full = frappe.get_doc(row["doctype"], row["name"])
				except Exception:
					continue
			try:
				verdict = is_transition_condition_satisfied(frappe._dict(transition), full)
			except Exception:
				# A condition naming a field that no longer exists throws even
				# with the whole document. Treat it as unsatisfied rather than
				# offering an action that would fail on the form.
				continue

		if verdict:
			return True

	return False


def _roles_out_of(entry: dict, state: str) -> list[str]:
	"""Who the document is waiting on, as roles worth naming.

	Automatic roles are dropped. "Waiting on All" tells the reader nothing --
	everybody holds All -- and it is how a draft that only its author can move
	would otherwise describe itself.
	"""
	return sorted(
		{
			t["allowed"]
			for t in entry["transitions"]
			if t["state"] == state and t["allowed"] and t["allowed"] not in AUTOMATIC_ROLES
		}
	)


def _decorate(rows: list, index: dict, user: str):
	"""Attach level, pending-since and the trail, in batched queries."""
	if not rows:
		return

	for row in rows:
		entry = index[row["doctype"]]
		row["level"] = {
			"current": entry["level_of"].get(row["state"]),
			"total": entry["total_levels"],
		}
		row.setdefault("my_level", None)

	since = pending_since(rows)
	trails = level_trail(rows, index)
	names = _full_names(rows, trails)

	now = now_datetime()
	for row in rows:
		key = (row["doctype"], row["name"])
		entered = since.get(key) or row["modified"]
		row["pending_since"] = entered
		row["pending_hours"] = round(time_diff_in_hours(now, entered), 1)
		row["owner_name"] = names.get(row.pop("owner", None), "")
		row["trail"] = trails.get(key, [])

	_name_approvers(rows)


def _name_approvers(rows: list):
	"""Replace the scoped approver ids with full names, then remove the ids.

	This is the same privacy gate `_full_names` applies to `owner`: the
	approver fields hold email addresses -- two of the three are free-text
	`Data` -- and they are selected only so the gate can compare them. None of
	them may reach the browser, so every field named in scope.py is popped off
	the row here whether it was used or not.

	It is also what turns "waiting on Head Leave Approver" into "waiting on
	Pushkar Desai". The role name is useless to a requester when 47 people
	hold it; the name is the thing they asked for.
	"""
	if not rows:
		return

	names = approver_gate.approver_names(rows)

	for row in rows:
		scope_entry = scope.for_doctype(row["doctype"])
		if not scope_entry:
			row["waiting_on_names"] = []
			continue

		if not scope.is_gated(scope_entry):
			# Nobody is named on an Accounts document, so the row falls back to
			# `waiting_on_roles` -- which is what its workflow actually says.
			row["waiting_on_names"] = []
			continue

		row["waiting_on_names"] = [
			# A stale approver -- an address that is no longer a User, which 9
			# Gate Pass rows carry -- is reported as missing rather than
			# rendered as a half-address.
			names.get(approver) or _("No longer a user")
			for approver in scope.named_approvers(scope_entry, row["state"], row)
		]

		for field in approver_gate.select_fields(scope_entry):
			row.pop(field, None)


def pending_since(rows: list) -> dict:
	"""When each document entered the state it is in now.

	The Open Workflow Action row for the current state is created at the moment
	the document arrives there, which makes its `creation` the most accurate
	answer available -- no field on the document itself records this.

	Two caveats, both measured. Open rows go stale: 7.5% of sampled rows sit on
	a state the document has already left, so the row's `workflow_state` has to
	match the current one before its timestamp is trusted. And when a document
	returns to a state it has visited before, core skips creating a new row
	entirely, so the timestamp will be the first visit. `modified` is the
	fallback in both cases.
	"""
	grouped = {}
	for row in rows:
		grouped.setdefault(row["doctype"], []).append(row["name"])

	out = {}
	for doctype, names in grouped.items():
		actions = frappe.get_all(
			"Workflow Action",
			filters={
				"reference_doctype": doctype,
				"reference_name": ["in", names],
				"status": "Open",
			},
			fields=["reference_name", "workflow_state", "creation"],
			ignore_permissions=True,
		)
		current = {row["name"]: row["state"] for row in rows if row["doctype"] == doctype}
		for action in actions:
			if action["workflow_state"] != current.get(action["reference_name"]):
				continue
			key = (doctype, action["reference_name"])
			if key not in out or action["creation"] < out[key]:
				out[key] = action["creation"]

	return out


def level_trail(rows: list, index: dict) -> dict:
	"""Who cleared which level, per document, in one query per doctype.

	Roles are deliberately absent from the output. Core marks a transition
	complete by picking the alphabetically first matching permitted role
	(`order by role limit 1`), which is not necessarily the role the person
	actually acted under, so `completed_by_role` is not trustworthy enough to
	show. Names and dates are, and they are already visible on the document's
	own timeline to anybody who can read it.
	"""
	grouped = {}
	for row in rows:
		grouped.setdefault(row["doctype"], []).append(row["name"])

	trails = {}
	for doctype, names in grouped.items():
		entry = index[doctype]
		actions = frappe.get_all(
			"Workflow Action",
			filters={
				"reference_doctype": doctype,
				"reference_name": ["in", names],
				"status": "Completed",
			},
			fields=["reference_name", "workflow_state", "completed_by", "modified"],
			order_by="creation asc",
			ignore_permissions=True,
		)

		done = {}
		for action in actions:
			done.setdefault(action["reference_name"], []).append(action)

		for row in rows:
			if row["doctype"] != doctype:
				continue

			cleared = done.get(row["name"], [])
			steps = []
			for action in cleared:
				steps.append(
					{
						"level": entry["level_of"].get(action["workflow_state"]),
						"state": action["workflow_state"],
						"status": "done",
						"by": action["completed_by"],
						"on": action["modified"],
					}
				)

			steps.append(
				{
					"level": entry["level_of"].get(row["state"]),
					"state": row["state"],
					"status": "waiting",
					"by": None,
					"on": None,
				}
			)

			trails[(doctype, row["name"])] = steps

	return trails


def _full_names(rows: list, trails: dict) -> dict:
	"""One query for every user id about to be shown, resolved to display names.

	This is also the privacy gate: the ids are keys here and are replaced by
	names before anything is returned, so no address reaches the browser.
	"""
	ids = {row.get("owner") for row in rows if row.get("owner")}
	for steps in trails.values():
		ids.update(step["by"] for step in steps if step.get("by"))
	ids.discard(None)

	if not ids:
		return {}

	users = frappe.get_all(
		"User",
		filters={"name": ["in", list(ids)]},
		fields=["name", "full_name"],
		ignore_permissions=True,
	)
	names = {u["name"]: u["full_name"] or u["name"] for u in users}

	for steps in trails.values():
		for step in steps:
			if step.get("by"):
				step["by_name"] = names.get(step["by"], "")
			else:
				step["by_name"] = ""
			step.pop("by", None)

	return names
