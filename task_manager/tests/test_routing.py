# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Routing tests for the three HRMS request types.

Two kinds of test live here, and the first kind matters more than it looks.

**Conformance.** Everything in scope.py is a hardcoded string: doctype names,
fieldnames, state names, workflow names. Every one of them is a silent failure
if it drifts -- a filter on a fieldname that no longer exists returns an empty
list and raises nothing, and `Gate Pass.emoloyee` shows how easily the
plausible spelling is the wrong one. `TestScopeConformance` asserts each
string against the live schema so a rename breaks a test instead of quietly
emptying a tab.

**Gate behaviour.** The rest proves the thing the gate was built for: that a
role holder who is not named on a request does not see it as theirs. These run
against the documents already pending on the site rather than seeding their
own, for the same reason test_isolation.py does -- the holes being closed exist
because of real role grants on real workflows, which a fixture would not
reproduce. Nothing here creates, modifies or deletes a document.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from task_manager import scope
from task_manager.services import (
	approvals,
	approver_gate,
	cache,
	drafts,
	todos,
	workflow_index,
)


def _pending(doctype: str, limit: int = 1) -> list[dict]:
	"""Requests sitting at an approval state right now, with their approvers."""
	entry = scope.for_doctype(doctype)
	states = list(entry["levels"])

	filters = {"workflow_state": ["in", states], "docstatus": ["<", 2]}
	filters.update(entry["row_filters"])

	return frappe.get_all(
		doctype,
		filters=filters,
		fields=["name", "owner", "workflow_state as state"] + approver_gate.select_fields(entry),
		order_by="modified desc",
		limit=limit,
		ignore_permissions=True,
	)


def _pending_an_approver_can_see(doctype: str) -> tuple[dict, str] | tuple[None, None]:
	"""A pending request plus a named approver who can actually list it.

	The second half is a precondition, not a detail. `User Permission` rows
	scoped to these doctypes pin ~290 users to their own employee record, so
	a named approver frequently cannot see the request at all until it is
	shared with them -- 127 of 140 pending Gate Passes are in that state. Tab
	2 is built on `get_list` and does not bypass it for anybody, so a request
	its approver cannot list is one Task Manager cannot show, and asserting
	otherwise would be testing the permission system rather than the gate.

	`approver_gate.unshared` reports that population; this picks a request
	outside it.
	"""
	entry = scope.for_doctype(doctype)
	original = frappe.session.user

	for row in _pending(doctype, limit=40):
		for approver in scope.named_approvers(entry, row["state"], row):
			if not frappe.db.get_value("User", approver, "enabled"):
				continue
			if frappe.db.get_value("User", approver, "user_type") != "System User":
				continue
			try:
				frappe.set_user(approver)
				seen = frappe.get_list(
					doctype, filters={"name": row["name"]}, fields=["name"], ignore_ifnull=True
				)
			except Exception:
				seen = None
			finally:
				frappe.set_user(original)
			if seen:
				return row, approver

	return None, None


def _pending_needing_share(doctype: str) -> tuple[str, str] | tuple[None, None]:
	"""A pending request whose named approver cannot submit it yet.

	Sharing is skipped when the approver can already act -- most can, and a
	share for them would be a DocShare row that grants nothing. So a test of
	the sharing path has to find a request that actually needs it, or it
	asserts against a deliberate no-op.
	"""
	entry = scope.for_doctype(doctype)

	for row in _pending(doctype, limit=60):
		approvers = [
			a
			for a in scope.named_approvers(entry, row["state"], row)
			if frappe.db.get_value("User", a, "enabled")
		]
		if not approvers:
			continue
		doc = frappe.get_doc(doctype, row["name"])
		for approver in approvers:
			if approver == (row["owner"] or "").lower():
				continue
			if not frappe.has_permission(doc=doc, ptype="submit", user=approver):
				return row["name"], approver

	return None, None


