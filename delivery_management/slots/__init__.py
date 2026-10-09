"""Public API of the delivery slot engine.

Import from here, not from the submodules::

    from delivery_management.slots import reserve, release, profile_cart

    profile = profile_cart(sales_order.items)
    reserve("Sales Order", so.name,
            area="Jidhafs", delivery_date="2026-10-08", delivery_time="16:00",
            ac_date="2026-10-09", ac_units=profile.ac_units)
    ...
    release("Sales Order", so.name)  # idempotent
"""

from delivery_management.slots.cart import CartProfile, classify_items, get_ac_item_codes, is_ac_item, profile_cart
from delivery_management.slots.engine import (
	SlotUnavailableError,
	get_ac_calendar,
	get_ac_day_status,
	get_delivery_slots,
	get_reservations,
	move_reservation,
	rebuild_from_ledger,
	release,
	release_reservation,
	reschedule,
	reserve,
	resync_ac_days,
	resync_area,
	set_ac_day_capacity,
	set_ac_day_manual_block,
	set_slot_manual_block,
)
