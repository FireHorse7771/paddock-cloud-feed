from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import hashlib, math, re, time
ORIGIN = 'https://sport-iframe.thregvrvd.xyz'
LIST = ORIGIN+'/uk/sports/horseracing'
PUBLIC = 'https://pbcorp.xyz/uk/sport/events/'
def utc(): return datetime.now(timezone.utc).isoformat()


def event_path(value):
    route=urlsplit(value)
    if route.netloc and (route.scheme!='https' or route.netloc!=urlsplit(ORIGIN).netloc):
        raise ValueError('Unexpected event origin')
    if route.query or route.fragment or not re.fullmatch(r'/uk/events/race-\d+-\d+',route.path):
        raise ValueError('Unexpected event route')
    return route.path

def parse_event(text, path, observed_at):
    path=event_path(path)
    lines=[x.strip() for x in text.splitlines() if x.strip()]
    if lines.count('Переможець')!=1 or 'Купон' not in lines:
        raise ValueError('One explicit WIN market required')
    market=lines.index('Переможець'); end=lines.index('Купон',market)
    head=lines[:market];rows=lines[market+1:end]
    race=next((x for x in head if re.fullmatch(r'RACE \d+',x,re.I)),None)
    day=next((x for x in head if x in ('СЬОГОДНІ','ЗАВТРА')),None)
    clock=next((x for x in head if re.fullmatch(r'\d{2}:\d{2}',x)),None)
    meeting_index=max(i for i,x in enumerate(head) if x=='Кінні перегони')
    meeting=head[meeting_index+1]
    number=int(re.search(r'race-(\d+)-',path).group(1))
    if not race or int(race.split()[-1])!=number or not day or not clock or not meeting or '.' not in meeting:
        raise ValueError('Event identity incomplete')
    if len(rows)%4 or not 2<=len(rows)//4<=40:
        raise ValueError('Incomplete or additional markets')
    runners=[];horses=set();numbers=set()
    for i in range(0,len(rows),4):
        num,horse,jockey,quote=rows[i:i+4]
        if not num.isdigit() or int(num)<1 or not horse or not jockey:
            raise ValueError('Incomplete runner')
        price=float(quote.replace(',','.'))
        if not math.isfinite(price) or not 1<price<=10000 or horse.casefold() in horses or int(num) in numbers:
            raise ValueError('Invalid or repeated quote')
        horses.add(horse.casefold());numbers.add(int(num))
        runners.append(dict(bookmaker_number=int(num),horse=horse,jockey=jockey,decimal_odds=price))
    captured=datetime.fromisoformat(observed_at)
    if captured.tzinfo is None:raise ValueError('Timezone required')
    date=captured.astimezone(ZoneInfo('Europe/Kyiv')).date()
    if day=='ЗАВТРА':date+=timedelta(days=1)
    return dict(event_id=path.split('-')[-1], race_number=number, race_label=race,
        meeting=meeting, displayed_time=clock,date_label=day,race_date=date.isoformat(),
        observed_at=observed_at, public_url=PUBLIC+path.rsplit('/',1)[-1],
        market='WIN',runners=runners,complete_market=True,
        source_url=ORIGIN+path,source_updated_at=None,
        executable_prices_verified=False,forecast_pairing_verified=False)

async def collect(context, archive, budget_seconds=110):
    import asyncio
    started=time.monotonic(); cards=[]; failures=[]; seen=set(); paths=[]
    page=await context.new_page()
    try:
        await page.goto(LIST,wait_until='domcontentloaded',timeout=35000)
        await page.locator('#page-wrapper a[href*="/uk/sports/horseracing/"]').first.wait_for(timeout=25000)
        links=await page.locator('#page-wrapper a[href*="/uk/sports/horseracing/"]').evaluate_all(
            '(es)=>es.map(e=>({href:e.getAttribute("href"),text:e.innerText}))')
        courses={x['href']:x['text'] for x in links if x['href'].startswith('/uk/sports/horseracing/')}
        courses=sorted(courses,key=lambda x:('kempton-' not in x,x))[:12]
        for course in courses:
            if time.monotonic()-started>budget_seconds:
                failures.append('scan_budget');break
            try:
                await page.goto(ORIGIN+course,wait_until='domcontentloaded',timeout=25000)
                await page.locator('#page-wrapper a[href*="/uk/events/race-"]').first.wait_for(timeout=10000)
                found=await page.locator('#page-wrapper a[href*="/uk/events/race-"]').evaluate_all('(es)=>es.map(e=>e.getAttribute("href"))')
                for item in found:
                    path=event_path(item)
                    if path not in seen:
                        seen.add(path);paths.append(path)
            except Exception:
                failures.append(course)
    finally:
        await page.close()
    if len(paths)>60:failures.append('scope_limit')
    queue=asyncio.Queue()
    for path in paths[:60]:queue.put_nowait(path)
    async def worker():
        worker_page=await context.new_page()
        try:
            while not queue.empty():
                path=queue.get_nowait()
                if time.monotonic()-started>budget_seconds:
                    failures.append(path);continue
                try:
                    await worker_page.goto(ORIGIN+path,wait_until='domcontentloaded',timeout=20000)
                    if worker_page.url!=ORIGIN+path:raise ValueError('Event navigation redirected')
                    await worker_page.get_by_text('Переможець',exact=True).first.wait_for(timeout=10000)
                    await worker_page.locator('span.advanced-outcome-regular-title').first.wait_for(timeout=10000)
                    for attempt in range(5):
                        text=await worker_page.locator('body').inner_text(timeout=5000)
                        raw=text.encode('utf8');sha=hashlib.sha256(raw).hexdigest()
                        archive.mkdir(parents=True,exist_ok=True);receipt=archive/(sha+'.txt')
                        if not receipt.exists():receipt.write_bytes(raw)
                        elif receipt.read_bytes()!=raw:raise ValueError('Receipt collision')
                        try:
                            card=parse_event(text,path,utc());break
                        except (ValueError,StopIteration):
                            if attempt==4:raise
                            await asyncio.sleep(.3)
                    card['dom_sha256']=sha;cards.append(card)
                except Exception as error:
                    failures.append(dict(route=path,reason=type(error).__name__+': '+str(error)[:180]))
                await asyncio.sleep(.5)
        finally:
            await worker_page.close()
    # At most two read-only pages; no account, odds clicks or hidden API calls.
    await asyncio.gather(worker(),worker())
    cards.sort(key=lambda c:('Кемптон' not in c['meeting'],c['meeting'],c['race_number']))
    return cards,len(courses),len(seen),failures