class TestScopeConformance(FrappeTestCase):
	"""Every string in scope.py, checked against the live site."""

	def test_doctypes_exist_and_are_submittable(self):
		for doctype in scope.doctypes():
			self.assertTrue(frappe.db.exists("DocType", doctype), f"{doctype} is missing")

	def test_employee_and_approver_fields_exist(self):
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			meta = frappe.get_meta(doctype)

			if entry["employee_field"]:
				# The one most likely to rot: `emoloyee` on Gate Pass is a typo
				# in the schema, so a well-meaning fix upstream breaks routing
				# here.
				self.assertTrue(
					meta.has_field(entry["employee_field"]),
					f"{doctype}.{entry['employee_field']} does not exist",
				)
			for field in scope.all_approver_fields(entry):
				self.assertTrue(
					meta.has_field(field), f"{doctype}.{field} does not exist"
				)

	def test_the_two_families_stay_coherent(self):
		"""`gated` is not decoration -- every branch in the app reads it.

		A gated entry must name somebody, because the gate is the only thing
		standing between one person's leave and 47 role holders. An ungated
		entry must name nobody, because if it did, the role fallback the
		Accounts tabs run on would be quietly bypassing a named approver.
		"""
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			fields = scope.all_approver_fields(entry)

			if scope.is_gated(entry):
				self.assertTrue(
					fields,
					f"{doctype} is gated but names no approver field",
				)
				self.assertTrue(
					entry["employee_field"],
					f"{doctype} is gated but declares no employee field",
				)
			else:
				self.assertFalse(
					fields,
					f"{doctype} is ungated but names approver fields {fields}",
				)
				self.assertIsNone(
					entry["employee_field"],
					f"{doctype} is ungated, so its requester must be its owner",
				)

	def test_finished_states_are_forced_terminal(self):
		"""The trap that cost Attendance Request 10,791 rows.

		The graph rule is "a state with an outgoing transition is non-terminal".
		Three of the Accounts workflows carry an administrative Cancel or Reject
		out of their finished state, so without `extra_terminal` every approved
		document is parked in its raiser's Tab 3 forever. This asserts the
		forcing is still in place, per doctype, against the live workflow.
		"""
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			states = {
				t.state
				for t in frappe.get_all(
					"Workflow Transition",
					filters={"parent": entry["workflow"]},
					fields=["state"],
				)
			}
			approval_states = set(entry["levels"])
			requester_states = set(entry["requester_states"])
			forced = set(entry["extra_terminal"])

			# Anything with a route out that is neither an approval step nor the
			# raiser's own is administrative, and has to be forced terminal.
			stranded = states - approval_states - requester_states - forced
			self.assertFalse(
				stranded,
				f"{doctype}: {sorted(stranded)} has a route out but is neither an "
				f"approval step, a requester state, nor forced terminal",
			)

	def test_row_filter_fields_exist(self):
		for doctype in scope.doctypes():
			meta = frappe.get_meta(doctype)
			for field in scope.for_doctype(doctype)["row_filters"]:
				self.assertTrue(meta.has_field(field), f"{doctype}.{field} does not exist")

	def test_workflow_is_the_active_one(self):
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			active = frappe.db.get_value(
				"Workflow", {"document_type": doctype, "is_active": 1}, "name"
			)
			self.assertEqual(
				active,
				entry["workflow"],
				f"{doctype}: scope.py names {entry['workflow']}, the site runs {active}",
			)

	def test_every_declared_state_exists_on_its_workflow(self):
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			real = set(
				frappe.get_all(
					"Workflow Document State",
					filters={"parent": entry["workflow"]},
					pluck="state",
				)
			)
			declared = set(entry["levels"]) | set(entry["requester_states"]) | set(
				entry["extra_terminal"]
			)
			self.assertFalse(
				declared - real,
				f"{doctype}: states not on the workflow: {sorted(declared - real)}",
			)

	def test_levels_are_numbered_within_total(self):
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			for state, step in entry["levels"].items():
				self.assertGreaterEqual(step["level"], 1, f"{doctype}/{state}")
				self.assertLessEqual(
					step["level"], entry["total_levels"], f"{doctype}/{state}"
				)

	def test_a_gated_level_names_somebody(self):
		"""Only gated entries must name an approver.

		This used to be an unconditional assertion inside the test above and
		had been failing since the four Accounts doctypes were declared:
		`Journal Entry/Pending by Accounts Manager names nobody` is not a
		defect, it is the entire reason `gated` exists. Not one of those four
		carries an approver field -- every field on all four was scanned for
		`approv`, `authoris` and `approved_by` -- and a shared role queue is
		the designed model for them.

		So the assertion is kept, and narrowed to the entries it is actually
		true of. For a gated entry an empty `approver_fields` is still a real
		defect: `may_act` would return False for everybody and the document
		would reach no tab at all.
		"""
		for doctype in scope.doctypes():
			entry = scope.for_doctype(doctype)
			if not scope.is_gated(entry):
				continue
			for state, step in entry["levels"].items():
				self.assertTrue(
					step["approver_fields"], f"{doctype}/{state} names nobody"
				)


class TestScopeHelpers(FrappeTestCase):
	"""Pure functions, no database."""

	def setUp(self):
		self.leave = scope.for_doctype("Leave Application")
		self.od = scope.for_doctype("Attendance Request")

	def test_may_act_matches_the_named_approver(self):
		row = {"leave_approver": "boss@example.com"}
		self.assertTrue(
			scope.may_act(self.leave, "Pending Leave Approval", row, "boss@example.com")
		)
		self.assertFalse(
			scope.may_act(self.leave, "Pending Leave Approval", row, "someone@example.com")
		)

	def test_may_act_is_case_and_space_insensitive(self):
		"""Two of the three approver fields are free-text Data, not Link User."""
		row = {"approver": "  Boss@Example.COM "}
		gate_pass = scope.for_doctype("Gate Pass")
		self.assertTrue(
			scope.may_act(gate_pass, "Pending For Approval", row, "boss@example.com")
		)

	def test_a_blank_approver_lets_nobody_through(self):
		for value in ("", None, "   "):
			row = {"leave_approver": value}
			self.assertFalse(
				scope.may_act(self.leave, "Pending Leave Approval", row, "boss@example.com"),
				f"blank approver {value!r} must not match",
			)

	def test_either_approver_may_clear_on_duty(self):
		"""On Duty has one state and two acceptable approvers, not a sequence."""
		row = {
			"custom_approver": "manager@example.com",
			"custom_head_leave_approver": "hod@example.com",
		}
		state = "Pending Approval Of Department HOD"
		self.assertTrue(scope.may_act(self.od, state, row, "manager@example.com"))
		self.assertTrue(scope.may_act(self.od, state, row, "hod@example.com"))
		self.assertFalse(scope.may_act(self.od, state, row, "other@example.com"))

	def test_a_non_approval_state_is_nobodys_to_approve(self):
		row = {"leave_approver": "boss@example.com"}
		self.assertFalse(scope.may_act(self.leave, "Draft", row, "boss@example.com"))
		self.assertEqual(scope.level_of(self.leave, "Draft"), None)

	def test_declared_levels_replace_the_graph_walk(self):
		"""The walk makes Draft level 1 and Pending Leave Approval level 2."""
		walked = {
			"Draft": 1,
			"Pending Leave Approval": 2,
			"Pending by Head Leave Approver": 3,
			"Approved by Leave Approver": None,
		}
		applied = scope.apply_levels(self.leave, walked)
		self.assertEqual(applied["Draft"], None)
		self.assertEqual(applied["Pending Leave Approval"], 1)
		self.assertEqual(applied["Pending by Head Leave Approver"], 2)
		self.assertEqual(applied["Approved by Leave Approver"], None)


class TestIndexHonoursScope(FrappeTestCase):
	def setUp(self):
		self.index = workflow_index.get_workflow_index(force=True)["workflows"]

	def test_scoped_doctypes_are_indexed_and_flagged(self):
		for doctype in scope.doctypes():
			self.assertIn(doctype, self.index, f"{doctype} is not in the workflow index")
			self.assertTrue(self.index[doctype]["in_scope"])

	def test_total_levels_come_from_scope(self):
		for doctype in scope.doctypes():
			self.assertEqual(
				self.index[doctype]["total_levels"],
				scope.for_doctype(doctype)["total_levels"],
			)

	def test_finished_on_duty_requests_are_terminal(self):
		"""10,791 approved On Duty requests have an outgoing Cancel transition.

		Without the override the graph calls them pending, which parks every
		one of them in its requester's Tab 3 forever.
		"""
		entry = self.index["Attendance Request"]
		for state in ("Approved By Department HOD", "Approved By HR Head"):
			self.assertIn(state, entry["terminal"], f"{state} should be terminal")
			self.assertNotIn(state, entry["non_terminal"])

	def test_generic_workflows_keep_their_derived_levels(self):
		"""The scope must not leak into the other 44 active workflows."""
		generic = [dt for dt in self.index if not self.index[dt]["in_scope"]]
		self.assertTrue(generic, "expected unscoped workflows on this site")
		for doctype in generic:
			entry = self.index[doctype]
			derived = max([lvl for lvl in entry["level_of"].values() if lvl] or [0])
			self.assertEqual(entry["total_levels"], derived, doctype)


