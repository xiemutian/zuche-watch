"""大庆全网点新能源车源监控。保留 monitor.py 作为原东站监控。"""
from __future__ import annotations
import json, os, re, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent; STATE=ROOT/'state_all.json'; HISTORY=ROOT/'history_all.json'; REPORT=ROOT/'check-result-all.json'
HOME='https://m.zuche.com/'; CITY='大庆'; CITY_API='/action/carrctapi/order/cityList/v1'; DEPT_API='/action/carrctapi/order/deptList/v1'; CAR_API='/resource/carrctapi/order/chooseCar/v3'
# 神州接口把安达作为“大庆”城市联动网点返回；按实际租车范围纳入安达，排除杜尔伯特和肇东。
INCLUDE_LINKED_STATION_IDS={'139846'}
EXCLUDE_STATION_IDS={'95181','142920'}
WINDOWS=[('2026-10-16 18:00','2026-10-17 18:00'),('2026-10-17 09:00','2026-10-18 09:00')]
LIMIT=Decimal('150')
NEV_RE=re.compile(r'(纯电|电动|新能源|插电|增程|特斯拉|理想|腾势|岚图|尚界|小鹏|蔚来|零跑|极氪|比亚迪|方程豹|阿维塔|智己|问界|鸿蒙智行|深蓝)',re.I)
class SetupError(Exception): pass
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def success(x):
 if not isinstance(x,dict) or x.get('status')!='SUCCESS' or x.get('code')!=1 or not isinstance(x.get('content'),dict): raise SetupError('官网接口未返回明确成功结果')
 return x['content']
def post(ctx,uri,payload):
 r=ctx.post('/api/gw.do?'+urllib.parse.urlencode({'uri':uri}),form={'data':json.dumps(payload,ensure_ascii=False)},timeout=35000)
 try:
  if r.status!=200 or not r.url.startswith('https://m.zuche.com/'): raise SetupError('官网HTTPS响应无效')
  x=r.json(); success(x); return x
 finally:r.dispose()
def city_id(data):
 cs=[x for x in success(data).get('allCities',[]) if isinstance(x,dict) and x.get('cityName')==CITY]
 if len(cs)!=1 or not str(cs[0].get('cityId','')).isdigit(): raise SetupError('大庆城市编号不唯一')
 return str(cs[0]['cityId'])
def stations(data):
 out={}; ds=success(data).get('districtList')
 if not isinstance(ds,list): raise SetupError('网点列表不完整')
 for d in ds:
  if not isinstance(d,dict) or not isinstance(d.get('deptList'),list): raise SetupError('网点分区结构异常')
  for s in d['deptList']:
   if not isinstance(s,dict): continue
   sid=str(s.get('deptId','')); addr=str(s.get('deptAddress',''))
   # 只保留实际地址在大庆市行政区内的网点，排除接口同城联动返回的安达/肇东。
   # 保留完整官方网点清单；不可服务网点也要记录，不能因 inWorkFlag=false 而静默消失。
   if sid.isdigit() and sid not in EXCLUDE_STATION_IDS and ('大庆市' in addr or sid in INCLUDE_LINKED_STATION_IDS): out[sid]=s
 if not out: raise SetupError('没有找到大庆市可服务取车网点')
 return sorted(out.values(),key=lambda x:(str(x.get('deptName')),str(x.get('deptId'))))
def price(m):
 try:
  a=Decimal(str(m['dailyPrice'])); b=Decimal(str(m['packagePrice'])); p=min(a,b)
  if not p.is_finite() or p<=0:return None
  return p
 except (KeyError,ValueError,InvalidOperation): return None
def is_nev(m): return bool(NEV_RE.search(str(m.get('modelName',''))+' '+str(m.get('modelDesc',''))))
def cars(data,sid,cid,pickup,ret):
 groups=[g for g in success(data).get('deptHangModels',[]) if isinstance(g,dict) and str(g.get('deptId'))==sid and str(g.get('cityId'))==cid]
 if len(groups)!=1 or not isinstance(groups[0].get('models'),list): raise SetupError('缺少指定网点完整车型列表')
 result=[]; seen=set()
 for m in groups[0]['models']:
  if not isinstance(m,dict) or not str(m.get('modelId','')).isdigit() or str(m['modelId']) in seen: raise SetupError('车型列表编号缺失或重复')
  seen.add(str(m['modelId'])); inv=m.get('inventoryType')
  if isinstance(inv,bool) or str(inv) not in ('0','1','2','3'): raise SetupError('库存状态未知')
  p=price(m)
  if int(inv) in (0,1) and p is None: raise SetupError('可选车型价格无效')
  if int(inv) in (0,1) and p is not None and p < LIMIT and is_nev(m): result.append({'model_id':str(m['modelId']),'name':str(m['modelName']),'price':str(p),'inventory_type':int(inv),'station_id':sid,'station_name':str(groups[0].get('deptName')),'pickup':pickup,'return':ret})
 return result
