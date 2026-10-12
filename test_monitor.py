import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import monitor as m

class SafetyTests(unittest.TestCase):
    def test_official_origin(self):
        for url in ['https://zuche.com.evil.test/#/rent/list','http://m.zuche.com/#/rent/list','https://user:pass@m.zuche.com/#/rent/list']:
            self.assertFalse(m.official(url))
        self.assertTrue(m.official('https://m.zuche.com/#/rent/list'))

    def test_wrong_context_cannot_notify(self):
        for text in ['大庆西站高铁自助点 10月17日08:00—10月18日08:00 特斯拉 ￥199 车型详情',
                     '大庆东站服务点 10月17日09:00—10月18日08:00 特斯拉 ￥199 车型详情',
                     '大庆东站服务点 10月17日08:00—10月18日08:00 验证码 特斯拉 ￥199 车型详情']:
            self.assertEqual(m.assess(text, 'https://m.zuche.com/#/rent/list')['state'], 'unknown')

    def test_text_only_is_not_stock_proof(self):
        text='大庆东站服务点 10月17日08:00—10月18日08:00 特斯拉 ￥199 车型详情'
        self.assertEqual(m.assess(text, 'https://m.zuche.com/#/rent/list')['state'], 'unknown')

    def test_unknown_preserves_notification_dedup(self):
        old={'present_matches':['特斯拉'], 'last_status':'possible'}
        state, fresh=m.transition(old, {'state':'unknown', 'matches':[], 'why':'网络异常'})
        self.assertEqual(state['present_matches'], ['特斯拉'])
        self.assertFalse(fresh)
        self.assertIn('last_checked_utc', state)
        self.assertEqual(state['consecutive_unknown'], 1)

    def test_disappearance_then_reappearance(self):
        old={'present_matches':['特斯拉'], 'last_status':'possible'}
        state, fresh=m.transition(old, {'state':'notfound','matches':[], 'why':'已验证列表'})
        self.assertFalse(fresh)
        self.assertEqual(state['present_matches'], [])
        state, fresh=m.transition(state, {'state':'possible','matches':['特斯拉'], 'why':'已验证'})
        self.assertEqual(fresh, ['特斯拉'])
        state, fresh=m.transition(state, {'state':'possible','matches':['特斯拉'], 'why':'已验证'})
        self.assertFalse(fresh)

if __name__=='__main__':
    unittest.main()
