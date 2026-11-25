# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""The workflow metadata engine.

Everything in this module is metadata: which workflows are active, what states
they have, which role may move a document out of which state, and what level
each state sits at. None of it is user data, so the whole index is cached once
and shared by every user. The user-specific slice is `for_user`, which is
derived from the index and never cached globally.

Three things this engine has to survive, all of them real on this site:

* Four active workflows point at DocTypes that no longer exist (Payment
  Requisition, Pickup Request, LOP Reversal Request, Submit To Hiring Manager).
  `frappe.get_meta` raises DoesNotExistError on those, so every workflow is
  loaded defensively and a broken one is skipped rather than allowed to take
  the whole page down.

* Workflow depth goes to eleven levels, not the three or four a tracker strip
  is usually drawn for.

* Nine workflows are fully cyclic, so the level walk needs a start state it
  cannot always derive. See levels.py for why, and for the override hooks.
"""

import frappe
from frappe.permissions import AUTOMATIC_ROLES
from frappe.utils import cint

from task_manager import levels, scope

INDEX_KEY = "task_manager:workflow_index"
INDEX_TTL = 1800


def get_workflow_index(force: bool = False) -> dict:
	"""Active workflows keyed by doctype, with level numbers resolved."""
	if not force:
		cached = frappe.cache.get_value(INDEX_KEY)
		if cached:
			return cached

	index = {"workflows": {}, "skipped": {}, "unworkflowed": []}

	workflows = frappe.get_all(
		"Workflow",
		filters={"is_active": 1},
		fields=["name", "document_type", "workflow_state_field", "send_email_alert"],
		ignore_permissions=True,
	)

	for workflow in workflows:
		doctype = workflow.document_type
		try:
			entry = _build(workflow)
		except Exception:
			index["skipped"][doctype] = "error"
			frappe.log_error(
				title=f"Task Manager: could not index workflow {workflow.name}"[:140],
				message=frappe.get_traceback(),
			)
			continue

		if isinstance(entry, str):
			index["skipped"][doctype] = entry
			continue

		index["workflows"][doctype] = entry

	# The other family: submittable doctypes nobody ever drew a workflow for.
	# Stored on the same blob and therefore cleared by the same hook and
	# expired by the same TTL, so the two halves of coverage can never be of
	# different ages. It is metadata like everything else here -- a list of
	# doctype names, no user in it -- which is what makes it shareable.
	index["unworkflowed"], index["unscannable"] = _unworkflowed(index["workflows"])

	frappe.cache.set_value(INDEX_KEY, index, expires_in_sec=INDEX_TTL)
	return index


# A table this big, with no index on `owner`, is not worth a full scan to
# find one user`s drafts.
#
# This is not a guess. Measured on this site: a draft scan across the 49
# workflow-less doctypes one real user can read cost 4.8 seconds, and 3.5 of
# those were a single table -- `Attendance`, 398,494 rows, indexed on
# employee, status, modified and attendance_date but not on owner. Every other
# table in the set came in under 150ms and the median was 4ms. Dropping that
# one table took the scan to roughly 0.3s.
#
# The threshold is set well above anything a person types. 398k attendance
# rows are generated from shifts and checkins; nobody raised them, and a
# "what did I create" card is not where they belong even if the scan were
# free. The next largest table in the set has 19,164 rows and is scanned.
#
# An index on `owner` lifts the limit entirely, which is why it is checked:
# 45 tables on this site already have one and are never excluded by size.
# Adding one to a table this list names would be the proper fix for that
# doctype, and is a schema change nobody should make as a side effect of a
# dashboard.
#
# What is excluded is reported, never silently dropped -- see `unscannable`
# below and the `truncated` flag it raises in services/drafts.py.
BIG_TABLE_ROWS = 100_000


def _unworkflowed(workflows: dict) -> tuple[list[str], list[str]]:
	"""Submittable doctypes with no active workflow, sorted.

	`is_submittable` is the whole test. It is the site saying this document
	has a draft position and a final one, which is the only thing
	services/drafts.py needs to be true -- it reports drafts and nothing else.

	Three exclusions, all of them for things that cannot hold a draft a user
	raised: single doctypes have one row that belongs to nobody, child tables
	are rows inside another document, and a doctype an active workflow already
	owns is the workflow engine`s to report. Letting that last one through
	would put the same document in the card twice, once from each side.

	Sorted by name so DOCTYPE_BUDGET cuts the same tail every time. A budget
	that drops a different arbitrary doctype on each build would make the
	card flicker between loads for no reason the reader could see.

	Returns two lists: the ones worth scanning, and the ones held back by
	BIG_TABLE_ROWS. The second is returned rather than discarded so the card
	can say it did not look everywhere.
	"""
	found = frappe.get_all(
		"DocType",
		filters={"is_submittable": 1, "issingle": 0, "istable": 0},
		pluck="name",
		ignore_permissions=True,
	)
	candidates = sorted(dt for dt in found if dt not in workflows)

	rows, indexed = _table_stats()
	if rows is None:
		# No stats available -- a non-MariaDB backend, or a user without
		# access to information_schema. Scanning everything is slow but
		# correct, and this function must not be the thing that empties the
		# card on a backend it was not measured against.
		return candidates, []

	scannable = []
	held_back = []
	for doctype in candidates:
		table = "tab" + doctype
		if rows.get(table, 0) > BIG_TABLE_ROWS and table not in indexed:
			held_back.append(doctype)
		else:
			scannable.append(doctype)

	return scannable, held_back


def _table_stats() -> tuple[dict | None, set]:
	"""Approximate row counts and which tables index `owner`, in two queries.

	Both come from information_schema, so this is metadata about the schema
	rather than about anybody`s documents -- which is what lets it live on the
	shared index with everything else here.

	TABLE_ROWS is an InnoDB estimate and can be off by a wide margin. That is
	fine for the only question asked of it: is this table nearer 400,000 rows
	or nearer 4,000. Nothing downstream treats it as a count.
	"""
	try:
		rows = {
			r[0]: (r[1] or 0)
			for r in frappe.db.sql(
				"""
				select TABLE_NAME, TABLE_ROWS
				from information_schema.TABLES
				where TABLE_SCHEMA = database()
				"""
			)
		}
		indexed = {
			r[0]
			for r in frappe.db.sql(
				"""
				select distinct TABLE_NAME
				from information_schema.STATISTICS
				where TABLE_SCHEMA = database() and COLUMN_NAME = %s
				""",
				("owner",),
			)
		}
	except Exception:
		return None, set()

	return rows, indexed


def clear_index(doc=None, method=None):
	"""Hooked onto Workflow changes, so an edited workflow is picked up at once."""
	frappe.cache.delete_value(INDEX_KEY)


def _build(workflow) -> dict | str:
	doctype = workflow.document_type

	# A workflow whose doctype was deleted is still returned as active. Asking
	# frappe for its meta raises, so the existence check has to come first.
	if not frappe.db.exists("DocType", doctype):
		return "doctype missing"

	state_field = workflow.workflow_state_field
	if not state_field:
		return "no state field"

	meta = frappe.get_meta(doctype)
	if not meta.has_field(state_field):
		return "state field not on doctype"

	states = frappe.get_all(
		"Workflow Document State",
		filters={"parent": workflow.name},
		fields=["state", "doc_status", "is_optional_state"],
		order_by="idx",
		ignore_permissions=True,
	)
	transitions = frappe.get_all(
		"Workflow Transition",
		filters={"parent": workflow.name},
		fields=["state", "action", "next_state", "allowed", "condition", "allow_self_approval"],
		order_by="idx",
		ignore_permissions=True,
	)

	if not states or not transitions:
		return "no states or transitions"

	outgoing = {}
	for transition in transitions:
		outgoing.setdefault(transition.state, []).append(transition)

	# Three sources of forced-terminal states: levels.py for generic graph
	# corrections, scope.py for the request types that declare their own, and
	# the derived rule below for everything else. They are unioned rather than
	# one overriding the other -- each knows about states the others do not.
	scope_entry = scope.for_doctype(doctype)
	forced_terminal = (
		levels.extra_terminal(workflow.name)
		| scope.extra_terminal(doctype)
		| _finished_but_cancellable(states, outgoing)
	)
	terminal = {
		s.state for s in states if s.state not in outgoing or s.state in forced_terminal
	}

	# De-duplicated, order preserved. The States table has no uniqueness
	# constraint and `PO` declares seven rows for four distinct states, so a
	# plain comprehension hands `non_terminal` the same state three times --
	# which then becomes a repeated value inside a SQL `IN`, a double-counted
	# state in `total_levels`, and three identical entries in anything that
	# renders the list. Harmless in some places and wrong in others; cheaper
	# to fix once here.
	seen_states = []
	for state in states:
		if state.state not in seen_states:
			seen_states.append(state.state)

	non_terminal = [s for s in seen_states if s not in terminal]

	starts = _starts(workflow.name, states, transitions)
	level_of = _levels(starts, states, outgoing, terminal, workflow.name)

	# A scoped doctype's levels are declared, not derived. See
	# scope.apply_levels for the concrete wrong answer the walk gives on
	# Leave Application.
	if scope_entry:
		level_of = scope.apply_levels(scope_entry, level_of)
		total_levels = scope_entry["total_levels"]
	else:
		total_levels = max([lvl for lvl in level_of.values() if lvl] or [0])

	# Which roles can move a document out of which state, and whether doing so
	# needs a per-document condition evaluated. The condition flag is what lets
	# the approvals service load full documents only where it must.
	#
	# Transitions granted to an automatic role are deliberately left out of
	# states_by_role. `All`, `Desk User`, `Guest` and `Administrator` are held
	# by everyone, so a transition allowed to one of them confers no authority
	# on anybody -- it is "submit my own document", not "approve someone
	# else's". Counting them produced the worst wrong answer this page has
	# given: shivam.singh, who holds nothing but All, Desk User, Employee and
	# three app roles, was shown 127 other people's Purchase Receipt drafts as
	# approvals waiting on him, because that workflow grants
	# Draft -> Pending Approval From Store HOD to `All`.
	#
	# This is not rare here: 55 of the 306 transitions on active workflows are
	# granted to an automatic role, nearly all of them the Draft step.
	#
	# They stay in `transitions`, because the document's own author still needs
	# them -- the condition check and the waiting-on display both read that
	# list.
	states_by_role = {}
	conditional = {}
	self_approval = {}
	for transition in transitions:
		if transition.state in terminal:
			continue
		if transition.allowed and transition.allowed not in AUTOMATIC_ROLES:
			states_by_role.setdefault(transition.allowed, set()).add(transition.state)
		if transition.condition:
			conditional.setdefault(transition.state, set()).add(transition.allowed)
		if transition.allow_self_approval:
			self_approval.setdefault(transition.state, set()).add(transition.allowed)

	return {
		"workflow": workflow.name,
		"doctype": doctype,
		"module": meta.module,
		"state_field": state_field,
		"is_submittable": bool(meta.is_submittable),
		"states": seen_states,
		# Where the graph begins -- the raiser`s own position, before anybody
		# has been asked for anything. Carried out of the build because Tab 2
		# has to be able to tell a draft from an approval step on the 40
		# workflows that declare nothing in scope.py. See `requester_states`
		# in approvals._gather for what goes wrong without it.
		"start_states": sorted(starts),
		"terminal": sorted(terminal),
		"non_terminal": non_terminal,
		"level_of": level_of,
		"total_levels": total_levels,
		"in_scope": bool(scope_entry),
		"states_by_role": {role: sorted(v) for role, v in states_by_role.items()},
		"conditional_states": {state: sorted(v) for state, v in conditional.items()},
		"self_approval_states": {state: sorted(v) for state, v in self_approval.items()},
		"transitions": transitions,
	}


def _finished_but_cancellable(states, outgoing) -> set[str]:
	"""Approved states whose only remaining move is to cancel the document.

	The graph rule the rest of this module runs on is "a state with an
	outgoing transition is non-terminal". It is right almost everywhere and
	badly wrong here. A document that has been approved is finished. That its
	workflow still offers Cancel does not make it pending -- cancelling a
	finished document is an administrative act, not a level of approval, and
	nobody is waiting to perform it.

	scope.py has corrected this by hand for four states since the first
	release, and the comment there records what it costs to miss: 10,791
	finished Attendance Requests parked in their requesters` tabs forever and
	offered to HR Head as work. That hand correction only ever covered the
	doctypes somebody had declared. Once coverage widened to every workflow on
	the site the same trap reopened everywhere else, measured at **19,374 open
	documents across 11 workflows**, of which 18,713 are Purchase Receipts
	sitting in `Approved By Store Head`.

	The test is read off the workflow`s own declarations, in two parts:

	  the state is doc_status 1    the site calls this document submitted and
	                               done, not a draft and not in progress
	  every route out is to a      the only thing anyone can still do is undo
	  doc_status 2 state           it

	Keyed on doc_status, never on the action name. `Payment Request` names its
	one route out of `Approved By Account Manager` **Reject**, not Cancel, and
	a rule that matched on the word would have missed it while matching four
	others that happen to be spelled the right way. Checked against the four
	states scope.py declares by hand: this rule reproduces all four, which is
	the evidence that it is the same rule rather than a near-miss.

	Deliberately narrow in two ways. A doc_status 0 state is never caught,
	even when cancelling is all that is left -- an unsent draft belongs to its
	author and Tab 3 is right to show it. And a state with any route to a
	doc_status 0 or 1 state is never caught, however the route is named,
	because something other than undoing is still possible from there.

	The hand declarations in scope.py are left exactly as they are. They are
	now redundant with this rule, and keeping them costs a set union and says
	out loud which states somebody actually checked.
	"""
	doc_status = _doc_status_of(states)
	cancelling = _cancelling_states(states)

	finished = set()
	for state, routes in outgoing.items():
		if doc_status.get(state) != 1:
			continue
		# A blank next_state is not a cancelling state, so a half-configured
		# transition keeps its source non-terminal. Leaving a document
		# visible is the safe direction to be wrong in.
		if routes and all(route.next_state in cancelling for route in routes):
			finished.add(state)

	return finished


def _doc_status_of(states) -> dict:
	"""doc_status per state, resolving duplicates the way core resolves them.

	The States table is a child table with no uniqueness constraint, and
	workflows on this site really do declare the same state more than once
	with *different* doc_status values. `PO` declares
	`Pending Approval Of Purchase Head` three times as 0, 0 and 1, and
	`Cancelled By Purchase Head` twice as 2 and 0.

	The obvious `{s.state: cint(s.doc_status) for s in states}` is a trap:
	a dict comprehension keeps the LAST row, so `Cancelled By Purchase Head`
	resolved to 0 and the cancel-only rule stopped recognising it as a
	cancellation -- which left all 15,506 approved Purchase Orders
	non-terminal and in somebody`s queue.

	First row wins here, because that is what core does. `apply_workflow`
	picks the state it is moving into with
	`next(d for d in workflow.states if d.state == transition.next_state)`
	and takes `doc_status` off that row, so the first declaration is the one
	that actually governs the document. Reading it any other way would make
	this page disagree with the form.
	"""
	resolved = {}
	for state in states:
		if state.state not in resolved:
			resolved[state.state] = cint(state.doc_status)
	return resolved


def _cancelling_states(states) -> set:
	"""States that cancel the document -- ANY declaration saying so is enough.

	Deliberately a different rule from `_doc_status_of`, and it is core that
	makes them different. `send_email_alert`/`apply_workflow` resolve a state
	by its first row, but the cancellation test is

	    cancelling_states = [s.state for s in workflow.states if s.doc_status == "2"]

	-- any row, not the first. So `Cancelled By Purchase Head`, declared 2
	and then 0, is a cancelling state to core even though its first row says
	0, and this page has to agree or it will offer a Cancel route as though
	it led somewhere a document could still be worked on.
	"""
	return {state.state for state in states if cint(state.doc_status) == 2}


def _starts(workflow_name, states, transitions) -> list[str]:
	"""Where the walk begins: the state or states a document is raised into.

	Split out of `_levels` because two callers now need it. The level walk
	needs somewhere to start from, and `_build` needs to record the answer --
	a start state is by definition the position the document is in before
	anybody has been asked to approve anything, which is exactly the
	distinction Tab 2 lacks on an undeclared workflow.
	"""
	incoming = {t.next_state for t in transitions if t.next_state}
	all_states = [s.state for s in states]

	override_start = levels.start_state(workflow_name)
	if override_start in all_states:
		return [override_start]

	# doc_status is a Select field, so it arrives as the *string* "0".
	# Comparing it to the integer 0 silently matched nothing, which left
	# this list empty for all 43 workflows and quietly fell through to
	# "the first row of the States table" -- the very heuristic this
	# function exists to avoid. cint is what makes the rule work.
	draftish = [s.state for s in states if cint(s.doc_status) == 0]
	roots = [s for s in draftish if s not in incoming]
	# Seed from *every* root, not just the first. Several workflows here
	# have more than one entry point -- Item Creation Form is reachable at
	# both "Forwarded to the item creation" and "Pending Item Creation" --
	# and walking from one of them leaves the other half of the graph
	# unnumbered. Fully cyclic workflows have no root at all, which is why
	# the chain still ends at the draft states, then simply the first one.
	return roots or draftish or all_states[:1]


def _levels(starts, states, outgoing, terminal, workflow_name=None) -> dict:
	"""Level number per state: shortest forward distance from the start, 1-based.

	Terminal states get None -- Approved is not a level anybody waits at. A
	non-terminal state the walk cannot reach also gets None rather than a
	fabricated number, and the tracker renders that honestly.
	"""
	all_states = [s.state for s in states]
	override = levels.LEVEL_OVERRIDES.get(workflow_name) or {}

	level_of = {s: None for s in all_states}
	frontier = list(starts)
	for state in starts:
		level_of[state] = 1
	seen = set(starts)

	depth = 1
	while frontier:
		depth += 1
		nxt = []
		for state in frontier:
			for transition in outgoing.get(state, []):
				target = transition.next_state
				if not target or target in seen or target not in level_of:
					continue
				seen.add(target)
				level_of[target] = depth
				nxt.append(target)
		frontier = nxt

	for state in terminal:
		level_of[state] = None

	for state, lvl in override.items():
		if state in level_of:
			level_of[state] = lvl

	return level_of


# Whether a module hidden in the user's profile should also hide their work.
#
# It should not, and the default is False, because the two mean different
# things. Read permission is a *permission*: it decides what a user is allowed
# to see. A blocked module is a *navigation preference*: it decides what
# clutters their sidebar. Frappe only ever applies blocked modules to the Desk
# sidebar and config pages -- never to get_list, never to has_permission.
#
# Treating the preference as a permission produced a concrete wrong answer on
# this site: shivam.singh raised leave application HR-LAP-2026-01117, can read
# it, and is waiting on it -- but `HR` is blocked in his profile, so the whole
# doctype was dropped and his own request vanished from the page. Three of 43
# doctypes survived the filter for him, and all four tabs read zero.
#
# Set this True only if the intent really is "a blocked module hides my own
# documents too". Read permission below is unaffected either way: it is the
# security boundary and is enforced again by get_list on every document.
RESPECT_BLOCKED_MODULES = False


def for_user(user: str, index: dict | None = None, respect_blocked_modules: bool | None = None) -> dict:
	"""The slice of the index this user may actually see.

	Read permission is the filter that matters, and it is the security
	boundary: `frappe.get_list` enforces it again on every document fetched
	later, so the check here only avoids pointless queries.

	Blocked modules are consulted only when RESPECT_BLOCKED_MODULES says so --
	see the note above for why that defaults to off.
	"""
	index = index or get_workflow_index()
	roles = set(frappe.get_roles(user))

	if respect_blocked_modules is None:
		respect_blocked_modules = RESPECT_BLOCKED_MODULES
	blocked = blocked_modules(user) if respect_blocked_modules else set()

	out = {}
	for doctype, entry in index["workflows"].items():
		# The scope cut, and it belongs here rather than in the index build:
		# the index stays a complete picture of all 47 active workflows, and
		# this is "which of them does this release show". Flipping the flag
		# either way needs no cache rebuild.
		#
		# Asked through the predicate rather than read off the constant,
		# because two flags decide it now -- see scope.restrict_to_scope.
		if scope.restrict_to_scope() and not scope.in_scope(doctype):
			continue
		if blocked and entry["module"] in blocked:
			continue
		try:
			if not frappe.has_permission(doctype, "read", user=user):
				continue
		except Exception:
			continue

		my_states = set()
		for role in roles:
			my_states.update(entry["states_by_role"].get(role, []))

		pending = set(entry["non_terminal"])
		out[doctype] = {
			**entry,
			"my_states": sorted(my_states),
			# States where the document is pending, but not on this user -- the
			# basis of "raised by me, waiting on somebody else".
			"others_states": sorted(pending - my_states),
		}

	return out


def draft_doctypes_for_user(user: str, index: dict | None = None) -> list[str]:
	"""Workflow-less submittable doctypes this user may read.

	The permission check here is the same cheap pre-filter `for_user` runs,
	and it carries the same caveat: it exists to avoid pointless queries, not
	to keep anybody out. `get_list` in services/drafts.py is the boundary and
	runs again on every row.

	Blocked modules are not consulted at all. The reasoning in
	RESPECT_BLOCKED_MODULES applies with more force here, not less: these are
	documents the reader created themselves, and a sidebar preference hiding
	a user`s own unsent drafts from them would be the same bug that lost
	shivam.singh his own leave application, with a worse excuse.

	Doctypes held back by BIG_TABLE_ROWS are already absent from
	`index["unworkflowed"]`; `held_back_for_user` reports them separately so
	the card can admit it did not look everywhere.

	Returns [] when coverage is narrow, so the caller needs no second check.
	"""
	if not scope.COVER_ALL_SUBMITTABLE:
		return []

	index = index or get_workflow_index()

	out = []
	for doctype in index.get("unworkflowed") or []:
		try:
			if not frappe.has_permission(doctype, "read", user=user):
				continue
		except Exception:
			continue
		out.append(doctype)

	return out


def held_back_for_user(user: str, index: dict | None = None) -> list[str]:
	"""Readable doctypes the draft scan skipped because the table is too big.

	Same permission pre-filter as `draft_doctypes_for_user`, because a
	doctype the reader cannot see was not held back from them -- it was never
	theirs to be shown, and reporting it as a gap would be a lie in the other
	direction.
	"""
	if not scope.COVER_ALL_SUBMITTABLE:
		return []

	index = index or get_workflow_index()

	out = []
	for doctype in index.get("unscannable") or []:
		try:
			if not frappe.has_permission(doctype, "read", user=user):
				continue
		except Exception:
			continue
		out.append(doctype)

	return out


def covered_doctypes(index: dict | None = None) -> list[str]:
	"""Every doctype this page covers, both families, for Tab 1 to filter on.

	ToDo is the one list that is not workflow-driven, so it cannot derive its
	own scope -- it has to be handed one. Handing it this means a ToDo is
	shown when it points at something the rest of the page would also show,
	which is the only definition that cannot drift.

	Narrow coverage returns the declared seven, which is what Tab 1 filtered
	on before any of this existed.
	"""
	if not scope.COVER_ALL_SUBMITTABLE:
		return scope.doctypes()

	index = index or get_workflow_index()
	return sorted(
		set(index["workflows"])
		| set(index.get("unworkflowed") or [])
		# Held back from the *draft scan* for cost, not from coverage. A ToDo
		# pointing at one is a row somebody already found and assigned, so
		# showing it costs one indexed lookup, not a table scan.
		| set(index.get("unscannable") or [])
	)


def blocked_modules(user: str) -> set[str]:
	"""Modules hidden for this user, plus the ones hidden for everybody.

	Administrator's own block list is how Frappe models a global hide, so it
	counts for every user -- that is the same rule the Desk sidebar follows.
	"""
	hidden = set()
	for name in ("Administrator", user):
		try:
			hidden.update(frappe.get_cached_doc("User", name).get_blocked_modules())
		except Exception:
			continue
	return hidden