class TestScopeRestriction(FrappeTestCase):
	"""Only the three request types reach the approval tabs.

	The site runs 47 active workflows. Purchase Order, Expense Claim, Material
	Request and Purchase Receipt all have one, and before the cut they shared
	Tabs 2-4 with the HRMS requests -- and crowded them out, because Tab 2
	merges every doctype, sorts oldest first and stops at 200.
	"""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_site_really_does_have_other_active_workflows(self):
		"""Guards the test below from passing because there is nothing to cut."""
		others = frappe.get_all(
			"Workflow",
			filters={"is_active": 1, "document_type": ["not in", scope.doctypes()]},
			pluck="document_type",
		)
		self.assertTrue(others, "expected non-scope active workflows on this site")
		for expected in ("Purchase Order", "Expense Claim"):
			self.assertIn(expected, others)

	def test_for_user_returns_only_scoped_doctypes(self):
		if not scope.restrict_to_scope():
			self.skipTest("scope restriction deliberately switched off")

		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=5,
		)
		self.assertTrue(users, "needs at least one enabled system user")

		for user in users:
			index = workflow_index.for_user(user)
			with self.subTest(user=user):
				self.assertFalse(
					set(index) - set(scope.doctypes()),
					f"{user} sees out-of-scope doctypes: {sorted(set(index) - set(scope.doctypes()))}",
				)

	def test_administrator_is_scoped_too(self):
		"""The cut is not a permission filter, so the superuser gets it as well."""
		if not scope.restrict_to_scope():
			self.skipTest("scope restriction deliberately switched off")

		index = workflow_index.for_user("Administrator")
		self.assertFalse(set(index) - set(scope.doctypes()))

	def test_the_full_index_is_still_complete(self):
		"""The cut is a view, not a lobotomy -- the engine still sees all 47."""
		full = workflow_index.get_workflow_index(force=True)["workflows"]
		self.assertGreater(len(full), len(scope.doctypes()))
		self.assertIn("Purchase Order", full)

	def test_no_approval_tab_row_is_out_of_scope(self):
		if not scope.restrict_to_scope():
			self.skipTest("scope restriction deliberately switched off")

		user = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=1,
		)
		if not user:
			self.skipTest("needs an enabled system user")

		cache.clear(user[0])
		frappe.set_user(user[0])
		frappe.local.cache = {}
		index = workflow_index.for_user(user[0])

		for name, tab in (
			("needs_action", approvals.needs_my_action(user[0], index)),
			("awaiting_others", approvals.awaiting_others(user[0], index)),
			("approved_by_me", approvals.approved_by_me(user[0], index)),
		):
			seen = {row["doctype"] for row in tab["rows"]}
			with self.subTest(tab=name):
				self.assertFalse(seen - set(scope.doctypes()), f"{name} leaked {seen}")


