"""神州租车大庆东站车型监控 v2.4（城市卡片点击修复与无敏感信息诊断版）。

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
    hits = extract_card_matches(text)
    return {'state': 'possible' if hits else 'notfound', 'matches': hits,
            'why': '车型卡片包含目标关键词' if hits else '已验证列表，目前未看到目标车型'}


def transition(previous: dict, result: dict) -> tuple[dict, list[str]]:
    before = previous.get('present_matches', [])
    if not isinstance(before, list):
        before = []
    after = result['matches'] if result['state'] == 'possible' else ([] if result['state'] == 'notfound' else before)
    fresh = [x for x in after if x not in before] if result['state'] == 'possible' else []
    if (after == before and previous.get('last_status') == result['state']
            and previous.get('last_detail') == result['why']):
        return previous, fresh
    new = {'present_matches': after, 'last_status': result['state'],
           'last_detail': result['why'],
           'last_changed_utc': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    return new, fresh


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


def open_city_picker(page, which: str):
    """点城市【值】所属的卡片，而不是无点击行为的'取车城市'标签。"""
    if city_chooser_visible(page):
        return
    if which != '取车':
        raise SetupError('只实现了取车城市的主动选择')
    label = page.get_by_text('取车城市', exact=True)
    print('城市入口父级结构（仅元素元数据）：' + json.dumps(city_dom_outline(page), ensure_ascii=False), flush=True)
    if label.count() < 1:
        raise SetupError('首页没有独立的取车城市标签')
    node = label.first
    # 神州H5标签与城市值通常是相邻/上下两行，监听器可能在共同父级上。
    # 优先尝试低层父节点，避免误点包含去订车按钮的整张表单卡片。
    for depth in range(1, 6):
        node = node.locator('xpath=..')
        try:
            r = node.bounding_box(timeout=1800)
            if not r:
                continue
            w,h = r['width'],r['height']
            if w < 65 or h < 35 or h > 250 or w > 420:
                continue
            txt=node.inner_text(timeout=1800)
            if '去订车' in txt:
                continue
            if '取车地址' in txt:
                positions=[(.17,.60),(.25,.76)]
            else:
                positions=[(.65,.72),(.50,.55)]
            for xratio,yratio in positions:
                x=max(1,min(388,r['x']+w*xratio))
                y=max(1,min(842,r['y']+h*yratio))
                print(f'城市卡片点击：祖先层级{depth}, 坐标({int(x)},{int(y)}), 容器{int(w)}x{int(h)}',flush=True)
                page.mouse.click(x,y)
                page.wait_for_timeout(650)
                if city_chooser_visible(page):
                    print('成功打开选择城市页面',flush=True)
                    return
                # 页面出现非城市选择的导航时立即停止，不在错误页面继续试点。
                if page.get_by_text('取车城市',exact=True).count()==0:
                    raise SetupError('点击后进入未知页面；停止尝试以防误操作')
        except SetupError:
            raise
        except Exception as error:
            print('城市卡片点击未成功：'+type(error).__name__,flush=True)
    # 某些布局在城市名称组件绑定 onClick，而不是整个标签的父级。
    # 尝试靠近标题的城市值组件；不使用宽泛的全文搜索来误点广告。
    for loc in [page.locator('[class*="city" i]').filter(has_text='取车城市'),
                page.locator('[class*="addressCity" i]')]:
        try:
            for j in range(min(loc.count(),4)):
                el=loc.nth(j)
                if el.is_visible() and el.bounding_box():
                    box=el.bounding_box()
                    if box['height']>250 or box['width']>420:continue
                    el.click(timeout=2300)
                    page.wait_for_timeout(500)
                    if city_chooser_visible(page):
                        print('通过城市选择容器成功打开选择城市',flush=True)
                        return
        except Exception:pass
    safe_cloud_diagnostics(page, '尝试城市卡片点击后')
    raise SetupError('已尝试城市名称所在卡片，但仍未进入城市选择页；请提供城市入口父级结构日志')


def select_city(page, which: str):
    """按截图匹配神州H5：选择城市 -> 当前定位城市：大庆 / 搜索城市。"""
    body = page.locator('body')
    open_city_picker(page, which)
    if not city_chooser_visible(page):
        raise SetupError('城市卡片点击后，页面未出现搜索城市或当前定位城市')

    # 截图中“当前定位城市：大庆”是一整个文本区域，不能用 exact=True 找孤立“大庆”。
    current = page.get_by_text(re.compile(r'当前定位城市\s*[：:]\s*大庆'))
    try:
        for i in range(min(current.count(), 5)):
            item = current.nth(i)
            if item.is_visible():
                item.click(timeout=2300)
                page.wait_for_timeout(650)
                if home_city_selected(body.inner_text(timeout=10000)):
                    print('城市选取：使用当前定位城市（大庆）')
                    return
                break
    except Exception:
        pass

    # 当前定位行可能不可点击，或者当前城市不一定是大庆。改用顶部“搜索城市”。
    fields = [
        page.get_by_placeholder('搜索城市'),
        page.locator('input[placeholder*="搜索城市"]'),
        page.locator('input[placeholder*="城市"]'),
        page.locator('input[type="search"]'),
    ]
    filled = False
    for field in fields:
        try:
            for i in range(min(field.count(), 5)):
                inp = field.nth(i)
                if inp.is_visible():
                    inp.fill(CITY, timeout=2700)
                    filled = True
                    break
            if filled:
                break
        except Exception:
            continue
    if not filled:
        # 兼容“搜索城市”是一个需先点开的伪输入区域。
        label = page.get_by_text('搜索城市', exact=True)
        try:
            if label.count() and label.first.is_visible():
                label.first.click(timeout=2500)
        except Exception:
            pass
        for field in fields:
            try:
                if field.count() and field.last.is_visible():
                    field.last.fill(CITY, timeout=2700)
                    filled = True
                    break
            except Exception:
                continue
    if not filled:
        raise SetupError('城市选择页已打开，但无法填写“搜索城市”输入框')
    page.wait_for_timeout(800)

    # 搜索结果可能显示“大庆”或“大庆市”，使用精确文本，避免点击“当前定位城市”大区域。
    choices = [
        page.get_by_text('大庆', exact=True),
        page.get_by_text('大庆市', exact=True),
        page.locator('li').filter(has_text=re.compile(r'^\s*大庆市?\s*$')),
        page.locator('[role="option"]').filter(has_text=re.compile(r'^\s*大庆市?\s*$')),
    ]
    click_first_visible(page, choices, '大庆城市搜索结果')
    page.wait_for_timeout(650)
    if not home_city_selected(body.inner_text(timeout=10000)):
        raise SetupError('已选择“大庆”，但首页未显示正确的取车城市')
    print('城市选取：搜索城市并选择大庆')


def select_station(page, which: str = '取车'):
    """与用户 10/12 截图一致：先打开地址页，切换“网点”标签，点击完整网点名。"""
    controls = [
        page.get_by_text('取车地址', exact=False),
        page.get_by_text('地址/街道/酒店/景点名', exact=False),
        page.get_by_text('请选择取车网点', exact=False),
        page.locator('[class*="pickup"] [class*="address"]'),
    ]
    click_first_visible(page, controls, '取车地址')
    page.wait_for_timeout(500)
    try:
        tab = page.get_by_text('网点', exact=True)
        if tab.count() and tab.first.is_visible():
            tab.first.click(timeout=2000)
            page.wait_for_timeout(450)
    except Exception:
        pass
    # 截图顶部“最近选择”中会直接出现该网点；否则搜索或查找龙凤区区域。
    candidates = [page.get_by_text(STATION, exact=True)]
    for loc in candidates:
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=3000)
                page.wait_for_timeout(750)
                return
        except Exception:
            pass
    search = [page.locator('input[placeholder*="输入地址"]'),
              page.locator('input[placeholder*="搜索"]'),
              page.locator('input[type="search"]')]
    for loc in search:
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.fill(STATION)
                page.wait_for_timeout(1000)
                break
        except Exception:
            continue
    click_first_visible(page, [page.get_by_text(STATION, exact=True),
                               page.get_by_text(STATION, exact=False)], STATION)
    page.wait_for_timeout(700)


def find_date_control(page, which: str):
    # 当前 H5 首页存在两个「10月xx日 周x HH:MM」控件。
    # 先尝试具名控件，再在日期样式匹配元素中查找。
    names = ['取车时间', '取车日期'] if which == '取车' else ['还车时间', '还车日期']
    for name in names:
        locator = page.get_by_text(name, exact=False)
        try:
            if locator.count() and locator.last.is_visible():
                locator.last.click(timeout=2500)
                return
        except Exception:
            continue
    dates = page.get_by_text(re.compile(r'\d{1,2}月\d{1,2}日'))
    if dates.count() >= 2:
        dates.nth(0 if which == '取车' else 1).click(timeout=2500)
        return
    raise SetupError(f'找不到{which}日期入口')


def choose_date_time(page, which: str, day: int):
    find_date_control(page, which)
    page.wait_for_timeout(600)
    # 适用“日历选日 + 小时/分钟”的常见 H5 组件，但具体 DOM 必须由运行日志验证。
    day_candidates = [
        page.locator('[role="dialog"] button').filter(has_text=re.compile(rf'^\s*{day}\s*$')),
        page.locator('[class*="calendar"] [class*="day"]').filter(has_text=re.compile(rf'^\s*{day}\s*$')),
        page.get_by_text(str(day), exact=True),
    ]
    click_first_visible(page, day_candidates, f'10月{day}日')
    page.wait_for_timeout(350)
    # 仅选择明确表示08:00的时间选项；不会默认把原时间当成08点。
    tm = [page.get_by_text('08:00', exact=True), page.get_by_text('8:00', exact=True),
          page.locator('input[type="time"]')]
    found_time = False
    for loc in tm:
        try:
            if loc.count() and loc.last.is_visible():
                if loc.last.evaluate('(el)=>el.tagName') == 'INPUT':
                    loc.last.fill('08:00')
                else:
                    loc.last.click(timeout=2500)
                found_time = True
                break
        except Exception:
            continue
    # 若组件采用滚轮，需要人工适配，禁止默认为时间设置成功。
    if not found_time:
        raise SetupError(f'{which}日期可能已选中，但未找到08:00时间控件，请提供该页面截图')
    try:
        confirm = page.get_by_text('确定', exact=True)
        if confirm.count() and confirm.last.is_visible():
            confirm.last.click(timeout=2000)
    except Exception:
        pass
    page.wait_for_timeout(450)


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
            # 默认 Chromium 即使 is_mobile=True 也可能仍采用桌面 UA。
            # 指定 Android Chrome 移动端 UA，尽量获取与手机接近的 H5 页面。
            context_opt = {'locale':'zh-CN','timezone_id':'Asia/Shanghai',
                           'viewport': {'width':390,'height':844},
                           'device_scale_factor':1, 'is_mobile':True, 'has_touch':True,
                           'user_agent': ('Mozilla/5.0 (Linux; Android 13; Pixel 7) '
                                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                                          'Chrome/131.0.0.0 Mobile Safari/537.36')}
            state = os.environ.get('LOGIN_STATE_B64', '').strip()
            if state:
                context_opt['storage_state'] = json.loads(base64.b64decode(state, validate=True).decode('utf-8'))
            ctx = browser.new_context(**context_opt)
            page = ctx.new_page()
            stage = '打开首页'
            page.goto(HOME, wait_until='domcontentloaded', timeout=40000)
            page.wait_for_timeout(3000)
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
            if not (date_time_present(summary, PICKUP) and date_time_present(summary, RETURN)):
                choose_date_time(page, '取车', 17)
                page.wait_for_timeout(450)
                summary = page.locator('body').inner_text(timeout=10000)
                if not date_time_present(summary, RETURN):
                    choose_date_time(page, '还车', 18)
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
    if not key:
        print('缺少 GitHub Secret: BARK_KEY')
        return 2
    if os.getenv('MODE', 'monitor') == 'test_push':
        bark(key, '神州租车云端推送测试', '推送通道正常。这不是车源提醒。')
        print('测试推送已发出')
        return 0
    # 距离使用日期很近才有意义；超过取车时间停止提醒。
    now = datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai'))
    if now >= datetime(2026, 10, 17, 8, 0, tzinfo=__import__('zoneinfo').ZoneInfo('Asia/Shanghai')):
        print('已过约定取车时刻：2026-10-17 08:00，停止监控和通知。')
        return 0
    try:
        result = gather()
    except Exception as exc:
        result = {'state':'unknown', 'matches':[], 'why':'浏览器启动失败：' + type(exc).__name__}
    print('检查结果：', result['state'], result['why'])
    last = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    updated, fresh = transition(last, result)
    if fresh:
        try:
            bark(key, '神州租车：发现目标车型',
                 '大庆东站｜10月17日08:00—18日08:00｜出现：' + '、'.join(fresh)
                 + '。仅表示车型列表匹配，请打开官方页面核实库存并预订。')
            print('Bark 发现通知已发出：', '、'.join(fresh))
        except Exception as exc:
            print('Bark 通知失败：', type(exc).__name__)
            updated['present_matches'] = last.get('present_matches', [])
            STATE.write_text(json.dumps(updated, ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
            return 1
    if result['state']=='unknown':
        print('本轮结果不确定，不发送车源发现通知，不认定车型已消失。')
        if last.get('last_status') != 'unknown':
            try:
                bark(key, '神州租车监控需要检查',
                     '大庆东站监控本次未能确认网页条件；请查看 GitHub Actions 运行日志。原因：' + result['why'][:80])
                print('已发送一次技术故障提醒')
            except Exception as err:
                print('技术故障提醒发送失败：'+type(err).__name__)
    STATE.write_text(json.dumps(updated, ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0


if __name__=='__main__':
    sys.exit(main())