def gather():
 with sync_playwright() as pw:
  opts={}; pu=urllib.request.getproxies().get('https')
  if pu:
   p=urllib.parse.urlsplit(pu); opts['proxy']={'server':p.scheme+'://'+p.hostname+(':'+str(p.port) if p.port else '')}
   if p.username:opts['proxy']['username']=urllib.parse.unquote(p.username)
   if p.password:opts['proxy']['password']=urllib.parse.unquote(p.password)
  ctx=pw.request.new_context(base_url=HOME,extra_http_headers={'Referer':HOME},**opts)
  try:
   cid=city_id(post(ctx,CITY_API,{})); found=[]; checked=[]; failures=[]; warnings=[]
   for pickup,ret in WINDOWS:
    lease={'cityId':cid,'entrance':1,'estimatedPickupTime':pickup,'estimatedReturnTime':ret}
    # 先用不带租期的官方清单枚举全部网点；带租期接口会隐藏当时不可服务的网点。
    ss=stations(post(ctx,DEPT_API,{'cityId':cid,'pickupFlag':1,'entrance':1}))
    for s in ss:
     sid=str(s['deptId']); name=str(s.get('deptName')); addr=str(s.get('deptAddress'))
     try:
      back=stations(post(ctx,DEPT_API,{**lease,'pickupFlag':0,'pickupDeptId':sid}))
      # deptList 在“该租期无车/不可服务”时会隐藏网点；这不是车型查询的充分理由。
      # 继续请求 chooseCar，避免把“无库存”误报成“不能同网点还车”。
      return_open=any(str(x.get('deptId'))==sid and x.get('inWorkFlag') is True for x in back)
      if not return_open: warnings.append({'id':sid,'name':name,'pickup':pickup,'return':ret,'reason':'lease_dept_hidden_or_no_inventory'})
      req={'pickupCityId':cid,'returnCityId':cid,'pickupDeptId':sid,'pickupTime':pickup,'returnTime':ret,'entrance':1,'holidaysWaitingFlag':0}
      hit=cars(post(ctx,CAR_API,req),sid,cid,pickup,ret); found.extend(hit); checked.append({'id':sid,'name':name,'address':addr,'pickup':pickup,'return':ret,'count':len(hit),'return_open':return_open})
     except Exception as e: failures.append({'id':sid,'name':name,'pickup':pickup,'return':ret,'reason':type(e).__name__})
   if failures and not checked: raise SetupError('所有网点查询失败')
   return {'state':'possible' if found else 'notfound','matches':found,'checked':checked,'failures':failures,'warnings':warnings,'city_id':cid,'verified_utc':now(),'source':'official_web_api'}
  finally:ctx.dispose()
def bark(key,items):
 if not key:return False,'BARK_KEY_missing'
 title=urllib.parse.quote('大庆新能源车源新增',safe='')
 body=urllib.parse.quote('；'.join(f"{x['station_name']} {x['name']} ¥{x['price']} {x['pickup']}" for x in items),safe='')
 url='https://api.day.app/'+key+'/'+title+'/'+body
 req=urllib.request.Request(url,method='GET')
 try:
  with urllib.request.urlopen(req,timeout=20) as r:
   return (True,'') if r.status==200 else (False,f'HTTP_{r.status}')
 except urllib.error.HTTPError as e:return False,f'HTTP_{e.code}'
 except urllib.error.URLError as e:return False,f'URL_{type(e.reason).__name__}'
 except TimeoutError:return False,'timeout'
 except Exception as e:return False,type(e).__name__
def main():
 old=json.loads(STATE.read_text()) if STATE.exists() else {'active':{}}
 retry_failed=False
 if HISTORY.exists():
  try:
   prior=json.loads(HISTORY.read_text())
   retry_failed=any(isinstance(x,dict) and x.get('push')=='failed' for x in (prior[-3:] if isinstance(prior,list) else []))
  except Exception: pass
 cutoff=datetime(2026,10,17,1,tzinfo=timezone.utc)
 if datetime.now(timezone.utc)>=cutoff: result={'state':'stopped','matches':[],'why':'监控租期已结束','verified_utc':now()}
 else:
  try: result=gather()
  except Exception as e: result={'state':'unknown','matches':[],'why':type(e).__name__,'verified_utc':now()}
 key=os.getenv('BARK_KEY',''); current={f"{x['station_id']}|{x['pickup']}|{x['return']}|{x['model_id']}":x for x in result.get('matches',[])}
 prev={} if retry_failed else (old.get('active',{}) if isinstance(old,dict) else {}); fresh=[]
 if result['state']=='possible': fresh=[x for k,x in current.items() if k not in prev]
 pushed=False; push_error=''
 if fresh:pushed,push_error=bark(key,fresh)
 if result['state']=='possible':
  result['new_count']=len(fresh); result['push']='sent' if pushed else ('not_triggered' if not fresh else 'failed')
  if push_error: result['push_error']=push_error
  # Bark 失败时保留旧去重集合，下一轮继续重试，不把未送达车型标记为已通知。
  newactive=current if (not fresh or pushed) else prev
 elif result['state']=='notfound': result['new_count']=0; result['push']='not_triggered'; newactive={}
 else: result['new_count']=0; result['push']='not_triggered'; newactive=prev
 result['active']=newactive; result['verified_utc']=result.get('verified_utc',now()); STATE.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n'); REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 hist=[]
 if HISTORY.exists():
  try: hist=json.loads(HISTORY.read_text())
  except Exception: hist=[]
 hist=(hist if isinstance(hist,list) else [])+[{k:result.get(k) for k in ('state','new_count','push','verified_utc','checked','failures','warnings')}]; HISTORY.write_text(json.dumps(hist[-240:],ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:result.get(k) for k in ('state','new_count','push','verified_utc')},ensure_ascii=False)); return 0 if result['state']!='unknown' else 1
if __name__=='__main__': raise SystemExit(main())
