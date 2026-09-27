"""Live RLS smoke test. Configure test-only Supabase identities to enable it."""
import os
import unittest
import uuid


@unittest.skipUnless(os.getenv("SUPABASE_TEST_DSN") and os.getenv("RLS_TEST_FARMER_A_UID") and os.getenv("RLS_TEST_FARMER_B_UID"),
                     "Set SUPABASE_TEST_DSN and two test farmer Auth UUIDs after applying the migration")
class SupabaseFarmerRLSTests(unittest.TestCase):
    def test_farmer_reads_own_crop_but_not_another_farmers_crop(self):
        import psycopg
        farmer_a, farmer_b = os.environ["RLS_TEST_FARMER_A_UID"], os.environ["RLS_TEST_FARMER_B_UID"]
        with psycopg.connect(os.environ["SUPABASE_TEST_DSN"]) as conn:
            with conn.transaction():
                ids = {}
                for uid, label in ((farmer_a, "A"), (farmer_b, "B")):
                    user = conn.execute("SELECT id FROM public.users WHERE auth_uid=%s", (uid,)).fetchone()
                    if not user:
                        self.fail("Both test Auth users must have been provisioned in public.users")
                    ids[label] = user[0]
                crop_ids = {}
                for label in ("A", "B"):
                    crop_ids[label] = conn.execute("INSERT INTO public.crops(user_id,name) VALUES(%s,%s) RETURNING id", (ids[label], f"RLS-{uuid.uuid4().hex}")).fetchone()[0]
                conn.execute("SET LOCAL ROLE authenticated")
                conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (f'{{"sub":"{farmer_a}","role":"authenticated"}}',))
                self.assertEqual(conn.execute("SELECT count(*) FROM public.crops WHERE id=%s", (crop_ids["A"],)).fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM public.crops WHERE id=%s", (crop_ids["B"],)).fetchone()[0], 0)
                self.assertEqual(conn.execute("DELETE FROM public.crops WHERE id=%s", (crop_ids["B"],)).rowcount, 0)


if __name__ == "__main__":
    unittest.main()
