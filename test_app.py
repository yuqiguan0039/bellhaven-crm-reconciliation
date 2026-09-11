import unittest
from app import norm_street, care_value, match_score, requires_chow

class Tests(unittest.TestCase):
    def test_street_normalization(self): self.assertEqual(norm_street("1120 West Main Street"),norm_street("1120 W Main St"))
    def test_care_mapping(self): self.assertEqual(care_value(["Memory Support"]),"Memory Care")
    def test_address_dominates(self):
        w={"street":"210 Orchard Lane","city":"Maplewood","state":"OH","zip":"44280","name":"Bellhaven of Maplewood","phone":"(614) 250-9447"}
        a={"billing_street":"210 Orchard Ln","billing_city":"Maplewood","billing_state":"OH","billing_zip":"44280","name":"Old Maplewood","phone":"614-250-9447"}
        self.assertGreater(match_score(w,a)[0],.9)
    def test_chow_rule_boundary(self):
        self.assertTrue(requires_chow({"lifetime_revenue":84000,"outstanding_ar":12400}))
        self.assertFalse(requires_chow({"lifetime_revenue":47000,"outstanding_ar":0}))
if __name__=="__main__": unittest.main()
