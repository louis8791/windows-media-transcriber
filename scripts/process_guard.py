"""Hidden, bounded native execution; descendants belong to this run on Windows."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class ProcessFailure(RuntimeError):
    def __init__(self, reason, stage, message):
        self.reason, self.stage = reason, stage
        super().__init__(message)


class JobLimits(c.Structure):
    _fields_ = [('time1', c.c_longlong), ('time2', c.c_longlong), ('flags', w.DWORD),
                ('min', c.c_size_t), ('max', c.c_size_t), ('active', w.DWORD),
                ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class IoCounters(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class ExtendedLimits(c.Structure):
    _fields_ = [('basic', JobLimits), ('io', IoCounters), ('process_memory', c.c_size_t),
                ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]


class OwnedProcess:
    def __init__(self, command, **kwargs):
        if os.name != 'nt':
            raise RuntimeError('此執行器需要 Windows Job Object。')
        self.kernel = k = c.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR];k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE,w.HANDLE]
        k.TerminateJobObject.argtypes = [w.HANDLE,w.UINT]
        k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = k.CreateJobObjectW(None,None)
        if not self.handle:raise c.WinError(c.get_last_error())
        limits = ExtendedLimits();limits.basic.flags = 0x2000  # kill owned tree on handle close
        self.process = None
        try:
            if not k.SetInformationJobObject(self.handle,9,c.byref(limits),c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            # Attach before execution, so even a fast child cannot escape ownership.
            self.process = subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW | 0x4,**kwargs)
            if not k.AssignProcessToJobObject(self.handle,w.HANDLE(int(self.process._handle))):
                raise c.WinError(c.get_last_error())
            resume = c.WinDLL('ntdll').NtResumeProcess
            resume.argtypes=[w.HANDLE];resume.restype=c.c_long
            if resume(w.HANDLE(int(self.process._handle))) < 0:
                raise RuntimeError('無法啟動已隔離的工作程序。')
        except Exception:
            if self.process is not None:self.process.kill();self.process.wait(timeout=5)
            self.close();raise

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle,1)
            self.kernel.CloseHandle(self.handle);self.handle=None
        if self.process is not None:
            self.process.wait(timeout=10)


def run_guarded(command, stage, *, cwd, timeout, idle_timeout, cancel=None, emit=print, progress_paths=()):
    if timeout <= 0 or idle_timeout <= 0:raise ValueError('逾時必須大於零。')
    owned = OwnedProcess(command,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
    process=owned.process;events=queue.Queue();tail=bytearray()
    def reader():
        try:
            while True:
                data=process.stdout.read1(4096)
                if not data:break
                events.put(data)
        finally:events.put(None)
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    started=last=time.monotonic();ended=False;previous=None
    try:
        while True:
            now=time.monotonic()
            if cancel is not None and Path(cancel).exists():
                raise ProcessFailure('cancelled',stage,'已停止；已完成分段保留，下次可續跑。')
            if now-started >= timeout:raise ProcessFailure('timeout',stage,f'{stage}超過總等待上限 {timeout} 秒。')
            if now-last >= idle_timeout:raise ProcessFailure('stalled',stage,f'{stage}連續 {idle_timeout} 秒沒有進度。')
            try:
                data=events.get(timeout=.05)
                if data is None:ended=True
                else:
                    last=now;tail.extend(data);del tail[:-8000]
                    emit(data.decode('utf-8',errors='replace').rstrip())
            except queue.Empty:pass
            signatures=[]
            for path in progress_paths:
                try:signatures.append((Path(path).stat().st_size,Path(path).stat().st_mtime_ns))
                except OSError:signatures.append(None)
            if previous is not None and signatures != previous:last=now
            previous=signatures
            if ended and process.poll() is not None:break
        if process.returncode:
            detail=tail.decode('utf-8',errors='replace')
            normalized=detail.casefold();reason='process_exit'
            for candidate,words in [('out_of_memory',('out of memory','bad_alloc','cannot allocate memory')),
                                    ('disk_space',('no space left','disk full','not enough space')),
                                    ('permission',('permission denied','access is denied'))]:
                if any(word in normalized for word in words):reason=candidate;break
            raise ProcessFailure(reason,stage,f'{stage}失敗，退出碼 {process.returncode}：{detail[-1600:]}')
        return bytes(tail)
    finally:
        owned.close();thread.join(timeout=2);process.stdout.close()


def memory_status():
    class Memory(c.Structure):
        _fields_=[('length',w.DWORD),('load',w.DWORD)]+[(name,c.c_ulonglong) for name in ('total','available','total_page','available_page','total_virtual','available_virtual','extended')]
    data=Memory();data.length=c.sizeof(data)
    if not c.windll.kernel32.GlobalMemoryStatusEx(c.byref(data)):raise c.WinError()
    return data


def available_memory():
    # Windows models are pageable: available commit, rather than free RAM alone,
    # is the allocation budget. Physical pressure is reported separately.
    return memory_status().available_page