class TestWideCoverage(FrappeTestCase):
	"""COVER_ALL_SUBMITTABLE: what widening coverage may and may not do.

	The inverse of TestScopeRestriction above -- those tests assert the cut is
	applied, these assert that lifting it did not take the floor out with it.
	Each one skips when coverage is narrow, so flipping the flag back moves
	the coverage of this file rather than breaking it.

	Nothing here creates, modifies or deletes a document.
	"""

	def setUp(self):
		if not scope.COVER_ALL_SUBMITTABLE:
			self.skipTest("wide coverage deliberately switched off")
		self.addCleanup(frappe.set_user, "Administrator")

	def _a_system_user(self) -> str:
		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=1,
		)
		if not users:
			self.skipTest("needs an enabled system user")
		return users[0]

	# ---- what the wider index now admits ----

	def test_the_index_is_no_longer_cut_to_the_declared_types(self):
		index = workflow_index.for_user("Administrator")
		self.assertTrue(
			set(index) - set(scope.doctypes()),
			"wide coverage should admit workflows beyond the declared seven",
		)
		self.assertIn("Purchase Order", index)

	def test_the_declared_types_are_still_declared(self):
		"""Widening adds doctypes. It does not stop the gate applying."""
		index = workflow_index.for_user("Administrator")
		for doctype in scope.doctypes():
			if doctype in index:
				with self.subTest(doctype=doctype):
					self.assertTrue(index[doctype]["in_scope"])

	# ---- the generic requester rule ----

	def test_every_workflow_entry_declares_its_start_states(self):
		full = workflow_index.get_workflow_index(force=True)["workflows"]
		for doctype, entry in full.items():
			with self.subTest(doctype=doctype):
				self.assertTrue(
					entry["start_states"],
					f"{doctype} has no start state, so Tab 2 cannot tell its "
					f"drafts from its approval steps",
				)
				for state in entry["start_states"]:
					self.assertIn(state, entry["states"])

	def test_a_start_state_is_never_offered_as_an_approval(self):
		"""The 193-rows-of-other-peoples-drafts regression, generically.

		An undeclared workflow has no `levels` table to filter against, so a
		start state would otherwise reach Tab 2 for anybody holding the role
		that moves it on -- `Draft -> Send For Approval` is granted to
		`Accounts User`, 42 holders.
		"""
		user = self._a_system_user()
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}

		index = workflow_index.for_user(user)
		tab = approvals.needs_my_action(user, index)

		for row in tab["rows"]:
			entry = index[row["doctype"]]
			with self.subTest(doctype=row["doctype"], state=row["state"]):
				self.assertNotIn(row["state"], entry["start_states"])

	# ---- approved is finished, whatever Cancel is still offered ----

	def test_an_approved_state_whose_only_move_is_cancel_is_terminal(self):
		"""Derived from the workflow, not declared per doctype.

		scope.py corrected four of these by hand. Once coverage widened to
		every workflow the same trap reopened across the rest: 19,374 open
		documents in 11 workflows, 18,713 of them Purchase Receipts sitting
		in `Approved By Store Head`, all of them finished and none of them
		waiting on anybody.
		"""
		full = workflow_index.get_workflow_index(force=True)["workflows"]

		for doctype, entry in full.items():
			doc_status = {}
			for row in frappe.get_all(
				"Workflow Document State",
				filters={"parent": entry["workflow"]},
				fields=["state", "doc_status"],
				ignore_permissions=True,
			):
				doc_status[row["state"]] = cint(row["doc_status"])

			routes = {}
			for transition in entry["transitions"]:
				routes.setdefault(transition["state"], []).append(transition)

			for state, out in routes.items():
				if doc_status.get(state) != 1:
					continue
				if {doc_status.get(t["next_state"]) for t in out} != {2}:
					continue
				with self.subTest(doctype=doctype, state=state):
					self.assertIn(state, entry["terminal"])
					self.assertNotIn(state, entry["non_terminal"])
					self.assertIsNone(entry["level_of"].get(state))

	def test_the_rule_reproduces_what_scope_declares_by_hand(self):
		"""If it did not, it would be a near-miss rather than the same rule."""
		full = workflow_index.get_workflow_index(force=True)["workflows"]
		checked = 0

		for doctype in scope.doctypes():
			entry = full.get(doctype)
			declared = scope.extra_terminal(doctype)
			if not entry or not declared:
				continue
			for state in declared:
				checked += 1
				with self.subTest(doctype=doctype, state=state):
					self.assertIn(state, entry["terminal"])

		self.assertTrue(checked, "scope.py declares no extra_terminal states")

	def test_no_tab_shows_a_document_in_a_terminal_state(self):
		user = self._a_system_user()
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}

		index = workflow_index.for_user(user)
		for name, tab in (
			("needs_action", approvals.needs_my_action(user, index)),
			("awaiting_others", approvals.awaiting_others(user, index)),
			("approved_by_me", approvals.approved_by_me(user, index)),
		):
			for row in tab["rows"]:
				# Draft rows come from the workflow-less family and are not
				# in the index at all; they have no terminal set to be in.
				if row["doctype"] not in index:
					continue
				with self.subTest(tab=name, doctype=row["doctype"]):
					self.assertNotIn(row["state"], index[row["doctype"]]["terminal"])

	# ---- the workflow-less family ----

	def test_unworkflowed_is_submittable_and_has_no_workflow(self):
		index = workflow_index.get_workflow_index(force=True)
		unworkflowed = index["unworkflowed"]
		self.assertTrue(unworkflowed, "expected submittable doctypes with no workflow")

		for doctype in unworkflowed[:25]:
			with self.subTest(doctype=doctype):
				self.assertNotIn(doctype, index["workflows"])
				meta = frappe.get_meta(doctype)
				self.assertTrue(meta.is_submittable)
				self.assertFalse(meta.issingle)
				self.assertFalse(meta.istable)

	def test_unworkflowed_is_sorted_so_the_budget_cuts_the_same_tail(self):
		unworkflowed = workflow_index.get_workflow_index(force=True)["unworkflowed"]
		self.assertEqual(unworkflowed, sorted(unworkflowed))

	def _a_user_with_drafts(self):
		"""The first reader in a small sample who has an unsent draft.

		Sampled rather than seeded, for the reason the module docstring
		gives: this file asserts against the documents already on the site.
		Skips when the sample turns up nothing, which is an honest "no data
		for this" rather than a silent pass.
		"""
		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=25,
		)
		for user in users:
			frappe.set_user(user)
			frappe.local.cache = {}
			mine = drafts.my_drafts(
				user,
				workflow_index.draft_doctypes_for_user(user),
				workflow_index.held_back_for_user(user),
			)
			if mine["rows"]:
				return user, mine

		self.skipTest("no sampled user has drafts on a workflow-less doctype")

	def test_a_held_back_doctype_is_reported_rather_than_dropped(self):
		"""BIG_TABLE_ROWS is a cost decision, so it has to be an admitted gap.

		A table skipped for size is a doctype whose drafts this card will not
		find. That is acceptable only while the card says so -- a silent skip
		would present a partial list as a complete one, which is the failure
		the LIMIT reporting elsewhere in this app exists to avoid.
		"""
		index = workflow_index.get_workflow_index(force=True)
		unscannable = index.get("unscannable") or []
		if not unscannable:
			self.skipTest("no table on this site is over BIG_TABLE_ROWS")

		for doctype in unscannable:
			with self.subTest(doctype=doctype):
				self.assertNotIn(doctype, index["unworkflowed"])
				self.assertNotIn(doctype, index["workflows"])
				# Held back from the scan, never from coverage -- a ToDo
				# pointing at one is still a row Tab 1 may show.
				self.assertIn(doctype, workflow_index.covered_doctypes(index))

		user = self._a_system_user()
		frappe.set_user(user)
		frappe.local.cache = {}
		if workflow_index.held_back_for_user(user):
			mine = drafts.my_drafts(
				user,
				workflow_index.draft_doctypes_for_user(user),
				workflow_index.held_back_for_user(user),
			)
			self.assertTrue(mine["truncated"])

	def test_a_draft_row_is_the_readers_own_and_unsubmitted(self):
		# Looks for a reader who actually has drafts rather than taking the
		# first system user and skipping. Most users have none, so the
		# first-user version of this test skipped every run and asserted
		# nothing -- which is worse than no test, because it reads as covered.
		user, mine = self._a_user_with_drafts()

		for row in mine["rows"][:25]:
			with self.subTest(doctype=row["doctype"], name=row["name"]):
				owner, docstatus = frappe.db.get_value(
					row["doctype"], row["name"], ["owner", "docstatus"]
				)
				self.assertEqual(owner, user)
				self.assertEqual(docstatus, 0)
				self.assertEqual(row["kind"], "draft")

	def test_drafts_never_reach_the_approval_tab(self):
		"""The decision recorded in services/drafts.py, asserted.

		A draft on a workflow-less doctype is work its author has not
		finished, never an approval somebody is waiting on. Tab 2 must not
		contain a single doctype from that family.
		"""
		user = self._a_system_user()
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}

		unworkflowed = set(workflow_index.get_workflow_index()["unworkflowed"])
		index = workflow_index.for_user(user)
		tab = approvals.needs_my_action(user, index)

		seen = {row["doctype"] for row in tab["rows"]}
		self.assertFalse(seen & unworkflowed, f"drafts leaked into Tab 2: {seen & unworkflowed}")

	def test_the_requests_tab_carries_both_kinds_and_labels_them(self):
		user = self._a_system_user()
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}

		index = workflow_index.for_user(user)
		tab = approvals.awaiting_others(user, index)

		for row in tab["rows"]:
			with self.subTest(doctype=row["doctype"], name=row["name"]):
				self.assertIn(row["kind"], ("workflow", "draft"))
				# Both kinds are sorted together and rendered by one
				# template, so both have to carry the ordering key and
				# everything the card reads.
				self.assertIsNotNone(row.get("pending_hours"))
				self.assertIn("level", row)
				self.assertIn("waiting_on_names", row)

		self.assertEqual(
			tab["drafts"], sum(1 for row in tab["rows"] if row["kind"] == "draft")
		)

	def test_the_requests_tab_is_sorted_oldest_first_across_both_kinds(self):
		user = self._a_system_user()
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}

		index = workflow_index.for_user(user)
		rows = approvals.awaiting_others(user, index)["rows"]
		if len(rows) < 2:
			self.skipTest("needs at least two rows to have an order")

		hours = [row["pending_hours"] for row in rows]
		self.assertEqual(hours, sorted(hours, reverse=True))

	# ---- the ToDo narrowing follows coverage rather than disappearing ----

	def test_tab_one_is_still_filtered_and_still_excludes_item(self):
		covered = workflow_index.covered_doctypes()
		self.assertTrue(covered)
		self.assertNotIn(
			"Item",
			covered,
			"Item is neither submittable nor workflow-driven; its 12,754 open "
			"ToDos are the reason Tab 1 was narrowed in the first place",
		)
		for doctype in scope.doctypes():
			self.assertIn(doctype, covered)


