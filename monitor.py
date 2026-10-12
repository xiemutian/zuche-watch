"""神州租车大庆东站车型监控 v3（精确DOM选择与严格状态记录）。

安全原则：网页交互/上下文确认失败时仅记录 unknown，不发送有车通知。
该程序的 Playwright 控件适配未在真实神州网页验证，首次使用请检查 Actions 日志。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE = Path(__file__).with_name('state.json')
HOME = 'https://m.zuche.com/'

# 监控条件固定在程序里，避免使用只能代表列表页面的 #/rent/list URL。
CITY = '大庆'
STATION = '大庆东站服务点'
PICKUP = '2026-10-17 08:00'
RETURN = '2026-10-18 08:00'
KEYWORDS = ['特斯拉', '日产N7', '日产 N7']

# 如果页面更改了控件，需要根据 Actions 日志适配；不要删除严谨的租期校验。


def normalize(value: Any) -> str:
    return re.sub(r'[\s\u00a0\u200b\-_·・.,，。:：/()（）\[\]【】]', '', str(value or '')).lower()


def official(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ''
        return p.scheme == 'https' and not p.username and not p.password and (host == 'zuche.com' or host.endswith('.zuche.com'))
    except ValueError:
        return False


def date_present(text: str, iso: str) -> bool:
    date, hour = iso.split(' ')
    year, month, day = (int(i) for i in date.split('-'))
    patterns = [
        rf'{year}\s*[-/年.]\s*0?{month}\s*[-/月.]\s*0?{day}',
        rf'(?<!\d)0?{month}\s*月\s*0?{day}\s*日?',
        rf'(?<!\d)0?{month}[-/]0?{day}(?!\d)',
    ]
    return any(re.search(p, text) for p in patterns)


def date_time_present(text: str, iso: str) -> bool:
    """能识别截图中的“10月17日\n周六 08:00”和“10月17日 08:00”。"""
    date, hhmm = iso.split(' ')
    year, month, day = (int(i) for i in date.split('-'))
    date_patterns = [
        rf'{year}\s*[-/年.]\s*0?{month}\s*[-/月.]\s*0?{day}\s*日?',
        rf'(?<!\d)0?{month}\s*月\s*0?{day}\s*日?',
        rf'(?<!\d)0?{month}[-/]0?{day}(?!\d)'
    ]
    h, mi = map(int, hhmm.split(':'))
    time_re = re.compile(rf'(?<!\d)0?{h}\s*[:：]\s*{mi:02d}(?!\d)')
    next_date = re.compile(r'\d{1,2}\s*月\s*\d{1,2}\s*日')
    for dp in date_patterns:
        for dm in re.finditer(dp, text):
            tail = text[dm.end(): dm.end() + 55]
            tm = time_re.search(tail)
            if tm is None:
                continue
            other_date = next_date.search(tail)
            if other_date and other_date.start() < tm.start():
                continue
            return True
    return False


def check_context(text: str, url: str, trusted_steps: set[str] | None = None) -> tuple[bool, str]:
    """截图验证：车源列表顶部显示网点与完整租期，不再另查城市。"""
    if not official(url):
        return False, '非神州租车 HTTPS 官方域名'
    parts = urllib.parse.urlsplit(url)
    if 'rent/list' not in parts.fragment and 'rent/list' not in parts.path:
        return False, '不是车型列表页面'
    normalized = normalize(text)
    if any(normalize(x) in normalized for x in ('滑动验证', '验证码', '请先登录', '访问频繁', '登录后继续', '异常访问')):
        return False, '页面需要登录、验证或出现访问异常'
    # 列表页的顶部标题示例：大庆东站服务点\n10月17日 08:00 － 10月18日 08:00。
    # 顶部无需再次显示“大庆”，网点全名已经唯一确认。
    heading = text[:450]
    if normalize(STATION) not in normalize(heading):
        return False, '车型列表标题未显示大庆东站服务点'
    if not date_time_present(heading, PICKUP):
        return False, '车型列表标题无法确认10月17日08:00'
    if not date_time_present(heading, RETURN):
        return False, '车型列表标题无法确认10月18日08:00'
    # 排除只有页面头部、地图而车辆列表未成功加载的场景。
    if '车型详情' not in text or not re.search(r'[¥￥]\s*\d+', text):
        return False, '列表尚未显示带价格的车型卡片'
    return True, '网点、日期时间及车型卡片已验证'


def extract_card_matches(text: str) -> list[str]:
    """识别截图中的“车型名称 / 配置 / ￥价格 / 车型详情”卡片。"""
    hits = set()
    for piece in text.split('车型详情')[:-1]:
        matches = list(re.finditer(r'[¥￥]\s*\d+', piece))
        if not matches:
            continue
        # 以距“车型详情”最近的一个标价为准，避免将页面广告关键词当车名。
        before_price = piece[:matches[-1].start()]
        lines = [line.strip() for line in before_price.splitlines() if line.strip()]
        model_lines = lines[-2:]
        model = normalize(' '.join(model_lines))
        if '特斯拉' in model:
            hits.add('特斯拉')
        if '日产n7' in model or re.search(r'(?<![a-z0-9])n7(?![a-z0-9])', ' '.join(model_lines), re.I):
            hits.add('日产N7')
    return [x for x in ('特斯拉', '日产N7') if x in hits]


def assess(text: str, url: str, trusted_steps: set[str] | None = None) -> dict:
    valid, why = check_context(text, url, trusted_steps)
    if not valid:
        return {'state': 'unknown', 'matches': [], 'why': why}
    if not trusted_steps or 'inventory_verified' not in trusted_steps:
        return {'state':'unknown', 'matches':[], 'why':'车型卡片库存与取还上下文尚未完成结构化校验'}
    hits = extract_card_matches(text)
    return {'state': 'possible' if hits else 'notfound', 'matches': hits,
            'why': '车型卡片包含目标关键词' if hits else '已验证列表，目前未看到目标车型'}


def transition(previous: dict, result: dict) -> tuple[dict, list[str]]:
    before = previous.get('present_matches', [])
    if not isinstance(before, list):
        before = []
    after = result['matches'] if result['state'] == 'possible' else ([] if result['state'] == 'notfound' else before)
    fresh = [x for x in after if x not in before] if result['state'] == 'possible' else []
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    changed = (before != after or previous.get('last_status') != result['state'] or previous.get('last_detail') != result['why'])
    new = {**previous, 'schema_version': 3, 'present_matches': after,
           'last_status': result['state'], 'last_detail': result['why'],
           'last_changed_utc': now if changed else previous.get('last_changed_utc', now),
           'last_checked_utc': now,
           'consecutive_unknown': previous.get('consecutive_unknown', 0) + 1 if result['state'] == 'unknown' else 0,
           'query': {'city':CITY, 'pickup_station':STATION, 'return_station':STATION,
                     'pickup':PICKUP, 'return':RETURN, 'timezone':'Asia/Shanghai', 'targets':['特斯拉','日产N7']},
           'run_url': 'https://github.com/' + os.getenv('GITHUB_REPOSITORY', 'xiemutian/zuche-watch') + '/actions/runs/' + os.getenv('GITHUB_RUN_ID', '')}
    if result['state'] in ('possible', 'notfound'):
        new['last_verified_utc'] = now
        new['last_verified_status'] = result['state']
    new['verification'] = result.get('verification', {})
    return new, fresh


def save_status(state: dict):
    # 临时文件原子替换，运行失败也由Actions always步骤提交状态。
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(STATE)
    Path('check-result.json').write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    history = Path(__file__).with_name('history.json')
    try:
        entries = json.loads(history.read_text(encoding='utf-8')) if history.exists() else []
    except (OSError, ValueError):
        entries = []
    entries.append({k: state[k] for k in ('last_checked_utc','last_status','last_detail','present_matches','run_url','verification') if k in state})
    history.write_text(json.dumps(entries[-240:], ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('### 神州租车监控结果\n\n')
            f.write('| 项目 | 结果 |\n|---|---|\n')
            for label, value in [('状态', state['last_status']), ('检查时间（UTC）', state['last_checked_utc']),
                                  ('取还网点', STATION), ('租期（北京时间）', PICKUP+' — '+RETURN),
                                  ('目标车型', '特斯拉 / 日产N7'), ('说明', state['last_detail']),
                                  ('连续未确认次数', state['consecutive_unknown'])]:
                f.write('| '+label+' | '+str(value).replace('|','/').replace('\n',' ')+' |\n')
            f.write('\nunknown 表示本轮未验证成功，不表示无车；仅完整验证后发送车型通知。\n')



class SetupError(Exception):
    pass


def click_first_visible(page, locators: list, description: str, timeout=2500):
    for loc in locators:
        try:
            for i in range(min(loc.count(), 8)):
                item = loc.nth(i)
                if item.is_visible(timeout=timeout):
                    item.click(timeout=timeout)
                    return
        except Exception:
            continue
    raise SetupError(f'无法定位{description}控件（可能是网站界面变化）')


def home_city_selected(text: str) -> bool:
    """校验首页顶部的取车城市，避免广告或其他地名误触发跳过选择。"""
    front = (text or '')[:450]
    label = re.search(r'取车城市', front)
    if not label:
        return False
    return bool(re.search(r'(?<![\u4e00-\u9fa5])大庆(?!东站)', front[label.end():label.end() + 100]))


def safe_cloud_diagnostics(page, stage: str):
    """公开仓库仅记录布尔值及页面结构，不输出 Cookie、URL 参数或网页原文。"""
    try:
        parsed = urllib.parse.urlsplit(page.url)
        body = page.locator('body')
        txt = body.inner_text(timeout=5000) if body.count() else ''
        words = ('神州租车', '取车城市', '取车地址', '去订车',
                 '选择城市', '搜索城市', '当前定位城市', '大庆',
                 '验证码', '安全验证', '登录', '访问频繁', '系统繁忙')
        d = {
            '阶段': stage,
            '域名': parsed.hostname,
            '路径': parsed.path,
            '路由': parsed.fragment.split('?')[0][:75],
            '可见文字长度': len(txt),
            '页面关键词': {word: word in txt for word in words},
            '取车城市精确元素数': page.get_by_text('取车城市', exact=True).count(),
            '取车城市模糊元素数': page.get_by_text('取车城市', exact=False).count(),
            '大庆独立元素数': page.get_by_text('大庆', exact=True).count(),
            '页面输入框数量': page.locator('input').count(),
        }
        print('云端网页结构诊断（不含个人信息）：' + json.dumps(d, ensure_ascii=False), flush=True)
    except Exception as error:
        print('网页诊断无法完成：' + type(error).__name__, flush=True)


def city_chooser_visible(page) -> bool:
    """同时确认标题与搜索/定位信息，避免把首页其它城市文案当作选择器。"""
    txt = page.locator('body').inner_text(timeout=9000)
    return ('选择城市' in txt[:350] and
            ('搜索城市' in txt[:550] or '当前定位城市' in txt[:550]))


def city_dom_outline(page):
    """只输出标签与祖先结构信息，不记录用户网页文字。"""
    label = page.get_by_text('取车城市', exact=True)
    try:
        return label.first.evaluate("""(el) => {
          let n=el, rows=[];
          for(let depth=0; n && depth<6; depth++, n=n.parentElement){
            const rect=n.getBoundingClientRect();
            const t=(n.innerText||'').trim();
            rows.push({level:depth,tag:n.tagName.toLowerCase(),
              className:String(n.className||'').slice(0,90),
              role:n.getAttribute('role')||'',
              width:Math.round(rect.width),height:Math.round(rect.height),
              chars:t.length, hasAddress:t.includes('取车地址'),
              hasOrder:t.includes('去订车'),hasCity:t.includes('取车城市')});
          }
          return rows;
        }""")
    except Exception as error:
        return {'error':type(error).__name__}


def wait_home(page):
    page.locator('.selector.pickup .city .label-con em').wait_for(state='visible', timeout=30000)
    page.get_by_role('button', name='去订车', exact=True).wait_for(state='visible', timeout=30000)


def open_city_picker(page, which: str):
    """2026-10-12真实网页验证：点击城市值，标签本身无导航行为。"""
    if which != '取车':
        raise SetupError('只实现取车城市选择')
    wait_home(page)
    print('城市步骤：点击实际城市值', flush=True)
    page.locator('.selector.pickup .city .label-con em').click(timeout=10000)
    page.wait_for_url(re.compile(r'/citylist(?:\?|$)'), timeout=15000)
    print('城市步骤：已到citylist，等待标题', flush=True)
    page.get_by_text('选择城市', exact=True).wait_for(state='visible', timeout=30000)


def select_city(page, which: str):
    open_city_picker(page, which)
    page.locator('#city-re-input').fill('', timeout=10000)
    page.locator('#city-re-input').press_sequentially(CITY, delay=100)
    page.locator('#city-re-input').press('ArrowRight')
    print('城市搜索输入核验：'+str(page.locator('#city-re-input').input_value() == CITY), flush=True)
    choice = page.locator('.search-city-item').filter(has_text=re.compile('^大庆$'))
    print('城市步骤：等待大庆选项', flush=True)
    choice.wait_for(state='visible', timeout=30000)
    print('大庆选项结构：'+choice.evaluate('(el)=>el.outerHTML'), flush=True)
    choice.click(timeout=10000)
    print('城市步骤：已点大庆，等待首页', flush=True)
    page.wait_for_url(re.compile(r'#/rent$'), timeout=15000)
    wait_home(page)
    page.locator('.selector.pickup .city .label-con em').filter(has_text=re.compile('^大庆$')).wait_for(state='visible', timeout=30000)
    if page.locator('.selector.pickup .city .label-con em').inner_text().strip() != CITY:
        raise SetupError('首页取车城市未准确显示大庆')
    print('城市已验证：大庆', flush=True)


def select_station(page, which: str = '取车'):
    wait_home(page)
    page.locator('.selector.pickup .addr .label-con em').click(timeout=10000)
    page.wait_for_url(re.compile(r'/poisearch(?:\?|$)'), timeout=15000)
    page.get_by_text('网点', exact=True).wait_for(state='visible', timeout=30000)
    cancel = page.get_by_role('button', name='取消', exact=True)
    if cancel.count() and cancel.is_visible():
        cancel.click()
    page.get_by_text('网点', exact=True).click(timeout=10000)
    station = page.get_by_text(STATION, exact=True)
    station.wait_for(state='visible', timeout=30000)
    station.click(timeout=10000)
    page.wait_for_url(re.compile(r'#/rent$'), timeout=15000)
    wait_home(page)
    page.locator('.selector.pickup .addr .label-con em').filter(has_text=re.compile('^大庆东站服务点$')).wait_for(state='visible', timeout=30000)
    if page.locator('.selector.pickup .addr .label-con em').inner_text().strip() != STATION:
        raise SetupError('首页取车网点未准确显示大庆东站服务点')


def select_wheel_time(page, selector: str, target='08:00'):
    wheel = page.locator(selector + ' .am-picker-col')
    wheel.wait_for(state='visible', timeout=15000)
    items = wheel.locator('.am-picker-col-item')
    for attempt in range(8):
        values = items.all_text_contents()
        if target not in values:
            raise SetupError('网点时间选项不包含08:00')
        selected = wheel.locator('.am-picker-col-item-selected')
        if selected.count() != 1:
            raise SetupError('时间滚轮未显示唯一选中项')
        current = selected.inner_text().strip()
        if current == target:
            return
        # 使用实际滚轮行高进行鼠标拖动，不注入组件状态或伪造时间。
        delta = values.index(target) - values.index(current)
        row = selected.bounding_box()
        box = wheel.bounding_box()
        if not row or not box:
            raise SetupError('无法读取时间滚轮尺寸')
        step = max(-2, min(2, delta))
        x = box['x'] + box['width'] / 2
        y = box['y'] + box['height'] / 2
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x, y - step * row['height'], steps=20)
        page.wait_for_timeout(250)
        page.mouse.up()
        page.wait_for_timeout(600)
    raise SetupError('时间滚轮未准确选中08:00')


def choose_rental_period(page):
    wait_home(page)
    dates = page.locator('.select-car').get_by_text(re.compile(r'10月\d+日'))
    dates.first.click(timeout=10000)
    month = page.locator('.single-month').filter(has=page.get_by_text('2026年10月', exact=True))
    month.wait_for(state='visible', timeout=15000)
    page.get_by_text('清空', exact=True).click(timeout=10000)
    for day, label in [(17, '取车'), (18, '还车')]:
        cell = month.locator('.cell').filter(has=page.locator('.date-wrapper > .date').filter(has_text=re.compile(rf'^{day}$')))
        if cell.count() != 1:
            raise SetupError(f'10月{day}日单元格不唯一')
        cell.click(timeout=10000)
        page.wait_for_timeout(350)
        if label not in cell.inner_text():
            raise SetupError(f'10月{day}日未标记为{label}')
    select_wheel_time(page, '.pickup-time')
    select_wheel_time(page, '.return-time')
    page.get_by_text('确定时间', exact=True).click(timeout=10000)
    page.locator('.confirm-panel').wait_for(state='hidden', timeout=10000)


def read_vehicle_list(page):
    """循环滚动加载结果；避免只看截图首次露出的3辆车。"""
    chunks = []
    seen = set()
    for _ in range(12):
        sample = page.locator('body').inner_text(timeout=10000)
        if sample not in seen:
            chunks.append(sample)
            seen.add(sample)
        page.evaluate("""() => {
          let root = document.scrollingElement || document.documentElement;
          root.scrollTop += Math.max(500, window.innerHeight * .75);
          // 车型列表有可能是独立的滚动容器。
          for (const e of document.querySelectorAll('main,section,article,div,ul')) {
            if (e.clientHeight > 260 && e.scrollHeight > e.clientHeight + 200) {
              const css = getComputedStyle(e);
              if (css.overflowY === 'auto' || css.overflowY === 'scroll') {
                e.scrollTop += Math.max(500, e.clientHeight * .8);
              }
            }
          }
        }""")
        page.wait_for_timeout(550)
    return '\n'.join(chunks)


def gather():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        page = None
        stage = '启动浏览器'
        try:
            # m.zuche.com本身提供H5；使用标准Chrome环境，与真实云端浏览器操作一致。
            context_opt = {'locale':'zh-CN','timezone_id':'Asia/Shanghai',
                           'viewport': {'width':390,'height':844},
                           'device_scale_factor':1, 'is_mobile':False, 'has_touch':False}
            state = os.environ.get('LOGIN_STATE_B64', '').strip()
            if state:
                context_opt['storage_state'] = json.loads(base64.b64decode(state, validate=True).decode('utf-8'))
            ctx = browser.new_context(**context_opt)
            page = ctx.new_page()
            stage = '打开首页'
            page.goto(HOME, wait_until='domcontentloaded', timeout=40000)
            wait_home(page)
            # 异步渲染的站点在 domcontentloaded 后可能仍是一片空白。
            # 最多额外等待9秒，让首页关键元素有时间出现。
            for _ in range(6):
                initial_text = page.locator('body').inner_text(timeout=10000)
                if '去订车' in initial_text or ('取车城市' in initial_text and '取车地址' in initial_text):
                    break
                page.wait_for_timeout(1500)
            stage = '选择大庆市'
            safe_cloud_diagnostics(page, stage)
            # 登录会话可能已经保存城市；可跳过重复选择。
            summary = page.locator('body').inner_text(timeout=10000)
            if not home_city_selected(summary):
                select_city(page, '取车')
            print('步骤1：检查/选择取车城市（大庆）', flush=True)
            safe_cloud_diagnostics(page, '城市选择完成')
            stage = '选择大庆东站服务点'
            select_station(page)
            print('步骤2：已点击取车网点')
            # 用户截图里“跨网点还车”关闭，和取车网点相同，无需还车控件。
            stage = '设置取车与还车时间'
            summary = page.locator('body').inner_text(timeout=10000)
            choose_rental_period(page)
            print('步骤3：已执行租期选择，进入提交前校验')
            stage = '提交前检查日期和网点'
            summary = page.locator('body').inner_text(timeout=10000)
            checks = [STATION in summary, date_time_present(summary, PICKUP), date_time_present(summary, RETURN)]
            if not all(checks):
                raise SetupError('首页无法确认以下条件：'+','.join(
                    n for n, ok in zip(['大庆东站服务点','10月17日08:00','10月18日08:00'], checks) if not ok))
            stage = '进入车型列表'
            click_first_visible(page, [page.get_by_text('去订车', exact=True),
                                       page.get_by_text('立即选车', exact=True)], '去订车')
            page.wait_for_timeout(5000)
            stage = '验证车型列表内容'
            text = read_vehicle_list(page)
            result = assess(text, page.url)
            if result['state'] == 'unknown':
                debug_capture(page, stage, result['why'])
            return result
        except Exception as exc:
            why = f'{stage}：{type(exc).__name__}'
            if isinstance(exc, SetupError):
                why += '：'+str(exc)
            if page is not None:
                debug_capture(page, stage, why)
            return {'state':'unknown','matches':[], 'why':why}
        finally:
            browser.close()


def debug_capture(page, stage: str, reason: str):
    """公开仓库安全模式：只记录步骤与原因，不保存网页截图和登录信息。"""
    print('诊断阶段：'+stage+'；结果：'+reason[:160])
    safe_cloud_diagnostics(page, stage)
    try:
        info = page.evaluate(r"""() => ({
          searchTargetOutline: Array.from(document.querySelectorAll('.city-re-name-text,.search-city-item')).filter(e=>e.textContent.trim()==='大庆').map(e=>e.parentElement.outerHTML.slice(0,1800)),
          searchInput: Array.from(document.querySelectorAll('#city-re-input')).map(e=>({tag:e.tagName,hasCorrectValue:e.value==='大庆',outer:e.outerHTML.slice(0,500)})),
          selectedCity: Array.from(document.querySelectorAll('.selector.pickup .city em')).map(e=>e.textContent).filter(t=>/^[\u4e00-\u9fff]{1,12}$/.test(t)),
          cityStructure: Array.from(document.querySelectorAll('.selector.pickup .city,.selector.pickup .city *')).map(e=>({tag:e.tagName,class:e.className,rect:JSON.stringify(e.getBoundingClientRect()),isDaqing:e.textContent.trim()==='大庆'})),
          months: Array.from(document.querySelectorAll('.month-title')).map(e=>e.textContent).filter(t=>/^\d{4}年\d{1,2}月$/.test(t)),
          calendarSelected: Array.from(document.querySelectorAll('.rent-date-start,.rent-date-end')).map(e=>({class:e.className,text:(e.innerText||'').replace(/[^0-9取还车]/g,'')})),
          times: Array.from(document.querySelectorAll('.am-picker-col-item-selected')).map(e=>e.textContent).filter(t=>/^\d{2}:\d{2}$/.test(t)),
          visibleClasses: Array.from(document.querySelectorAll('div,section')).filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}).map(e=>e.className).filter(c=>typeof c==='string' && /calendar|date|time|car|rent|vehicle|list/.test(c)).slice(0,65)
        })""")
        print('安全控件诊断：'+json.dumps(info, ensure_ascii=False))
    except Exception:
        pass


def bark(key, title, body):
    key = key.strip()
    if key.startswith('https://'):
        uri = urllib.parse.urlsplit(key)
        if uri.hostname != 'api.day.app':
            raise ValueError('仅支持官方 Bark 地址 api.day.app')
        key = next((v for v in uri.path.split('/') if v), '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,180}', key):
        raise ValueError('BARK_KEY 格式错误')
    payload = json.dumps({'device_key':key, 'title':title, 'body':body,
                          'group':'神州租车监控', 'level':'timeSensitive',
                          'url': HOME}, ensure_ascii=False).encode()
    req = urllib.request.Request('https://api.day.app/push', payload,
                                 {'Content-Type':'application/json; charset=utf-8'}, method='POST')
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
        if resp.status != 200 or data.get('code', 200) != 200:
            raise RuntimeError('Bark 返回失败状态')


def main():
    key = os.getenv('BARK_KEY', '').strip()
    mode = os.getenv('MODE', 'monitor')
    if mode == 'test_push':
        if not key:
            print('缺少 GitHub Secret: BARK_KEY')
            return 2
        try:
            bark(key, '神州租车云端推送测试', '推送通道正常。这不是车源提醒。')
            print('测试推送已发出')
            return 0
        except Exception as exc:
            print('推送测试失败：'+type(exc).__name__)
            return 1
    try:
        last = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
        if not isinstance(last, dict):
            raise ValueError('state格式错误')
    except (OSError, ValueError):
        state, _ = transition({}, {'state':'unknown','matches':[], 'why':'去重状态文件无法读取，停止通知'})
        save_status(state)
        return 1
    now = datetime.now(timezone.utc)
    if now >= datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc):
        state, _ = transition(last, {'state':'stopped','matches':[], 'why':'已到约定取车时刻，停止网页查询与通知'})
        save_status(state)
        print('已停止监控：2026-10-17 08:00（北京时间）')
        return 0
    if mode != 'diagnose' and not key:
        state, _ = transition(last, {'state':'unknown','matches':[], 'why':'缺少GitHub Secret: BARK_KEY'})
        save_status(state)
        return 2
    try:
        result = gather()
    except Exception as exc:
        result = {'state':'unknown', 'matches':[], 'why':'浏览器启动失败：' + type(exc).__name__}
    print('检查结果：', result['state'], result['why'], flush=True)
    updated, fresh = transition(last, result)
    if mode == 'diagnose':
        # 诊断不改变已发送通知的去重集合。
        updated['present_matches'] = last.get('present_matches', [])
        updated['last_push_status'] = 'disabled_for_diagnosis'
    elif fresh:
        try:
            bark(key, '神州租车：目标车型可选',
                 '大庆东站服务点取还｜2026年10月17日08:00—18日08:00｜车型：' + '、'.join(fresh)
                 + '。已核验网点、租期和可选车型列表；价格库存以实际预订时为准。')
            updated['last_push_status'] = 'sent'
            updated['last_push_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            print('Bark车型通知已成功发送：'+'、'.join(fresh), flush=True)
        except Exception as exc:
            updated['present_matches'] = last.get('present_matches', [])
            updated['last_push_status'] = 'failed:' + type(exc).__name__
            save_status(updated)
            print('Bark通知失败，将在下一次准确匹配时重试：'+type(exc).__name__, flush=True)
            return 1
    if result['state'] == 'unknown':
        print('本轮未确认，不发送车源通知，不认定车型消失。', flush=True)
        if mode != 'diagnose' and last.get('last_status') != 'unknown':
            try:
                bark(key, '神州租车监控查询异常',
                     '大庆东站本轮无法完成严格校验，不代表无车。原因：'+result['why'][:80]
                     +'。请查看Actions摘要。')
                updated['last_fault_push_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            except Exception as exc:
                print('故障提醒失败：'+type(exc).__name__, flush=True)
    save_status(updated)
    return 1 if result['state'] == 'unknown' else 0


if __name__ == '__main__':
    sys.exit(main())
