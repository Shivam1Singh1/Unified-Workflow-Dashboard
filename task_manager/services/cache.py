# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Per-user cache for the assembled Task Manager payload.

One key per user, never a shared one. Frappe prepends the site name to every
key it writes, so two sites on this bench cannot collide either.

The payload is expensive to build -- a cold assembly costs seconds on this
site, not milliseconds -- so the TTL is what keeps the page usable. It is also
why `clear` exists: a user's own action must be reflected at once rather than
up to TTL seconds later.
"""

import frappe

NAMESPACE = "task_manager"

# Long enough that a user clicking between tabs never pays for a rebuild,
# short enough that somebody else's approval shows up without a hard refresh.
TTL_SECONDS = 120


def _key(user: str) -> str:
	return f"{NAMESPACE}:{user}"


def get(user: str):
	if not user:
		return None
	return frappe.cache.get_value(_key(user))


def set(user: str, data, ttl: int = TTL_SECONDS):
	if not user:
		return
	frappe.cache.set_value(_key(user), data, expires_in_sec=ttl)


def clear(user: str):
	if not user:
		return
	frappe.cache.delete_value(_key(user))


def clear_for_user(doc, method=None):
	"""Drop a user`s payload the moment their access changes.

	Hooked on `User` and on `User Permission`. Both decide what this page is
	allowed to show, and neither has anything to do with the documents the
	`on_update` hooks in hooks.py watch -- so without this the page keeps
	serving the old answer for up to TTL_SECONDS after a change.

	That window is the whole point. Every tab is rebuilt from `get_list` and
	`frappe.has_permission` against the live session, so the *content* is
	correct the moment it is rebuilt; what is not correct is a cached payload
	assembled while the user still held a role they have since lost. Two
	minutes of showing somebody rows they may no longer open is two minutes
	too many when the reason they lost the role is that they should not be
	looking.

	Roles live in a child table of `User`, so removing one saves the parent
	and fires `on_update` here. Disabling a user does the same.

	Deliberately narrow: it clears exactly one key, this app`s own, for the
	one user whose access changed. It never touches Frappe`s caches -- core
	already clears those on the same events -- and it never walks every user.
	"""
	user = None

	if doc.doctype == "User":
		user = doc.name
	elif doc.doctype == "User Permission":
		user = doc.get("user")

	if user:
		clear(user)


# NOT hooked, and the reason is cost. Editing a `Role` or a `Custom DocPerm`
# changes what every holder of that role may see, which would mean walking
# every user and clearing each key -- on a site with 719 enabled users, on an
# event that fires while somebody is editing a permission grid.
#
# The exposure is bounded by TTL_SECONDS and nothing else: a role whose
# permissions are narrowed takes up to two minutes to be reflected on this
# page for people who already have a payload cached. Narrow TTL_SECONDS if
# that is not acceptable; the page rebuilds in well under a second.