class TestDuplicateWorkflowStates(FrappeTestCase):
	"""The States table has no uniqueness constraint, and this site relies on it.

	`PO` declares nine rows for five distinct states, two of them with
	conflicting doc_status: `Cancelled By Purchase Head` as 2 and then 0, and
	`Pending Approval Of Purchase Head` as 0, 0 and 1.

	Reading that table into a plain dict keeps the LAST row, which made
	`Cancelled By Purchase Head` resolve to 0. The cancel-only rule then
	stopped recognising it as a cancellation and all 15,506 approved Purchase
	Orders stayed non-terminal.

	Not guarded on COVER_ALL_SUBMITTABLE -- a malformed States table is a
	property of the site, not of this release.
	"""

	def _rows(self, workflow):
		return frappe.get_all(
			"Workflow Document State",
			filters={"parent": workflow},
			fields=["state", "doc_status"],
			order_by="idx",
			ignore_permissions=True,
		)

	def test_the_index_never_repeats_a_state(self):
		full = workflow_index.get_workflow_index(force=True)["workflows"]
		for doctype, entry in full.items():
			with self.subTest(doctype=doctype):
				self.assertEqual(
					len(entry["states"]),
					len(set(entry["states"])),
					"duplicate states leak into SQL IN clauses and level counts",
				)
				self.assertEqual(
					len(entry["non_terminal"]), len(set(entry["non_terminal"]))
				)

	def test_doc_status_resolves_to_the_first_row_like_core(self):
		"""core apply_workflow: next(d for d in states if d.state == ...)."""
		full = workflow_index.get_workflow_index()["workflows"]
		checked = 0

		for doctype, entry in full.items():
			rows = self._rows(entry["workflow"])
			first = {}
			for row in rows:
				first.setdefault(row["state"], cint(row["doc_status"]))
			resolved = workflow_index._doc_status_of(
				[frappe._dict(r) for r in rows]
			)
			with self.subTest(doctype=doctype):
				self.assertEqual(resolved, first)
			checked += 1

		self.assertTrue(checked)

	def test_any_row_saying_two_makes_a_state_cancelling(self):
		"""core: [s.state for s in workflow.states if s.doc_status == \"2\"]."""
		full = workflow_index.get_workflow_index()["workflows"]

		for doctype, entry in full.items():
			rows = [frappe._dict(r) for r in self._rows(entry["workflow"])]
			expected = {r.state for r in rows if cint(r.doc_status) == 2}
			with self.subTest(doctype=doctype):
				self.assertEqual(workflow_index._cancelling_states(rows), expected)

	def test_a_state_declared_two_then_zero_still_cancels(self):
		"""The exact shape that broke Purchase Order."""
		rows = [
			frappe._dict({"state": "Cancelled By Purchase Head", "doc_status": "2"}),
			frappe._dict({"state": "Cancelled By Purchase Head", "doc_status": "0"}),
		]
		self.assertIn(
			"Cancelled By Purchase Head", workflow_index._cancelling_states(rows)
		)
		# ...and the first-row rule still reports 2 for it here, because 2 is
		# the first row. The two helpers only diverge when the order differs.
		self.assertEqual(
			workflow_index._doc_status_of(rows)["Cancelled By Purchase Head"], 2
		)

	def test_purchase_order_is_finished_once_the_purchase_head_approves(self):
		full = workflow_index.get_workflow_index()["workflows"]
		entry = full.get("Purchase Order")
		if not entry:
			self.skipTest("no active Purchase Order workflow on this site")

		self.assertIn("Approved By Purchase Head", entry["terminal"])
		self.assertNotIn("Approved By Purchase Head", entry["non_terminal"])
		self.assertIsNone(entry["level_of"].get("Approved By Purchase Head"))


