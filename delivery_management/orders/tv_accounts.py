"""TV (Xtream) accounts for POS sales.

A POS sale, Invoice or Order, gets one account per TV unit (stock UOM). An item is a
TV when it is one of the TV Items of Delivery Settings, or when its Item Group is one
of the TV Item Groups (or under one) and it is not an Excluded Item. Desk orders get
no accounts: only the POS submit (``orders.service``) calls ``add_to_pos_sale``.

The accounts live in the external accounts database (table ``lines``), not in ERPNext.
Usernames continue the member's ``ERP-<n>`` series, which the website orders also use,
so the next number is read from that database. The password is the customer's mobile.
The credentials go in ``posa_notes``, so they show on the invoice and the order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import cint

from delivery_management.slots.cart import _group_range, stock_qty
from delivery_management.slots.settings import get_settings

USERNAME_PREFIX = "ERP-"

_INSERT = """insert into `lines` (
	member_id, username, password, exp_date, admin_enabled, enabled, admin_notes, reseller_notes,
	bouquet, allowed_outputs, max_connections, is_restreamer, allowed_ips, allowed_ua, is_trial,
	created_at, created_by, is_mag, force_server_id, is_isplock, pin, pool
) values (%s, %s, %s, %s, 1, 1, '', '', '[2]', '[1,2]', 1, 0, '', '', 0, %s, 1, 0, 0, 0, '', '')"""


@dataclass(frozen=True)
class AccountsDB:
	host: str
	port: int
	database: str
	user: str
	password: str
	member_id: int
	valid_days: int

	def connect(self):
		import pymysql

		return pymysql.connect(host=self.host, port=self.port, user=self.user, password=self.password,
			database=self.database, autocommit=False, connect_timeout=10)


def get_accounts_db() -> AccountsDB | None:
	"""The accounts database, or None when Delivery Settings has no host (no accounts made)."""
	doc = frappe.get_cached_doc("Delivery Settings")
	if not doc.get("tv_account_host"):
		return None
	return AccountsDB(
		host=doc.tv_account_host,
		port=cint(doc.tv_account_port) or 3306,
		database=doc.tv_account_database,
		user=doc.tv_account_user,
		password=doc.get_password("tv_account_password", raise_exception=False) or "",
		member_id=cint(doc.tv_account_member_id),
		valid_days=cint(doc.tv_account_valid_days) or 365,
	)


def get_tv_item_codes(item_codes) -> set[str]:
	"""The subset of ``item_codes`` that are TVs."""
	settings = get_settings()
	codes = {c for c in item_codes if c}
	listed = codes & set(settings.tv_items)
	rest = codes - listed - set(settings.tv_excluded_items)
	roots = [r for r in (_group_range(g) for g in settings.tv_item_groups) if r]
	if not rest or not roots:
		return listed
	rows = frappe.db.sql(
		"""select i.name, g.lft, g.rgt
		from `tabItem` i join `tabItem Group` g on g.name = i.item_group
		where i.name in %(codes)s""",
		{"codes": tuple(rest)},
		as_dict=True,
	)
	return listed | {r.name for r in rows if any(r.lft >= lft and r.rgt <= rgt for lft, rgt in roots)}


def tv_units(items) -> int:
	codes = get_tv_item_codes([row.get("item_code") for row in items])
	return sum(cint(stock_qty(row)) for row in items if row.get("item_code") in codes)


def add_to_pos_sale(invoice: dict) -> None:
	"""Create the accounts for the TV units of a POS sale and append them to its notes.

	Called just before the ERPNext documents are made. If the sale then fails, the
	accounts are deleted again. If the accounts database is down, the sale goes on
	without accounts (as in the old system) and an Error Log says why.
	"""
	db = get_accounts_db()
	if not db:
		return
	units = tv_units(invoice.get("items") or [])
	if not units:
		return
	password = invoice.get("contact_mobile") or frappe.db.get_value("Customer", invoice.get("customer"), "mobile_no")
	if not password:
		frappe.log_error(_("TV accounts not created"),
			_("Customer {0} has no mobile number, which is the account password.").format(invoice.get("customer")))
		return
	try:
		usernames = _insert_accounts(db, units, password)
	except Exception:
		frappe.log_error(_("TV accounts not created"), reference_doctype="Customer",
			reference_name=invoice.get("customer"))
		return
	frappe.db.after_rollback.add(lambda: _delete_accounts(db, usernames))
	credentials = [f"Username ({n}): {u}\nPassword ({n}): {password}" for n, u in enumerate(usernames, 1)]
	invoice["posa_notes"] = "\n".join(filter(None, [invoice.get("posa_notes"), *credentials]))


def _insert_accounts(db: AccountsDB, units: int, password: str) -> list[str]:
	conn = db.connect()
	try:
		with conn.cursor() as cursor:
			cursor.execute(
				"select username from `lines` where member_id = %s and username like %s order by id desc limit 1 for update",
				(db.member_id, USERNAME_PREFIX + "%"),
			)
			row = cursor.fetchone()
			last = cint(row[0][len(USERNAME_PREFIX):]) if row else 0
			usernames = [f"{USERNAME_PREFIX}{str(last + n).zfill(2)}" for n in range(1, units + 1)]
			now = int(time.time())
			expires = now + db.valid_days * 86400
			cursor.executemany(_INSERT, [(db.member_id, u, password, expires, now) for u in usernames])
		conn.commit()
		return usernames
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


def _delete_accounts(db: AccountsDB, usernames: list[str]) -> None:
	"""The sale failed after the accounts were made: remove them."""
	try:
		conn = db.connect()
		try:
			with conn.cursor() as cursor:
				cursor.execute("delete from `lines` where member_id = %s and username in %s",
					(db.member_id, tuple(usernames)))
			conn.commit()
		finally:
			conn.close()
	except Exception:
		frappe.log_error(_("TV accounts of a failed sale not deleted: {0}").format(", ".join(usernames)))
