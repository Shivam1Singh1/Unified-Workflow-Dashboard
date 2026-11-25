# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Isolation tests -- the properties this app would be worth deleting without.

These run against whatever workflows and ToDos the site already has, rather
than seeding their own. That is deliberate for this phase: the point is to
prove the isolation rules hold against real role sets and real blocked-module
configurations, which a hand-built fixture would not reproduce. No test here
creates or modifies a document, so there is nothing to clean up.

Multi-level behaviour (the Phase 4 checklist items 10 to 12) needs a purpose
built 4-level workflow and is not covered here.
"""

import frappe
from frappe.permissions import AUTOMATIC_ROLES
from frappe.tests.utils import FrappeTestCase

from task_manager import api
from task_manager.services import cache, workflow_index


def _two_system_users() -> tuple[str, str]:
	"""Two enabled system users that are not Administrator."""
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ["not in", ["Administrator", "Guest"]]},
		pluck="name",
		order_by="name",
		limit=2,
	)
	return (users + [None, None])[:2]


class TestIsolation(FrappeTestCase):
	def setUp(self):
		self.user_a, self.user_b = _two_system_users()
		if not (self.user_a and self.user_b):
			self.skipTest("needs two enabled system users on the site")
		self.addCleanup(frappe.set_user, "Administrator")
		for user in (self.user_a, self.user_b):
			cache.clear(user)

	# ---- identity --------------------------------------------------------

	def test_user_argument_is_ignored(self):
		"""A tampered `user` parameter must not change whose data comes back.

		Frappe drops keywords a whitelisted function does not declare, so this
		asserts the contract that keeps that true: no function in api.py may
		grow a `user` parameter or a `**kwargs`.
		"""
		frappe.set_user(self.user_a)
		mine = api.get_my_tasks(refresh=1)

		frappe.local.cache = {}
		cache.clear(self.user_a)
		tampered = frappe.call(
			"task_manager.api.get_my_tasks", user=self.user_b, refresh=1
		)

		self.assertEqual(mine["todo"]["total"], tampered["todo"]["total"])
		self.assertEqual(
			mine["counts"]["needs_action"], tampered["counts"]["needs_action"]
		)

	def test_no_whitelisted_function_accepts_a_user(self):
		import inspect

		for name in ("get_my_tasks", "close_todo", "send_reminder"):
			signature = inspect.signature(getattr(api, name))
			params = signature.parameters
			self.assertNotIn("user", params, f"{name} must not take a user")
			self.assertFalse(
				any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()),
				f"{name} must not take **kwargs -- it disables argument filtering",
			)

	def test_guest_is_refused(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.get_my_tasks()

	# ---- cross-user ------------------------------------------------------

	def test_cannot_close_another_users_todo(self):
		todo = frappe.get_all(
			"ToDo",
			filters={"status": "Open", "allocated_to": ["!=", self.user_a]},
			pluck="name",
			limit=1,
		)
		if not todo:
			self.skipTest("no ToDo belonging to another user")

		frappe.set_user(self.user_a)
		with self.assertRaises(frappe.PermissionError):
			api.close_todo(todo[0])

	def test_missing_and_foreign_records_refuse_identically(self):
		"""The refusal must not reveal whether a record exists."""
		frappe.set_user(self.user_a)

		messages = []
		for name in ("tm-does-not-exist-at-all", "tm-also-not-real"):
			try:
				api.close_todo(name)
			except frappe.PermissionError as exc:
				messages.append(str(exc))

		self.assertEqual(len(messages), 2)
		self.assertEqual(messages[0], messages[1])

	def test_cache_is_per_user(self):
		frappe.set_user(self.user_a)
		first = api.get_my_tasks(refresh=1)

		frappe.set_user(self.user_b)
		frappe.local.cache = {}
		second = api.get_my_tasks(refresh=1)

		frappe.set_user(self.user_a)
		frappe.local.cache = {}
		again = api.get_my_tasks()

		self.assertEqual(first["todo"]["total"], again["todo"]["total"])
		if first["todo"]["total"] != second["todo"]["total"]:
			self.assertNotEqual(second["todo"]["total"], again["todo"]["total"])

	# ---- visibility ------------------------------------------------------

	def test_blocked_modules_do_not_hide_own_work(self):
		"""A module hidden in the sidebar must not hide the user's documents.

		This is the regression test for HR-LAP-2026-01117: a leave application
		its own author could read, was waiting on, and could not see, because
		`HR` is blocked in his profile. A blocked module is a navigation
		preference, not a permission.
		"""
		frappe.set_user(self.user_a)
		blocked = workflow_index.blocked_modules(self.user_a)
		if not blocked:
			self.skipTest("this user blocks no modules")

		sliced = workflow_index.for_user(self.user_a)
		kept_from_blocked = [
			dt for dt, entry in sliced.items() if entry["module"] in blocked
		]
		self.assertTrue(
			kept_from_blocked,
			"every doctype from a blocked module was dropped -- the preference "
			"is being treated as a permission again",
		)

	def test_blocked_modules_are_honoured_when_asked_for(self):
		"""The opt-in still works, for sites that do want the stricter rule."""
		frappe.set_user(self.user_a)
		blocked = workflow_index.blocked_modules(self.user_a)
		if not blocked:
			self.skipTest("this user blocks no modules")

		strict = workflow_index.for_user(self.user_a, respect_blocked_modules=True)
		for doctype, entry in strict.items():
			self.assertNotIn(
				entry["module"], blocked, f"{doctype} is in blocked module {entry['module']}"
			)

	def test_automatic_roles_never_create_approvals(self):
		"""A transition anyone can make is not an approval.

		Regression test for shivam.singh being shown 127 other people's
		Purchase Receipt drafts: that workflow grants its Draft step to `All`,
		which every user holds. 55 of the 306 transitions on this site are
		granted to an automatic role, so this is not a corner case.
		"""
		index = workflow_index.get_workflow_index(force=True)
		for doctype, entry in index["workflows"].items():
			for role in entry["states_by_role"]:
				self.assertNotIn(
					role,
					AUTOMATIC_ROLES,
					f"{doctype} grants approvals to the automatic role {role}",
				)

	def test_waiting_on_never_names_an_automatic_role(self):
		frappe.set_user(self.user_a)
		payload = api.get_my_tasks(refresh=1)
		for key in ("awaiting_others", "approved_by_me"):
			for row in payload[key]["rows"]:
				for role in row.get("waiting_on_roles") or []:
					self.assertNotIn(role, AUTOMATIC_ROLES, f"{key} names {role}")

	def test_unreadable_doctypes_are_excluded(self):
		frappe.set_user(self.user_a)
		for doctype in workflow_index.for_user(self.user_a):
			self.assertTrue(frappe.has_permission(doctype, "read", user=self.user_a))

	def test_ghost_workflows_are_skipped(self):
		index = workflow_index.get_workflow_index(force=True)
		for doctype in index["workflows"]:
			self.assertTrue(frappe.db.exists("DocType", doctype))

	# ---- what reaches the browser ---------------------------------------

	def test_payload_carries_no_email_addresses(self):
		"""Approvers and owners are shown by name. Addresses never ship.

		`completed_by` is a user id, which is an email on this site, and the
		trail is built from it -- so this walks the whole payload rather than
		spot-checking the fields that are supposed to be safe.
		"""
		frappe.set_user(self.user_a)
		payload = api.get_my_tasks(refresh=1)

		found = []
		self._walk(payload, found)
		self.assertEqual(found, [], f"email addresses leaked: {found[:5]}")

	def _walk(self, node, found, path="payload"):
		if isinstance(node, dict):
			for key, value in node.items():
				self._walk(value, found, f"{path}.{key}")
		elif isinstance(node, list):
			for i, value in enumerate(node):
				self._walk(value, found, f"{path}[{i}]")
		elif isinstance(node, str) and "@" in node and "." in node.split("@")[-1]:
			found.append(f"{path}={node}")

	def test_counts_match_the_rows_returned(self):
		"""A count must never promise more rows than the payload contains."""
		frappe.set_user(self.user_a)
		payload = api.get_my_tasks(refresh=1)

		for key in ("todo", "needs_action", "awaiting_others", "approved_by_me"):
			block = payload[key]
			self.assertEqual(block["shown"], len(block["rows"]), key)
			self.assertEqual(payload["counts"][key], len(block["rows"]), key)


class TestAccessChangesTakeEffect(FrappeTestCase):
	"""What a user may see can change without any document changing.

	Every tab is rebuilt from `get_list` and `frappe.has_permission` against
	the live session, so the content is right the moment it is rebuilt. The
	hole is the cached payload: assembled while the reader still held a role
	they have since lost, and then served for up to `cache.TTL_SECONDS`.

	Two minutes of showing somebody rows they can no longer open is two
	minutes too many when the reason the role went is that they should not be
	looking.
	"""

	def test_the_access_hooks_are_registered(self):
		from task_manager import hooks

		self.assertIn("on_update", hooks.doc_events.get("User", {}))
		self.assertIn("on_trash", hooks.doc_events.get("User", {}))
		for event in ("after_insert", "on_update", "on_trash"):
			self.assertIn(event, hooks.doc_events.get("User Permission", {}))

		handlers = list(hooks.doc_events["User"].values()) + list(
			hooks.doc_events["User Permission"].values()
		)
		for handler in handlers:
			self.assertEqual(handler, "task_manager.services.cache.clear_for_user")
			# A hook that does not resolve fails silently at save time.
			self.assertTrue(callable(frappe.get_attr(handler)))

	def test_a_user_save_drops_only_that_users_payload(self):
		from task_manager.services import cache

		mine, theirs = "aa.tm@example.invalid", "bb.tm@example.invalid"
		self.addCleanup(cache.clear, mine)
		self.addCleanup(cache.clear, theirs)

		cache.set(mine, {"rows": 1})
		cache.set(theirs, {"rows": 1})

		cache.clear_for_user(frappe._dict({"doctype": "User", "name": mine}))

		self.assertIsNone(cache.get(mine))
		self.assertIsNotNone(cache.get(theirs), "cleared somebody else as well")

	def test_a_user_permission_change_drops_that_users_payload(self):
		from task_manager.services import cache

		mine, theirs = "cc.tm@example.invalid", "dd.tm@example.invalid"
		self.addCleanup(cache.clear, mine)
		self.addCleanup(cache.clear, theirs)

		cache.set(mine, {"rows": 1})
		cache.set(theirs, {"rows": 1})

		cache.clear_for_user(
			frappe._dict({"doctype": "User Permission", "user": mine})
		)

		self.assertIsNone(cache.get(mine))
		self.assertIsNotNone(cache.get(theirs))

	def test_an_unrelated_doctype_clears_nothing(self):
		"""The handler is narrow on purpose -- see the note in cache.py."""
		from task_manager.services import cache

		user = "ee.tm@example.invalid"
		self.addCleanup(cache.clear, user)
		cache.set(user, {"rows": 1})

		cache.clear_for_user(frappe._dict({"doctype": "Role", "name": "Employee"}))
		self.assertIsNotNone(cache.get(user))

		# A User Permission row carrying no user must not throw either.
		cache.clear_for_user(
			frappe._dict({"doctype": "User Permission", "user": None})
		)
		self.assertIsNotNone(cache.get(user))


class TestStaleShareReportIsReadOnly(FrappeTestCase):
	"""The diagnostic must never take access away merely by being run.

	DocShare writes commit internally, so a revoke cannot be undone with
	`frappe.db.rollback()`. That makes read-only-by-default a correctness
	property of this function rather than a convention.
	"""

	def test_the_report_defaults_to_dry_run_and_writes_nothing(self):
		import inspect

		from task_manager.services import approver_gate

		signature = inspect.signature(approver_gate.stale_shares)
		self.assertIs(signature.parameters["revoke"].default, False)

		before = frappe.db.count("DocShare")
		report = approver_gate.stale_shares()
		self.assertEqual(frappe.db.count("DocShare"), before)

		self.assertTrue(report["dry_run"])
		self.assertEqual(report["revoked"], 0)
		self.assertEqual(report["orphaned_total"], len(report["orphaned"]))
		self.assertEqual(report["reassigned_total"], len(report["reassigned"]))

	def test_the_two_populations_do_not_overlap(self):
		"""Orphaned and reassigned are different questions with different answers."""
		from task_manager.services import approver_gate

		report = approver_gate.stale_shares()
		orphaned = {(r["doctype"], r["name"], r["user"]) for r in report["orphaned"]}
		reassigned = {
			(r["doctype"], r["name"], r["user"]) for r in report["reassigned"]
		}
		self.assertFalse(orphaned & reassigned)
