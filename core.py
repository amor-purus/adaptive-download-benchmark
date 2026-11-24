"""Bounded, reproducible HTTP download benchmark. Standard library, Python 3.10+."""
import concurrent.futures as cf
import csv
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MIB = 1024**2
TIMEOUT = 15
BLOCK = 64 * 1024
LEVELS = (1, 2, 4, 8, 16)


class BenchmarkError(Exception):
    pass


class Stop(BenchmarkError):
    pass


def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username is not None or p.password is not None or any(c.isspace() for c in url):
        raise BenchmarkError('请输入不含账号密码的 HTTP/HTTPS 文件链接。')
    return url


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    return p.scheme + '://' + (p.hostname or '')  # no path/query/token in reports


def request(url, headers=None):
    h = {'Accept-Encoding': 'identity', 'User-Agent': 'AdaptiveDownloadBenchmark/0.1'}
    h.update(headers or {})
    try:
        response = urllib.request.urlopen(urllib.request.Request(check_url(url), headers=h), timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        code = e.code
        e.close()
        if code in (403, 429):
            raise Stop('服务器拒绝访问或限流 (HTTP %s)，已停止。' % code) from None
        raise BenchmarkError('HTTP %s' % code) from None
    except (OSError, urllib.error.URLError):
        raise BenchmarkError('连接失败或超时。') from None
    try:
        check_url(response.geturl())
        if urllib.parse.urlsplit(url).scheme == 'https' and urllib.parse.urlsplit(response.geturl()).scheme != 'https':
            raise Stop('拒绝 HTTPS 降级。')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise BenchmarkError('服务器使用非 identity 内容编码。')
        return response
    except BaseException:
        response.close()
        raise


def content_range(value):
    m = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', value or '')
    if not m:
        raise BenchmarkError('无效 Content-Range。')
    a,b,total = map(int, m.groups())
    if not 0 <= a <= b < total:
        raise BenchmarkError('Content-Range 越界。')
    return a,b,total


def strong_etag(value):
    return value if value and re.fullmatch(r'"[^"\r\n]*"', value) else None


def probe(url):
    start = time.perf_counter()
    with request(url, {'Range': 'bytes=0-0'}) as r:
        size = None
        ranged = r.status == 206
        if ranged:
            a,b,size = content_range(r.headers.get('Content-Range'))
            if (a,b) != (0,0) or len(r.read(2)) != 1:
                raise BenchmarkError('Range 探测返回内容不符。')
        elif r.status == 200:
            length = r.headers.get('Content-Length')
            size = int(length) if length and length.isdecimal() else None
        else:
            raise BenchmarkError('不支持的 HTTP 响应。')
        if size is not None and size <= 0:
            raise BenchmarkError('空文件无需性能测试。')
        return {'host':safe_url(r.geturl()), 'redirected': r.geturl() != url,
                'size':size, 'range':ranged, 'etag':strong_etag(r.headers.get('ETag')),
                'probe_seconds':round(time.perf_counter()-start,4),
                'type':r.headers.get('Content-Type','')}


def split_ranges(size, n, offset=0):
    n = min(n, size)
    q,r = divmod(size,n)
    result=[]
    a=offset
    for i in range(n):
        b=a+q+(i<r)-1
        result.append((a,b))
        a=b+1
    return result


def read_exact(r, length, cancel, output):
    count=0
    deadline=time.monotonic()+max(30, length/MIB*8)
    while count < length:
        if cancel.is_set():
            raise Stop('已取消。')
        if time.monotonic()>deadline:
            raise BenchmarkError('单分片耗时超过安全限制。')
        data=r.read(min(BLOCK,length-count))
        if not data:
            raise BenchmarkError('响应提前结束。')
        output.write(data)
        count+=len(data)
    if r.read(1):
        raise BenchmarkError('响应超过预期范围。')


def get_range(url, info, a,b, output, cancel):
    if cancel.is_set():
        raise Stop('已取消。')
    h={'Range':'bytes=%s-%s'%(a,b),'If-Match':info['etag']}
    with request(url,h) as r:
        if r.status != 206 or content_range(r.headers.get('Content-Range')) != (a,b,info['size']):
            raise BenchmarkError('服务器未正确返回指定范围。')
        if r.headers.get('ETag') != info['etag']:
            raise Stop('资源 ETag 已变化，停止以防止文件混合。')
        read_exact(r,b-a+1,cancel,output)


def native_sample(url, info, offset, length, n, cancel, dest):
    ranges=split_ranges(length,n,offset)
    parts=[dest/('part-%d'%i) for i in range(len(ranges))]
    local_stop=threading.Event()
    class Combined:
        def is_set(self):
            return cancel.is_set() or local_stop.is_set()
    def worker(i):
        with parts[i].open('wb') as out:
            get_range(url,info,*ranges[i],out,Combined())
    with cf.ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        fs=[pool.submit(worker,i) for i in range(len(ranges))]
        try:
            for f in cf.as_completed(fs):
                f.result()
        except BaseException:
            local_stop.set()
            for f in fs:
                f.cancel()
            raise
    digest=hashlib.sha256()
    for part in parts:
        with part.open('rb') as src:
            for block in iter(lambda:src.read(BLOCK),b''):
                digest.update(block)
    return digest.hexdigest()


def curl_sample(url, info, offset, length, cancel, dest, version):
    exe=shutil.which('curl.exe') or shutil.which('curl')
    if not exe:
        raise BenchmarkError('curl 未安装。')
    a,b=offset,offset+length-1
    body=dest/'curl.bin'
    headers=dest/'headers.txt'
    args=[exe,'--disable','--silent','--show-error','--fail','--location','--max-redirs','8',
          '--proto','=http,https','--proto-redir','=https' if url.startswith('https:') else '=http,https',
          '--connect-timeout','10','--max-time','90','--max-filesize',str(length),
          '--header','Accept-Encoding: identity','--header','If-Match: '+info['etag'],
          '--range','%d-%d'%(a,b),'--dump-header',str(headers),'--output',str(body),version,'--',url]
    proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        while proc.poll() is None:
            if cancel.wait(.1):
                raise Stop('已取消。')
            if body.exists() and body.stat().st_size>length:
                raise Stop('curl 超过样本预算。')
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    raw=headers.read_text(encoding='latin1') if headers.exists() else ''
    blocks=re.split(r'\r?\n\r?\n',raw)
    last=next((s for s in reversed(blocks) if s.startswith('HTTP/')),'')
    status=re.match(r'HTTP/\S+\s+(\d+)',last)
    if status and status.group(1) in ('403','429'):
        raise Stop('服务器拒绝访问或限流，已停止。')
    if proc.returncode or not status or status.group(1)!='206':
        raise BenchmarkError('curl 下载失败或 Range 不支持。')
    hs=dict((k.lower(),v.strip()) for k,v in (line.split(':',1) for line in last.splitlines()[1:] if ':' in line))
    if content_range(hs.get('content-range'))!=(a,b,info['size']) or hs.get('etag')!=info['etag'] or hs.get('content-encoding','identity').lower()!='identity':
        raise Stop('curl 响应与固定资源不一致。')
    if body.stat().st_size!=length:
        raise BenchmarkError('curl 样本大小不符。')
    return sha256_file(body)


def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(BLOCK),b''):
            h.update(block)
    return h.hexdigest()


