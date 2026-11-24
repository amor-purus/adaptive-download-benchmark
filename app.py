"""Windows desktop interface; run python app.py."""
import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import uuid
import core


class App:
    def __init__(self, root):
        self.root=root
        self.q=queue.Queue()
        self.cancel=threading.Event()
        self.info=self.result=self.url=None
        self.busy=False
        root.title('自适应下载性能分析工具 0.1.0')
        root.geometry('820x660')
        root.minsize(650,540)
        frame=ttk.Frame(root,padding=20)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='自适应下载性能分析工具',font=('Microsoft YaHei UI',19)).pack(anchor='w')
        ttk.Label(frame,text='输入直接 HTTP/HTTPS 文件链接，自动比较下载方案。').pack(anchor='w',pady=(4,12))
        self.entry=ttk.Entry(frame)
        self.entry.pack(fill='x')
        opts=ttk.Frame(frame)
        opts.pack(fill='x',pady=12)
        self.sample=tk.StringVar(value='4')
        self.rounds=tk.StringVar(value='2')
        self.connections=tk.StringVar(value='16')
        for i,(label,var,values) in enumerate([('单次样本 MiB',self.sample,('1','2','4','8')),('每组次数',self.rounds,('2','3')),('最大连接数',self.connections,('1','2','4','8','16'))]):
            ttk.Label(opts,text=label).grid(row=0,column=i,padx=5,sticky='w')
            ttk.Combobox(opts,textvariable=var,values=values,state='readonly',width=14).grid(row=1,column=i,padx=5,sticky='w')
        buttons=ttk.Frame(frame)
        buttons.pack(fill='x')
        self.start=ttk.Button(buttons,text='检测并测试',command=self.begin)
        self.start.pack(side='left')
        self.stop=ttk.Button(buttons,text='取消',command=self.cancel.set,state='disabled')
        self.stop.pack(side='left',padx=6)
        self.save=ttk.Button(buttons,text='按推荐方案下载',command=self.save_file,state='disabled')
        self.save.pack(side='left',padx=6)
        self.open_report=ttk.Button(buttons,text='打开报告目录',command=self.show_report,state='disabled')
        self.open_report.pack(side='left')
        self.status=tk.StringVar(value='就绪')
        ttk.Label(frame,textvariable=self.status,wraplength=750).pack(anchor='w',pady=12)
        self.table=ttk.Treeview(frame,columns=('name','connections','speed','runs'),show='headings',height=10)
        for k,l,w in [('name','下载方案',240),('connections','连接数',90),('speed','中位速度 MiB/s',150),('runs','成功次数',90)]:
            self.table.heading(k,text=l)
            self.table.column(k,width=w,anchor='center')
        self.table.pack(fill='both',expand=True)
        self.notes=tk.Text(frame,height=6,wrap='word',state='disabled')
        self.notes.pack(fill='x',pady=(12,0))
        self.report=None
        root.after(100,self.poll)
        root.protocol('WM_DELETE_WINDOW',self.close)

    def log(self,s):
        self.notes.config(state='normal')
        self.notes.insert('end',s+'\n')
        self.notes.see('end')
        self.notes.config(state='disabled')

    def set_busy(self,value):
        self.busy=value
        self.start.config(state='disabled' if value else 'normal')
        self.stop.config(state='normal' if value else 'disabled')
        self.save.config(state='disabled' if value or not self.info or self.info.get('size') is None or self.result is None else 'normal')

    def worker(self, fn):
        def run():
            try:
                fn()
            except Exception as e:
                self.q.put(('error',str(e) if isinstance(e,core.BenchmarkError) else '操作失败：'+type(e).__name__))
            finally:
                self.q.put(('done',None))
        self.cancel.clear()
        self.set_busy(True)
        threading.Thread(target=run,daemon=True).start()

    def begin(self):
        try:
            url=core.check_url(self.entry.get().strip())
        except (ValueError,core.BenchmarkError) as e:
            messagebox.showerror('输入错误',str(e));return
        sample,rounds,maximum=map(int,(self.sample.get(),self.rounds.get(),self.connections.get()))
        # Conservative upper bound: at most 7 candidates, 3 repeats, plus headers/probe.
        budget=sample*rounds*(len([n for n in core.LEVELS if n<=maximum])+2)
        if not messagebox.askyesno('流量确认','样本有效载荷最多约 %d MiB（另有协议开销），不支持安全分片时自动跳过。继续？'%budget):
            return
        self.info=self.result=self.report=None
        self.open_report.config(state='disabled')
        for item in self.table.get_children():self.table.delete(item)
        self.url=url
        def task():
            info=core.probe(url)
            self.q.put(('progress','探测完成：%s；Range=%s；强 ETag=%s'%(info['host'],info['range'],bool(info['etag']))))
            result=core.benchmark(url,info,sample,rounds,maximum,self.cancel,lambda x:self.q.put(('progress',x)))
            stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
            report=core.export_report(Path.home()/'Downloads'/'AdaptiveDownloadBenchmark'/stamp,info,result)
            self.q.put(('result',(info,result,report)))
        self.worker(task)

    def save_file(self):
        if not self.info or not self.result:return
        if self.result.get('reason','').startswith(('服务器拒绝','资源 ETag','同一范围','curl 响应','已取消')):
            messagebox.showwarning('停止','请重新测试；当前结果不适合继续下载。');return
        if self.result['rows'] and not self.result['recommendation']:
            messagebox.showwarning('停止','测试未得出可靠方案，请重新测试。');return
        name=filedialog.asksaveasfilename(title='选择目标文件名')
        if not name:return
        if Path(name).exists():
            messagebox.showwarning('文件存在','请选择新文件名。');return
        from tkinter.simpledialog import askstring
        sha=askstring('可选完整性校验','输入官方 SHA-256（可留空；取消则不下载）：',parent=self.root)
        if sha is None:return
        choice=self.result['recommendation']
        if not messagebox.askyesno('确认下载','方案：%s\n文件大小：%.2f MiB\n开始正式下载？'%((choice or {}).get('name','未测试单连接'),self.info['size']/core.MIB)):return
        def task():
            self.q.put(('progress','正在下载及校验完整文件…'))
            result=core.download(self.url,self.info,choice,name,self.cancel,expected_sha=sha.strip())
            self.q.put(('downloaded',result))
        self.worker(task)

    def show_report(self):
        if self.report:
            os.startfile(str(self.report))

    def poll(self):
        try:
            while True:
                kind,data=self.q.get_nowait()
                if kind=='progress':
                    self.status.set(data);self.log(data)
                elif kind=='result':
                    self.info,self.result,self.report=data
                    for s in self.result['summary']:
                        self.table.insert('', 'end',values=(s['name'],s['connections'],'%.3f'%s['median_mib_s'],s['runs']))
                    choice=self.result['recommendation']
                    msg=('推荐：'+choice['name']) if choice else self.result['reason']
                    self.status.set(msg);self.log(msg);self.log('报告：'+str(self.report))
                    self.open_report.config(state='normal')
                elif kind=='downloaded':
                    self.status.set('文件下载成功，长度校验通过。')
                    self.log('SHA-256: '+data['sha256'])
                    self.log('可信哈希核对：'+('通过' if data['trusted_hash_verified'] else '未提供可信值'))
                elif kind=='error':
                    self.status.set(data);self.log(data)
                elif kind=='done':
                    self.set_busy(False)
        except queue.Empty:pass
        self.root.after(100,self.poll)

    def close(self):
        if self.busy:
            self.cancel.set()
            messagebox.showinfo('正在取消','请在任务结束后关闭窗口。')
        else:self.root.destroy()


if __name__=='__main__':
    root=tk.Tk()
    App(root)
    root.mainloop()
