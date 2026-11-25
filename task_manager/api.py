# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""The only whitelisted surface of this app.

Nothing else in `task_manager` is callable from a browser. Everything under
services/ is internal, which is what makes the rules below enforceable in one
place.

Three rules hold for every function here:

1. **The user is never an argument.** Identity comes from
   `frappe.session.user` and nowhere else. Frappe drops any keyword a
   whitelisted function does not declare, so `?user=someone.else` never
   reaches the body -- but only while no function here takes `**kwargs`. None
   do, and none should.

2. **Fixed signatures.** Adding `**kwargs` to any function below silently
   disables rule 1.

3. **Client identifiers are re-checked, never trusted.** `close_todo` and
   `send_reminder` receive names from the browser and re-fetch the record
   server-side before touching it.

Every entry point and every tab is wrapped in logging. An exception anywhere
lands in the Error Log with the user, the operation and the arguments that
produced it, so a failure the user reports as "it did not work" can be found
without reproducing it. See `_log_failure`.
"""

import frappe
from frappe import _

from task_manager.services import (
	approvals,
	cache,
	profile,
	reminders,
	summary,
	todos,
	workflow_index,
)

# A forced rebuild costs seconds. `refresh=1` is allowed to skip the cache,
# but not faster than this, so a caller cannot pin a worker by repeating it.
#
# Nothing in the page sends it today -- the Refresh button that did was
# removed, since the dashboard reloads itself on a timer. The throttle and
# the argument both stay: this is the cache-busting path, and it is the
# caller that went away, not the need for the path.
FORCED_REFRESH_COOLDOWN = 20

LOG_PREFIX = "Task Manager"


def _log_failure(operation: str, context: str = ""):
	"""Record a failure with enough context to diagnose it later.

	Titles are prefixed so the whole app can be found in the Error Log with
	one filter. The user is always recorded: almost every bug this page has
	had depended on *whose* roles, modules or documents were involved, and
	a traceback without the user has been useless every time.
	"""
	user = getattr(frappe.session, "user", "unknown")
	frappe.log_error(
		title=f"{LOG_PREFIX}: {operation} failed"[:140],
		message=(
			f"operation : {operation}\n"
			f"user      : {user}\n"
			f"context   : {context or '-'}\n\n"
			f"{frappe.get_traceback()}"
		),
	)


def _log_refusal(operation: str, context: str):
	"""Record a permission refusal.

	Kept separate from failures on purpose. A refusal is the system working,
	but a pattern of them is worth seeing -- either somebody is probing, or a
	button is being offered to people who may not use it.
	"""
	user = getattr(frappe.session, "user", "unknown")
	frappe.log_error(
		title=f"{LOG_PREFIX}: {operation} refused"[:140],
		message=f"operation : {operation}\nuser      : {user}\ncontext   : {context}\n",
	)


def _session_user() -> str:
	"""The caller, or a refusal. Never derived from anything client-supplied."""
	user = frappe.session.user

	if not user or user == "Guest":
		_log_refusal("session", "guest or empty session")
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	# Website users have no business on a desk page, and this endpoint is the
	# only way to reach the data behind it.
	if frappe.db.get_value("User", user, "user_type") != "System User":
		_log_refusal("session", f"user_type is not System User")
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	return user


@frappe.whitelist()
def get_my_tasks(refresh: int = 0) -> dict:
	"""Everything the page shows, for the session user, in one call."""
	user = _session_user()

	try:
		refresh = frappe.utils.cint(refresh)
		if refresh and not _may_force_refresh(user):
			refresh = 0

		if not refresh:
			cached = cache.get(user)
			if cached:
				cached["cached"] = True
				return cached

		index = workflow_index.for_user(user)
	except Exception:
		_log_failure("get_my_tasks (index)", f"refresh={refresh}")
		raise

	# Each tab is built independently. One failing doctype or one broken
	# workflow condition should cost that tab, not the whole page -- and the
	# Error Log should say which tab it was.
	payload = {
		"todo": _tab("todo", lambda: todos.get_my_todos(user)),
		"needs_action": _tab("needs_action", lambda: approvals.needs_my_action(user, index)),
		"awaiting_others": _tab("awaiting_others", lambda: approvals.awaiting_others(user, index)),
		"approved_by_me": _tab("approved_by_me", lambda: approvals.approved_by_me(user, index)),
		# Who the reader is and what this release covers. Cheap -- two cached
		# lookups and a read of scope.py -- and it travels with the payload so
		# the page never has to ask a second endpoint.
		#
		# Not wrapped in _tab: that returns an empty *tab* on failure, which
		# is the wrong shape here and would hand the template a row list where
		# it expects a person.
		"profile": _profile(user),
		"generated_at": frappe.utils.now(),
		"cached": False,
	}
	payload["counts"] = {key: payload[key]["shown"] for key in
		("todo", "needs_action", "awaiting_others", "approved_by_me")}

	# The headline, the two charts and the ranked queue are all derived from
	# the four lists above -- no further queries.
	try:
		payload.update(summary.build(payload))
	except Exception:
		_log_failure("summary", "aggregates")
		payload.update({"summary": {}, "workload": {}, "age_buckets": {}, "queue": []})

	# A payload with a broken tab is not worth caching: the next request
	# should try again rather than serve the gap for two minutes.
	if not any(payload[key].get("failed") for key in payload["counts"]):
		cache.set(user, payload)

	return payload


def _profile(user: str) -> dict:
	"""The profile block, or an empty one. A broken card never costs the page."""
	try:
		return profile.build(user)
	except Exception:
		_log_failure("profile", f"user={user}")
		return {}


def _tab(name: str, build):
	try:
		return build()
	except Exception:
		_log_failure(f"tab {name}", f"tab={name}")
		# An empty block with `failed` set, so the page can say this tab broke
		# instead of quietly showing zero.
		return {"rows": [], "shown": 0, "total": 0, "truncated": False, "failed": True}


def _may_force_refresh(user: str) -> bool:
	key = f"task_manager:refresh:{user}"
	if frappe.cache.get_value(key):
		return False
	frappe.cache.set_value(key, 1, expires_in_sec=FORCED_REFRESH_COOLDOWN)
	return True


@frappe.whitelist()
def close_todo(name: str) -> dict:
	"""Close one of my own ToDos.

	ToDo's permission rule would let any role holder close anybody's ToDo, so
	ownership is re-checked here against the session user. The refusal is the
	same generic message whether the row is missing or belongs to someone else.
	"""
	user = _session_user()

	allocated_to = frappe.db.get_value("ToDo", name, "allocated_to")
	if not allocated_to or allocated_to != user:
		_log_refusal("close_todo", f"todo={name} allocated_to={allocated_to or 'missing'}")
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	try:
		todo = frappe.get_doc("ToDo", name)
		todo.status = "Closed"
		todo.save()
	except Exception:
		_log_failure("close_todo", f"todo={name}")
		raise

	cache.clear(user)
	return {"ok": True, "name": name}


@frappe.whitelist()
def send_reminder(doctype: str, name: str) -> dict:
	"""Nudge the people a document of mine is waiting on. Phase 3."""
	user = _session_user()

	try:
		result = reminders.send(doctype, name, user)
	except frappe.PermissionError:
		_log_refusal("send_reminder", f"{doctype} {name}")
		raise
	except Exception:
		_log_failure("send_reminder", f"{doctype} {name}")
		raise

	cache.clear(user)
	return result