class TestTodosOnFinishedDocuments(FrappeTestCase):
	"""An assignment outlives the approval that created it.

	Frappe opens a ToDo when a document is assigned and relies on somebody
	pressing Close. Approving the document does not touch the assignment, so
	Tab 1 was showing work on documents that were finished months ago --
	`Supplier 10947`, approved and read-only on its own form, still carried
	an open assignment.

	Not guarded on COVER_ALL_SUBMITTABLE: the bug predates wide coverage.
	"""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_no_todo_points_at_a_document_in_a_terminal_state(self):
		workflows = workflow_index.get_workflow_index()["workflows"]
		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=25,
		)

		checked = 0
		for user in users:
			cache.clear(user)
			frappe.set_user(user)
			frappe.local.cache = {}

			for row in todos.get_my_todos(user)["rows"]:
				doctype = row.get("reference_type")
				entry = workflows.get(doctype)
				if not entry:
					continue
				state = frappe.db.get_value(
					doctype, row["reference_name"], entry["state_field"]
				)
				checked += 1
				with self.subTest(user=user, doctype=doctype, name=row["reference_name"]):
					self.assertNotIn(state, entry["terminal"])

		if not checked:
			self.skipTest("no sampled user has a to-do on a workflow doctype")

	def test_a_finished_reference_is_taken_out_of_the_total_as_well(self):
		"""Otherwise the page reports a backlog it is deliberately hiding."""
		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=25,
		)

		for user in users:
			cache.clear(user)
			frappe.set_user(user)
			frappe.local.cache = {}

			raw = frappe.get_all(
				"ToDo",
				filters={"allocated_to": user, "status": "Open"},
				or_filters=todos._scope_or_filters(),
				fields=["reference_type", "reference_name"],
				limit_page_length=0,
				ignore_permissions=True,
			)
			live, finished = todos._live_references(raw, user)
			stale = sum(
				1
				for r in raw
				if (r["reference_type"], r["reference_name"]) in finished
			)
			if not stale:
				continue

			block = todos.get_my_todos(user)
			self.assertEqual(block["total"], max(len(raw) - stale, block["shown"]))
			self.assertGreaterEqual(block["total"], block["shown"])
			return

		self.skipTest("no sampled user has a to-do on a finished document")

	def test_live_references_holds_no_module_state(self):
		"""The file-level rule, asserted rather than trusted.

		A worker serves many users in sequence. The split into live/finished
		is returned, never stashed, so two readers in one process cannot see
		each other`s references.
		"""
		import task_manager.services.todos as module

		leaked = [
			name
			for name, value in vars(module).items()
			if isinstance(value, (set, dict, list)) and not name.startswith("__")
		]
		self.assertFalse(leaked, "module-level mutable state: {0}".format(leaked))


class TestApprovedByMeIsOnlyApprovals(FrappeTestCase):
	"""Submitting your own document is not approving it.

	Core writes a Completed Workflow Action for every transition a user
	performs, `Draft -> Submit` included. "Stuck after you" reads that table
	as "levels I cleared", so a requester who has never approved anything was
	getting their own requests back, captioned "You approved step 1".

	Not guarded on COVER_ALL_SUBMITTABLE: the bug predates wide coverage and
	the fix has to outlive a revert of it.
	"""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_a_start_state_action_never_counts_as_a_level_cleared(self):
		users = frappe.get_all(
			"User",
			filters={
				"enabled": 1,
				"user_type": "System User",
				"name": ["not in", ["Administrator", "Guest"]],
			},
			pluck="name",
			limit=15,
		)

		checked = 0
		for user in users:
			cache.clear(user)
			frappe.set_user(user)
			frappe.local.cache = {}

			index = workflow_index.for_user(user)
			rows = approvals.approved_by_me(user, index)["rows"]

			for row in rows:
				entry = index[row["doctype"]]
				cleared = frappe.get_all(
					"Workflow Action",
					filters={
						"reference_doctype": row["doctype"],
						"reference_name": row["name"],
						"completed_by": user,
						"status": "Completed",
					},
					pluck="workflow_state",
					ignore_permissions=True,
				)
				checked += 1
				with self.subTest(user=user, doctype=row["doctype"], name=row["name"]):
					# At least one of the actions this user completed has to
					# be a real level. A row kept alive only by the submission
					# of its own author is the defect.
					self.assertTrue(
						[s for s in cleared if s not in entry["start_states"]],
						"{0} is in Stuck-after-you but {1} only ever moved it "
						"out of a start state".format(row["name"], user),
					)

					# `my_level` is deliberately NOT asserted non-None. A
					# non-terminal state the level walk cannot reach gets
					# None rather than a fabricated number, and 101 real rows
					# are in exactly that position -- `Material Request /
					# Pending Approval from R&D siRNA-Vivo` is unreachable in
					# a graph with several entry points. The tracker renders
					# the gap honestly; asserting a number here would be
					# asserting a promise the index does not make.

		if not checked:
			self.skipTest("no sampled user has anything stuck after them")

	def test_a_genuine_approver_still_sees_the_document(self):
		"""The filter must not take the real approvals out with the drafts."""
		action = frappe.get_all(
			"Workflow Action",
			filters={"status": "Completed", "completed_by": ["not in", ["Administrator", ""]]},
			fields=["reference_doctype", "reference_name", "completed_by", "workflow_state"],
			order_by="creation desc",
			limit_page_length=400,
			ignore_permissions=True,
		)

		index_cache = {}
		for row in action:
			user = row["completed_by"]
			if user not in index_cache:
				frappe.set_user(user)
				frappe.local.cache = {}
				index_cache[user] = workflow_index.for_user(user)
			index = index_cache[user]

			entry = index.get(row["reference_doctype"])
			if not entry or row["workflow_state"] in entry["start_states"]:
				continue
			if row["workflow_state"] not in entry["level_of"]:
				continue
			# A real level cleared by a real user on a document that is still
			# open and not back on their own desk -- this is the case the tab
			# exists for, so it must survive the filter.
			current = frappe.db.get_value(
				row["reference_doctype"], row["reference_name"], entry["state_field"]
			)
			if not current or current in entry["terminal"] or current in entry["my_states"]:
				continue

			cache.clear(user)
			frappe.set_user(user)
			frappe.local.cache = {}
			names = {
				r["name"] for r in approvals.approved_by_me(user, index)["rows"]
			}
			self.assertIn(
				row["reference_name"],
				names,
				"{0} cleared {1} on {2} and no longer sees it".format(
					user, row["workflow_state"], row["reference_name"]
				),
			)
			return

		self.skipTest("no sampled action is a genuine level on a still-open document")


