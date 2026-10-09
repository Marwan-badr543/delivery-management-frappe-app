# Manual concurrency check (two real DB connections). Not part of run-tests.
# Usage (from the bench sites dir): ../env/bin/python ../apps/delivery_management/delivery_management/tests/race_check.py [slot|ac]
"""Two real DB connections race for the LAST car of a slot. Exactly one must win.
Creates its own _DM Race data and deletes it at the end."""
import datetime, os, sys, threading, time
os.chdir("/home/marwan/frappe/frappe-bench-v14/sites")
import frappe

SITE = "v14.local"; AREA = "_DM Race Area"
DATE = datetime.date(2031, 3, 4); TIME = "10:00"
NOW = datetime.datetime(2031, 3, 3, 9, 0)
results = {}
MODE = sys.argv[1] if len(sys.argv) > 1 else "slot"
barrier = threading.Barrier(2)

def setup():
    frappe.init(site=SITE); frappe.connect(); frappe.set_user("Administrator")
    cleanup(commit=False)
    frappe.get_doc({"doctype": "Delivery Area", "area_name": AREA, "cars_number": 1}).insert()
    from delivery_management.slots import set_ac_day_capacity
    set_ac_day_capacity(DATE, 3)  # room for one 2-unit order, not two
    frappe.get_doc({"doctype": "Delivery Work Hour", "area": AREA, "hour": TIME}).insert()
    refs = [frappe.get_doc({"doctype": "ToDo", "description": f"_DM race {i}"}).insert().name for i in range(2)]
    frappe.db.commit(); frappe.destroy()
    return refs

def cleanup(commit=True):
    for dt in ("Delivery Slot Reservation", "Delivery Disabled Slot", "Delivery Slot Counter", "Delivery Work Hour"):
        frappe.db.delete(dt, {"area": AREA})
    frappe.db.delete("Delivery Area", {"name": AREA})
    frappe.db.delete("Delivery Slot Reservation", {"delivery_date": DATE, "slot_type": "AC"})
    frappe.db.delete("AC Day Capacity", {"delivery_date": DATE})
    frappe.db.delete("ToDo", {"description": ["like", "_DM race%"]})
    if commit: frappe.db.commit()

def worker(label, ref, hold):
    frappe.init(site=SITE); frappe.connect(); frappe.set_user("Administrator")
    from delivery_management.slots import reserve
    try:
        barrier.wait()
        reserve("ToDo", ref, area=AREA, delivery_date=DATE, delivery_time=TIME, now=NOW) if MODE == "slot" else reserve("ToDo", ref, ac_date=DATE, ac_units=2, now=NOW)
        time.sleep(hold)  # keep the lock while the other one tries
        frappe.db.commit(); results[label] = "WON"
    except Exception as e:
        frappe.db.rollback(); results[label] = f"LOST: {type(e).__name__}: {e}"
    finally:
        frappe.destroy()

for round_ in range(3):
    refs = setup()
    t = [threading.Thread(target=worker, args=(f"A{round_}", refs[0], 1.0)),
         threading.Thread(target=worker, args=(f"B{round_}", refs[1], 1.0))]
    [x.start() for x in t]; [x.join() for x in t]
    frappe.init(site=SITE); frappe.connect()
    count = frappe.db.get_value("Delivery Slot Counter", {"area": AREA}, "reserved_count")
    ledger = frappe.db.count("Delivery Slot Reservation", {"area": AREA, "status": "Reserved"})
    blocked = frappe.db.count("Delivery Disabled Slot", {"area": AREA, "source": "Capacity"})
    if MODE == "ac":
        count = frappe.db.get_value("AC Day Capacity", {"delivery_date": DATE}, "reserved_units")
        ledger = frappe.db.count("Delivery Slot Reservation", {"delivery_date": DATE, "slot_type": "AC", "status": "Reserved"})
        blocked = 1; count = 1 if count == 2 else count
    print(f"round {round_}: {results}  counter={count} ledger={ledger} capacity_blocks={blocked}")
    ok = count == 1 and ledger == 1 and blocked == 1 and sum(v == "WON" for k, v in results.items() if k.endswith(str(round_))) == 1
    print("  ->", "PASS" if ok else "FAIL")
    cleanup(); frappe.destroy()
