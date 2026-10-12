"""神州租车网页关键词云端监控：公开网页可见文字 + Bark 推送。"""
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).with_name('state.json')


def norm(s):
    return re.sub(r'[\s\u00a0\u200b·・.。_\-—()（）\[\]【】/]', '', str(s or '')).lower()


def terms(s):
    return list(dict.fromkeys(t.strip() for t in re.split(r'[,，;；|\n]', s or '') if t.strip()))[:12]


def official(url):
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme == 'https' and not p.username and not p.password and (host == 'zuche.com' or host.endswith('.zuche.com'))
    except ValueError:
        return False


def scan(content, url, names, marker='', verifications=()):
    if not official(url):
        return {'state':'unknown', 'matches':[], 'why':'已跳出神州租车官网'}
    text = (content or '')[:280000]
    if len(text.strip()) < 40:
        return {'state':'unknown', 'matches':[], 'why':'页面尚未完成加载'}
    if re.search(r'安全验证|滑动验证|验证码|访问频繁|操作过于频繁|异常访问|请先登录|重新登录|登录后继续|系统繁忙', text[:3500]):
        return {'state':'unknown', 'matches':[], 'why':'页面要求登录、安全验证或暂不可访问'}
    n = norm(text)
    if marker and norm(marker) not in n:
        return {'state':'unknown', 'matches':[], 'why':'选车页校验文字不符'}
    if any(norm(t) not in n for t in verifications):
        return {'state':'unknown', 'matches':[], 'why':'城市、日期等必要条件不符'}
    home = ('热门车型推荐' in text and re.search(r'精选神州用户推荐|自由出发\s*自在神州', text) and not re.search(r'综合排序|价格排序|筛选车型|筛选车辆|车辆列表|车型列表|可租车型|日均价|含税总价', text))
    if home and not marker:
        return {'state':'unknown', 'matches':[], 'why':'当前疑似首页广告推荐'}
    hits = [x for x in names if norm(x) in n]
    if hits:
        return {'state':'possible', 'matches':hits, 'why':'网页可见文字命中：' + '、'.join(hits)}
    if len(text.strip()) < 250 and re.search(r'加载中|正在搜索|请稍候|正在为您查询', text):
        return {'state':'unknown', 'matches':[], 'why':'页面尚在加载中'}
    return {'state':'notfound', 'matches':[], 'why':'未找到关键词'}


def transition(last, result):
    before = last.get('present_matches', [])
    if not isinstance(before, list):
        before = []
    after = result['matches'] if result['state'] == 'possible' else ([] if result['state'] == 'notfound' else before)
    new = [x for x in after if x not in before] if result['state'] == 'possible' else []
    # 结果未变化时不更新状态文件，避免每次巡检都往仓库提交一次记录。
    if after == before and last.get('last_status') == result['state'] and last.get('last_detail') == result['why']:
        return last, new
    return {'present_matches':after, 'last_status':result['state'], 'last_detail':result['why'],
            'last_changed_utc':datetime.now(timezone.utc).isoformat(timespec='seconds')}, new


def retrieve(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        try:
            opt = {'locale':'zh-CN', 'timezone_id':'Asia/Shanghai', 'viewport':{'width':1280,'height':900}}
            s = os.environ.get('LOGIN_STATE_B64', '').strip()
            if s:
                state = json.loads(base64.b64decode(s, validate=True).decode('utf-8'))
                opt['storage_state'] = state
            ctx = browser.new_context(**opt)
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=40000)
            except Exception:
                print('页面加载出现超时，尝试读取已加载内容')
            page.wait_for_timeout(7000)
            text = page.locator('body').inner_text(timeout=10000) if page.locator('body').count() else ''
            if len(text.strip()) < 80:
                page.wait_for_timeout(6000)
                text = page.locator('body').inner_text(timeout=10000) if page.locator('body').count() else ''
            return text, page.url
        finally:
            browser.close()


def send(key, title, body):
    value = key.strip()
    if value.startswith('https://'):
        p = urllib.parse.urlsplit(value)
        if p.hostname != 'api.day.app':
            raise ValueError('只支持 Bark 官方 api.day.app')
        value = next((v for v in p.path.split('/') if v), '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,180}', value):
        raise ValueError('Bark Key 格式不正确')
    data = json.dumps({'device_key':value, 'title':title, 'body':body, 'group':'神州租车监控',
                       'level':'timeSensitive', 'url':'https://m.zuche.com/'}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request('https://api.day.app/push', data=data,
              headers={'Content-Type':'application/json; charset=utf-8'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as r:
        response = json.loads(r.read())
        if r.status != 200 or response.get('code', 200) != 200:
            raise RuntimeError('Bark 推送未被服务器接受')


def main():
    key = os.getenv('BARK_KEY', '').strip()
    if not key:
        print('缺少 Secrets：BARK_KEY')
        return 2
    if os.getenv('MODE') == 'test_push':
        send(key, '神州租车云端推送测试', '已连接云端。这是测试消息，不代表发现车源。')
        print('Bark 测试发送成功（请以 iPhone 实际收取情况为准）')
        return 0
    url = os.getenv('WATCH_URL', '').strip()
    names = terms(os.getenv('KEYWORDS', ''))
    if not official(url) or not names:
        print('WATCH_URL 或 KEYWORDS 未配置正确')
        return 2
    last = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    try:
        text, final_url = retrieve(url)
        result = scan(text, final_url, names, os.getenv('PAGE_MARKER',''), terms(os.getenv('VERIFY_TERMS','')))
    except Exception as err:
        result = {'state':'unknown', 'matches':[], 'why':'浏览器运行失败：' + type(err).__name__}
    print('检查结果：', result['state'], result['why'])
    updated, fresh = transition(last, result)
    if fresh:
        try:
            send(key, '神州租车：发现目标车型', '页面出现「' + '、'.join(fresh) + '」，请打开官网确认实际车源、城市和租期。')
            print('已发送 Bark 命中通知：', '、'.join(fresh))
        except Exception as err:
            print('推送失败：', type(err).__name__)
            updated['present_matches'] = last.get('present_matches', [])
            STATE.write_text(json.dumps(updated,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            return 1
    if result['state'] == 'unknown':
        print('页面异常：保留上次命中状态，不以此作为车源消失证据。')
    STATE.write_text(json.dumps(updated, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return 0

if __name__ == '__main__':
    sys.exit(main())
