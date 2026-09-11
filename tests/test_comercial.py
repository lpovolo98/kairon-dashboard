import unittest
from comercial.pricing import price,final_price,discounted,validate_campaign,Revisar

class CommercialTests(unittest.TestCase):
    def test_vat_and_internal_tax(self):
        taxes=[{'amount':21,'amount_type':'percent','price_include':False},{'amount':8.7,'amount_type':'percent','price_include':False}]
        self.assertEqual(final_price(100,taxes),129.7)
    def test_included_tax_not_added_twice(self):
        self.assertEqual(final_price(121,[{'amount':21,'amount_type':'percent','price_include':True}]),121)
    def test_compound_tax_blocks(self):
        with self.assertRaises(Revisar):final_price(100,[{'amount':21,'amount_type':'percent','include_base_amount':True}])
    def test_missing_tax_blocks(self):
        with self.assertRaises(Revisar):final_price(100,[])
    def test_reference_discount_amounts(self):
        self.assertEqual(discounted(3250,3),3152.5)
        self.assertEqual(discounted(3250,5),3087.5)
        self.assertEqual(discounted(3250,7),3022.5)
    def test_rules_use_product_before_category(self):
        product={'id':1,'product_tmpl_id':[8,'x'],'categ_id':[4,'c'],'lst_price':100,'standard_price':50}
        rules=[{'id':2,'applied_on':'2_product_category','categ_id':[4,'c'],'min_quantity':0,'compute_price':'fixed','fixed_price':80}, {'id':1,'applied_on':'1_product','product_tmpl_id':[8,'x'],'min_quantity':0,'compute_price':'fixed','fixed_price':90}]
        self.assertEqual(price(product,rules,{},'2026-09-01')[0],90)
        rules[1]['date_end']='2026-08-31 23:59:59'
        self.assertEqual(price(product,rules,{},'2026-09-01')[0],80)
    def test_cost_formula_markup(self):
        p={'id':1,'product_tmpl_id':[8,'x'],'categ_id':[4,'c'],'lst_price':100,'standard_price':50}
        r={'id':1,'applied_on':'3_global','compute_price':'formula','base':'standard_price','price_discount':-33}
        self.assertEqual(price(p,[r],{},'2026-09-01')[0],66.5)
    def test_scales_not_cumulative(self):
        a={'title':'Oferta','products':[1],'tiers':[{'min':1,'discount':10},{'min':3,'discount':15}]}
        c={'name':'Mes','start':'2026-09-01','end':'2026-09-30','actions':[a]}
        self.assertEqual(validate_campaign(c,[{'id':1}]),c)
        a['tiers'][1]['min']=1
        with self.assertRaises(Revisar):validate_campaign(c,[{'id':1}])
    def test_overlap_blocks(self):
        a={'title':'Oferta','products':[1],'tiers':[{'min':1,'discount':10}]}
        with self.assertRaises(Revisar):validate_campaign({'name':'Mes','start':'2026-09-01','end':'2026-09-30','actions':[a,a]},[{'id':1}])
    def test_combo_quantities(self):
        a={'title':'Combo','kind':'combo','products':[1,2],'quantities':{'1':12},'gifts':{'2':6}}
        c={'name':'Mes','start':'2026-09-01','end':'2026-09-30','actions':[a]}
        validate_campaign(c,[{'id':1},{'id':2}]);a['gifts']['2']=0
        with self.assertRaises(Revisar):validate_campaign(c,[{'id':1},{'id':2}])

if __name__=='__main__':unittest.main()
