import tempfile
import unittest
from datetime import datetime, timedelta

from orders_store import (
    TEHRAN,
    ScheduledOrderStore,
    allocate_batch_quantities,
    build_order_plan,
    calendar_data,
    format_jalali_datetime,
    gregorian_to_jalali,
    jalali_to_gregorian,
    parse_jalali_datetime,
)


class JalaliCalendarTests(unittest.TestCase):
    def test_jalali_gregorian_round_trip(self):
        gregorian = jalali_to_gregorian(1405, 8, 20)
        self.assertEqual(gregorian, (2026, 11, 11))
        self.assertEqual(gregorian_to_jalali(*gregorian), (1405, 8, 20))

    def test_persian_digits_and_milliseconds_are_supported(self):
        value = parse_jalali_datetime("۱۴۰۵/۰۸/۲۱", "۰۹:۱۵:۳۰.۱۲۵")
        self.assertEqual(value.microsecond, 125000)
        self.assertEqual(format_jalali_datetime(value), "1405/08/21 09:15:30.125")

    def test_invalid_jalali_day_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_jalali_datetime("1405/12/30", "10:00")

    def test_calendar_starts_on_saturday_column(self):
        result = calendar_data(1405, 8)
        self.assertEqual(result["month_name"], "آبان")
        self.assertGreaterEqual(result["first_weekday"], 0)
        self.assertLess(result["first_weekday"], 7)


class BatchPlannerTests(unittest.TestCase):
    def test_builds_millisecond_slots_before_target(self):
        plan = build_order_plan(10_000, 1_000, 300)
        self.assertEqual(plan["count"], 4)
        self.assertEqual([s["milliseconds"] for s in plan["slots"]], [9000, 9300, 9600, 9900])

    def test_quantity_split_distributes_remainder_to_first_slots(self):
        self.assertEqual(allocate_batch_quantities(11, 4, "total"), [3, 3, 3, 2])
        self.assertEqual(allocate_batch_quantities(2, 3, "each"), [2, 2, 2])

    def test_batch_limit_is_enforced(self):
        with self.assertRaises(ValueError):
            build_order_plan(20_000, 10_000, 1)

    def test_batch_interval_accepts_minimum_and_maximum(self):
        self.assertEqual(build_order_plan(10_000, 1, 1)["count"], 1)
        self.assertEqual(build_order_plan(10_000, 1_000, 1_000)["count"], 1)

    def test_batch_interval_rejects_zero_and_values_above_1000(self):
        for interval in (0, 1001):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    build_order_plan(10_000, 1_000, interval)


class ScheduledOrderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScheduledOrderStore(self.temp.name + "/orders.json")
        self.now = datetime(2026, 11, 11, 9, 0, tzinfo=TEHRAN)

    def tearDown(self):
        self.temp.cleanup()

    def fields(self):
        return {
            "symbol": "خودرو",
            "isin": "IRO1IKCO0001",
            "side": "buy",
            "price": "12500",
            "quantity": "100",
            "execute_at_date": "1405/08/21",
            "execute_time": "09:01:00",
        }

    def test_order_is_persisted_and_becomes_ready_when_due(self):
        order = self.store.create_order(self.fields(), now=self.now)
        self.assertEqual(order["status"], "scheduled")
        reloaded = ScheduledOrderStore(self.temp.name + "/orders.json")
        self.assertEqual(len(reloaded.list_orders("active")), 1)
        due_now = datetime.fromisoformat(order["execute_at"]) + timedelta(milliseconds=1)
        self.assertEqual(reloaded.mark_due(due_now), 1)
        self.assertEqual(reloaded.list_orders("active")[0]["status"], "ready")

    def test_manual_confirmation_moves_order_to_history(self):
        order = self.store.create_order(self.fields(), now=self.now)
        due_now = datetime.fromisoformat(order["execute_at"]) + timedelta(seconds=1)
        self.store.mark_due(due_now)
        self.store.confirm_manual(order["id"])
        self.assertEqual(self.store.list_orders("history")[0]["status"], "manual_done")
        self.assertEqual(self.store.clear_history(), 1)

    def test_batch_creation_persists_each_slot(self):
        fields = self.fields()
        fields.update({
            "execute_time": "09:01:00",
            "batch_lead_seconds": "1",
            "batch_interval_ms": "250",
            "batch_quantity_mode": "total",
        })
        result = self.store.create_batch(fields, now=self.now)
        self.assertEqual(result["count"], 4)
        self.assertEqual(len(self.store.list_orders("active")), 4)

    def test_past_schedule_is_rejected(self):
        fields = self.fields()
        fields["execute_at_date"] = "1405/08/19"
        with self.assertRaises(ValueError):
            self.store.create_order(fields, now=self.now)


    def test_batch_auto_orders_are_persisted_for_one_shot_submission(self):
        fields = self.fields()
        fields["execution_mode"] = "auto_order"
        fields.update({
            "batch_lead_seconds": "1",
            "batch_interval_ms": "250",
            "batch_quantity_mode": "total",
        })
        result = self.store.create_batch(fields, now=self.now)
        self.assertEqual(result["count"], 4)
        active = self.store.list_orders("active")
        self.assertEqual(len(active), 4)
        self.assertEqual({row["status"] for row in active}, {"auto_scheduled"})
        self.assertEqual({row["execution_mode"] for row in active}, {"auto_order"})
        self.assertEqual([row["quantity"] for row in active], [25, 25, 25, 25])
        first_due = datetime.fromisoformat(active[0]["execute_at"]) + timedelta(milliseconds=1)
        claimed = self.store.claim_due_auto_orders(first_due, limit=1)
        self.assertEqual([row["id"] for row in claimed], [active[0]["id"]])
        self.store.complete_auto_order(active[0]["id"], {"ok": True, "status": 200})
        self.assertEqual(self.store.list_orders("history")[0]["status"], "submitted")

    def test_auto_batch_supports_buy_and_sell(self):
        for side in ("buy", "sell"):
            fields = self.fields()
            fields.update({
                "side": side, "execution_mode": "auto_order",
                "batch_lead_seconds": "1", "batch_interval_ms": "500",
                "batch_quantity_mode": "each",
            })
            result = self.store.create_batch(fields, now=self.now)
            self.assertEqual(result["count"], 2)
        active = self.store.list_orders("active")
        self.assertEqual(sum(row["side"] == "buy" for row in active), 2)
        self.assertEqual(sum(row["side"] == "sell" for row in active), 2)

    def test_invalid_isin_is_rejected_for_auto_batch(self):
        fields = self.fields()
        fields["execution_mode"] = "auto_order"
        fields["isin"] = ""
        with self.assertRaises(ValueError):
            self.store.create_batch(fields, now=self.now)


if __name__ == "__main__":
    unittest.main()
