"""神州租车车源监控 v4：按官网实际车源接口查询，失败时不推送有车。

仅使用公开查询接口；不登录、不创建订单、不读取或输出账户凭证。
Playwright APIRequestContext 保持同一次查询会话，无需不稳定的城市选择器。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state.json'
HOME = 'https://m.zuche.com/'
CITY = '大庆'
STATION = '大庆东站服务点'
PICKUP = '2026-10-17 08:00'
RETURN = '2026-10-18 08:00'
CITY_API = '/action/carrctapi/order/cityList/v1'
DEPT_API = '/action/carrctapi/order/deptList/v1'
CAR_API = '/resource/carrctapi/order/chooseCar/v3'


class SetupError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def normalize(value):
    return re.sub(r'[\s\u00a0\u200b\-_·・.,，。:：/()（）]', '', str(value or '')).lower()


def official(url):
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme == 'https' and p.hostname == 'm.zuche.com' and not p.username and not p.password and p.port in (None, 443)
    except ValueError:
        return False


def assess(text, url, trusted_steps=None):
    """旧文本接口保留为拒绝路径；关键词/广告/截图文字不能证明库存。"""
    return {'state': 'unknown', 'matches': [], 'why': '纯文本不能确认准确查询条件与真实库存'}


def target_model(name):
    model = normalize(name)
    if '特斯拉' in model:
        return '特斯拉'
    if '日产' in model and 'n7' in model:
        return '日产N7'
    return ''


def require_success(data):
    if not isinstance(data, dict) or data.get('status') != 'SUCCESS' or data.get('code') != 1 or not isinstance(data.get('content'), dict):
        raise SetupError('官网接口未返回明确的成功结果，不认定有车或无车')
    return data['content']


def resolve_city(data):
    content = require_success(data)
    all_cities = content.get('allCities')
    if not isinstance(all_cities, list):
        raise SetupError('城市列表结构不符合官网查询协议')
    cities = [c for c in all_cities if isinstance(c, dict) and c.get('cityName') == CITY]
    if len(cities) != 1 or not str(cities[0].get('cityId', '')).isdigit():
        raise SetupError('未找到唯一、有效的大庆城市编号')
    return str(cities[0]['cityId'])


def resolve_station(data, expected_id=None):
    content = require_success(data)
    districts = content.get('districtList')
    if not isinstance(districts, list):
        raise SetupError('官网网点列表未完整返回')
    matches = {}
    for district in districts:
        if not isinstance(district, dict) or not isinstance(district.get('deptList'), list):
            raise SetupError('官网网点分区列表结构异常')
        for dept in district['deptList']:
            if isinstance(dept, dict) and dept.get('deptName') == STATION:
                dept_id = str(dept.get('deptId', ''))
                if not dept_id.isdigit() or '大庆市龙凤区' not in str(dept.get('deptAddress', '')):
                    raise SetupError('东站网点编号或地址无法准确确认')
                if dept.get('inWorkFlag') is not True:
                    raise SetupError('指定租期取车或还车不在网点可服务时间内')
                if dept_id in matches and matches[dept_id].get('inWorkFlag') != dept.get('inWorkFlag'):
                    raise SetupError('同一网点的营业状态存在冲突')
                matches[dept_id] = dept
    if len(matches) != 1:
        raise SetupError('未找到唯一的大庆东站服务点')
    dept_id = next(iter(matches))
    if expected_id is not None and dept_id != str(expected_id):
        raise SetupError('取车和还车网点编号不一致')
    return dept_id


def assess_listing(data, city_id, dept_id, request):
    expected = {'pickupCityId': str(city_id), 'returnCityId': str(city_id),
                'pickupDeptId': str(dept_id), 'pickupTime': PICKUP,
                'returnTime': RETURN, 'entrance': '1', 'holidaysWaitingFlag': '0'}
    if any(str(request.get(k, '')) != value for k, value in expected.items()):
        raise SetupError('车源请求的网点、租期或查询类型不一致')
    content = require_success(data)
    groups = content.get('deptHangModels')
    if not isinstance(groups, list):
        raise SetupError('官网未返回结构化车型列表')
    selected = [g for g in groups if isinstance(g, dict) and str(g.get('deptId')) == str(dept_id)]
    if len(selected) != 1:
        raise SetupError('车源响应缺少唯一的东站车型列表，不能使用附近网点替代')
    group = selected[0]
    if group.get('deptName') != STATION or str(group.get('cityId')) != str(city_id):
        raise SetupError('车型列表所属城市或网点与请求不一致')
    models = group.get('models')
    if not isinstance(models, list):
        raise SetupError('东站车型列表字段缺失')
    names, seen, hits, targets = [], set(), set(), []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get('modelName'), str) or not model['modelName'].strip():
            raise SetupError('车型名称字段不完整')
        model_id = str(model.get('modelId', ''))
        if not model_id.isdigit() or model_id in seen:
            raise SetupError('车型编号缺失或重复，列表无法完整核验')
        seen.add(model_id)
        inventory = model.get('inventoryType')
        if isinstance(inventory, bool) or str(inventory) not in ('0', '1', '2', '3'):
            raise SetupError('官网返回未适配的库存状态，停止通知')
        inventory = int(inventory)
        label = target_model(model['modelName'])
        price = None
        if inventory in (0, 1):
            try:
                prices = [Decimal(str(model[key])) for key in ('dailyPrice', 'packagePrice')]
                if not all(p.is_finite() and p > 0 for p in prices):
                    raise InvalidOperation
                price = str(min(prices))
            except (KeyError, ValueError, InvalidOperation):
                raise SetupError('可选车型缺少有效价格，列表无法核验') from None
        names.append({'model_id': model_id, 'name': model['modelName'],
                      'inventory_type': inventory, 'daily_price': price})
        if label:
            targets.append({'name': model['modelName'], 'available': inventory in (0, 1), 'daily_price': price})
            if inventory in (0, 1):
                hits.add(label)
    return {'state': 'possible' if hits else 'notfound',
            'matches': [x for x in ('特斯拉', '日产N7') if x in hits],
            'why': '官网确认指定网点和租期存在目标可选车型' if hits else '已核验东站完整车型列表，目前无目标可选车型',
            'verification': {'source': 'official_web_api', 'city_id': str(city_id),
                             'pickup_dept_id': str(dept_id), 'return_dept_id': str(dept_id),
                             'pickup_and_return_open': True, 'pickup': PICKUP, 'return': RETURN,
                             'model_count': len(models), 'target_models': targets,
                             'observed_models': names, 'verified_utc': utc_now()}}


def api_json(context, uri, payload):
    response = context.post('/api/gw.do?' + urllib.parse.urlencode({'uri': uri}),
                            form={'data': json.dumps(payload, ensure_ascii=False)}, timeout=35000)
    try:
        if not official(response.url) or response.status != 200:
            raise SetupError('官网查询请求未获得官方HTTPS成功响应')
        result = response.json()
        require_success(result)
        return result
    finally:
        response.dispose()


def gather():
    from playwright.sync_api import sync_playwright
    stage = '创建官网查询会话'
    context = None
    with sync_playwright() as pw:
        try:
            options = {}
            # 与Python标准HTTP客户端一致，使用运行环境已有的代理配置。
            # 不输出代理地址或认证信息；GitHub Runner无代理时直连。
            proxy_url = urllib.request.getproxies().get('https')
            if proxy_url:
                proxy = urllib.parse.urlsplit(proxy_url)
                if proxy.scheme not in ('http', 'https') or not proxy.hostname:
                    raise SetupError('运行环境HTTPS代理配置无效')
                server = proxy.scheme + '://' + proxy.hostname
                if proxy.port:
                    server += ':' + str(proxy.port)
                options['proxy'] = {'server': server}
                if proxy.username:
                    options['proxy']['username'] = urllib.parse.unquote(proxy.username)
                if proxy.password:
                    options['proxy']['password'] = urllib.parse.unquote(proxy.password)
            context = pw.request.new_context(base_url=HOME, ignore_https_errors=False,
                                             extra_http_headers={'Referer': HOME}, **options)
            stage = '核验大庆城市'
            city_id = resolve_city(api_json(context, CITY_API, {}))
            print('官网城市已核验：大庆，编号 ' + city_id, flush=True)
            lease = {'cityId': city_id, 'entrance': 1, 'estimatedPickupTime': PICKUP,
                     'estimatedReturnTime': RETURN}
            stage = '核验取车网点与营业时间'
            dept_id = resolve_station(api_json(context, DEPT_API, {**lease, 'pickupFlag': 1}))
            stage = '核验同网点还车与营业时间'
            resolve_station(api_json(context, DEPT_API, {**lease, 'pickupFlag': 0, 'pickupDeptId': dept_id}), dept_id)
            print('官网取还网点及营业时间已核验：' + STATION + '，编号 ' + dept_id, flush=True)
            stage = '查询并核验官网完整车型列表'
            request = {'pickupCityId': city_id, 'returnCityId': city_id, 'pickupDeptId': dept_id,
                       'pickupTime': PICKUP, 'returnTime': RETURN, 'entrance': 1, 'holidaysWaitingFlag': 0}
            result = assess_listing(api_json(context, CAR_API, request), city_id, dept_id, request)
            print('真实官网车型数量：' + str(result['verification']['model_count']), flush=True)
            return result
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SetupError) else type(exc).__name__
            return {'state': 'unknown', 'matches': [], 'why': stage + '：' + reason}
        finally:
            if context is not None:
                context.dispose()


def transition(previous, result):
    before = previous.get('present_matches', [])
    if not isinstance(before, list):
        before = []
    after = result['matches'] if result['state'] == 'possible' else ([] if result['state'] == 'notfound' else before)
    fresh = [x for x in after if x not in before] if result['state'] == 'possible' else []
    now = utc_now()
    changed = (before != after or previous.get('last_status') != result['state'] or previous.get('last_detail') != result['why'])
    old_count = previous.get('consecutive_unknown', 0)
    if not isinstance(old_count, int):
        old_count = 0
    new = {**previous, 'schema_version': 4, 'present_matches': after,
           'last_status': result['state'], 'last_detail': result['why'],
           'last_changed_utc': now if changed else previous.get('last_changed_utc', now),
           'last_checked_utc': now, 'consecutive_unknown': old_count + 1 if result['state'] == 'unknown' else 0,
           'query': {'city': CITY, 'pickup_station': STATION, 'return_station': STATION,
                     'pickup': PICKUP, 'return': RETURN, 'timezone': 'Asia/Shanghai', 'targets': ['特斯拉', '日产N7']},
           'run_url': 'https://github.com/' + os.getenv('GITHUB_REPOSITORY', 'xiemutian/zuche-watch') + '/actions/runs/' + os.getenv('GITHUB_RUN_ID', ''),
           'verification': result.get('verification', {})}
    if result['state'] in ('possible', 'notfound'):
        new['last_verified_utc'] = now
        new['last_verified_status'] = result['state']
    return new, fresh


def save_status(state):
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(STATE)
    (ROOT / 'check-result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    history = ROOT / 'history.json'
    try:
        entries = json.loads(history.read_text(encoding='utf-8')) if history.exists() else []
        if not isinstance(entries, list):
            entries = []
    except (OSError, ValueError):
        entries = []
    item = {k: state[k] for k in ('last_checked_utc', 'last_status', 'last_detail', 'present_matches', 'run_url')}
    item['verified_model_count'] = state.get('verification', {}).get('model_count')
    item['last_push_status'] = state.get('last_push_status')
    entries.append(item)
    history.write_text(json.dumps(entries[-240:], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('### 神州租车监控结果\n\n| 项目 | 结果 |\n|---|---|\n')
            rows = [('状态', state['last_status']), ('检查时间（UTC）', state['last_checked_utc']),
                    ('取还网点', STATION), ('租期（北京时间）', PICKUP + ' — ' + RETURN),
                    ('目标车型', '特斯拉 / 日产N7'), ('实际核验车型数', state.get('verification', {}).get('model_count', '未确认')),
                    ('数据来源', '官网实际车源接口'), ('说明', state['last_detail']),
                    ('连续未确认次数', state['consecutive_unknown']), ('本轮推送', state.get('last_push_status', '未触发'))]
            for label, value in rows:
                f.write('| ' + label + ' | ' + str(value).replace('|', '/').replace('\n', ' ') + ' |\n')
            f.write('\nunknown 不代表无车；仅成功核验城市、同网点取还、完整租期、营业状态和目标库存后通知。\n')


def bark(key, title, body):
    key = key.strip()
    if key.startswith('https://'):
        uri = urllib.parse.urlsplit(key)
        if uri.hostname != 'api.day.app' or uri.username or uri.password:
            raise ValueError('Bark地址格式错误')
        key = next((part for part in uri.path.split('/') if part), '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,180}', key):
        raise ValueError('BARK_KEY格式错误')
    payload = json.dumps({'device_key': key, 'title': title, 'body': body,
                          'group': '神州租车监控', 'level': 'timeSensitive', 'url': HOME}, ensure_ascii=False).encode()
    request = urllib.request.Request('https://api.day.app/push', payload,
                                     {'Content-Type': 'application/json; charset=utf-8'}, method='POST')
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
        if response.status != 200 or data.get('code') != 200:
            raise RuntimeError('Bark未返回明确成功状态')


def main():
    key = os.getenv('BARK_KEY', '').strip()
    mode = os.getenv('MODE', 'monitor')
    if mode == 'test_push':
        if not key:
            print('缺少GitHub Secret: BARK_KEY')
            return 2
        try:
            bark(key, '神州租车云端推送测试', '推送通道正常。这不是车源提醒。')
            print('测试推送已发出')
            return 0
        except Exception as exc:
            print('推送测试失败：' + type(exc).__name__)
            return 1
    try:
        last = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
        if not isinstance(last, dict):
            raise ValueError
    except (OSError, ValueError):
        state, _ = transition({}, {'state': 'unknown', 'matches': [], 'why': '去重状态文件无法读取，停止通知'})
        save_status(state)
        return 1
    if datetime.now(timezone.utc) >= datetime(2026, 10, 17, tzinfo=timezone.utc):
        state, _ = transition(last, {'state': 'stopped', 'matches': [], 'why': '已到约定取车时刻，停止查询与通知'})
        state['last_push_status'] = 'stopped'
        save_status(state)
        print('停止监控：2026-10-17 08:00（北京时间）')
        return 0
    if mode != 'diagnose' and not key:
        state, _ = transition(last, {'state': 'unknown', 'matches': [], 'why': '缺少GitHub Secret: BARK_KEY'})
        save_status(state)
        return 2
    try:
        result = gather()
    except Exception as exc:
        result = {'state': 'unknown', 'matches': [], 'why': '查询会话创建失败：' + type(exc).__name__}
    print('检查结果：', result['state'], result['why'], flush=True)
    updated, fresh = transition(last, result)
    updated['last_push_status'] = 'not_triggered'
    if mode == 'diagnose':
        updated['present_matches'] = last.get('present_matches', [])
        updated['last_push_status'] = 'disabled_for_diagnosis'
    elif fresh:
        try:
            bark(key, '神州租车：目标车型可选', STATION + '取还｜2026年10月17日08:00—18日08:00｜车型：' + '、'.join(fresh)
                 + '。官网已核验网点、租期、营业状态与可选库存；实际价格和库存以预订时为准。')
            updated['last_push_status'] = 'sent'
            updated['last_push_utc'] = utc_now()
            print('Bark车型通知已成功发送：' + '、'.join(fresh), flush=True)
        except Exception as exc:
            updated['present_matches'] = last.get('present_matches', [])
            updated['last_push_status'] = 'failed:' + type(exc).__name__
            save_status(updated)
            print('Bark通知失败，下次准确匹配时重试：' + type(exc).__name__, flush=True)
            return 1
    if result['state'] == 'unknown':
        print('本轮未确认，不发送车源通知，不认定车型消失。', flush=True)
        if mode != 'diagnose' and last.get('last_status') != 'unknown':
            try:
                bark(key, '神州租车监控查询异常', '本轮无法完成严格校验，不代表无车。原因：' + result['why'][:80] + '。请查看Actions摘要。')
                updated['last_fault_push_utc'] = utc_now()
            except Exception as exc:
                print('故障提醒失败：' + type(exc).__name__, flush=True)
    save_status(updated)
    return 1 if result['state'] == 'unknown' else 0


if __name__ == '__main__':
    sys.exit(main())