class TestNamedApproverGate(FrappeTestCase):
	"""The hole this was built to close, per doctype."""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def _as(self, user: str, doctype: str | None = None):
		"""Build Tab 2 as `user`, optionally narrowed to one doctype.

		Narrowing is not a convenience, it is what makes these assertions
		mean anything. Tab 2 merges every readable doctype, sorts oldest
		first and cuts at 200 rows, and on this site that window is routinely
		filled by abandoned Purchase Receipt and Material Request drafts from
		2023 -- the first run of these tests found a real approver whose 200
		oldest pending rows were all `MAT-PRE-2023-*`, with their actual
		HRMS requests below the cut.

		That matters in both directions. A positive assertion would fail for
		a reason that has nothing to do with the gate, and -- worse -- a
		negative assertion would *pass* for that reason, reporting isolation
		that had not been tested. One doctype at a time removes the cap from
		the question.
		"""
		cache.clear(user)
		frappe.set_user(user)
		frappe.local.cache = {}
		index = workflow_index.for_user(user)
		if doctype:
			index = {doctype: index[doctype]} if doctype in index else {}
		return approvals.needs_my_action(user, index)

	def _names(self, tab) -> set:
		return {row["name"] for row in tab["rows"]}

	def test_named_approver_sees_their_own_queue(self):
		for doctype in scope.doctypes():
			row, user = _pending_an_approver_can_see(doctype)
			if not row:
				continue

			with self.subTest(doctype=doctype):
				self.assertIn(row["name"], self._names(self._as(user, doctype)))

	def test_an_unnamed_role_holder_does_not_see_it(self):
		"""The empty-condition transitions, which is where the hole was.

		`Gate Pass: Pending For Approval -> Approve` is allowed to
		`Department HOD` with no condition, and `On Duty Request` grants the
		same shape to `Head Leave Approver`. A holder of that role who is not
		named on the request must not be offered it.
		"""
		cases = (
			("Gate Pass", "Department HOD"),
			("Attendance Request", "Head Leave Approver"),
			("Leave Application", "Leave Approver"),
		)

		for doctype, role in cases:
			rows = _pending(doctype, limit=1)
			if not rows:
				continue
			row = rows[0]
			entry = scope.for_doctype(doctype)
			approvers = set(scope.named_approvers(entry, row["state"], row))

			holders = frappe.get_all(
				"Has Role",
				filters={"role": role, "parenttype": "User"},
				pluck="parent",
				limit=60,
			)
			outsider = next(
				(
					u
					for u in holders
					if u.lower() not in approvers
					and u not in ("Administrator", "Guest")
					and frappe.db.get_value("User", u, "enabled")
					and frappe.db.get_value("User", u, "user_type") == "System User"
				),
				None,
			)
			if not outsider:
				continue

			with self.subTest(doctype=doctype, role=role):
				self.assertNotIn(
					row["name"],
					self._names(self._as(outsider, doctype)),
					f"{outsider} holds {role} but is not named on {doctype} {row['name']}",
				)

	def test_work_from_home_never_appears_as_on_duty(self):
		"""82 Work From Home rows share the doctype and are out of scope."""
		wfh = frappe.get_all(
			"Attendance Request",
			filters={
				"reason": "Work From Home",
				"workflow_state": "Pending Approval Of Department HOD",
				"docstatus": ["<", 2],
			},
			fields=["name", "custom_approver", "custom_head_leave_approver"],
			limit=1,
			ignore_permissions=True,
		)
		if not wfh:
			self.skipTest("no pending Work From Home request")

		approver = wfh[0]["custom_approver"] or wfh[0]["custom_head_leave_approver"]
		if not (approver and frappe.db.get_value("User", approver, "enabled")):
			self.skipTest("its approver is not a usable user")

		shown = self._names(self._as(approver, "Attendance Request"))
		self.assertNotIn(wfh[0]["name"], shown)

		# The assertion above would pass on an empty tab, which proves
		# nothing. If anything is shown, every row of it must be On Duty.
		if shown:
			reasons = set(
				frappe.get_all(
					"Attendance Request",
					filters={"name": ["in", list(shown)]},
					pluck="reason",
					ignore_permissions=True,
				)
			)
			self.assertEqual(reasons, {"On Duty"})

	def test_no_approver_address_reaches_the_payload(self):
		"""The approver fields are selected for the gate and must be stripped.

		They hold email addresses -- `owner` is already scrubbed this way, and
		these have to be too.
		"""
		checked = 0
		for doctype in scope.doctypes():
			row, user = _pending_an_approver_can_see(doctype)
			if not row:
				continue

			tab = self._as(user, doctype)
			self.assertTrue(tab["rows"], f"{doctype}: nothing to inspect")

			for shown in tab["rows"]:
				row_scope = scope.for_doctype(shown["doctype"])
				for field in approver_gate.select_fields(row_scope):
					self.assertNotIn(
						field, shown, f"{field} must not be returned to the browser"
					)
				self.assertIn("waiting_on_names", shown)
				checked += 1

		if not checked:
			self.skipTest("no visible pending request on any scoped doctype")

	def test_unroutable_requests_are_counted_not_hidden(self):
		"""A request whose approver field is blank can never pass the gate."""
		blank = frappe.get_all(
			"Gate Pass",
			filters={
				"approver": ["in", ["", None]],
				"workflow_state": "Pending For Approval",
				"docstatus": ["<", 2],
			},
			pluck="name",
			limit=1,
			ignore_permissions=True,
		)
		if not blank:
			self.skipTest("no unroutable Gate Pass pending")

		entry = scope.for_doctype("Gate Pass")
		row = {"doctype": "Gate Pass", "state": "Pending For Approval", "approver": ""}
		self.assertEqual(approver_gate.unroutable([row]), 1)
		self.assertFalse(scope.may_act(entry, "Pending For Approval", row, "anyone@example.com"))