def candidates(length, use_curl=True, max_connections=16):
    result=[('native',n,'') for n in LEVELS if n<=max_connections and (n==1 or length//n>=256*1024)]
    exe=shutil.which('curl.exe') or shutil.which('curl')
    if use_curl and exe:
        result.append(('curl',1,'--http1.1'))
        try:
            v=subprocess.run([exe,'--disable','--version'],capture_output=True,text=True,timeout=5).stdout
            if re.search(r'\bHTTP2\b',v):
                result.append(('curl',1,'--http2'))
        except (OSError,subprocess.SubprocessError):
            pass
    return result


def recommend(rows, rounds, count):
    if len(rows)!=rounds*count or any(r['status']!='OK' for r in rows):
        return [],None
    groups={}
    for r in rows:
        groups.setdefault(r['name'],[]).append(r)
    if len(groups)!=count or any(len(v)!=rounds for v in groups.values()):
        return [],None
    summary=[]
    for name,rs in groups.items():
        summary.append({'name':name,'engine':rs[0]['engine'],'connections':rs[0]['connections'],
                        'protocol':rs[0]['protocol'],'runs':len(rs),'median_mib_s':statistics.median(r['mib_s'] for r in rs),
                        'min_mib_s':min(r['mib_s'] for r in rs)})
    best=max(s['median_mib_s'] for s in summary)
    if best<=0:
        return summary,None
    eligible=[s for s in summary if s['median_mib_s']>=.95*best]
    choice=min(eligible,key=lambda s:(s['connections'],-s['min_mib_s'],-s['median_mib_s']))
    return summary,choice


def benchmark(url, info, sample_mib, rounds, max_connections, cancel, progress=lambda _:None):
    if not info['range'] or not info['etag'] or not info['size']:
        return {'rows':[],'summary':[],'recommendation':None,'reason':'无有效 Range 或强 ETag：不能可靠地比较分片方案。建议单连接；未实测。'}
    if rounds not in (2,3) or sample_mib not in (1,2,4,8) or max_connections not in LEVELS:
        raise BenchmarkError('无效测试配置。')
    length=min(info['size'],sample_mib*MIB)
    cs=candidates(length,max_connections=max_connections)
    # Reuse exactly the same range across engines within each round.
    rows=[]
    expected={}
    reason=''
    try:
        for r in range(rounds):
            offset=(info['size']-length)*r//max(1,rounds-1)
            for j in range(len(cs)):
                if cancel.is_set():
                    raise Stop('已取消。')
                engine,n,version=cs[(j+r)%len(cs)]
                name=('原生 '+str(n)+' 连接') if engine=='native' else ('curl '+version.removeprefix('--'))
                progress('第 %d/%d 轮：%s'%(r+1,rounds,name))
                row={'round':r+1,'name':name,'engine':engine,'connections':n,'protocol':version,'offset':offset,'bytes':length,'seconds':0,'mib_s':0,'status':'Failed','error':''}
                try:
                    with tempfile.TemporaryDirectory(prefix='adb-sample-') as tmp:
                        start=time.perf_counter()
                        if engine=='native':
                            digest=native_sample(url,info,offset,length,n,cancel,Path(tmp))
                        else:
                            digest=curl_sample(url,info,offset,length,cancel,Path(tmp),version)
                        elapsed=max(.000001,time.perf_counter()-start)
                        if r in expected and expected[r]!=digest:
                            raise Stop('同一范围的样本 SHA-256 不一致。')
                        expected[r]=digest
                        row.update(seconds=elapsed,mib_s=length/MIB/elapsed,status='OK')
                except (BenchmarkError,OSError,subprocess.SubprocessError) as e:
                    row['error']=str(e) if isinstance(e,BenchmarkError) else '本地或网络 I/O 错误。'
                    rows.append(row)
                    raise Stop(row['error']) from None
                rows.append(row)
                if cancel.wait(.75):
                    raise Stop('已取消。')
    except BenchmarkError as e:
        reason=str(e)
    summary,choice=recommend(rows,rounds,len(cs))
    return {'rows':rows,'summary':summary,'recommendation':choice,'reason':reason or ('测试完成' if choice else '没有有效推荐')}


def download(url, info, choice, destination, cancel, progress=lambda _:None, expected_sha=''):
    dest=Path(destination)
    if dest.exists():
        raise BenchmarkError('目标文件已存在，拒绝覆盖。')
    if expected_sha and not re.fullmatch(r'[A-Fa-f0-9]{64}',expected_sha):
        raise BenchmarkError('SHA-256 必须是 64 位十六进制。')
    current=probe(url)
    if info['etag'] and (current['etag']!=info['etag'] or current['size']!=info['size']):
        raise Stop('资源已变化，请重新测试。')
    if current['size'] is None:
        raise BenchmarkError('文件长度未知，第一版不执行正式下载。')
    with tempfile.TemporaryDirectory(prefix='.adb-',dir=dest.parent) as tmp:
        root=Path(tmp)
        merged=root/'complete'
        # curl is benchmarked as a single-connection engine; use its selected HTTP mode for final full range.
        if choice and choice['engine']=='curl' and current['range'] and current['etag']:
            curl_sample(url,current,0,current['size'],cancel,root,choice['protocol'])
            (root/'curl.bin').rename(merged)
        elif current['range'] and current['etag'] and choice and choice['engine']=='native':
            n=choice['connections']
            # Keep actual file transfers to the proven connection count.
            native_sample(url,current,0,current['size'],n,cancel,root)
            with merged.open('wb') as out:
                for i in range(min(n,current['size'])):
                    with (root/('part-%d'%i)).open('rb') as src:
                        shutil.copyfileobj(src,out,BLOCK)
        else:
            with request(url,{'If-Match':current['etag']} if current['etag'] else {}) as r, merged.open('wb') as out:
                if r.status!=200 or (current['etag'] and r.headers.get('ETag')!=current['etag']):
                    raise Stop('完整下载响应与资源不一致。')
                read_exact(r,current['size'],cancel,out)
        if cancel.is_set():
            raise Stop('已取消。')
        if merged.stat().st_size!=current['size']:
            raise BenchmarkError('完整文件长度校验失败。')
        digest=sha256_file(merged)
        if expected_sha and digest.lower()!=expected_sha.lower():
            raise BenchmarkError('SHA-256 与用户提供的可信值不一致。')
        if dest.exists():
            raise BenchmarkError('目标文件已存在，拒绝覆盖。')
        # Hard link publishes without overwrite; same volume because temp lives beside destination.
        os.link(merged,dest)
    return {'bytes':current['size'],'sha256':digest,'trusted_hash_verified':bool(expected_sha)}


def export_report(directory, info, result):
    root=Path(directory)
    root.mkdir(parents=True,exist_ok=False)
    public_info={k:v for k,v in info.items() if k!='etag'}
    payload={'resource':public_info,**result}
    (root/'report.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    for filename,rows in [('runs.csv',result['rows']),('summary.csv',result['summary'])]:
        if rows:
            with (root/filename).open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        else:
            (root/filename).write_text('status\nno_data\n',encoding='utf-8-sig')
    esc=lambda s:html.escape(str(s))
    best=max((s['median_mib_s'] for s in result['summary']),default=1) or 1
    bars=''.join('<p>'+esc(s['name'])+' — %.3f MiB/s</p><div style="height:12px;background:#315eb5;width:%.2f%%"></div>'%(s['median_mib_s'],100*s['median_mib_s']/best) for s in result['summary'])
    table='<table><tr><th>轮次</th><th>方案</th><th>字节</th><th>秒</th><th>MiB/s</th><th>状态</th><th>错误</th></tr>'+''.join('<tr>'+''.join('<td>'+esc(r[k])+'</td>' for k in ('round','name','bytes','seconds','mib_s','status','error'))+'</tr>' for r in result['rows'])+'</table>'
    choice=result['recommendation']
    title=choice['name'] if choice else '无可靠推荐'
    (root/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>下载性能报告</title><style>body{font:16px/1.6 system-ui;max-width:1000px;margin:32px auto;padding:16px}td,th{border:1px solid #ddd;padding:6px}table{border-collapse:collapse;width:100%}</style><h1>下载性能报告</h1><h2>推荐：'+esc(title)+'</h2><p>'+esc(result['reason'])+'</p><pre>'+esc(json.dumps(public_info,ensure_ascii=False,indent=2))+'</pre><p>同轮同范围，使用实际字节数/端到端耗时；中位数排序，95% 内优先较低连接数。仅代表本次样本。</p>'+bars+table+'</html>',encoding='utf-8')
    return root
