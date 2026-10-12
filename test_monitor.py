"""构造数据仅检查安全逻辑，不作为真实库存证明。"""
import copy
import unittest
import monitor as m

def ok(content):
    return {'status':'SUCCESS','code':1,'content':content}

def listing(name='特斯拉Model 3', inventory=1):
    return ok({'deptHangModels':[{'deptId':88783,'deptName':m.STATION,'cityId':87,'models':[
        {'modelId':101,'modelName':name,'inventoryType':inventory,'dailyPrice':'200','packagePrice':'200'}]}]})

def query():
    return {'pickupCityId':'87','returnCityId':'87','pickupDeptId':'88783','pickupTime':m.PICKUP,
            'returnTime':m.RETURN,'entrance':1,'holidaysWaitingFlag':0}

class SafetyTests(unittest.TestCase):
    def test_origin_and_text_only(self):
        for url in ['https://zuche.com.evil.test/','http://m.zuche.com/','https://user:pass@m.zuche.com/','https://m.zuche.com:8443/']:
            self.assertFalse(m.official(url))
        self.assertTrue(m.official(m.HOME))
        self.assertEqual(m.assess('特斯拉 ¥200 车型详情',m.HOME)['state'],'unknown')

    def test_unique_city(self):
        city={'cityId':'87','cityName':m.CITY}
        self.assertEqual(m.resolve_city(ok({'allCities':[city]})),'87')
        with self.assertRaises(m.SetupError):m.resolve_city(ok({'allCities':[city,city]}))

    def test_station_and_opening(self):
        dept={'deptId':88783,'deptName':m.STATION,'deptAddress':'黑龙江省大庆市龙凤区东站西停车场','inWorkFlag':True}
        data=ok({'districtList':[{'deptList':[dept]}]})
        self.assertEqual(m.resolve_station(data,'88783'),'88783')
        for key,value in [('deptName','大庆西站'),('inWorkFlag',False),('deptAddress','北京市')]:
            wrong=copy.deepcopy(data);wrong['content']['districtList'][0]['deptList'][0][key]=value
            with self.assertRaises(m.SetupError):m.resolve_station(wrong,'88783')
        with self.assertRaises(m.SetupError):m.resolve_station(data,'88755')

    def test_request_conditions(self):
        for key,value in [('pickupTime','2025-10-17 08:00'),('returnTime','2026-10-18 09:00'),
                          ('pickupDeptId','88755'),('returnCityId','1'),('holidaysWaitingFlag',1)]:
            q=query();q[key]=value
            with self.assertRaises(m.SetupError):m.assess_listing(listing(),'87','88783',q)

    def test_neighbor_station_and_server_failure(self):
        for key,value in [('deptId',88755),('deptName','盛景商城自助点'),('cityId',1)]:
            data=listing();data['content']['deptHangModels'][0][key]=value
            with self.assertRaises(m.SetupError):m.assess_listing(data,'87','88783',query())
        data=listing();data['status']='ERROR'
        with self.assertRaises(m.SetupError):m.assess_listing(data,'87','88783',query())

    def test_active_targets(self):
        for name,label in [('特斯拉Model 3','特斯拉'),('东风日产N7','日产N7')]:
            r=m.assess_listing(listing(name),'87','88783',query())
            self.assertEqual(r['state'],'possible');self.assertEqual(r['matches'],[label])

    def test_waitlist(self):
        for inventory in (2,3):
            r=m.assess_listing(listing(inventory=inventory),'87','88783',query())
            self.assertEqual(r['state'],'notfound');self.assertFalse(r['matches'])

    def test_bad_inventory_and_price(self):
        for inventory in (None,4,True):
            with self.assertRaises(m.SetupError):m.assess_listing(listing(inventory=inventory),'87','88783',query())
        for price in ('0','-1','NaN','bad'):
            data=listing();data['content']['deptHangModels'][0]['models'][0]['dailyPrice']=price
            with self.assertRaises(m.SetupError):m.assess_listing(data,'87','88783',query())

    def test_model_name_only_and_duplicate(self):
        data=listing('大众朗逸');data['content']['deptHangModels'][0]['models'][0]['modelDesc']='特斯拉 日产N7'
        self.assertEqual(m.assess_listing(data,'87','88783',query())['state'],'notfound')
        self.assertFalse(m.target_model('N7'))
        data=listing();models=data['content']['deptHangModels'][0]['models'];models.append(copy.deepcopy(models[0]))
        with self.assertRaises(m.SetupError):m.assess_listing(data,'87','88783',query())

    def test_unknown_preserves_dedup(self):
        state,fresh=m.transition({'present_matches':['特斯拉'],'last_status':'possible'},
                                 {'state':'unknown','matches':[],'why':'网络异常'})
        self.assertEqual(state['present_matches'],['特斯拉']);self.assertFalse(fresh)
        self.assertEqual(state['consecutive_unknown'],1);self.assertIn('last_checked_utc',state)

    def test_disappear_and_reappear(self):
        state,fresh=m.transition({'present_matches':['特斯拉'],'last_status':'possible'},
                                 {'state':'notfound','matches':[],'why':'已核验'})
        self.assertFalse(fresh)
        state,fresh=m.transition(state,{'state':'possible','matches':['特斯拉'],'why':'已核验'})
        self.assertEqual(fresh,['特斯拉'])
        state,fresh=m.transition(state,{'state':'possible','matches':['特斯拉'],'why':'已核验'})
        self.assertFalse(fresh)

if __name__=='__main__':unittest.main()