class TestApproverSharing(FrappeTestCase):
	"""The share that makes the approval tabs non-empty.

	Nothing here writes. The hook is exercised for its *refusals* -- the
	states it must not share on -- and the diagnostic is checked for
	self-consistency, because a share that fires on a draft would hand an
	approver a request its author has not sent yet.
	"""

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_hook_is_registered_for_every_scoped_doctype(self):
		for doctype in scope.doctypes():
			handlers = frappe.get_hooks("doc_events").get(doctype, {}).get("on_update") or []
			self.assertIn(
				"task_manager.services.approver_gate.on_request_update",
				handlers,
				f"{doctype} is not wired for the request-update hook",
			)

	def test_enforcement_is_not_registered_while_the_flag_is_off(self):
		"""Installing this app must not change who can approve what."""
		if scope.ENFORCE_NAMED_APPROVER:
			self.skipTest("enforcement has been switched on deliberately")

		for doctype in scope.doctypes():
			handlers = frappe.get_hooks("doc_events").get(doctype, {}).get("validate") or []
			self.assertNotIn(
				"task_manager.services.approver_gate.enforce_named_approver", handlers
			)

	def test_nothing_is_shared_for_a_state_nobody_approves(self):
		"""A draft is not shared: its author has not sent it anywhere yet."""
		shares = []

		original = frappe.share.add
		frappe.share.add = lambda *a, **k: shares.append((a, k))
		self.addCleanup(lambda: setattr(frappe.share, "add", original))

		entry = scope.for_doctype("Gate Pass")
		for state in ("Draft", "Approved by Leave Approver", ""):
			doc = frappe._dict(
				doctype="Gate Pass",
				name="tm-test-not-real",
				owner="someone@example.com",
				workflow_state=state,
				approver="boss@example.com",
				get=lambda key, default=None, _s=state: {
					"workflow_state": _s,
					"approver": "boss@example.com",
				}.get(key, default),
			)
			approver_gate.share_with_named_approvers(doc)

		self.assertEqual(shares, [], "a non-approval state must not be shared")
		self.assertNotIn(
			"Draft", entry["levels"], "Draft must not be declared an approval step"
		)

	def test_the_share_is_actually_created(self):
		"""Guards a bug that produced no error and no share.

		`share_with_named_approvers` logs and swallows, so that it can never
		block somebody saving a leave request. The cost of that is a silent
		failure mode, and the first version hit it: the call was written
		`frappe.share.add`, which raises AttributeError wherever the submodule
		has not already been imported. Every share was swallowed into the
		Error Log. Only an assertion that a DocShare row appears catches this
		-- the function returns nothing either way.

		This test writes, and rolls back.
		"""
		self.addCleanup(frappe.db.rollback)

		target = None
		for doctype in scope.doctypes():
			name, approver = _pending_needing_share(doctype)
			if name:
				target = (doctype, name, approver)
				break
		if not target:
			self.skipTest("every pending request's approver can already submit")

		doctype, name, approver = target
		frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name})

		approver_gate.share_with_named_approvers(frappe.get_doc(doctype, name))

		shared_with = {
			s.lower()
			for s in frappe.get_all(
				"DocShare",
				filters={"share_doctype": doctype, "share_name": name},
				pluck="user",
			)
		}
		self.assertIn(
			approver, shared_with, f"no share was created for {approver} on {doctype} {name}"
		)

	def test_sharing_grants_submit_not_just_read(self):
		"""Read alone is not enough -- approving a submittable doc submits it."""
		self.addCleanup(frappe.db.rollback)

		target = None
		for doctype in scope.doctypes():
			name, approver = _pending_needing_share(doctype)
			if name:
				target = (doctype, name, approver)
				break
		if not target:
			self.skipTest("every pending request's approver can already submit")

		doctype, name, approver = target
		frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name})
		approver_gate.share_with_named_approvers(frappe.get_doc(doctype, name))

		share = frappe.get_all(
			"DocShare",
			filters={"share_doctype": doctype, "share_name": name, "user": approver},
			fields=["read", "write", "submit"],
		)
		self.assertTrue(share, "expected a share row")
		self.assertEqual(share[0]["submit"], 1)
		self.assertEqual(share[0]["read"], 1)

	def test_sharing_is_idempotent(self):
		"""Saving a request repeatedly must not accumulate DocShare rows."""
		self.addCleanup(frappe.db.rollback)

		target = None
		for doctype in scope.doctypes():
			name, approver = _pending_needing_share(doctype)
			if name:
				target = (doctype, name, approver)
				break
		if not target:
			self.skipTest("every pending request's approver can already submit")

		doctype, name, approver = target
		frappe.db.delete("DocShare", {"share_doctype": doctype, "share_name": name})

		doc = frappe.get_doc(doctype, name)
		for _ in range(3):
			approver_gate.share_with_named_approvers(doc)

		self.assertEqual(
			frappe.db.count(
				"DocShare",
				{"share_doctype": doctype, "share_name": name, "user": approver},
			),
			1,
			"sharing three times must leave exactly one DocShare row",
		)

	def test_the_visibility_diagnostic_adds_up(self):
		for doctype in scope.doctypes():
			report = approver_gate.unshared(doctype)
			with self.subTest(doctype=doctype):
				self.assertEqual(
					report["pending"],
					report["visible_to_approver"]
					+ report["blocked"]
					+ report["no_approver_named"],
					report,
				)

	def test_backfill_defaults_to_a_dry_run(self):
		"""The one function here that writes must not write unless told to."""
		import inspect

		default = inspect.signature(approver_gate.backfill_shares).parameters["dry_run"].default
		self.assertIs(default, True)

		before = frappe.db.count("DocShare")
		report = approver_gate.backfill_shares("Gate Pass")
		self.assertTrue(report["dry_run"])
		self.assertEqual(report["shares_written"], 0)
		self.assertEqual(frappe.db.count("DocShare"), before)


class TestRequesterIsTheEmployee(FrappeTestCase):
	def test_employee_of_resolves_and_prefers_active(self):
		employee = frappe.get_all(
			"Employee",
			filters={"status": "Active", "user_id": ["!=", ""]},
			fields=["name", "user_id"],
			limit=1,
			ignore_permissions=True,
		)
		if not employee:
			self.skipTest("no active employee with a user")

		self.assertEqual(
			approver_gate.employee_of(employee[0]["user_id"]), employee[0]["name"]
		)

	# A user with no Employee record must fall back to authorship rather than
	# matching every request whose employee field happens to be empty.
	def test_a_user_without_an_employee_record_falls_back_to_owner(self):
		self.assertIsNone(approver_gate.employee_of("not-a-user@example.invalid"))
		self.assertIsNone(
			approver_gate.requester_filters(
				scope.for_doctype("Leave Application"), "not-a-user@example.invalid"
			)
		)
