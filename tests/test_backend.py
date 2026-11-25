"""Integration and unit tests for Small Business Leads segmentation and intelligence features."""
import os
import unittest
from fastapi.testclient import TestClient

# Ensure test access
os.environ["ACCESS_KEY"] = "test_key"
os.environ["VALIDATE_TWILIO_SIGNATURE"] = "false"

from app.main import app, db, init_db
from app.qualification import qualify_lead


class TestSmallBusinessIntelligence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)
        cls.headers = {"Cookie": "dialer_key=test_key"}

    def test_01_health_check(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("db", data)

    def test_02_segment_isolation_existing_leads(self):
        """Verify medium_large leads are retrieved and not mixed with small_business."""
        res = self.client.get("/api/leads?segment=medium_large", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        leads = res.json()
        self.assertIsInstance(leads, list)
        for l in leads:
            self.assertEqual(l.get("segment"), "medium_large")

    def test_03_create_small_business_lead_with_qualification(self):
        """Verify adding a small business lead automatically computes score and AI opportunity."""
        payload = {
            "name": "Lone Star Express Plumbing",
            "phone": "(214) 555-0199",
            "city": "Dallas",
            "state": "TX",
            "category": "Emergency Plumbing",
            "notes": "Family-run emergency plumber, 24/7 service, 3 trucks",
            "website": "https://lonestarplumbing.example.com",
            "owner_name": "Dave Miller",
            "employee_estimate": "1-5",
            "is_emergency": 1,
            "is_after_hours": 1,
            "google_rating": 4.9,
            "review_count": 85,
            "segment": "small_business"
        }
        res = self.client.post("/api/leads", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        created = res.json()
        self.assertEqual(created["segment"], "small_business")
        self.assertEqual(created["phone"], "+12145550199")
        self.assertGreaterEqual(created["lead_score"], 80)
        self.assertEqual(created["ai_opportunity"], "High potential")
        self.assertIn("Emergency", created["ai_reasoning"])
        self.assertEqual(created["campaign"], "Small Business HVAC & Plumbing")

        # Verify it shows up under small_business but NOT under medium_large
        sb_res = self.client.get("/api/leads?segment=small_business", headers=self.headers)
        self.assertTrue(any(l["phone"] == "+12145550199" for l in sb_res.json()))

        ml_res = self.client.get("/api/leads?segment=medium_large", headers=self.headers)
        self.assertFalse(any(l["phone"] == "+12145550199" for l in ml_res.json()))

    def test_04_small_business_stats_kpis(self):
        """Verify summary stats for small business leads."""
        res = self.client.get("/api/leads/stats?segment=small_business", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        stats = res.json()
        self.assertEqual(stats["segment"], "small_business")
        self.assertIn("total_leads", stats)
        self.assertIn("high_priority_leads", stats)
        self.assertIn("high_opportunity_leads", stats)
        self.assertIn("not_called_leads", stats)
        self.assertGreaterEqual(stats["total_leads"], 1)

    def test_05_csv_import_preview_mode(self):
        """Verify CSV import preview mode evaluates rows without inserting."""
        csv_content = (
            "Business Name,Phone Number,City,State,Trade,Notes,Owner\n"
            "Austin Air Pros,(512) 555-0144,Austin,TX,HVAC,Independent AC repair 24/7,Mark Bell\n"
            "Central TX Drains,(512) 555-0188,Austin,TX,Plumbing,Small local crew,\n"
        )
        files = {"file": ("test_leads.csv", csv_content.encode("utf-8"), "text/csv")}
        res = self.client.post("/api/leads/import", files=files, data={"segment": "small_business", "preview": "true"}, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("preview"))
        self.assertEqual(data.get("valid_new_leads"), 2)
        self.assertEqual(len(data.get("sample_leads")), 2)

        # Confirm they were NOT inserted yet
        sb_res = self.client.get("/api/leads?segment=small_business&q=Austin+Air+Pros", headers=self.headers)
        self.assertEqual(len(sb_res.json()), 0)

    def test_06_csv_import_commit(self):
        """Verify CSV import inserts leads with small_business segment."""
        csv_content = (
            "Business Name,Phone Number,City,State,Trade,Notes,Owner\n"
            "Austin Air Pros,(512) 555-0144,Austin,TX,HVAC,Independent AC repair 24/7,Mark Bell\n"
        )
        files = {"file": ("test_leads.csv", csv_content.encode("utf-8"), "text/csv")}
        res = self.client.post("/api/leads/import", files=files, data={"segment": "small_business", "preview": "false"}, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("added"), 1)

        # Confirm it exists now
        sb_res = self.client.get("/api/leads?segment=small_business&q=Austin+Air+Pros", headers=self.headers)
        self.assertEqual(len(sb_res.json()), 1)
        lead = sb_res.json()[0]
        self.assertEqual(lead["segment"], "small_business")
        self.assertEqual(lead["owner_name"], "Mark Bell")
        self.assertEqual(lead["campaign"], "Small Business HVAC & Plumbing")

    def test_07_bulk_actions(self):
        """Verify bulk verification and status updates."""
        # Find the lead we inserted
        with db() as c:
            row = c.one("SELECT id FROM leads WHERE phone = '+15125550144'")
            lead_id = row["id"]

        # Bulk verify
        res = self.client.post("/api/leads/bulk-action", json={"action": "verify", "lead_ids": [lead_id]}, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["updated"], 1)

        # Bulk status update
        res = self.client.post("/api/leads/bulk-action", json={"action": "status", "lead_ids": [lead_id], "status": "interested"}, headers=self.headers)
        self.assertEqual(res.status_code, 200)

        # Verify status changed
        with db() as c:
            row = c.one("SELECT status, verified_at FROM leads WHERE id = ?", (lead_id,))
            self.assertEqual(row["status"], "interested")
            self.assertTrue(bool(row["verified_at"]))

    def test_08_export_leads_csv(self):
        """Verify CSV export contains segment and intelligence columns."""
        res = self.client.get("/api/leads/export?segment=small_business", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("content-type", ""))
        content = res.text
        self.assertIn("segment", content)
        self.assertIn("lead_score", content)
        self.assertIn("ai_opportunity", content)
        self.assertIn("employee_estimate", content)
        self.assertIn("Lone Star Express Plumbing", content)

    def test_09_discovery_job_api(self):
        """Verify discovery job launch and tracking."""
        payload = {
            "trade": "Plumbing",
            "state": "AZ",
            "cities": "Phoenix",
            "max_leads": 5,
            "min_score": 50,
            "min_opportunity": "Low potential"
        }
        res = self.client.post("/api/discovery/start", json=payload, headers=self.headers)
        self.assertEqual(res.status_code, 200)
        job = res.json()
        job_id = job["job_id"]
        self.assertTrue(bool(job_id))

        # Check job status
        poll = self.client.get(f"/api/discovery/jobs/{job_id}", headers=self.headers)
        self.assertEqual(poll.status_code, 200)
        self.assertIn(poll.json()["status"], ("running", "completed"))

    def test_10_cleanup_test_leads(self):
        """Clean up the test leads added in tests."""
        test_phones = ("+12145550199", "+15125550144")
        with db() as c:
            for p in test_phones:
                c.run("DELETE FROM leads WHERE phone = ?", (p,))


if __name__ == "__main__":
    unittest.main()
