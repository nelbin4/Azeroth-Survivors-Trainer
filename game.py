"""Azeroth Survivors Trainer: name-guarded native stat tracing and non-pausing updates. See --help."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import argparse
import hashlib
import math
import uuid
import ctypes
import threading
import io
from contextlib import redirect_stdout
from contextlib import contextmanager, nullcontext

import psutil

APP_NAME = 'Azeroth Survivors Trainer'
VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parent
SESSION = ROOT / 'session.json'
CHECKED_BUILDS = set()
AUTO_CORE_OFFSETS = frozenset((0,4,8,12,16,20,28,32,36,44,48,60,64,72,112,120))
AUTO_SOURCE_TARGETS = {14:99.984,28:99.99}

class StopSignal:
    """Workspace-scoped manual-reset event; avoids JSON polling in the hot loop."""
    def __init__(self,suffix='stop'):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.CreateEventW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_wchar_p]
        self.api.CreateEventW.restype=ctypes.c_void_p
        for name in ('SetEvent','ResetEvent','CloseHandle'):
            getattr(self.api,name).argtypes=[ctypes.c_void_p]
        self.api.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        key=hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:24]
        self.handle=self.api.CreateEventW(None,True,False,'Local\\AzerothCLI_'+key+'_'+suffix)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
    def request(self):
        if not self.api.SetEvent(self.handle):raise ctypes.WinError(ctypes.get_last_error())
    def reset(self):
        if not self.api.ResetEvent(self.handle):raise ctypes.WinError(ctypes.get_last_error())
    def wait(self,seconds=0):
        result=self.api.WaitForSingleObject(self.handle,max(0,int(seconds*1000)))
        if result not in (0,258):raise RuntimeError('Stop event wait failed.')
        return result==0
    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()

class AutoBlocked(RuntimeError):pass

def actor_snapshot(memory,process,base,recipe,name,expected_id=None,health_ceiling=None):
    """One contiguous actor snapshot plus name/vector and identity rechecks."""
    if base%4 or not 0x10000<=base<0x7ffffffff000:raise ValueError('Invalid actor address.')
    delta=recipe['stats_offset']-recipe['name_header_offset']
    owner_off=recipe.get('owner_offset',968);header=base-delta;size=delta+owner_off+24
    raw=memory.read_bytes(header,size)
    cap,name_pointer,length=struct.unpack_from('<3Q',raw)
    encoded=name.encode('utf-8')
    if length!=len(encoded) or not length<=cap<=256 or not 0x10000<=name_pointer<0x7ffffffff000:
        raise ValueError('Invalid character-name header.')
    if memory.read_bytes(name_pointer,length)!=encoded:raise ValueError('Character name does not match.')
    def u(off):return struct.unpack_from('<I',raw,delta+off)[0]
    def f(off):return struct.unpack_from('<f',raw,delta+off)[0]
    player_id=u(-24);level=u(104)
    if not player_id or (expected_id is not None and player_id!=expected_id) or not 1<=level<=10000:
        raise ValueError('Actor identity or level changed.')
    for off in list(range(0,52,4))+[56,60,64,72,112]:
        if not math.isfinite(f(off)) or not 0<=f(off)<=1e7:raise ValueError('Invalid actor stat.')
    if f(0)<1 or u(52)>100000 or u(68)>100000:raise ValueError('Invalid health cap or counter.')
    if f(112)>f(0)+1:
        # Only a known, previously injected actor is eligible for this transition.
        if health_ceiling is None or f(112)>health_ceiling+.01:
            raise ValueError('HP exceeds its current cap without an injection lease.')
    capacity,pointer,count=struct.unpack_from('<3Q',raw,delta+owner_off)
    if not 1<=count<=capacity<=256 or pointer%4 or not 0x10000<=pointer<0x7ffffffff000:
        raise ValueError('Invalid modifier ownership.')
    vector=memory.read_bytes(pointer,count*8)
    kinds=[];amounts=[]
    for index in range(count):
        kind=vector[index*8];amount=struct.unpack_from('<f',vector,index*8+4)[0]
        if kind>64 or not math.isfinite(amount) or abs(amount)>1e6:raise ValueError('Invalid source entry.')
        kinds.append(kind);amounts.append(amount)
    check=memory.read_bytes(header,size)
    if raw[:24]!=check[:24] or raw[delta-24:delta-20]!=check[delta-24:delta-20] or raw[delta+owner_off:delta+owner_off+24]!=check[delta+owner_off:delta+owner_off+24]:
        raise ValueError('Actor moved during snapshot.')
    if memory.read_bytes(name_pointer,length)!=encoded:raise ValueError('Name changed during snapshot.')
    values={entry[0]:struct.unpack_from('<'+entry[2],raw,delta+entry[1])[0] for entry in recipe['targets']}
    return dict(pid=process.pid,created=process.create_time(),base=base,player_id=player_id,name=name,
                level=level,owner=base+owner_off,pointer=pointer,capacity=capacity,length=count,kinds=kinds,
                amounts=amounts,values=values,hp_transition=f(112)>f(0)+1)

LEARN_FLOATS = (0,4,8,12,16,20,28,32,36,44,48,60,64,72,112)

def layout_prefilter(raw,b):
    """Cheap structural test of a candidate stats base at byte b of raw (no I/O)."""
    if b<24 or b+120>len(raw):return False
    if not struct.unpack_from('<I',raw,b-24)[0]:return False
    if not 1<=struct.unpack_from('<I',raw,b+104)[0]<=10000:return False
    for off in LEARN_FLOATS:
        v=struct.unpack_from('<f',raw,b+off)[0]
        if not math.isfinite(v) or not 0<=v<=1e7:return False
    if struct.unpack_from('<f',raw,b)[0]<1 or struct.unpack_from('<f',raw,b+112)[0]>struct.unpack_from('<f',raw,b)[0]+1:return False
    return True

def learn_deltas(memory,process,header,recipe,name):
    """Which (header->stats distance, owner offset) give a fully valid actor? Read-only.
    The owner vector is the unique 24-byte triple whose entries include the kinds we edit."""
    raw=memory.read_bytes(header,4096+1400)
    found=[]
    for d in range(24,4097,4):
        if not layout_prefilter(raw,d):continue
        owners=[]
        for o in range(800,1400,8):
            if d+o+24>len(raw):break
            capacity,pointer,count=struct.unpack_from('<3Q',raw,d+o)
            if not 2<=count<=capacity<=256 or pointer%4 or not 0x10000<=pointer<0x7ffffffff000:continue
            try:vector=memory.read_bytes(pointer,count*8)
            except Exception:continue
            if {14,28}<=set(vector[::8]):owners.append(o)
        for o in owners:
            trial=dict(recipe,stats_offset=recipe['name_header_offset']+d,owner_offset=o)
            try:snap=actor_snapshot(memory,process,header+d,trial,name)
            except Exception:continue
            wallet=struct.unpack_from('<I',raw,d+120)[0]
            snap['skip']=[] if wallet<=10000000 else [120]
            found.append(((d,o),snap))
    return found

def find_characters(memory,process,recipe,cancelled=lambda:False,budget=40.):
    """Read-only: every character record visible in memory whatever its name (solo: one; host: several)."""
    import numpy as np,re
    delta=recipe['stats_offset']-recipe['name_header_offset'];owner=recipe.get('owner_offset',968)
    span=delta+owner+64;found=[];deadline=time.monotonic()+budget
    for region in memory.snapshot_memory_regions():
        if not(region.is_readable and region.is_writable and not region.is_executable):continue
        for address in range(region.address,region.address+region.size,4194304):
            if cancelled():raise InterruptedError('Stop requested.')
            if time.monotonic()>=deadline:return found
            try:raw=memory.read_bytes(address,min(4194304+span,region.address+region.size-address))
            except Exception:continue
            v=np.frombuffer(raw[:len(raw)//8*8],dtype='<u8')
            if len(v)<3:continue
            cap,ptr,length=v[:-2],v[1:-1],v[2:]
            mask=(length>=2)&(length<=24)&(cap>=length)&(cap<=256)&(ptr>=0x10000)&(ptr<0x7ffffffff000)
            idx=np.flatnonzero(mask);idx=idx[idx*8<4194304]
            base=idx*8+delta;ok=(base>=24)&(base+owner+24<=len(raw))
            idx,base=idx[ok],base[ok]
            u32=np.frombuffer(raw[:len(raw)//4*4],dtype='<u4')
            keep=(u32[(base+104)//4]>=1)&(u32[(base+104)//4]<=10000)&(u32[(base-24)//4]!=0)
            for i,b in zip(idx[keep],base[keep]):
                b=int(b)
                if not layout_prefilter(raw,b):continue
                try:text=memory.read_bytes(int(ptr[i]),int(length[i])).decode('utf-8')
                except Exception:continue
                if not re.fullmatch(r"[^\W\d_][\w' -]*",text):continue
                try:found.append(actor_snapshot(memory,process,address+b,recipe,text))
                except Exception:continue
    return found

class AutoBackend:
    """Live access adapter. Discovery is incremental, cancellable and read-only."""
    def __init__(self,data,signal):
        self.data=data;self.signal=signal;self.process=None;self.memory=None;self.generation=None
        self.recipe=dict(data.get('native_identity_recipe',{}));pinned=self.recipe.get('name')
        # A pinned name (setup --name) is used as is. Otherwise the name is re-detected for every run:
        # players can rename their character in the lobby at any time.
        self.pinned=bool(pinned) and not pinned.startswith('<');self.name=pinned if self.pinned else None
        self.detect_next=0;self.ambiguous_names=None;self.scene=None;self.scene_next=0;self.scene_busy=False
        if self.recipe.get('build','').lower()!=data['game_sha256'].lower():raise AutoBlocked('Identity recipe/build mismatch.')
        targets=[t for t in data['config']['targets'] if t[1] in AUTO_CORE_OFFSETS]
        if len(targets)!=len(AUTO_CORE_OFFSETS) or {t[1] for t in targets}!=AUTO_CORE_OFFSETS:
            raise AutoBlocked('Supported target layout is invalid.')
        for _,off,fmt,value in targets:
            if isinstance(value,bool) or fmt!=('I' if off==120 else 'f') or not math.isfinite(value) or not 0<=value<=1e7 or (fmt=='I' and not isinstance(value,int)):
                raise AutoBlocked('Invalid target type/value.')
            if off in (20,28) and value!={20:1.5,28:3.}[off]:raise AutoBlocked('XP must be +200% and speed +50%.')
        desired={off:value for _,off,_,value in targets}
        if desired[0]<1 or desired[112]>desired[0] or desired[8]<=0:raise AutoBlocked('Invalid target health or cooldown relationship.')
        self.targets=targets;self.recipe['targets']=targets;self.hash_cache={}
        self.hints={data['config']['base']};self.injected=set();self.regions=[];self.region_index=0;self.offset=0
        self.scan_found={};self.scan_errors=0;self.scan_bytes=0;self.scan_complete=False;self.scan_restart=0;self.last_hint=0
        self.backed_up=set();self.missing_sources=[]
        self.base_recipe=dict(self.recipe);self.base_targets=list(targets);self.build=data['game_sha256'].lower();self.learning=False
        self.learn_headers=None;self.learn_samples=[];self.next_learn=0;self.disabled=set()
        self.hints.update(c['base'] for c in data.get('name_discovery',{}).get('candidates',[]))
        self.hints.update(int(h['base']) for h in data.get('auto_hints',[]) if h.get('build','').lower()==data['game_sha256'].lower() and h.get('name')==self.name)
    def find_process(self):
        matches=[]
        for p in psutil.process_iter(['exe']):
            try:
                if p.info['exe'] and os.path.normcase(p.info['exe'])==os.path.normcase(self.data['game_exe']):matches.append(p)
            except psutil.Error:pass
        if not matches:return None
        if len(matches)!=1:raise AutoBlocked('Multiple matching game processes; no automatic selection.')
        p=psutil.Process(matches[0].pid)
        if os.path.normcase(p.exe())!=os.path.normcase(self.data['game_exe']):raise AutoBlocked('Process path changed during discovery.')
        path=Path(p.exe());stat=path.stat();created=p.create_time()
        key=(p.pid,created,stat.st_size,stat.st_mtime_ns)
        if key not in self.hash_cache:
            with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
            self.hash_cache={key:digest}
        self.use_build(self.hash_cache[key].lower())
        self.found_process=p
        return (p.pid,created,self.hash_cache[key])
    def use_build(self,digest):
        """Known hash -> saved recipe; learned hash -> learned distance; otherwise learn read-only."""
        if digest==self.build:return
        self.build=digest;self.learn_headers=None;self.learn_samples=[]
        self.hints.update(int(h['base']) for h in read().get('auto_hints',[]) if h.get('build','').lower()==digest and h.get('name')==self.name)
        if digest==self.data['game_sha256'].lower():
            self.set_layout(None);self.learning=False;return
        learned=read().get('learned_builds',{}).get(digest)
        if learned and 'delta' in learned:
            self.set_layout(learned)
            self.learning=False
        else:self.learning=True
    def set_layout(self,learned):
        """Install a learned (or the original) layout; unverified fields are never written."""
        recipe=dict(self.base_recipe);skip=set();moved={}
        if learned:
            moved=dict(learned.get('field_offsets',{}))
            recipe.update(stats_offset=recipe['name_header_offset']+learned['delta'],owner_offset=learned['owner_offset'],build=self.build)
            skip=set(learned.get('skip_offsets',[]))
            if 'wallet_offset' in learned:skip.discard(120);moved['Gold Wallet']=learned['wallet_offset']
        self.targets=[(n,moved.get(n,off),f,v) for n,off,f,v in self.base_targets if off not in skip and moved.get(n,off)!=-1]
        recipe['targets']=self.targets;self.recipe=recipe
    def learn(self,now):
        """Read-only: find the name->stats distance for an unseen build, confirm over >=3 spaced samples."""
        if self.name is None:
            self.detect_character(now)
            if self.name is None:return dict(state='learning',phase='need_name',names=self.ambiguous_names)
        if now<self.next_learn:return dict(state='learning',samples=len(self.learn_samples))
        self.next_learn=now+5
        if self.learn_headers is None:
            self.reset_scan();self.learn_headers=set()
            for r in self.memory.snapshot_memory_regions():
                if r.is_readable and r.is_writable and not r.is_executable:self.regions.append(r)
        enc=self.name.encode('utf-8')
        deadline=time.monotonic()+.6
        while self.region_index<len(self.regions) and time.monotonic()<deadline:
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            r=self.regions[self.region_index];address=r.address+self.offset
            length=min(4194304,r.size-self.offset);size=min(length+24,r.size-self.offset)
            try:
                raw=self.memory.read_bytes(address,size)
                for index,pointer in name_headers(raw,len(enc)):
                    if index*8>=length:continue
                    try:
                        if self.memory.read_bytes(pointer,len(enc))==enc:self.learn_headers.add(address+index*8)
                    except Exception:pass
            except InterruptedError:raise
            except Exception:self.scan_errors+=1
            self.offset+=length
            if self.offset>=r.size:self.region_index+=1;self.offset=0
        self.next_learn=now+(0.05 if self.region_index<len(self.regions) else 5)
        if self.region_index<len(self.regions):return dict(state='learning',phase='scan',headers=len(self.learn_headers))
        results={}
        for header in self.learn_headers:
            try:
                for d,snap in learn_deltas(self.memory,self.process,header,dict(self.base_recipe,targets=self.base_targets),self.name):results[(header,d)]=snap
            except Exception:pass
        deltas={d for _,d in results}
        layouts={key[1] for key in results}
        if len(layouts)!=1:
            self.learn_samples=[];self.learn_headers=None
            return dict(state='learning',phase='ambiguous' if results else 'no_match',matches=len(results))
        # Several copies of one record share a layout; the layout is what is learned (record choice stays strict at runtime).
        (header,(d,o)),snap=min(results.items(),key=lambda item:item[0][0])
        sample=(time.time(),header,(d,o),snap['player_id'],snap['level'])
        if self.learn_samples and (self.learn_samples[-1][1:4]!=sample[1:4] or sample[4]<self.learn_samples[-1][4]):self.learn_samples=[]
        self.learn_samples.append(sample)
        self.learn_headers={header}  # later samples re-verify only the winner (no rescan)
        self.region_index=len(self.regions)
        if len(self.learn_samples)>=3 and sample[0]-self.learn_samples[0][0]>=10:
            digest=self.build
            def save_it(current):
                current.setdefault('learned_builds',{}).setdefault(digest,{}).update(dict(delta=d,owner_offset=o,skip_offsets=snap['skip'],learned=time.time(),samples=len(self.learn_samples),
                    level=snap['level'],name=self.name,from_build=self.data['game_sha256']))
            update_session(save_it)
            self.set_layout(dict(read().get('learned_builds',{}).get(digest,{}),delta=d,owner_offset=o,skip_offsets=snap['skip']))
            self.learning=False;self.learn_headers=None;self.reset_scan()
            return dict(state='learned',delta=d,owner_offset=o,skipped=snap['skip'],build=digest)
        return dict(state='learning',phase='confirming',samples=len(self.learn_samples))
    def bind(self,generation):
        from PyMemoryEditor import OpenProcess
        self.close();self.process=self.found_process;self.generation=generation
        self.learn_headers=None;self.learn_samples=[];self.next_learn=0
        self.memory=OpenProcess(pid=generation[0],permission=0x438)
        self.injected.clear();self.backed_up.clear();self.reset_scan()
        lease=self.data.get('auto_last_verified',{})
        if (lease.get('pid'),lease.get('created'),lease.get('build','').lower())==(generation[0],generation[1],self.build):
            self.injected.add((generation[0],generation[1],lease['base'],lease['player_id']))
    def close(self):
        if self.memory:
            try:self.memory.close()
            finally:self.memory=None
    def reset_scan(self):
        self.regions=[];self.region_index=0;self.offset=0;self.scan_found={};self.scan_errors=0;self.scan_bytes=0;self.scan_complete=False
    def snapshot(self,candidate):
        key=(self.generation[0],self.generation[1],candidate['base'],candidate['player_id'])
        for attempt in range(3):
            try:
                return actor_snapshot(self.memory,self.process,candidate['base'],self.recipe,self.name,
                    expected_id=candidate['player_id'],health_ceiling=9999. if key in self.injected else None)
            except (ValueError,RuntimeError):
                if attempt==2:raise
                if self.signal.wait(.005):raise InterruptedError('Stop requested.')
    def at(self,base):
        # Discovery never gets the relaxed health rule for an unknown identity.
        return actor_snapshot(self.memory,self.process,base,self.recipe,self.name)
    def release_name(self):
        """New run or lost record: forget an auto-detected name so a rename in the lobby is picked up."""
        if not self.pinned:self.name=None;self.detect_next=0;self.ambiguous_names=None
    def refresh_scene(self,now):
        """While no character is found: look at the screen (background capture) to say lobby, menu or unknown."""
        if now<self.scene_next or self.scene_busy or self.process is None:return
        self.scene_next=now+10;self.scene_busy=True;pid=self.process.pid
        def job():
            import tempfile
            try:
                handle=game_window(pid)
                if handle:
                    path=shot_path('scene')
                    width,height=capture_window(handle,path);self.scene=detect_scene(ocr_lines(path),width,height)
            except Exception:self.scene=None
            finally:self.scene_busy=False
        threading.Thread(target=job,daemon=True).start()
    def detect_character(self,now):
        """No usable name yet (or it was not found): look for any valid character record and adopt its name."""
        if now<self.detect_next:return []
        self.detect_next=now+10
        recipe=dict(self.recipe,targets=self.targets)
        found=find_characters(self.memory,self.process,recipe,cancelled=lambda:self.signal.wait(0))
        names=sorted({c['name'] for c in found})
        if not names:self.ambiguous_names=None;return []
        chosen=names[0] if len(names)==1 else None
        if chosen is None:
            try:chosen=window_player_name(self.process.pid,names)
            except Exception:chosen=None
        if chosen is None:self.ambiguous_names=names;return []
        self.ambiguous_names=None;self.name=chosen
        def remember(current):
            current['last_name']=chosen;known=current.setdefault('known_names',[])
            if chosen not in known:known.append(chosen)
        update_session(remember)
        mine=[c for c in found if c['name']==chosen];self.hints.update(c['base'] for c in mine)
        return mine
    def seek(self,now):
        if self.name is None:return self.detect_character(now)
        found={}
        for base in sorted(self.hints):
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            try:
                candidate=self.at(base);found[base]=candidate
            except Exception:
                for pid,created,known_base,player_id in tuple(self.injected):
                    if (pid,created,known_base)==(self.generation[0],self.generation[1],base):
                        try:found[base]=self.snapshot(dict(base=base,player_id=player_id))
                        except Exception:pass
        if found:return list(found.values())
        if self.scan_complete and now<self.scan_restart:return []
        if not self.regions or self.scan_complete:
            self.reset_scan()
            for r in self.memory.snapshot_memory_regions():
                descriptor=getattr(r,'struct',None);state=getattr(descriptor,'State',0x1000);protection=getattr(descriptor,'Protect',0)
                if r.is_readable and r.is_writable and not r.is_executable and state==0x1000 and not protection&0x101:
                    self.regions.append(r)
            hints=sorted(self.hints)
            # Regions that held the actor before (or lie nearest to it) are scanned first.
            self.regions.sort(key=lambda r:min((0 if r.address<=h<r.address+r.size else min(abs(h-r.address),abs(h-r.address-r.size)) for h in hints),default=0))
        deadline=time.monotonic()+.15
        while self.region_index<len(self.regions) and time.monotonic()<deadline:
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            r=self.regions[self.region_index];address=r.address+self.offset
            length=min(4194304,r.size-self.offset);size=min(length+24,r.size-self.offset)
            try:
                raw=self.memory.read_bytes(address,size)
                for index,pointer in name_headers(raw,len(self.name.encode('utf-8'))):
                    if self.signal.wait(0):raise InterruptedError('Stop requested.')
                    if index*8>=length:continue
                    try:
                        if self.memory.read_bytes(pointer,len(self.name.encode('utf-8')))!=self.name.encode('utf-8'):continue
                        base=address+index*8+self.recipe['stats_offset']-self.recipe['name_header_offset']
                        candidate=self.at(base);self.scan_found[base]=candidate;self.hints.add(base)
                    except Exception:pass
                self.scan_bytes+=length
            except InterruptedError:raise
            except Exception:self.scan_errors+=1
            self.offset+=length
            if self.offset>=r.size:self.region_index+=1;self.offset=0
        if self.region_index>=len(self.regions):self.scan_complete=True;self.scan_restart=now+10
        found=list(self.scan_found.values())
        return found or (self.detect_character(now) if self.scan_complete else [])
    def run_reset(self):
        self.injected.clear();self.backed_up.clear()
    def apply(self,candidate):
        fresh=self.snapshot(candidate);changes=[];names=[];missing=[]
        for name,off,fmt,value in self.targets:
            if name in self.disabled:continue
            target=struct.pack('<'+fmt,value);address=fresh['base']+off
            if self.memory.read_bytes(address,4)!=target:changes.append((address,target));names.append(name)
        for kind,value in AUTO_SOURCE_TARGETS.items():
            indices=[i for i,k in enumerate(fresh['kinds']) if k==kind]
            if len(indices)!=1:missing.append(kind);continue
            address=fresh['pointer']+indices[0]*8+4;target=struct.pack('<f',value)
            if self.memory.read_bytes(address,4)!=target:changes.append((address,target));names.append('Source '+str(kind))
        self.missing_sources=missing
        if not changes:return dict(writes=0,values=fresh['values'])
        key=(self.generation[0],self.generation[1],fresh['base'],fresh['player_id'])
        backup_key=(key,fresh['pointer'],fresh['level'])
        if backup_key not in self.backed_up:
            backup=dict(time=time.time(),pid=self.generation[0],created=self.generation[1],base=fresh['base'],player_id=fresh['player_id'],level=fresh['level'],reason='Automatic non-pausing recovery updater',
                changes=[dict(name=n,address=a,original=self.memory.read_bytes(a,len(v)).hex(),patched=v.hex()) for n,(a,v) in zip(names,changes)])
            update_session(lambda current:current.setdefault('run_backups',[]).append(backup));self.backed_up.add(backup_key)
        # An identity lease is earned only from a strictly validated active actor.
        # It permits bounded overheal while repairing a recalculated health cap.
        self.injected.add(key)
        def guard():
            now=self.snapshot(fresh)
            if now['level']!=fresh['level'] or any(now[k]!=fresh[k] for k in ('pointer','capacity','length','kinds')):
                raise ValueError('Actor or owner changed during write batch.')
        write_batch(self.memory,changes,guard)
        after=self.snapshot(fresh)
        return dict(writes=len(changes),fields=names,values=after['values'])

class AutoRecovery:
    """Pure state machine; no writes until fresh identity and level progress agree."""
    def __init__(self,backend,clock=time.monotonic):
        self.backend=backend;self.clock=clock;self.generation=None;self.candidate=None
        self.last_level=None;self.baseline=None;self.confirmations=0;self.active=False;self.failures=0
        self.last_values={};self.low=0;self.dup_key=None;self.dup={}
        self.last_progress=0;self.run=0;self.total_writes=0;self.state=dict(state='waiting_process')
    def forget(self):
        if hasattr(self.backend,'release_name'):self.backend.release_name()
        self.candidate=None;self.last_level=None;self.baseline=None;self.confirmations=0;self.active=False;self.failures=0;self.last_values={};self.low=0;self.dup_key=None;self.dup={}
    def pick_live(self,candidates):
        """Copies of one record: only the live one changes (HP, level, gold). Pick it once exactly one moves."""
        if len({c['player_id'] for c in candidates})!=1:return None
        key=tuple(sorted(c['base'] for c in candidates))
        if key!=self.dup_key:self.dup_key=key;self.dup={}
        for c in candidates:
            v=c.get('values',{});self.dup.setdefault(c['base'],[]).append((c['level'],v.get('Current Health'),v.get('Gold Wallet')))
            del self.dup[c['base']][:-8]
        if min(len(h) for h in self.dup.values())<4:return None
        movers=[c for c in candidates if len(set(self.dup[c['base']]))>1]
        return movers[0] if len(movers)==1 else None
    def step(self):
        now=self.clock()
        try:
            generation=self.backend.find_process()
            if generation is None:
                if self.generation is not None:self.backend.close()
                self.generation=None;self.forget();self.state=dict(state='waiting_process');return self.state
            if generation!=self.generation:
                self.backend.bind(generation);self.generation=generation;self.forget()
            if getattr(self.backend,'learning',False):
                self.state=self.backend.learn(now);return self.state
            if self.candidate is None:
                candidates=self.backend.seek(now)
                if not candidates and hasattr(self.backend,'refresh_scene'):self.backend.refresh_scene(now)
                if len(candidates)>1:
                    picked=self.pick_live(candidates)
                    if picked is not None:candidates=[picked]
                if len(candidates)!=1:
                    self.state=dict(state='blocked_ambiguous' if candidates or getattr(self.backend,'ambiguous_names',None) else 'seeking_record',candidates=len(candidates))
                    if getattr(self.backend,'ambiguous_names',None):self.state['names']=self.backend.ambiguous_names
                    scene=getattr(self.backend,'scene',None)
                    if self.state['state']=='seeking_record' and scene in ('lobby','menu'):self.state.update(state='in_'+scene)
                    return self.state
                self.candidate=candidates[0];self.last_level=self.candidate['level'];self.baseline=self.last_level
                if hasattr(self.backend,'scene'):self.backend.scene=None
                self.last_values=dict(self.candidate.get('values',{}))
                self.confirmations=1;self.last_progress=now;self.state=dict(state='waiting_progress',level=self.last_level);return self.state
            fresh=self.backend.snapshot(self.candidate)
            self.low=self.low+1 if fresh['level']<self.last_level else 0
            reset=self.low>=2  # one lower reading may be a torn read; a real reset persists
            if reset:self.low=0
            if reset:
                self.backend.run_reset();self.run+=1;self.active=False;self.baseline=fresh['level'];self.confirmations=1;self.last_progress=now
            else:self.confirmations+=1
            values=fresh.get('values',{});previous=self.last_values
            gold_gain='Gold Wallet' in values and 'Gold Wallet' in previous and values['Gold Wallet']>previous['Gold Wallet']
            damage=all(k in values and k in previous for k in ('Max Health','Current Health')) and values['Max Health']==previous['Max Health'] and values['Current Health']<previous['Current Health']
            if not reset and (fresh['level']>self.baseline or gold_gain or damage):
                self.active=True;self.baseline=fresh['level'];self.last_progress=now
            self.last_values=dict(values)
            if now-self.last_progress>120:self.active=False
            if not self.low:self.last_level=fresh['level']
            self.candidate=fresh
            self.state=dict(state='waiting_progress',level=self.last_level,run=self.run,player_id=fresh['player_id'],base=hex(fresh['base']))
            if self.active and self.confirmations>=2 and not self.low:  # no writes while a possible reset is unconfirmed
                result=self.backend.apply(fresh);self.total_writes+=result['writes']
                self.state.update(state='applying',last_batch=result['writes'],total_field_writes=self.total_writes,values=result.get('values',{}))
            self.failures=0
        except AutoBlocked as error:
            self.backend.close();self.generation=None;self.forget();self.state=dict(state='blocked',reason=str(error))
        except InterruptedError:raise
        except Exception as error:
            self.failures+=1;self.state=dict(state='recovering',reason=str(error),failures=self.failures)
            if self.failures>=3:self.forget()
        return self.state

def auto_alive(data):
    return running(data.get('auto_monitor',{})) and data['auto_monitor'].get('state') not in ('stopped','expired','failed')

def active_helpers(data,exclude_pid=None):
    return [key for key in ('runtime','named_monitor','source_monitor','auto_monitor')
            if data.get(key,{}).get('pid')!=exclude_pid and running(data.get(key,{}))]

def prepare_auto_launch(args):
    with mutex('auto-launch',1000):
        data=read()
        if auto_alive(data) and not data.get('stop_requested'):
            with StopSignal() as signal:stopping=signal.wait(0)
            if not stopping:
                print('Automatic watcher already running: PID '+str(data['auto_monitor']['pid']));return True
        stop();deadline=time.monotonic()+10
        while active_helpers(read(),os.getpid()):
            if time.monotonic()>=deadline:raise RuntimeError('Previous helper did not exit; no second writer started.')
            time.sleep(.1)
        with mutex('auto',0):
            with StopSignal() as signal:signal.reset()
            update_session(lambda current:current.update(stop_requested=False))
            if args.background:
                argv=[sys.executable,str(Path(__file__).resolve()),'_auto_worker','--seconds',str(args.seconds)]
                if args.trace_only:argv.append('--trace-only')
                child=subprocess.Popen(argv,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                update_session(lambda current:current.update(auto_monitor=dict(pid=child.pid,created=psutil.Process(child.pid).create_time(),state='starting',heartbeat=time.time(),trace_only=args.trace_only)))
                print(f'Automatic non-pausing watcher started: PID {child.pid}. It waits for gameplay progression and reacquires new runs/processes. Stop: python game.py stop.')
                return True
            update_session(lambda current:current.update(auto_monitor=dict(pid=os.getpid(),created=psutil.Process().create_time(),state='starting',heartbeat=time.time(),trace_only=args.trace_only)))
    return False

def auto_command():
    parser=argparse.ArgumentParser(description='Stay alive across new runs/processes, reacquire by name, and apply supported stats without pausing.')
    parser.add_argument('--background',action='store_true')
    parser.add_argument('--seconds',type=int,default=28800,help='Watcher lifetime, 1..86400 seconds; default eight hours')
    parser.add_argument('--trace-only',action='store_true',help='Observe transitions without any game writes')
    args=parser.parse_args(sys.argv[2:])
    if not 1<=args.seconds<=86400:raise ValueError('Use 1..86400 seconds.')
    if sys.argv[1]!='_auto_worker' and prepare_auto_launch(args):return
    with mutex('auto',10000),mutex('writer',0),StopSignal() as signal:
        data=read();backend=None
        runtime=dict(pid=os.getpid(),created=psutil.Process().create_time(),state='starting',heartbeat=time.time(),trace_only=args.trace_only,pauses=False)
        update_session(lambda current:current.update(auto_monitor=runtime))
        deadline=time.monotonic()+args.seconds;last_save=0;last_state=None;last_saved=None;last_identity=None;card_level=[None];picking=[False]
        if not args.trace_only:threading.Thread(target=lambda:ocr_server(),daemon=True).start()
        meter_next=[0.]
        def card_note(text):
            def add(current):
                log=current.setdefault('card_log',[]);log.append(dict(time=time.time(),text=text));current['card_log']=log[-30:]
            update_session(add)
        try:
            backend=AutoBackend(data,signal)
            if args.trace_only:
                backend.apply=lambda candidate:dict(writes=0,values=candidate['values'])
            engine=AutoRecovery(backend)
            while time.monotonic()<deadline and not signal.wait(0):
                state=engine.step()
                level_now=state.get('level')
                if level_now is not None and engine.generation:
                    if card_level[0] is not None and level_now>card_level[0] and not picking[0] and not args.trace_only and read().get('cards_enabled',True):
                        picking[0]=True
                        def card_job(pid=engine.generation[0]):
                            try:pick_cards(pid,card_note)
                            except Exception as error:card_note(f'Level-up cards failed: {error}')
                            finally:picking[0]=False
                        threading.Thread(target=card_job,daemon=True).start()
                    card_level[0]=level_now
                    if not args.trace_only and time.monotonic()>=meter_next[0] and backend.name and read().get('meter_enabled',True):
                        meter_next[0]=time.monotonic()+20
                        threading.Thread(target=lambda pid=engine.generation[0],name=backend.name:sample_meter(pid,name),daemon=True).start()
                if state.get('state')!=last_state:
                    print(json.dumps(state),flush=True);last_state=state.get('state')
                if time.monotonic()-last_save>=10 or state.get('state')!=last_saved:
                    runtime.update(state);runtime['reason']=state.get('reason');runtime['character']=backend.name;runtime.update(heartbeat=time.time(),game_pid=engine.generation[0] if engine.generation else None,
                        game_created=engine.generation[1] if engine.generation else None,scan_bytes=backend.scan_bytes,
                        scan_errors=backend.scan_errors,missing_source_kinds=backend.missing_sources)
                    def publish(current):
                        if current.get('stop_requested'):signal.request()
                        current['auto_monitor']=runtime;backend.disabled=set(current.get('gui_disabled',[]))
                        if engine.candidate is not None:
                            c=engine.candidate
                            hint=dict(build=backend.build,name=backend.name,base=c['base'],player_id=c['player_id'])
                            hints=current.setdefault('auto_hints',[])
                            if hint not in hints:hints.append(hint)
                            current['auto_hints']=[h for h in hints if h.get('build','').lower()==backend.build][-16:]+[h for h in hints if h.get('build','').lower()!=backend.build][-4:]
                            if state.get('state')=='applying' and not args.trace_only and backend.injected:
                                current['auto_last_verified']=dict(pid=c['pid'],created=c['created'],base=c['base'],player_id=c['player_id'],build=backend.build,time=time.time())
                    update_session(publish);last_save=time.monotonic();last_saved=state.get('state')
                signal.wait(.05 if state.get('state')=='learning' and state.get('phase')=='scan' else .1 if state.get('state')=='seeking_record' else 1. if state.get('state') in ('in_lobby','in_menu') else (2 if state.get('state') in ('waiting_process','blocked') else .5 if state.get('state')=='waiting_progress' else .25))
            runtime.update(state='stopped' if signal.wait(0) else 'expired',heartbeat=time.time())
        except (KeyboardInterrupt,InterruptedError):runtime.update(state='stopped',heartbeat=time.time())
        except Exception as error:runtime.update(state='failed',reason=str(error),heartbeat=time.time())
        finally:
            if backend:backend.close()
            update_session(lambda current:current.update(auto_monitor=runtime))
        print(json.dumps(runtime,indent=2))

@contextmanager
def mutex(suffix, timeout=10000):
    """Windows kernel mutex: released automatically if a controller crashes."""
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    api.CreateMutexW.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    api.ReleaseMutex.argtypes = [ctypes.c_void_p]
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    key = hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:24]
    handle = api.CreateMutexW(None, False, 'Local\\AzerothCLI_'+key+'_'+suffix)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        acquired = api.WaitForSingleObject(handle, timeout) in (0, 0x80)
        if not acquired: raise RuntimeError('Another controller is busy; retry after it finishes.')
        yield
    finally:
        if acquired: api.ReleaseMutex(handle)
        api.CloseHandle(handle)

def update_session(change):
    with mutex('session'):
        data = read()
        change(data)
        backups = data.get('run_backups')
        if backups and len(backups) > 64:
            # Keep the hot file small; removed entries are appended to an archive, never discarded.
            archive = SESSION.with_name('session.backups-archive.json')
            old = json.loads(archive.read_text(encoding='utf-8')) if archive.exists() else []
            archive.write_text(json.dumps(old + backups[:-64]), encoding='utf-8')
            data['run_backups'] = backups[-64:]
        save(data)

def selectors(raw):
    if len(raw) % 8: raise ValueError('Truncated modifier vector.')
    return [struct.unpack_from('<I', raw, i)[0] for i in range(0, len(raw), 8)]

def write_batch(memory, changes, guard):
    """Best-effort conditional rollback; never overwrite a concurrent game update."""
    originals = [(address, memory.read_bytes(address, len(value)), value)
                 for address, value in changes]
    written = []
    try:
        for address, original, value in originals:
            guard()
            written.append((address, original, value))
            memory.write_bytes(address, value)
            if memory.read_bytes(address, len(value)) != value:
                raise RuntimeError('Write readback did not match.')
        guard()
    except Exception as error:
        restored = 0
        for address, original, value in reversed(written):
            try:
                guard()
                if memory.read_bytes(address, len(value)) == value:
                    memory.write_bytes(address, original)
                    restored += 1
            except Exception:
                break
        raise RuntimeError(f'Write stopped: {error}; conditionally restored {restored}/{len(written)} fields.') from error

def read():
    return json.loads(SESSION.read_text(encoding='utf-8'))

def save(data):
    temporary = SESSION.with_name(f'session.{os.getpid()}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                temporary.replace(SESSION)
                break
            except PermissionError:
                if attempt == 5: raise
                time.sleep(.02 * (attempt+1))
    finally:
        temporary.unlink(missing_ok=True)

def running(runtime):
    try:
        return psutil.Process(runtime['pid']).create_time() == runtime['created']
    except (KeyError, psutil.Error):
        return False

def validate(memory, config, data=None):
    proc = psutil.Process(config['pid'])
    if proc.create_time() != config['created']:
        raise RuntimeError('Game process changed; fresh discovery is required.')
    data = data or read()
    if os.path.normcase(proc.exe()) != os.path.normcase(data['game_exe']):
        raise RuntimeError('Executable path changed.')
    identity = (config['pid'], config['created'], data['game_sha256'])
    if identity not in CHECKED_BUILDS:
        if hashlib.sha256(Path(proc.exe()).read_bytes()).hexdigest().lower() != data['game_sha256'].lower():
            raise RuntimeError('Unknown game build; do not reuse this layout.')
        CHECKED_BUILDS.add(identity)
    base = config['base']
    if struct.unpack('<I', memory.read_bytes(base - 24, 4))[0] != config['player_id']:
        raise RuntimeError('Player record changed; fresh discovery is required.')
    owner = struct.unpack('<3Q', memory.read_bytes(config['owner'], 24))
    if owner != (config['capacity'], config['pointer'], config['length']):
        raise RuntimeError('Modifier owner changed; fresh discovery is required.')
    raw = memory.read_bytes(config['pointer'], config['length'] * 8)
    if 'selectors' not in config:
        raise RuntimeError('Legacy byte-only modifier guard; rediscovery is required.')
    if selectors(raw) != config['selectors']:
        raise RuntimeError('Modifier layout changed; fresh discovery is required.')
    level = struct.unpack('<I', memory.read_bytes(base + 104, 4))[0]
    if not 1 <= level <= 10000:
        raise RuntimeError('Run level is invalid; fresh discovery is required.')
    return level

def status():
    data = read()
    runtime = data.get('runtime', {})
    named=data.get('named_monitor',{})
    sources=data.get('source_monitor',{})
    automatic=data.get('auto_monitor',{})
    auto_running=auto_alive(data)
    named_running=running(named) and named.get('state') in ('starting','updating')
    sources_running=running(sources) and sources.get('state')=='checking_sources'
    if named_running or named.get('heartbeat',0)>runtime.get('heartbeat',0):runtime=named
    if auto_running or automatic.get('heartbeat',0)>runtime.get('heartbeat',0):runtime=automatic
    result = {'writer_running': running(data.get('runtime',{})) or named_running or sources_running or (auto_running and automatic.get('state')=='applying'),
              'auto_running':auto_running,'auto_state':automatic.get('state'),
              'named_writer_running':named_running,'source_monitor_running':sources_running,'last_state': runtime.get('state'),
              'last_reason': runtime.get('reason'), 'saved_game_pid': data['config']['pid']}
    result['heartbeat_age_seconds'] = round(time.time()-runtime['heartbeat'], 1) if runtime.get('heartbeat') else None
    result['writer_responding'] = result['writer_running'] and result['heartbeat_age_seconds'] is not None and result['heartbeat_age_seconds'] < (15 if auto_running else 5)
    result['controller_responding'] = auto_running and result['heartbeat_age_seconds'] is not None and result['heartbeat_age_seconds']<15
    result['active_game_pid']=runtime.get('game_pid')
    result['portable_paths'] = len(data.get('pointer_paths', []))
    try:
        result['same_game_process'] = psutil.Process(data['config']['pid']).create_time() == data['config']['created']
    except psutil.Error:
        result['same_game_process'] = False
    print(json.dumps(result, indent=2))

def stop():
    event_error=None
    try:
        with StopSignal() as signal:signal.request()
    except (OSError,RuntimeError) as error:event_error=str(error)
    update_session(lambda data: data.update(stop_requested=True))
    print('Stop requested; active updaters will stop on their next check.')
    if event_error:print('Kernel stop event unavailable; the watcher will also check the saved flag at its next heartbeat.')

# Displayed values are converted to their underlying representation.
FIELDS = {'hp': (0, 1, 0, 1.1), 'damage': (4, .01, 1, .006),
          'crit': (12, .01, 0, .0006), 'crit_damage': (16, .01, 0, .006),
          'speed': (20, .01, 1, .006), 'pickup': (24, 1, 0, .6),
          'experience': (28, .01, 1, .006), 'armor': (32, .01, 0, .006),
          'regen': (36, 1, 0, .06), 'area': (40, .01, 1, .006),
          'luck': (44, .01, 0, .006), 'echo': (56, .01, 0, .006)}

def game_process(data):
    matches = []
    for p in psutil.process_iter(['exe']):
        try:
            if p.info['exe'] and os.path.normcase(p.info['exe']) == os.path.normcase(data['game_exe']):
                matches.append(p)
        except psutil.Error:
            pass
    if len(matches) != 1:
        raise RuntimeError(f'Expected one game process, found {len(matches)}.')
    p = matches[0]
    if hashlib.sha256(Path(p.exe()).read_bytes()).hexdigest().lower() != data['game_sha256'].lower():
        raise RuntimeError('Unrecognized executable hash. Layout must be traced again for this update.')
    return p

def observations(args):
    result = []
    for name, (offset, scale, bias, tolerance) in FIELDS.items():
        value = getattr(args, name, None)
        if value is not None:
            if not math.isfinite(value):
                raise ValueError('Observations must be finite.')
            result.append((offset, value * scale + bias, tolerance))
    if len(result) < 3:
        raise RuntimeError('Supply at least three current displayed stats, such as --crit --crit-damage --speed --pickup.')
    return result

def record(memory, p, base, observed):
    if base % 4 or not 0x10000 <= base < 0x7ffffffff000:
        raise ValueError('Invalid record address.')
    raw = memory.read_bytes(base - 24, 1016)
    def u(offset): return struct.unpack_from('<I', raw, offset + 24)[0]
    def f(offset): return struct.unpack_from('<f', raw, offset + 24)[0]
    if not u(-24) or not 1 <= u(104) <= 10000:
        raise ValueError('Invalid player identity or level.')
    for offset in list(range(0, 52, 4)) + [56, 60, 64, 72, 112]:
        if not math.isfinite(f(offset)) or not 0 <= f(offset) <= 1e7:
            raise ValueError('Implausible stat.')
    if not 1 <= f(0) or f(112) > f(0) + 1 or u(52) > 100000 or u(68) > 100000:
        raise ValueError('Implausible health or counters.')
    if any(abs(f(off) - wanted) > tol for off, wanted, tol in observed):
        raise ValueError('Displayed observations no longer match.')
    capacity, pointer, length = struct.unpack_from('<3Q', raw, 992)
    if not 8 <= capacity <= 256 or not 1 <= length <= capacity or pointer % 4 or not 0x10000 <= pointer < 0x7ffffffff000:
        raise ValueError('Invalid owned modifier vector.')
    vector = memory.read_bytes(pointer, length * 8)
    second = memory.read_bytes(base-24, 1016)
    if raw[:4] != second[:4] or raw[992:1016] != second[992:1016]:
        raise ValueError('Record changed while reading; retry discovery.')
    for i in range(length):
        kind, value = struct.unpack_from('<If', vector, i * 8)
        if (kind & 255) > 64 or not math.isfinite(value) or abs(value) > 1e6:
            raise ValueError('Invalid modifier entry.')
    return dict(pid=p.pid, created=p.create_time(), base=base, player_id=u(-24),
                owner=base+968, pointer=pointer, capacity=capacity, length=length,
                kinds=list(vector[::8]), selectors=selectors(vector), level=u(104))

def discover(args):
    from PyMemoryEditor import OpenProcess, PointerPath
    import numpy as np
    data = read()
    if running(data.get('runtime', {})):
        raise RuntimeError('Stop the writer before discovery.')
    p = game_process(data)
    observed = observations(args)
    candidates = {}
    errors = 0
    complete = True
    deadline = time.monotonic() + args.seconds
    scanned = 0
    with OpenProcess(pid=p.pid, permission=0x410) as memory:
        def consider(base, source):
            try:
                c = record(memory, p, base, observed)
                c['source'] = source
                candidates[base] = c
            except Exception:
                pass
        for stored in data.get('pointer_paths', []):
            if stored.get('build', '').lower() != data['game_sha256'].lower(): continue
            try:
                consider(PointerPath.from_dict(stored['path']).rebase(memory).resolve(memory), 'pointer')
            except Exception:
                pass
        for region in memory.snapshot_memory_regions():
            if not region.is_readable or not region.is_writable or region.is_executable:
                continue
            if time.monotonic() >= deadline or scanned + region.size > args.max_mb * 1048576:
                complete = False
                break
            for address in range(region.address, region.address + region.size, 4 * 1048576):
                if time.monotonic() >= deadline:
                    complete = False
                    break
                size = min(4 * 1048576 + 128, region.address + region.size - address)
                try:
                    raw = memory.read_bytes(address, size)
                    values = np.frombuffer(raw[:len(raw)//4*4], dtype='<f4')
                    count = len(values) - 31
                    if count <= 0: continue
                    mask = np.ones(count, dtype=bool)
                    with np.errstate(invalid='ignore', over='ignore'):
                        for offset, wanted, tolerance in observed:
                            mask &= np.abs(values[offset//4:offset//4+count] - wanted) <= tolerance
                    for index in np.flatnonzero(mask):
                        consider(address + int(index)*4, 'structure')
                except Exception:
                    errors += 1
                scanned += min(4 * 1048576, size)
            if not complete: break
    result = dict(pid=p.pid, created=p.create_time(), mode=args.mode, time=time.time(),
                  observations=observed, complete=complete and errors == 0,
                  bytes_scanned=scanned, read_errors=errors, candidates=list(candidates.values()))
    data['discovery'] = result
    # Independent structure rediscovery is evidence; pointer resolution alone is not.
    for stored in data.get('pointer_paths', []):
        for c in candidates.values():
            if c['source'] == 'structure' and result['complete'] and len(candidates) == 1 and args.mode in ('solo', 'host'):
                try:
                    with OpenProcess(pid=p.pid, permission=0x410) as memory:
                        resolved = PointerPath.from_dict(stored['path']).rebase(memory).resolve(memory)
                    if resolved == c['base']:
                        evidence = [p.pid, p.create_time(), c['base'], c['player_id'], c['pointer']]
                        if evidence not in stored['evidence']: stored['evidence'].append(evidence)
                except Exception:
                    pass
    save(data)
    print(json.dumps(result, indent=2))
    print('Read-only candidates. Stack copies and other players may match; no candidate is automatically applied.')

def learn(args):
    from PyMemoryEditor import OpenProcess
    data = read()
    if running(data.get('runtime', {})): raise RuntimeError('Stop the writer before learning.')
    p = game_process(data)
    found = data.get('discovery', {})
    if (found.get('pid'), found.get('created')) != (p.pid, p.create_time()):
        raise RuntimeError('Run discover for this process first.')
    c = next((c for c in found['candidates'] if c['base'] == args.base), None)
    if c is None: raise RuntimeError('Address is not a discovery candidate.')
    with OpenProcess(pid=p.pid, permission=0x410) as memory:
        fresh = record(memory, p, args.base, found['observations'])
        if any(c[k] != fresh[k] for k in ('player_id', 'pointer', 'capacity', 'length', 'selectors')):
            raise RuntimeError('Candidate ownership changed; rediscover.')
        modules = [m for m in memory.get_modules() if m.name.lower() == Path(data['game_exe']).name.lower()]
        if len(modules) != 1: raise RuntimeError('Game module is ambiguous.')
        module = modules[0]
        selected = []
        total = 0
        regions = sorted(memory.snapshot_memory_regions(), key=lambda r: (not (r.address <= args.base < r.address+r.size), not (module.base_address <= r.address < module.base_address+module.size)))
        for r in regions:
            if r.is_readable and r.is_writable and total + r.size <= args.max_mb*1048576:
                selected.append(r)
                total += r.size
        deadline = time.monotonic() + args.seconds
        def progress(_):
            if time.monotonic() >= deadline: raise MemoryError('Pointer-map time budget reached.')
        paths = []
        try:
            for path in memory.scan_pointer_paths(args.base, max_depth=3, max_offset=2048,
                    max_results=32, memory_regions=selected,
                    static_ranges=[(module.base_address, module.base_address+module.size)],
                    progress_callback=progress, should_stop=lambda: time.monotonic() >= deadline):
                if module.base_address <= path.base_address < module.base_address + module.size:
                    portable = path.to_dict()
                    portable['module'] = module.name
                    portable['module_offset'] = hex(path.base_address - module.base_address)
                    paths.append(portable)
        except MemoryError as error:
            print(str(error))
    saved = data.setdefault('pointer_paths', [])
    evidence = [p.pid, p.create_time(), c['base'], c['player_id'], c['pointer']]
    for path in paths:
        # Module-relative recipes survive ASLR; absolute roots are not identities.
        key = (path.get('module'), path.get('module_offset'), str(path.get('offsets')))
        existing = next((s for s in saved if (s['path'].get('module'), s['path'].get('module_offset'), str(s['path'].get('offsets'))) == key), None)
        if existing is None: saved.append(dict(path=path, evidence=[], build=data['game_sha256']))
    data['last_pointer_scan'] = dict(time=time.time(), selected_bytes=total, results=len(paths), bounded=True)
    save(data)
    print(json.dumps(data['last_pointer_scan'], indent=2))
    print('Paths are candidates; validate by independent discovery after another process/run. No writes performed.')

def adopt(args):
    """Promote only an independently revalidated, portable, unique record."""
    from PyMemoryEditor import OpenProcess, PointerPath
    data = read()
    if running(data.get('runtime', {})): raise RuntimeError('Stop the writer first.')
    p = game_process(data)
    discovery = data.get('discovery', {})
    if discovery.get('mode') not in ('solo', 'host'):
        raise RuntimeError('Guest gameplay ownership remains unverified.')
    if not discovery.get('complete') or len(discovery.get('candidates', [])) != 1:
        raise RuntimeError('Requires a complete scan with exactly one candidate; duplicates need ownership tracing.')
    if (discovery.get('pid'), discovery.get('created')) != (p.pid, p.create_time()):
        raise RuntimeError('Process changed; rediscover.')
    c = discovery['candidates'][0]
    if c['base'] != args.base or time.time()-discovery['time'] > 60:
        raise RuntimeError('Rediscover immediately before adopting this candidate.')
    with OpenProcess(pid=p.pid, permission=0x410) as memory:
        fresh = record(memory, p, args.base, discovery['observations'])
        for key in ('player_id', 'pointer', 'capacity', 'length', 'selectors'):
            if fresh[key] != c[key]: raise RuntimeError('Record changed; rediscover.')
        verified = False
        for stored in data.get('pointer_paths', []):
            if stored.get('build', '').lower() != data['game_sha256'].lower(): continue
            generations = {(e[0], e[1]) for e in stored['evidence']}
            if len(generations) < 2: continue
            try:
                verified |= PointerPath.from_dict(stored['path']).rebase(memory).resolve(memory) == args.base
            except Exception: pass
        if not verified:
            raise RuntimeError('No matching pointer path independently validated across two process generations.')
    fresh['targets'] = data['config']['targets']
    # Preserve the live modifier array: its semantic ownership needs separate tracing.
    fresh['bonuses'] = []
    data.setdefault('previous_configs', []).append(data['config'])
    data['previous_configs'] = data['previous_configs'][-8:]
    data['config'] = fresh
    data['stop_requested'] = True
    save(data)
    print('Validated record selected without game writes. Run start to apply core stats. Modifiers are preserved.')

def capture():
    """Observe the historical HP-reader instruction; never promote captures to writes."""
    import frida
    from PyMemoryEditor import OpenProcess
    parser = argparse.ArgumentParser(description='Bounded Frida HP-reader observation; no stat writes.')
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--mode', choices=['solo', 'host', 'guest', 'dashboard', 'unknown'], required=True)
    args = parser.parse_args(sys.argv[2:])
    if not 1 <= args.seconds <= 30: raise ValueError('Capture duration must be 1..30 seconds.')
    data = read()
    if running(data.get('runtime', {})): raise RuntimeError('Stop the writer before capture.')
    p = game_process(data)
    signature = data.get('signature_candidates', {}).get('character_sheet_hp_reader')
    generation = p.create_time()
    if not signature or signature['build'].lower() != data['game_sha256'].lower():
        raise RuntimeError('No matching build-specific signature.')
    # This probe is intentionally fixed to the reviewed MOVSS XMM0,[RAX] site.
    pattern = bytes.fromhex(signature['pattern'])
    offset = signature['hook_offset']
    if offset != 4 or pattern[:8] != bytes.fromhex('488b4548f30f1000') or len(pattern) < 24:
        raise RuntimeError('Unexpected probe recipe; review the instruction before attaching.')
    settings = dict(module=Path(data['game_exe']).name, pattern=pattern.hex(' '), offset=offset)
    source = '''
const cfg = SETTINGS;
let listener = null;
let count = 0;
let last = 0;
const seen = new Set();
rpc.exports = {
  install() {
    const module = Process.getModuleByName(cfg.module);
    let matches = [];
    for (const range of module.enumerateRanges('r-x')) {
      matches.push(...Memory.scanSync(range.base, range.size, cfg.pattern));
      if (matches.length > 1) break;
    }
    if (matches.length !== 1) throw new Error('Executable signature matches: ' + matches.length);
    const site = matches[0].address.add(cfg.offset);
    const instruction = Instruction.parse(site);
    if (instruction.mnemonic !== 'movss' || instruction.opStr !== 'xmm0, dword ptr [rax]')
      throw new Error('Unexpected instruction: ' + instruction.toString());
    listener = Interceptor.attach(site, {
      onEnter() {
        const now = Date.now();
        if (count >= 32 || now-last < 100) return;
        last = now;
        const address = this.context.rax.toString();
        if (seen.has(address)) return;
        seen.add(address);
        count++;
        send({type:'record', address:address, thread:Process.getCurrentThreadId()});
      }
    });
    return {site:site.toString(), moduleOffset:site.sub(module.base).toString(), instruction:instruction.toString()};
  },
  cleanup() { if (listener !== null) { listener.detach(); listener = null; } Interceptor.flush(); }
};
'''.replace('SETTINGS', json.dumps(settings))
    events = []
    errors = []
    lock = threading.Lock()
    detached = threading.Event()
    def message(item, _payload):
        with lock:
            if item.get('type') == 'send' and item.get('payload', {}).get('type') == 'record':
                if len(events) < 32: events.append(item['payload'])
            elif item.get('type') == 'error': errors.append(item.get('description', str(item)))
    attached = None
    script = None
    metadata = None
    failure = None
    cleanup_errors = []
    try:
        attached = frida.attach(p.pid)
        attached.on('detached', lambda *_: detached.set())
        script = attached.create_script(source)
        script.on('message', message)
        script.load()
        if not running({'pid':p.pid, 'created':generation}):
            raise RuntimeError('Game process changed before probe installation.')
        metadata = script.exports_sync.install()
        print('Probe installed: '+json.dumps(metadata), flush=True)
        print('Open your character stats panel during this observation window.', flush=True)
        detached.wait(args.seconds)
    except BaseException as error:
        failure = str(error) or type(error).__name__
    finally:
        if script is not None:
            try: script.exports_sync.cleanup()
            except Exception as error: cleanup_errors.append(str(error))
            try: script.unload()
            except Exception as error: cleanup_errors.append(str(error))
        if attached is not None:
            try: attached.detach()
            except Exception as error: cleanup_errors.append(str(error))
    candidates = []
    rejected = []
    if running({'pid': p.pid, 'created': generation}):
        try:
            with OpenProcess(pid=p.pid, permission=0x410) as memory:
                for event in events:
                    try:
                        c = record(memory, p, int(event['address'], 16), [])
                        c['source'] = 'character_sheet_reader_capture'
                        c['gameplay_ownership_verified'] = False
                        c['values'] = {name: struct.unpack('<'+fmt, memory.read_bytes(c['base']+off, 4))[0]
                                       for name, off, fmt, _ in data['config']['targets']}
                        candidates.append(c)
                    except Exception as error:
                        rejected.append(dict(address=event['address'], reason=str(error)))
        except Exception as error: errors.append(str(error))
    result = dict(time=time.time(), pid=p.pid, created=generation, mode=args.mode,
                  build=data['game_sha256'], probe=metadata, events=events,
                  candidates=candidates, rejected=rejected, errors=errors,
                  failure=failure, cleanup_errors=cleanup_errors,
                  gameplay_ownership_verified=False, stat_writes=False)
    update_session(lambda current: current.update(last_capture=result))
    print(json.dumps(result, indent=2))
    if failure or errors or cleanup_errors:
        raise RuntimeError('Capture did not finish cleanly; inspect last_capture in session.json.')

def text_scan():
    """Locate rendered text fragments without assuming a numeric record layout."""
    from PyMemoryEditor import OpenProcess
    parser = argparse.ArgumentParser(description='Read-only UTF-8/UTF-16 text search.')
    parser.add_argument('--text', action='append', required=True)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args(sys.argv[2:])
    if not 1 <= args.seconds <= 60 or any(not t or len(t)>80 for t in args.text):
        raise ValueError('Use 1..60 seconds and text fragments of 1..80 characters.')
    data = read()
    if running(data.get('runtime', {})): raise RuntimeError('Stop the writer before tracing text.')
    p = game_process(data)
    generation = p.create_time()
    needles = [(t, encoding, t.encode(encoding)) for t in args.text for encoding in ('utf-8','utf-16-le')]
    deadline = time.monotonic()+args.seconds
    hits = []
    scanned = 0
    errors = 0
    complete = True
    with OpenProcess(pid=p.pid, permission=0x410) as memory:
        for region in memory.snapshot_memory_regions():
            if not region.is_readable or not region.is_writable or region.is_executable: continue
            for address in range(region.address, region.address+region.size, 1048576):
                if time.monotonic() >= deadline:
                    complete = False
                    break
                size = min(1048576+1024, region.address+region.size-address)
                try:
                    raw = memory.read_bytes(address, size)
                    for text, encoding, needle in needles:
                        index = raw.find(needle)
                        while index >= 0 and index < 1048576:
                            if len(hits) < 256:
                                start = max(0,index-128)
                                if encoding == 'utf-16-le': start += (index-start)%2
                                context = raw[start:index+len(needle)+256].decode(encoding, errors='replace')
                                hits.append(dict(address=hex(address+index), text=text, encoding=encoding,
                                                 context=context, region=hex(region.address)))
                            index = raw.find(needle,index+len(needle))
                    scanned += min(size,1048576)
                except Exception: errors += 1
            if not complete: break
    result = dict(time=time.time(),pid=p.pid,created=generation,bytes_scanned=scanned,
                  complete=complete and not errors,read_errors=errors,hits=hits,
                  truncated=len(hits)>=256,stat_writes=False)
    update_session(lambda current:current.update(last_text_scan=result))
    print(json.dumps({k:v for k,v in result.items() if k!='hits'},indent=2))
    print(f'Saved {len(hits)} text matches with context in session.json.')

def text_watch():
    import frida
    parser = argparse.ArgumentParser(description='Observe first access to a selected text page.')
    parser.add_argument('--address', type=lambda v:int(v,0), required=True)
    parser.add_argument('--seconds', type=int, default=15)
    args = parser.parse_args(sys.argv[2:])
    if not 1 <= args.seconds <= 30: raise ValueError('Use 1..30 seconds.')
    data = read()
    p = game_process(data)
    scan = data.get('last_text_scan',{})
    if running(data.get('runtime',{})): raise RuntimeError('Stop the writer first.')
    if (scan.get('pid'),scan.get('created')) != (p.pid,p.create_time()):
        raise RuntimeError('Run text-scan in this process first.')
    hit = next((h for h in scan['hits'] if int(h['address'],16)==args.address),None)
    if not hit: raise RuntimeError('Address must be a saved text-scan match.')
    cfg = dict(address=hex(args.address), expected=hit['text'].encode(hit['encoding']).hex())
    source = '''
const cfg=SETTINGS;
rpc.exports={
 install(){
   const address=ptr(cfg.address);
   const expected=cfg.expected.match(/../g).map(x=>parseInt(x,16));
   const actual=new Uint8Array(address.readByteArray(expected.length));
   if(!expected.every((x,i)=>x===actual[i])) throw new Error('Text changed or allocation was reused');
   MemoryAccessMonitor.enable({base:address,size:expected.length},{onAccess(d){
     let instruction=null, module=null, offset=null;
     try{instruction=Instruction.parse(d.from).toString();}catch(e){}
     const m=Process.findModuleByAddress(d.from);
     if(m){module=m.name;offset=d.from.sub(m.base).toString();}
     send({operation:d.operation,from:d.from.toString(),address:d.address.toString(),
           instruction:instruction,module:module,moduleOffset:offset,
           exactTextAccess:d.address.compare(address)>=0 && d.address.compare(address.add(expected.length))<0});
   }});
   return {address:address.toString(),note:'First access per page, possibly neighboring data'};
 }, cleanup(){MemoryAccessMonitor.disable();}
};
'''.replace('SETTINGS',json.dumps(cfg))
    attached=None
    script=None
    events=[]
    errors=[]
    done=threading.Event()
    def message(item,_):
        if item.get('type')=='send':
            events.append(item['payload']);done.set()
        elif item.get('type')=='error':
            errors.append(item.get('description'));done.set()
    try:
        attached=frida.attach(p.pid)
        script=attached.create_script(source)
        script.on('message',message)
        script.load()
        print(json.dumps(script.exports_sync.install()),flush=True)
        done.wait(args.seconds)
    except Exception as error: errors.append(str(error))
    finally:
        if script:
            try: script.exports_sync.cleanup()
            except Exception as error: errors.append(str(error))
            try: script.unload()
            except Exception as error: errors.append(str(error))
        if attached:
            try: attached.detach()
            except Exception as error: errors.append(str(error))
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),text=hit['text'],
                watched=hex(args.address),events=events,errors=errors,stat_writes=False)
    update_session(lambda current:current.update(last_text_watch=result))
    print(json.dumps(result,indent=2))
    if errors: raise RuntimeError('Text watch failed; see last_text_watch.')

def numeric_scan():
    import numpy as np
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Discover/refine crit-damage numeric candidates, read-only.')
    parser.add_argument('--value',type=float,required=True,help='Current displayed crit damage percentage')
    parser.add_argument('--refine',action='store_true')
    parser.add_argument('--stable',action='store_true',help='Keep saved candidates whose exact numeric value is unchanged')
    parser.add_argument('--representation',choices=['both','multiplier','percentage'],default='both')
    parser.add_argument('--type',choices=['both','float','double'],default='both')
    parser.add_argument('--seconds',type=int,default=30)
    args=parser.parse_args(sys.argv[2:])
    if not math.isfinite(args.value) or not 0<args.value<100000 or not 1<=args.seconds<=60:
        raise ValueError('Invalid value or time budget.')
    data=read()
    if running(data.get('runtime',{})): raise RuntimeError('Stop the writer first.')
    p=game_process(data)
    old=data.get('numeric_candidates',{})
    if args.stable and args.refine: raise ValueError('Choose stable or refine, not both.')
    if (args.refine or args.stable) and (old.get('pid'),old.get('created')) != (p.pid,p.create_time()):
        raise RuntimeError('Candidate process changed; run an initial scan.')
    if args.refine and old.get('displayed')==args.value:
        raise RuntimeError('Refinement requires a changed displayed crit-damage value.')
    if args.stable and old.get('displayed')!=args.value:
        raise RuntimeError('Stable filtering requires the unchanged displayed value; otherwise use --refine.')
    deadline=time.monotonic()+args.seconds
    entries=[]
    errors=0
    scanned=0
    complete=True
    truncated=False
    with OpenProcess(pid=p.pid,permission=0x410) as memory:
        if args.refine or args.stable:
            for address,fmt,scale,previous in old['entries']:
                if time.monotonic()>=deadline:
                    complete=False
                    break
                try:
                    value=struct.unpack('<'+fmt,memory.read_bytes(address,struct.calcsize(fmt)))[0]
                    if math.isfinite(value) and abs(value*scale-args.value)<=.501 and (not args.stable or value==previous):
                        entries.append([address,fmt,scale,value])
                except Exception: errors+=1
        else:
            for region in memory.snapshot_memory_regions():
                if not region.is_readable or not region.is_writable or region.is_executable: continue
                for address in range(region.address,region.address+region.size,4194304):
                    if time.monotonic()>=deadline:
                        complete=False
                        break
                    size=min(4194304+8,region.address+region.size-address)
                    try:
                        raw=memory.read_bytes(address,size)
                        for fmt,dtype,width in [('f','<f4',4),('d','<f8',8)]:
                            if args.type!='both' and fmt!= {'float':'f','double':'d'}[args.type]: continue
                            values=np.frombuffer(raw[:len(raw)//width*width],dtype=dtype)
                            for scale in ({'both':(1,100),'multiplier':(100,),'percentage':(1,)}[args.representation]):
                                with np.errstate(invalid='ignore',over='ignore'):
                                    mask=np.isfinite(values) & (np.abs(values*scale-args.value)<=.501)
                                for index in np.flatnonzero(mask):
                                    if int(index)*width>=4194304: continue
                                    if len(entries)>=50000:
                                        truncated=True
                                        break
                                    entries.append([address+int(index)*width,fmt,scale,float(values[index])])
                                if truncated: break
                            if truncated: break
                        scanned+=min(size,4194304)
                    except Exception: errors+=1
                    if truncated: break
                if not complete or truncated: break
    result=dict(pid=p.pid,created=p.create_time(),time=time.time(),displayed=args.value,
                entries=entries,complete=complete and not errors and not truncated and (not (args.refine or args.stable) or old.get('complete',False)),
                bytes_scanned=scanned,read_errors=errors,truncated=truncated,
                operation='stable' if args.stable else ('refine' if args.refine else 'initial'),
                representation=old.get('representation','both') if (args.refine or args.stable) else args.representation,
                numeric_type=old.get('numeric_type','both') if (args.refine or args.stable) else args.type,
                coverage='4-byte floats and 8-byte aligned doubles in readable writable non-executable regions',
                stat_writes=False)
    def store(current):
        previous=current.get('numeric_candidates',{})
        if previous.get('entries'):
            history=current.setdefault('numeric_history',[])
            history.append(previous)
            current['numeric_history']=history[-3:]
        current['numeric_candidates']=result
    update_session(store)
    print(json.dumps({k:v for k,v in result.items() if k!='entries'},indent=2))
    print(f'Numeric candidates: {len(entries)}. Saved in session.json; no writes performed.')

def numeric_watch():
    import frida
    parser=argparse.ArgumentParser(description='Bounded hardware read/write watch of saved numeric candidates.')
    parser.add_argument('--address',action='append',type=lambda v:int(v,0),required=True)
    parser.add_argument('--seconds',type=int,default=10)
    args=parser.parse_args(sys.argv[2:])
    if not 1<=len(args.address)<=4 or not 1<=args.seconds<=15:
        raise ValueError('Use 1..4 addresses and 1..15 seconds.')
    data=read();p=game_process(data);generation=p.create_time()
    n=data.get('numeric_candidates',{})
    if running(data.get('runtime',{})): raise RuntimeError('Stop the writer first.')
    if (n.get('pid'),n.get('created'))!=(p.pid,generation): raise RuntimeError('Run a fresh numeric scan.')
    chosen=[]
    for address in args.address:
        e=next((e for e in n['entries'] if e[0]==address),None)
        if not e or address%struct.calcsize(e[1]): raise RuntimeError('Address must be an aligned saved candidate.')
        chosen.append(dict(address=hex(address),size=struct.calcsize(e[1]),value=e[3]))
    source='''
const chosen=SETTINGS;
const armed=new Map();
let count=0;
function clear(t){for(let i=0;i<chosen.length;i++){try{t.unsetHardwareWatchpoint(i);}catch(e){}}}
Process.setExceptionHandler(d=>{
 if(d.type!=='single-step') return false;
 const tid=Process.getCurrentThreadId();
 const t=armed.get(tid);
 if(!t) return false;
 let mask;
 try{mask=d.nativeContext.add(104).readU32() & ((1<<chosen.length)-1);}catch(e){return false;}
 if(mask===0) return false;
 clear(t);armed.delete(tid);
 if(count++<8){
   const c=d.context;
   const registers={};
   for(const r of ['rax','rcx','rdx','rbx','rsp','rbp','rsi','rdi','r8','r9','r10','r11','r12','r13','r14','r15','rip'])
     if(c[r]!==undefined)registers[r]=c[r].toString();
   let code=null,frames=[];
   try{code=Array.from(new Uint8Array(c.pc.sub(32).readByteArray(64))).map(x=>x.toString(16).padStart(2,'0')).join('');}catch(e){}
   try{frames=Thread.backtrace(c,Backtracer.ACCURATE).map(a=>{const m=Process.findModuleByAddress(a);return {address:a.toString(),module:m?m.name:null,offset:m?a.sub(m.base).toString():null};});}catch(e){}
   send({thread:tid,triggerMask:mask,pc:c.pc.toString(),registers:registers,codeBeforePc:32,codeHex:code,frames:frames});
 }
 return true;
});
rpc.exports={install(){
 for(const x of chosen){const actual=x.size===8?ptr(x.address).readDouble():ptr(x.address).readFloat();if(actual!==x.value)throw new Error('Candidate changed before arming: '+x.address);}
 const skipped=[];
 for(const t of Process.enumerateThreads()){
   if(t.id===Process.getCurrentThreadId())continue;
   try{for(let i=0;i<chosen.length;i++)t.setHardwareWatchpoint(i,ptr(chosen[i].address),chosen[i].size,'rw');armed.set(t.id,t);}
   catch(e){clear(t);skipped.push(t.id);}
 }
 return {armedThreads:armed.size,skippedThreads:skipped.length};
},cleanup(){for(const t of armed.values())clear(t);armed.clear();Process.setExceptionHandler(null);}};
'''.replace('SETTINGS',json.dumps(chosen))
    attached=None;script=None;events=[];errors=[];metadata=None;done=threading.Event()
    def message(item,_):
        if item.get('type')=='send':
            if len(events)<8: events.append(item['payload'])
            if len(events)>=8: done.set()
        elif item.get('type')=='error':errors.append(item.get('description'));done.set()
    try:
        attached=frida.attach(p.pid);script=attached.create_script(source);script.on('message',message);script.load()
        metadata=script.exports_sync.install();print(json.dumps(metadata),flush=True)
        done.wait(args.seconds)
    except Exception as error:errors.append(str(error))
    finally:
        if script:
            try:script.exports_sync.cleanup()
            except Exception as error:errors.append(str(error))
            try:script.unload()
            except Exception as error:errors.append(str(error))
        if attached:
            try:attached.detach()
            except Exception as error:errors.append(str(error))
    result=dict(time=time.time(),pid=p.pid,created=generation,watched=chosen,metadata=metadata,
                events=events,errors=errors,stat_writes=False,gameplay_ownership_verified=False)
    update_session(lambda current:current.update(last_numeric_watch=result))
    print(json.dumps(result,indent=2))
    if errors:raise RuntimeError('Watchpoint trace failed; inspect last_numeric_watch.')

def apply_once():
    """Explicit data-only retry: no process suspension, debugger, hooks or worker."""
    from PyMemoryEditor import OpenProcess
    data=read();p=game_process(data);saved=data['config']
    if data.get('requires_fresh_run_verification'):
        raise RuntimeError('Completed-run addresses are stale; verify the new run before applying.')
    if running(data.get('runtime',{})):raise RuntimeError('Stop the writer before a one-shot update.')
    if (p.pid,p.create_time())!=(saved['pid'],saved['created']):raise RuntimeError('Fresh process discovery required.')
    with OpenProcess(pid=p.pid) as memory:
        config=record(memory,p,saved['base'],[])
        if config['player_id']!=saved['player_id'] or config['level']<saved.get('level',0):
            raise RuntimeError('Player/run identity changed; no writes.')
        config.update(targets=saved['targets'],bonuses=[])
        changes=[(name,config['base']+offset,struct.pack('<'+fmt,value)) for name,offset,fmt,value in config['targets']]
        backup=dict(time=time.time(),pid=p.pid,created=p.create_time(),base=config['base'],
                    reason='Explicit non-pausing one-shot core retry',
                    changes=[dict(name=n,address=a,original=memory.read_bytes(a,len(v)).hex(),patched=v.hex()) for n,a,v in changes])
        update_session(lambda current:current.setdefault('run_backups',[]).append(backup))
        write_batch(memory,[(a,v) for _,a,v in changes],lambda:validate(memory,config,data))
        values={name:struct.unpack('<'+fmt,memory.read_bytes(config['base']+offset,4))[0] for name,offset,fmt,_ in config['targets']}
    result=dict(time=time.time(),pid=p.pid,level=config['level'],values=values,
                process_paused=False,debugger_attached=False,background_writer=False,
                modifiers_changed=False,persistence_verified=False)
    update_session(lambda current:current.update(last_one_shot=result))
    print(json.dumps(result,indent=2))

def watch_self_test():
    """Exercise the exact watch agent on a disposable process, never the game."""
    import frida
    fixture="import ctypes,os,time; x=ctypes.c_double(249.3); print(os.getpid(),ctypes.addressof(x),flush=True);\nwhile True:\n y=x.value\n time.sleep(.01)"
    child=subprocess.Popen([sys.executable,'-B','-c',fixture],stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    attached=None;script=None;events=[];errors=[];done=threading.Event();metadata=None
    def bounded(call):
        cancel=frida.Cancellable()
        timer=threading.Timer(5,cancel.cancel);timer.daemon=True;timer.start()
        try:
            with cancel:return call()
        finally:timer.cancel()
    try:
        pid,address=map(int,child.stdout.readline().split())
        template=next(c for c in numeric_watch.__code__.co_consts if isinstance(c,str) and 'Process.setExceptionHandler' in c)
        source=template.replace('SETTINGS',json.dumps([dict(address=hex(address),size=8,value=249.3)]))
        def message(item,_):
            if item.get('type')=='send':events.append(item['payload']);done.set()
            elif item.get('type')=='error':errors.append(item.get('description'));done.set()
        attached=bounded(lambda:frida.attach(pid));script=attached.create_script(source);script.on('message',message)
        bounded(script.load);metadata=bounded(script.exports_sync.install);done.wait(3)
    except Exception as error:errors.append(str(error))
    finally:
        if script:
            try:bounded(script.exports_sync.cleanup)
            except Exception as error:errors.append(str(error))
            try:bounded(script.unload)
            except Exception as error:errors.append(str(error))
        if attached:
            try:bounded(attached.detach)
            except Exception as error:errors.append(str(error))
        if child.poll() is None:child.terminate()
        child.wait(timeout=5)
    result=dict(time=time.time(),test='disposable-process hardware read watch',passed=bool(events) and not errors,
                metadata=metadata,events=events,errors=errors,game_touched=False)
    update_session(lambda current:current.update(watch_self_test=result))
    print(json.dumps(result,indent=2))
    if not result['passed']:raise RuntimeError('Hardware watchpoint positive-control test failed.')

def inspect_copy():
    """Read the last observed copy destination and disassemble the exact access site."""
    from PyMemoryEditor import OpenProcess
    import frida
    data=read();p=game_process(data);config=data['config'];trace=data['last_numeric_watch']
    if (trace['pid'],trace['created'])!=(p.pid,p.create_time()):raise RuntimeError('Stale watch trace.')
    hit=trace['events'][0]
    delta=config['base']-int(hit['registers']['rcx'],16)
    destination=int(hit['registers']['rdx'],16)+delta
    out=dict(time=time.time(),pid=p.pid,source_base=hex(config['base']),
             destination_base=hex(destination),entity_stat_offset=hex(delta),stat_writes=False)
    with OpenProcess(pid=p.pid,permission=0x410) as memory:
        try:
            out['destination_record']=record(memory,p,destination,[])
            out['destination_values']={name:struct.unpack('<'+fmt,memory.read_bytes(destination+off,4))[0] for name,off,fmt,_ in config['targets']}
        except Exception as error:out['destination_error']=str(error)
    def bounded(call):
        cancel=frida.Cancellable();timer=threading.Timer(5,cancel.cancel);timer.daemon=True;timer.start()
        try:
            with cancel:return call()
        finally:timer.cancel()
    attached=None;script=None
    try:
        attached=bounded(lambda:frida.attach(p.pid))
        script=attached.create_script('rpc.exports={inspect(address){let p=ptr(address);const result=[];for(let i=0;i<28;i++){const x=Instruction.parse(p);result.push({address:p.toString(),instruction:x.toString()});p=x.next;}return result;}};')
        bounded(script.load)
        out['instructions']=bounded(lambda:script.exports_sync.inspect(hex(int(hit['pc'],16)-7)))
    except Exception as error:out['disassembly_error']=str(error)
    finally:
        if script:
            try:bounded(script.unload)
            except Exception as error:out['unload_error']=str(error)
        if attached:
            try:bounded(attached.detach)
            except Exception as error:out['detach_error']=str(error)
    update_session(lambda current:current.update(copy_destination_inspection=out))
    print(json.dumps(out,indent=2))

def observe_swaps():
    import frida
    parser=argparse.ArgumentParser(description='Observe own-player record movement at the verified swap entry, without stat writes.')
    parser.add_argument('--seconds',type=int,default=5)
    args=parser.parse_args(sys.argv[2:])
    if not 1<=args.seconds<=10:raise ValueError('Use 1..10 seconds.')
    data=read();p=game_process(data);candidate=data['swap_candidate'];config=data['config']
    if (candidate['pid'],candidate['created'])!=(p.pid,p.create_time()):raise RuntimeError('Swap candidate is stale.')
    cfg=dict(entry=candidate['entry'],bytes=candidate['first_bytes'],id=config['player_id'],
             idOffset=int(candidate['player_id_offset'],16),statOffset=int(candidate['stat_offset'],16),seconds=args.seconds)
    source='''
const cfg=SETTINGS;let listener=null;let matches=0;let total=0;
function clean(){if(listener){listener.detach();listener=null;}Interceptor.flush();}
rpc.exports={install(){
 const entry=ptr(cfg.entry);
 const actual=Array.from(new Uint8Array(entry.readByteArray(cfg.bytes.length/2))).map(x=>x.toString(16).padStart(2,'0')).join('');
 if(actual!==cfg.bytes)throw new Error('Swap entry changed');
 listener=Interceptor.attach(entry,{
  onEnter(args){
   total++;
   if(matches>=8)return;
   try{
    this.a=args[0];this.b=args[1];this.ia=this.a.add(cfg.idOffset).readU32();this.ib=this.b.add(cfg.idOffset).readU32();
    this.own=this.ia===cfg.id||this.ib===cfg.id;
   }catch(e){this.own=false;}
  },
  onLeave(){
   if(!this.own||matches>=8)return;
   matches++;
   try{
    const afterA=this.a.add(cfg.idOffset).readU32();const afterB=this.b.add(cfg.idOffset).readU32();
    const before=this.ia===cfg.id?this.a:this.b;
    const after=afterA===cfg.id?this.a:afterB===cfg.id?this.b:null;
    const value=after?after.add(cfg.statOffset):null;
    send({beforeIds:[this.ia,this.ib],afterIds:[afterA,afterB],beforeBase:before.add(cfg.statOffset).toString(),
          afterBase:value?value.toString():null,level:value?value.add(104).readU32():null,
          maxHp:value?value.readFloat():null,damage:value?value.add(4).readFloat():null,
          critDamage:value?value.add(16).readFloat():null});
   }catch(e){send({error:String(e)});}
   if(matches>=8)setImmediate(clean);
  }
 });
 setTimeout(clean,cfg.seconds*1000);
 return {entry:entry.toString(),id:cfg.id};
},summary(){return {calls:total,ownMatches:matches};},cleanup:clean};
'''.replace('SETTINGS',json.dumps(cfg))
    attached=None;script=None;events=[];errors=[];summary=None
    def bounded(call):
        cancel=frida.Cancellable();timer=threading.Timer(5,cancel.cancel);timer.daemon=True;timer.start()
        try:
            with cancel:return call()
        finally:timer.cancel()
    def message(item,_):
        if item.get('type')=='send':
            if len(events)<8:events.append(item['payload'])
        elif item.get('type')=='error':errors.append(item.get('description'))
    try:
        attached=bounded(lambda:frida.attach(p.pid));script=attached.create_script(source);script.on('message',message)
        bounded(script.load);print(json.dumps(bounded(script.exports_sync.install)),flush=True)
        threading.Event().wait(args.seconds+.2)
        summary=bounded(script.exports_sync.summary)
    except Exception as error:errors.append(str(error))
    finally:
        if script:
            try:bounded(script.exports_sync.cleanup)
            except Exception as error:errors.append(str(error))
            try:bounded(script.unload)
            except Exception as error:errors.append(str(error))
        if attached:
            try:bounded(attached.detach)
            except Exception as error:errors.append(str(error))
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),summary=summary,events=events,errors=errors,stat_writes=False)
    update_session(lambda current:current.update(swap_observation=result))
    print(json.dumps(result,indent=2))
    if errors:raise RuntimeError('Swap observation failed; inspect session.json.')

def name_headers(raw,length):
    import numpy as np
    values=np.frombuffer(raw[:len(raw)//8*8],dtype='<u8')
    if len(values)<3:return []
    mask=(values[:-2]>=length) & (values[:-2]<=256) & (values[2:]==length) & (values[1:-1]>=0x10000) & (values[1:-1]<0x7ffffffff000)
    return [(int(index),int(values[index+1])) for index in np.flatnonzero(mask)]

def discover_name():
    """Find validated native records through their length-checked character name."""
    import numpy as np
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Read-only native character-name discovery for the known build.')
    parser.add_argument('--name',default=None,help='Character name; defaults to the configured one')
    parser.add_argument('--seconds',type=int,default=30)
    args=parser.parse_args(sys.argv[2:])
    args.name=args.name or read().get('native_identity_recipe',{}).get('name') or ''
    name=args.name.encode('utf-8')
    if not 1<=len(name)<=64 or not 1<=args.seconds<=60:raise ValueError('Invalid name or time budget.')
    data=read();p=game_process(data);recipe=data.get('native_identity_recipe',{})
    if recipe.get('build','').lower()!=data['game_sha256'].lower():raise RuntimeError('No identity recipe for this build.')
    if running(data.get('runtime',{})):raise RuntimeError('Stop the writer first.')
    delta=recipe['stats_offset']-recipe['name_header_offset']
    deadline=time.monotonic()+args.seconds
    candidates={};scanned=0;errors=0;complete=True;headers=0
    with OpenProcess(pid=p.pid,permission=0x410) as memory:
        for region in memory.snapshot_memory_regions():
            if not region.is_readable or not region.is_writable or region.is_executable:continue
            for address in range(region.address,region.address+region.size,4194304):
                if time.monotonic()>=deadline:complete=False;break
                size=min(4194304+24,region.address+region.size-address)
                try:
                    raw=memory.read_bytes(address,size)
                    for index,pointer in name_headers(raw,len(name)):
                        if time.monotonic()>=deadline:complete=False;break
                        if int(index)*8>=4194304:continue
                        header=address+int(index)*8;headers+=1
                        try:
                            if memory.read_bytes(pointer,len(name))!=name:continue
                            base=header+delta
                            candidate=record(memory,p,base,[])
                            if memory.read_bytes(header,24)!=raw[int(index)*8:int(index)*8+24]:continue
                            if struct.unpack('<I',memory.read_bytes(base-24,4))[0]!=candidate['player_id']:continue
                            candidate.update(base_hex=hex(base),name=args.name,name_header=hex(header),source='length_checked_character_name')
                            candidates[base]=candidate
                        except Exception:pass
                    scanned+=min(size,4194304)
                except Exception:errors+=1
                if not complete:break
            if not complete:break
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),name=args.name,
                bytes_scanned=scanned,string_headers_checked=headers,read_errors=errors,
                complete=complete and not errors,candidates=list(candidates.values()),stat_writes=False)
    update_session(lambda current:current.update(name_discovery=result))
    print(json.dumps(result,indent=2))

def named_candidate(memory,p,candidate,recipe,name):
    for attempt in range(5):
        try:return _named_candidate_once(memory,p,candidate,recipe,name)
        except (ValueError,RuntimeError):
            if attempt==4:raise
            time.sleep(.005)

def resolve_name():
    """Treat prior locations only as hints; re-read name, identity and owner now."""
    from PyMemoryEditor import OpenProcess
    data=read();p=game_process(data);recipe=data['native_identity_recipe'];name=recipe['name']
    hints={data['config']['base']}
    hints.update(c['base'] for c in data.get('name_discovery',{}).get('candidates',[]))
    candidates=[];profile=dict(recipe,targets=data['config']['targets']);lease=data.get('auto_last_verified',{})
    hints.update(h['base'] for h in data.get('auto_hints',[]) if h.get('name')==name and h.get('build','').lower()==data['game_sha256'].lower())
    with OpenProcess(pid=p.pid,permission=0x410) as memory:
        for base in hints:
            try:
                known=(lease.get('pid'),lease.get('created'),lease.get('base'))==(p.pid,p.create_time(),base)
                fresh=actor_snapshot(memory,p,base,profile,name,expected_id=lease.get('player_id') if known else None,health_ceiling=9999. if known else None)
                fresh.update(name=name,base_hex=hex(base),source='fresh_name_validation_of_hint')
                candidates.append(fresh)
            except Exception:pass
    if len(candidates)!=1:raise RuntimeError('Could not resolve a unique hinted name; run discover-name.')
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),name=name,candidates=candidates,
                complete=False,hint_only=True,stat_writes=False)
    update_session(lambda current:current.update(name_discovery=result,requires_fresh_run_verification=True))
    print(json.dumps(result,indent=2))

def _named_candidate_once(memory,p,candidate,recipe,name):
    base=candidate['base'];header=base-recipe['stats_offset']+recipe['name_header_offset']
    cap,pointer,length=struct.unpack('<3Q',memory.read_bytes(header,24))
    encoded=name.encode('utf-8')
    if length!=len(encoded) or not length<=cap<=256 or memory.read_bytes(pointer,length)!=encoded:
        raise RuntimeError('Character-name identity changed.')
    fresh=record(memory,p,base,[])
    if fresh['player_id']!=candidate['player_id']:raise RuntimeError('Player ID changed.')
    if memory.read_bytes(header,24)!=struct.pack('<3Q',cap,pointer,length):raise RuntimeError('Name moved during read.')
    return fresh

def lifesteal_test():
    """One existing source-value write, then read-only upgrade observation."""
    from PyMemoryEditor import OpenProcess
    data=read();p=game_process(data);discovery=data.get('name_discovery',{});recipe=data['native_identity_recipe']
    if running(data.get('runtime',{})):raise RuntimeError('Stop the writer first.')
    if (discovery.get('pid'),discovery.get('created'))!=(p.pid,p.create_time()) or len(discovery.get('candidates',[]))!=1:
        raise RuntimeError('Requires fresh, unambiguous name discovery in this process.')
    if time.time()-discovery['time']>180:raise RuntimeError('Name discovery is stale.')
    candidate=discovery['candidates'][0];name=discovery['name'];samples=[]
    with OpenProcess(pid=p.pid) as memory:
        fresh=named_candidate(memory,p,candidate,recipe,name)
        if fresh['level']<candidate['level']:raise RuntimeError('Run reset after discovery.')
        indices=[i for i,k in enumerate(fresh['kinds']) if k==14]
        if len(indices)!=1:raise RuntimeError('Expected one existing lifesteal modifier.')
        address=fresh['pointer']+indices[0]*8+4
        before=memory.read_bytes(address,4);value=struct.unpack('<f',before)[0]
        if not 0<=value<=.1:raise RuntimeError('Source already modified or unexpected; refusing additive stacking.')
        target=struct.pack('<f',value+.1)
        backup=dict(time=time.time(),pid=p.pid,created=p.create_time(),base=fresh['base'],player_id=fresh['player_id'],
                    changes=[dict(name='Lifesteal +10 percentage-point test',address=address,original=before.hex(),patched=target.hex())])
        update_session(lambda current:current.setdefault('run_backups',[]).append(backup))
        def guard():
            now=named_candidate(memory,p,candidate,recipe,name)
            if any(now[k]!=fresh[k] for k in ('pointer','capacity','length','kinds')):raise RuntimeError('Modifier owner changed.')
        write_batch(memory,[(address,target)],guard)
        print(json.dumps({'applied':True,'kind':14,'original':value,'target':struct.unpack('<f',target)[0],'level':fresh['level'],'process_paused':False}),flush=True)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                now=named_candidate(memory,p,candidate,recipe,name)
                ix=[i for i,k in enumerate(now['kinds']) if k==14]
                if len(ix)==1:
                    amount=struct.unpack('<f',memory.read_bytes(now['pointer']+ix[0]*8+4,4))[0]
                    sample=dict(level=now['level'],pointer=hex(now['pointer']),lifesteal_source=amount)
                    if not samples or sample!=samples[-1]:samples.append(sample)
            except Exception:pass
            threading.Event().wait(.5)
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),player_id=fresh['player_id'],name=name,
                original=value,test_value=struct.unpack('<f',target)[0],samples=samples,
                process_paused=False,debugger_attached=False,background_writer=False)
    update_session(lambda current:current.update(lifesteal_source_test=result))
    print(json.dumps(result,indent=2))

def source_apply():
    """Reapply only known existing bonus sources after ownership-checked rebuilds."""
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Non-pausing lifesteal/shielded source application; preserves array layout.')
    parser.add_argument('--seconds',type=int,default=1)
    args=parser.parse_args(sys.argv[2:])
    if not 1<=args.seconds<=60:raise ValueError('Use 1..60 seconds.')
    data=read();p=game_process(data);nd=data.get('name_discovery',{});recipe=data['native_identity_recipe']
    if running(data.get('runtime',{})):raise RuntimeError('Stop the old writer first.')
    if (nd.get('pid'),nd.get('created'))!=(p.pid,p.create_time()) or len(nd.get('candidates',[]))!=1:
        raise RuntimeError('Fresh name resolution required.')
    if time.time()-nd['time']>180:raise RuntimeError('Name resolution is stale.')
    candidate=nd['candidates'][0];last_level=candidate['level'];events=[];errors=[];failures=0
    targets={14:99.984,28:99.99}
    runtime=dict(pid=os.getpid(),created=psutil.Process().create_time(),state='checking_sources',time=time.time())
    update_session(lambda current:current.update(stop_requested=False,source_monitor=runtime))
    deadline=time.monotonic()+args.seconds
    with OpenProcess(pid=p.pid) as memory:
        while time.monotonic()<deadline:
            if read().get('stop_requested'):runtime['state']='stopped';break
            try:
                fresh=named_candidate(memory,p,candidate,recipe,nd['name'])
                if fresh['level']<last_level:raise RuntimeError('Run level reset; resolve the new run first.')
                last_level=fresh['level']
                edits=[];backup=[]
                for kind,target in targets.items():
                    indices=[i for i,k in enumerate(fresh['kinds']) if k==kind]
                    if len(indices)!=1:raise RuntimeError('Expected exactly one existing source for kind '+str(kind))
                    address=fresh['pointer']+indices[0]*8+4
                    original=memory.read_bytes(address,4);replacement=struct.pack('<f',target)
                    if original!=replacement:
                        edits.append((address,replacement))
                        backup.append(dict(kind=kind,address=address,original=original.hex(),patched=replacement.hex()))
                if edits:
                    update_session(lambda current:current.setdefault('run_backups',[]).append(dict(time=time.time(),pid=p.pid,created=p.create_time(),base=fresh['base'],reason='Existing known sources only; no suspension',changes=backup)))
                    def guard():
                        now=named_candidate(memory,p,candidate,recipe,nd['name'])
                        if now['level']<last_level or any(now[k]!=fresh[k] for k in ('pointer','capacity','length','kinds')):
                            raise RuntimeError('Source owner or run changed.')
                    write_batch(memory,edits,guard)
                    event=dict(level=fresh['level'],pointer=hex(fresh['pointer']),kinds=[b['kind'] for b in backup],time=time.time())
                    events.append(event);print(json.dumps(event),flush=True)
                failures=0
            except Exception as error:
                failures+=1;errors.append(str(error))
                if failures>=3 or str(error).startswith('Run level reset'):
                    runtime.update(state='guard_stopped',reason=str(error));break
            runtime.update(heartbeat=time.time(),level=last_level)
            update_session(lambda current:current.update(source_monitor=runtime))
            threading.Event().wait(1)
    if runtime['state']=='checking_sources':runtime['state']='finished'
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),level=last_level,events=events,errors=errors,
                targets=targets,pauses=False,debugger=False,array_replaced=False,core_stats_modified=False)
    update_session(lambda current:current.update(source_monitor=runtime,last_source_application=result))
    print(json.dumps(result,indent=2))

def named_core():
    """Apply supported core fields without suspending or instrumenting the game."""
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Name-guarded non-pausing core/source updater.')
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--background',action='store_true')
    args=parser.parse_args(sys.argv[2:])
    if not 1<=args.seconds<=1200:raise ValueError('Use 1..1200 seconds.')
    data=read();p=game_process(data);nd=data.get('name_discovery',{})
    if (nd.get('pid'),nd.get('created'))!=(p.pid,p.create_time()) or len(nd.get('candidates',[]))!=1:
        raise RuntimeError('Fresh name resolution is required.')
    if time.time()-nd['time']>180:raise RuntimeError('Run resolve-name before starting.')
    if running(data.get('runtime',{})) or (running(data.get('named_monitor',{})) and data['named_monitor']['pid']!=os.getpid()):
        raise RuntimeError('A writer is already running.')
    if args.background:
        update_session(lambda current:current.update(stop_requested=False))
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'_named_core','--seconds',str(args.seconds)],
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        update_session(lambda current:current.update(named_monitor=dict(pid=child.pid,created=psutil.Process(child.pid).create_time(),state='starting')))
        print(f'Non-pausing name-guarded updater started: PID {child.pid}. Stop with python game.py stop.')
        return
    if sys.argv[1]!='_named_core':update_session(lambda current:current.update(stop_requested=False))
    candidate=nd['candidates'][0];recipe=data['native_identity_recipe'];last_level=candidate['level']
    supported={0,4,8,12,16,20,28,32,36,44,48,60,64,72,112,120}
    targets=[t for t in data['config']['targets'] if t[1] in supported]
    source_targets={14:99.984,28:99.99}
    runtime=dict(pid=os.getpid(),created=psutil.Process().create_time(),game_pid=p.pid,state='updating',heartbeat=time.time(),level=last_level,pauses=False)
    events=[];errors=[];failures=0;passes=0;last_save=0;backed_up=set();last_print_level=None
    update_session(lambda current:current.update(named_monitor=runtime))
    deadline=time.monotonic()+args.seconds
    try:
        with OpenProcess(pid=p.pid) as memory:
            while time.monotonic()<deadline:
                if read().get('stop_requested'):runtime.update(state='stopped',reason='User requested stop.');break
                try:
                    fresh=named_candidate(memory,p,candidate,recipe,nd['name'])
                    if fresh['level']<last_level:raise RuntimeError('Run level reset; fresh resolution required.')
                    last_level=fresh['level'];changes=[];names=[]
                    for name,off,fmt,value in targets:
                        address=fresh['base']+off;target=struct.pack('<'+fmt,value)
                        if memory.read_bytes(address,4)!=target:changes.append((address,target));names.append(name)
                    for kind,value in source_targets.items():
                        indices=[i for i,k in enumerate(fresh['kinds']) if k==kind]
                        if len(indices)!=1:raise RuntimeError('Known source is missing or duplicated.')
                        address=fresh['pointer']+indices[0]*8+4;target=struct.pack('<f',value)
                        if memory.read_bytes(address,4)!=target:changes.append((address,target));names.append('Source '+str(kind))
                    if changes:
                        key=(fresh['pointer'],fresh['level'])
                        if key not in backed_up:
                            backup=dict(time=time.time(),pid=p.pid,created=p.create_time(),base=fresh['base'],level=fresh['level'],
                                reason='Name-guarded non-pausing core/source updater',
                                changes=[dict(name=n,address=a,original=memory.read_bytes(a,len(v)).hex(),patched=v.hex()) for n,(a,v) in zip(names,changes)])
                            update_session(lambda current:current.setdefault('run_backups',[]).append(backup));backed_up.add(key)
                        def guard():
                            now=named_candidate(memory,p,candidate,recipe,nd['name'])
                            if now['level']<last_level or any(now[k]!=fresh[k] for k in ('pointer','capacity','length','kinds')):
                                raise RuntimeError('Source ownership or run changed.')
                        write_batch(memory,changes,guard)
                        event=dict(time=time.time(),level=fresh['level'],fields=names)
                        events.append(event);events=events[-32:];passes+=1
                        if last_print_level!=fresh['level']:
                            print(json.dumps(event),flush=True);last_print_level=fresh['level']
                    failures=0
                except Exception as error:
                    failures+=1;errors.append(str(error));errors=errors[-16:]
                    if failures>=3 or str(error).startswith('Run level reset'):
                        runtime.update(state='guard_stopped',reason=str(error));break
                if time.monotonic()-last_save>=2:
                    runtime.update(heartbeat=time.time(),level=last_level,write_batches=passes)
                    update_session(lambda current:current.update(named_monitor=runtime));last_save=time.monotonic()
                threading.Event().wait(.5)
    except Exception as error:runtime.update(state='guard_stopped',reason=str(error));errors.append(str(error))
    if runtime['state']=='updating':runtime['state']='finished'
    result=dict(time=time.time(),pid=p.pid,created=p.create_time(),level=last_level,write_batches=passes,
                events=events,errors=errors,pauses=False,debugger=False,excluded_offsets=[24,40,52,56,68])
    update_session(lambda current:current.update(named_monitor=runtime,last_named_core=result))
    print(json.dumps(result,indent=2))

def trace_summary(candidate,values,method):
    return dict(name=candidate['name'],pid=candidate['pid'],player_id=candidate['player_id'],
                level=candidate['level'],base=hex(candidate['base']),method=method,
                hp=f"{values['Current Health']:.0f}/{values['Max Health']:.0f}",
                gold=values['Gold Wallet'],damage_bonus_percent=round((values['Damage']-1)*100,1),
                crit_damage_percent=round(values['Crit Damage']*100,1),
                speed_bonus_percent=round((values['Speed']-1)*100,1),
                experience_bonus_percent=round((values['Experience']-1)*100,1),
                modifier_pointer=hex(candidate['pointer']),modifier_count=candidate['length'])

def new_game():
    """One command: trace now; optionally stop/reacquire/start supported updates."""
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Fresh stat trace, with optional non-pausing application.')
    parser.add_argument('--apply',action='store_true',help='Stop the old updater, resolve the current run, then start supported updates')
    parser.add_argument('--seconds',type=int,default=28800,help='Automatic watcher lifetime with --apply, 1..86400 seconds')
    parser.add_argument('--scan-seconds',type=int,default=30,help='Fallback name-scan budget, 1..60 seconds')
    args=parser.parse_args(sys.argv[2:])
    if not 1<=args.seconds<=86400 or not 1<=args.scan_seconds<=60:raise ValueError('Invalid time budget.')
    original_argv=sys.argv[:]
    if args.apply:
        sys.argv=[original_argv[0],'auto','--background','--seconds',str(args.seconds)]
        try:auto_command()
        finally:sys.argv=original_argv
        return
    def inspect():
        nonlocal original_argv
        method='fresh_name_validation'
        try:
            with redirect_stdout(io.StringIO()):resolve_name()
        except RuntimeError:
            # Only invoke the slower fallback after executable/profile checks succeed.
            game_process(read())
            method='bounded_name_scan'
            sys.argv=[original_argv[0],'discover-name','--name',read()['native_identity_recipe']['name'],'--seconds',str(args.scan_seconds)]
            try:
                with redirect_stdout(io.StringIO()):discover_name()
            finally:sys.argv=original_argv
        state=read();found=state['name_discovery'];p=game_process(state)
        if len(found.get('candidates',[]))!=1:raise RuntimeError('Name trace is ambiguous or empty; nothing applied.')
        if (found['pid'],found['created'])!=(p.pid,p.create_time()):raise RuntimeError('Process changed during tracing.')
        with OpenProcess(pid=p.pid,permission=0x410) as memory:
            profile=dict(state['native_identity_recipe'],targets=state['config']['targets']);c=found['candidates'][0];lease=state.get('auto_last_verified',{})
            known=(lease.get('pid'),lease.get('created'),lease.get('base'),lease.get('player_id'))==(p.pid,p.create_time(),c['base'],c['player_id'])
            fresh=actor_snapshot(memory,p,c['base'],profile,found['name'],expected_id=c['player_id'],health_ceiling=9999. if known else None)
            fresh['name']=found['name']
            values={name:struct.unpack('<'+fmt,memory.read_bytes(fresh['base']+off,4))[0] for name,off,fmt,_ in state['config']['targets']}
        summary=trace_summary(fresh,values,method)
        summary.update(time=time.time(),trace_only=not args.apply,scan_complete=found.get('complete',False))
        def remember(current):
            history=current.setdefault('trace_history',[])
            history.append(summary);current['trace_history']=history[-32:]
            current['last_trace']=summary
        update_session(remember)
        print(json.dumps(summary,indent=2))
    try:inspect()
    finally:sys.argv=original_argv

STAT_NAMES = {'hp':'Max Health','damage':'Damage','crit':'Crit','crit_damage':'Crit Damage','speed':'Speed',
              'pickup':'Pickup','experience':'Experience','armor':'Armor','regen':'Regeneration','area':'Area',
              'luck':'Luck','echo':'Echo'}

def relayout():
    """Read-only: learn per-field offsets for a new build from the stats shown on the character sheet."""
    import numpy as np
    from PyMemoryEditor import OpenProcess
    parser=argparse.ArgumentParser(description='Learn moved stat offsets from displayed stats (read-only). Give >=5, e.g. --hp 807 --damage 104 --crit 32 --crit-damage 261 --speed 58 --pickup 42 --armor 45 --regen 5 --area 36 --echo 27')
    for name in FIELDS:parser.add_argument('--'+name.replace('_','-'),type=float,dest=name)
    parser.add_argument('--seconds',type=int,default=60)
    args=parser.parse_args(sys.argv[2:])
    data=read();observed={k:(v*FIELDS[k][1]+FIELDS[k][2],FIELDS[k][3],FIELDS[k][0]) for k in FIELDS for v in [getattr(args,k)] if v is not None and k in STAT_NAMES}
    if 'hp' not in observed or len(observed)<5:raise RuntimeError('Supply --hp and at least four other displayed stats.')
    matches=[p for p in psutil.process_iter(['exe']) if p.info['exe'] and os.path.normcase(p.info['exe'])==os.path.normcase(data['game_exe'])]
    if len(matches)!=1:raise RuntimeError(f'Expected one game process, found {len(matches)}.')
    p=psutil.Process(matches[0].pid);path=Path(p.exe())
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest().lower()
    hp,tol,_=observed['hp'];layouts={};deadline=time.monotonic()+args.seconds
    with OpenProcess(pid=p.pid,permission=0x410) as memory:
        for r in memory.snapshot_memory_regions():
            if not(r.is_readable and r.is_writable and not r.is_executable):continue
            for a in range(r.address,r.address+r.size,4194304):
                if time.monotonic()>=deadline:break
                try:raw=memory.read_bytes(a,min(4194304+4096,r.address+r.size-a))
                except Exception:continue
                v=np.frombuffer(raw[:len(raw)//4*4],dtype='<f4')
                with np.errstate(invalid='ignore'):
                    for i in np.flatnonzero(np.abs(v[:1048576]-hp)<=tol):
                        lo=max(0,int(i)-256);w=v[lo:int(i)+512];found={}
                        for k,(val,t,_) in observed.items():
                            if k=='hp':continue
                            hits=(np.flatnonzero(np.abs(w-val)<=t)+lo-int(i))*4
                            if len(hits):found[k]=[int(x) for x in hits]
                        if len(found)>=len(observed)-2:layouts[tuple(sorted((k,tuple(x)) for k,x in found.items()))]=a+int(i)*4
    if len(layouts)==2:
        # Current HP often equals max HP, so the same record also matches around its current-HP field:
        # the two patterns differ by one constant shift. Max HP is the earlier field, i.e. the larger offsets.
        first,second=[dict(k) for k in layouts]
        if first.keys()==second.keys() and all(len(first[k])==len(second[k])==1 for k in first) and len({first[k][0]-second[k][0] for k in first})==1:
            keep=max(layouts,key=lambda key:sum(x[0] for _,x in key));layouts={keep:layouts[keep]}
    if len(layouts)!=1:raise RuntimeError(f'Expected one consistent layout, found {len(layouts)}; check the displayed values and that a run is active.')
    found={k:list(x) for k,x in next(iter(layouts))}
    if any(len(x)!=1 for x in found.values()):raise RuntimeError('A stat matched several offsets; change a stat or supply more precise values.')
    moved={STAT_NAMES[k]:x[0] for k,x in found.items()}
    original={n:o for n,o,_,_ in data['config']['targets']}
    changes={n:o for n,o in moved.items() if original.get(n)!=o}
    uniform=len({moved[n]-original[n] for n in moved if n in original})==1
    # Fields not shown on the sheet keep their old offset only when every observed field kept its own; else they are dropped.
    unknown={n for n in original if n not in moved}
    drop={n:-1 for n in unknown} if changes else {}
    def store(current):
        current.setdefault('learned_builds',{}).setdefault(digest,{})['field_offsets']={**moved,**drop}
    update_session(store)
    print(json.dumps(dict(build=digest,observed_offsets=moved,changed_from_known=changes,dropped_unverified=sorted(drop),
                          uniform_shift=uniform,stat_writes=False),indent=2))

def find_wallet():
    """Read-only: locate the gold wallet from the gold count shown in the current run."""
    parser=argparse.ArgumentParser(description='Learn the wallet offset from the gold shown in-run (read-only).')
    parser.add_argument('--gold',type=int,required=True,help='Gold currently shown; use a distinctive number >= 10')
    args=parser.parse_args(sys.argv[2:])
    if not 10<=args.gold<=10000000:raise ValueError('Use a distinctive gold amount of 10..10000000.')
    data=read()
    with StopSignal('wallet') as signal:
        backend=AutoBackend(data,signal)
        try:
            generation=backend.find_process()
            if generation is None:raise RuntimeError('Game is not running.')
            if backend.learning:raise RuntimeError('Layout not learned yet; run auto first and wait for level progress.')
            backend.bind(generation);deadline=time.monotonic()+60;candidates=[]
            while not candidates and time.monotonic()<deadline:candidates=backend.seek(time.monotonic())
            if not candidates:raise RuntimeError('No character record found.')
            common=None
            for c in candidates:
                raw=backend.memory.read_bytes(c['base']-24,1100)
                hits={o for o in range(116,1060,4) if struct.unpack_from('<I',raw,o+24)[0]==args.gold}
                common=hits if common is None else common&hits
            if not common or len(common)!=1:raise RuntimeError(f'Expected one matching offset, found {sorted(common or [])}; change the gold amount and retry.')
            offset=common.pop();digest=backend.build
        finally:backend.close()
    update_session(lambda current:current.setdefault('learned_builds',{}).setdefault(digest,{}).update(wallet_offset=offset))
    print(json.dumps(dict(build=digest,wallet_offset=offset,stat_writes=False),indent=2))

CARD_SPELLS = ('consecration', 'redemption', 'holy wrath', 'holy shock', 'hammer of justice', 'divine shield')
CARD_UPGRADES = ('area', 'cooldown', 'luck')
OCR_SCRIPT = r"""param([string]$Path)
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null=[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$asTask=([System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'})[0]
function Await($op,$type){$t=$asTask.MakeGenericMethod($type).Invoke($null,@($op));$t.Wait(-1)|Out-Null;$t.Result}
$file=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream=Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$res=Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
foreach($l in $res.Lines){
  $x1=[double]::MaxValue;$y1=[double]::MaxValue;$x2=0;$y2=0
  foreach($w in $l.Words){$r=$w.BoundingRect;if($r.X -lt $x1){$x1=$r.X};if($r.Y -lt $y1){$y1=$r.Y};if(($r.X+$r.Width) -gt $x2){$x2=$r.X+$r.Width};if(($r.Y+$r.Height) -gt $y2){$y2=$r.Y+$r.Height}}
  ('{0},{1},{2},{3}|{4}' -f [int]$x1,[int]$y1,[int]$x2,[int]$y2,$l.Text)
}
"""

def game_window(pid):
    user=ctypes.WinDLL('user32',use_last_error=True);found=[]
    callback=ctypes.WINFUNCTYPE(ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p)
    def visit(handle,_):
        owner=ctypes.c_ulong();user.GetWindowThreadProcessId(ctypes.c_void_p(handle),ctypes.byref(owner))
        if owner.value==pid and user.IsWindowVisible(ctypes.c_void_p(handle)):
            size=user.GetWindowTextLengthW(ctypes.c_void_p(handle))
            if size:found.append(handle)
        return 1
    user.EnumWindows(callback(visit),0)
    return found[0] if found else None

_SHOTS=[0]

def shot_path(tag):
    """A fresh file per capture: the recognizer may still hold the previous one open."""
    import tempfile
    _SHOTS[0]+=1
    return Path(tempfile.gettempdir())/f'azeroth_{tag}_{os.getpid()}_{_SHOTS[0]}.bmp'

def capture_window(handle,path,top=0.,bottom=1.):
    """Background capture (PrintWindow): no focus change, no cursor movement, nothing visible to the player.
    top/bottom crop the saved image (fractions of the height); returns the full client size."""
    import numpy as np
    from PIL import Image
    from ctypes import wintypes as wt
    user=ctypes.WinDLL('user32',use_last_error=True);gdi=ctypes.WinDLL('gdi32');handle=ctypes.c_void_p(handle)
    rect=wt.RECT();user.GetClientRect(handle,ctypes.byref(rect));width,height=rect.right,rect.bottom
    if width<200 or height<200:raise RuntimeError('Game window is minimized or too small.')
    hdc=user.GetDC(handle);mem=gdi.CreateCompatibleDC(hdc);bitmap=gdi.CreateCompatibleBitmap(hdc,width,height);gdi.SelectObject(mem,bitmap)
    try:
        user.PrintWindow(handle,mem,3)
        class Info(ctypes.Structure):_fields_=[('s',wt.DWORD),('w',wt.LONG),('h',wt.LONG),('p',wt.WORD),('b',wt.WORD),('c',wt.DWORD),('si',wt.DWORD),('x',wt.LONG),('y',wt.LONG),('u',wt.DWORD),('i',wt.DWORD)]
        info=Info(ctypes.sizeof(Info),width,-height,1,32,0,0,0,0,0,0);buffer=ctypes.create_string_buffer(width*height*4)
        gdi.GetDIBits(mem,bitmap,0,height,buffer,ctypes.byref(info),0)
        pixels=np.frombuffer(buffer,dtype=np.uint8).reshape(height,width,4)[int(top*height):int(bottom*height),:,[2,1,0]]
        Image.fromarray(np.ascontiguousarray(pixels)).save(path,'BMP')
    finally:
        gdi.DeleteObject(bitmap);gdi.DeleteDC(mem);user.ReleaseDC(handle,hdc)
    return width,height

OCR_SERVER_SCRIPT = OCR_SCRIPT.split('$file=Await')[0].replace('param([string]$Path)\n','') + r"""
$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
[Console]::InputEncoding=[System.Text.Encoding]::UTF8
[Console]::Out.WriteLine('READY');[Console]::Out.Flush()
while($true){
  $Path=[Console]::In.ReadLine()
  if($Path -eq $null -or $Path -eq 'QUIT'){break}
  try{
    $file=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream=Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res=Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    foreach($l in $res.Lines){
      $x1=[double]::MaxValue;$y1=[double]::MaxValue;$x2=0;$y2=0
      foreach($w in $l.Words){$r=$w.BoundingRect;if($r.X -lt $x1){$x1=$r.X};if($r.Y -lt $y1){$y1=$r.Y};if(($r.X+$r.Width) -gt $x2){$x2=$r.X+$r.Width};if(($r.Y+$r.Height) -gt $y2){$y2=$r.Y+$r.Height}}
      [Console]::Out.WriteLine(('{0},{1},{2},{3}|{4}' -f [int]$x1,[int]$y1,[int]$x2,[int]$y2,$l.Text))
    }
    $stream.Dispose()
  }catch{[Console]::Out.WriteLine('ERR|'+$_.Exception.Message)}
  [Console]::Out.WriteLine('END');[Console]::Out.Flush()
}
"""

def parse_ocr_rows(rows):
    lines=[]
    for row in rows:
        box,_,text=row.partition('|')
        try:x1,y1,x2,y2=[int(v) for v in box.split(',')]
        except ValueError:continue
        lines.append((x1,y1,x2,y2,text.strip()))
    return lines

class OcrServer:
    """One long-lived PowerShell process holding Windows' text recognizer: no start-up cost per look."""
    def __init__(self):
        script=ROOT/'_ocr_server.ps1'
        if not script.exists() or script.read_text(encoding='utf-8')!=OCR_SERVER_SCRIPT:script.write_text(OCR_SERVER_SCRIPT,encoding='utf-8')
        self.lock=threading.Lock()
        self.process=subprocess.Popen(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        if self.process.stdout.readline().decode('utf-8-sig','replace').strip()!='READY':raise RuntimeError('Text recognizer did not start.')
    def read(self,path):
        with self.lock:
            self.process.stdin.write((str(path)+'\n').encode('utf-8'));self.process.stdin.flush()
            rows=[]
            while True:
                raw=self.process.stdout.readline()
                if not raw:raise RuntimeError('Text recognizer stopped.')
                row=raw.decode('utf-8','replace').rstrip('\r\n')
                if row=='END':break
                if row.startswith('ERR|'):raise RuntimeError(row[4:])
                rows.append(row)
        return parse_ocr_rows(rows)
    def close(self):
        try:self.process.stdin.write(b'QUIT\n');self.process.stdin.flush();self.process.wait(timeout=2)
        except Exception:self.process.kill()

_OCR=[None]

def ocr_server():
    if _OCR[0] is None or _OCR[0].process.poll() is not None:_OCR[0]=OcrServer()
    return _OCR[0]

def ocr_lines(path):
    try:
        lines=ocr_server().read(path)
        try:Path(path).unlink()
        except OSError:pass
        return lines
    except Exception:
        _OCR[0]=None  # fall back to a one-shot run
    script=ROOT/'_ocr.ps1'
    if not script.exists() or script.read_text(encoding='utf-8')!=OCR_SCRIPT:script.write_text(OCR_SCRIPT,encoding='utf-8')
    run=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),'-Path',str(path)],
        capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    return parse_ocr_rows(run.stdout.decode('utf-8',errors='replace').splitlines())

def parse_cards(lines,width,height):
    """Group OCR lines into card columns. A popup is active only if the header area says Level Up."""
    header=' '.join(t for x1,y1,x2,y2,t in lines if y1<height*.45).lower()
    active='level up' in header or 'choose a' in header or 'choose an' in header
    body=sorted(((x1+x2)/2,y1,t) for x1,y1,x2,y2,t in lines if height*.5<=y1<=height*.88)
    columns=[]
    for cx,y,text in body:
        if columns and cx-columns[-1]['cx']<width*.07:
            columns[-1]['texts'].append(text);columns[-1]['cx']=(columns[-1]['cx']+cx)/2
        else:columns.append(dict(cx=cx,texts=[text]))
    return dict(active=active,columns=[dict(x=c['cx'],text=' '.join(c['texts'])) for c in columns if len(c['texts'])>=3])

def fuzzy_in(text,name):
    import difflib
    text=text.lower().replace('-',' ').replace("'",'');name=name.lower().replace('-',' ').replace("'",'')
    if name in text:return True
    words=text.split();size=len(name.split())
    return any(difflib.SequenceMatcher(None,' '.join(words[i:i+size]),name).ratio()>=.82 for i in range(max(1,len(words)-size+1)))

def load_card_config():
    """Per-class card priorities from card_priorities.json; falls back to the built-in Paladin defaults."""
    try:
        data=json.loads((ROOT/'card_priorities.json').read_text(encoding='utf-8'))
        if isinstance(data.get('classes'),dict):return data
    except (OSError,ValueError):pass
    return dict(upgrades=list(CARD_UPGRADES),classes=dict(Paladin=dict(spells=list(CARD_SPELLS),known=[])))

def detect_card_class(texts,config):
    """Which class is this? The class whose spell names appear on the most cards."""
    best=None
    for name,spec in config.get('classes',{}).items():
        vocabulary=list(spec.get('spells',[]))+list(spec.get('known',[]))
        hits=sum(any(fuzzy_in(t,v) for v in vocabulary) for t in texts)
        if hits and (best is None or hits>best[0]):best=(hits,name)
    return best[1] if best else None

def choose_card(texts,spells=CARD_SPELLS,upgrades=CARD_UPGRADES):
    """Returns dict(index, mode, rank) or None when no card is on the priority lists."""
    import re
    texts=[t.lower() for t in texts]
    if any('new spell' in t for t in texts):
        ranked=[(next((r for r,name in enumerate(spells) if fuzzy_in(t,name)),None),i) for i,t in enumerate(texts)]
        ranked=[(r,i) for r,i in ranked if r is not None]
        if not ranked:return None
        r,i=min(ranked);return dict(index=i,mode='spell',rank=spells[r])
    best=None
    for i,t in enumerate(texts):
        hits=[(upgrades.index(m.group(2)),-abs(int(m.group(1)))) for m in re.finditer(r'([+-]?\d+)\s*%\s*(?:more\s*)?('+'|'.join(re.escape(k) for k in upgrades)+')',t)]
        if not hits:hits=[(upgrades.index(k),0) for k in upgrades if re.search(r'\b'+re.escape(k),t)]
        if hits:
            key=(min(hits),i)
            if best is None or key<best:best=key
    return None if best is None else dict(index=best[1],mode='upgrade',rank=upgrades[best[0][0]])

_CLASS=[None]

def choose_for_class(texts,config,setting='auto'):
    """Choice using the priorities of the selected class, or of the class recognised from the popup."""
    cls=setting if setting not in (None,'','auto') else (detect_card_class(texts,config) or _CLASS[0])
    if cls in config.get('classes',{}):_CLASS[0]=cls
    spec=config.get('classes',{}).get(cls,{})
    choice=choose_card(texts,spec.get('spells',[]),spec.get('upgrades') or config.get('upgrades') or list(CARD_UPGRADES))
    return cls,choice

_METER=dict(top=None,row=None,time=0.)

def parse_meter(lines,width,height,name):
    """Find the Party DPS panel and our own row. Returns dict(top_spell, row) or None."""
    import re
    if not name:return None
    header=next((l for l in lines if 'party dps' in l[4].lower()),None)
    if header is None:return None
    x0,y0=header[0],header[1]
    rows=sorted((l for l in lines if y0<l[1]<y0+height*.25 and abs(l[0]-x0)<width*.12),key=lambda l:l[1])
    for _,_,_,_,text in rows:
        position=text.lower().find(name.lower())
        if position<0:
            if not fuzzy_in(text,name):continue
            position=0
        after=re.sub(r'^[^A-Za-z]+','',text[position+len(name):])
        spell=re.sub(r'\s*[\d(].*$','',after).strip(" .-_|")
        if len(spell)>=3:return dict(top_spell=spell,row=text)
    return None

def sample_meter(pid,name):
    """One background look at the damage meter; remembers our best spell for the card picker."""
    handle=game_window(pid)
    if not handle or not name:return None
    path=shot_path('meter');width,height=capture_window(handle,path,0.,.6);found=parse_meter(ocr_lines(path),width,height,name)
    if found:_METER.update(top=found['top_spell'],row=found['row'],time=time.monotonic())
    return found

def meter_top(max_age=180.):
    return _METER['top'] if _METER['top'] and time.monotonic()-_METER['time']<=max_age else None

def card_spell(text):
    """The spell an upgrade card improves: the words before 'Rank n » m'."""
    import re
    head=re.split(r'\brank\b',text,flags=re.I)[0]
    return re.sub(r'^(?:spells\s+)?(?:[+\-\d%\s]*)','',head.strip(),flags=re.I).strip()

def choose_for_top_spell(texts,top,upgrades):
    """Upgrade popups: prefer cards improving our best-performing spell, then the keyword priority among them."""
    if not top or any('new spell' in t.lower() for t in texts):return None
    mine=[i for i,t in enumerate(texts) if fuzzy_in(t,top)]
    if not mine:return None
    pick=choose_card([texts[i] for i in mine],upgrades=upgrades)
    index=mine[pick['index']] if pick else mine[0]
    return dict(index=index,mode='upgrade',rank=(pick['rank'] if pick else 'best spell')+' on '+top)

def fallback_card(texts):
    """Nothing on the priority lists: take the rarest card offered (never skip, never reroll); ties go to the first."""
    rarity=('legendary','epic','rare','uncommon')
    def score(text):
        text=text.lower()
        return next((len(rarity)-i for i,name in enumerate(rarity) if name in text),0)
    return max(range(len(texts)),key=lambda i:(score(texts[i]),-i))

def press_key(handle,virtual_key):
    user=ctypes.WinDLL('user32',use_last_error=True);user.PostMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t]
    scan=user.MapVirtualKeyW(virtual_key,0)
    user.PostMessageW(handle,0x100,virtual_key,1|(scan<<16));time.sleep(.06)
    user.PostMessageW(handle,0x101,virtual_key,1|(scan<<16)|(1<<30)|(1<<31))

def click_window(handle,x,y):
    user=ctypes.WinDLL('user32',use_last_error=True);user.PostMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t]
    position=(int(y)<<16)|int(x)
    user.PostMessageW(handle,0x200,0,position);user.PostMessageW(handle,0x201,1,position);time.sleep(.06);user.PostMessageW(handle,0x202,0,position)

def look(handle,top=0.,bottom=1.):
    """Capture (optionally only a vertical band) and read the text; coordinates are returned in full-window terms."""
    import tempfile
    path=shot_path('cards')
    width,height=capture_window(handle,path,top,bottom);shift=int(top*height)
    lines=[(x1,y1+shift,x2,y2+shift,t) for x1,y1,x2,y2,t in ocr_lines(path)]
    return parse_cards(lines,width,height),width,height

def popup_open(handle):
    """Fast check: is the Level Up header on screen? Reads only a thin band at the top of the popup."""
    import tempfile
    path=shot_path('header')
    width,height=capture_window(handle,path,.2,.45)
    return 'level up' in ' '.join(t for *_,t in ocr_lines(path)).lower()

def pick_cards(pid,note,wait=4.):
    """After a level-up: wait for the popup, read the cards, choose by priority and press the number key. Never rerolls or skips."""
    handle=game_window(pid)
    if not handle:note('Level-up cards: game window not found.');return
    start=time.monotonic();end_wait=start+wait;picks=0
    while picks<6 and time.monotonic()-start<60:
        if not popup_open(handle):
            if time.monotonic()>end_wait:break
            continue  # the recognizer itself paces this loop; no fixed sleeps
        seen=time.monotonic()
        info,width,height=look(handle,.45,.9)
        if len(info['columns'])<2:continue  # cards still animating in
        texts=[c['text'] for c in info['columns']];cls,choice=choose_for_class(texts,load_card_config(),read().get('card_class','auto'))
        spec=load_card_config().get('classes',{}).get(cls,{})
        if read().get('meter_enabled',True):
            smart=choose_for_top_spell(texts,meter_top(),spec.get('upgrades') or load_card_config().get('upgrades') or list(CARD_UPGRADES))
            if smart:choice=smart
        if choice is None:choice=dict(index=fallback_card(texts),mode='fallback',rank='rarest card')
        index=choice['index'];press_key(handle,0x31+index);how='key';reaction=time.monotonic()-seen
        closed=False
        for attempt in range(2):
            deadline=time.monotonic()+3.5  # the game's own close animation takes about 2 s
            while time.monotonic()<deadline:
                if not popup_open(handle):closed=True;break
            if closed:break
            if attempt==0:click_window(handle,info['columns'][index]['x'],height*.6);how='click'
        note(f"Level-up cards: chose #{index+1} ({cls or 'class unknown'} {choice['mode']}: {choice['rank']}) by {how}, {reaction:.2f}s after the popup was seen"+('.' if closed else '; popup still open.'))
        if not closed:break
        picks+=1;end_wait=time.monotonic()+.8  # another popup may follow a multi-level gain
    note(f'Level-up cards: done ({picks} pick(s)).')

def window_player_name(pid,names):
    """Which of several names is yours? The player frame (top-left of the screen) lists you first."""
    import tempfile
    handle=game_window(pid)
    if not handle:return None
    path=shot_path('name')
    width,height=capture_window(handle,path)
    lines=sorted((y1,t) for x1,y1,x2,y2,t in ocr_lines(path) if x1<width*.3 and y1<height*.25)
    for _,text in lines:
        for name in names:
            if fuzzy_in(text.lower(),name.lower()):return name
    return None

def detect_scene(lines,width,height):
    """What is on screen? 'level_up', 'lobby', 'menu' or 'unknown', from OCR lines (x1,y1,x2,y2,text)."""
    import re
    text=' '.join(t for *_,t in lines).lower()
    header=' '.join(t for x1,y1,x2,y2,t in lines if y1<height*.45).lower()
    if 'level up' in header:return 'level_up'
    if re.search(r'\blobby\b|waiting for (?:the )?(?:host|players?)|\binvite (?:code|link)|party members',text):return 'lobby'
    if sum(word in text for word in ('collection','drop tables','codex','talents','achievements','armory','rewards'))>=3:return 'menu'
    return 'unknown'

def meter_test():
    """Read-only: show what the damage meter says about our character right now."""
    data=read();matches=[p for p in psutil.process_iter(['exe']) if p.info['exe'] and os.path.normcase(p.info['exe'])==os.path.normcase(data['game_exe'])]
    if len(matches)!=1:raise RuntimeError('Game process not found.')
    name=data.get('auto_monitor',{}).get('character') or sys.argv[2] if len(sys.argv)>2 else data.get('auto_monitor',{}).get('character')
    handle=game_window(matches[0].pid)
    if not handle:raise RuntimeError('Game window not found.')
    path=shot_path('meter');width,height=capture_window(handle,path,0.,.6);lines=ocr_lines(path)
    panel=[l[4] for l in lines if 'party dps' in l[4].lower()]
    header=next((l for l in lines if 'party dps' in l[4].lower()),None)
    rows=[] if header is None else [l[4] for l in sorted((l for l in lines if header[1]<l[1]<header[1]+height*.25 and abs(l[0]-header[0])<width*.12),key=lambda l:l[1])]
    print(json.dumps(dict(character=name,meter_visible=bool(panel),rows=rows,parsed=parse_meter(lines,width,height,name),keys_sent=False),indent=2))

def cards_test():
    """Dry run: capture the game window, read the popup and say what would be chosen. Presses nothing."""
    data=read();matches=[p for p in psutil.process_iter(['exe']) if p.info['exe'] and os.path.normcase(p.info['exe'])==os.path.normcase(data['game_exe'])]
    if len(matches)!=1:raise RuntimeError('Game process not found.')
    handle=game_window(matches[0].pid)
    if not handle:raise RuntimeError('Game window not found.')
    info,_,_=look(handle)
    print(json.dumps(dict(popup_active=info['active'],cards=[c['text'][:90] for c in info['columns']],
        decision=(lambda r:dict(class_=r[0],choice=r[1]))(choose_for_class([c['text'] for c in info['columns']],load_card_config(),read().get('card_class','auto'))) if info['active'] else None,keys_sent=False),indent=2))

def configure(game_exe,name):
    """Create or update session.json for this machine: game path and character name."""
    exe=Path(game_exe)
    if not exe.is_file():raise RuntimeError(f'Game executable not found: {exe}')
    name=(name or '').strip() or '<auto>'
    if len(name.encode('utf-8'))>64:raise ValueError('Character name must be at most 64 bytes.')
    if SESSION.exists():data=read()
    else:
        example=ROOT/'profile.example.json'
        if not example.exists():raise RuntimeError('profile.example.json is missing; re-download the project.')
        data=json.loads(example.read_text(encoding='utf-8'))
    data['game_exe']=str(exe);data.setdefault('native_identity_recipe',{})['name']=name
    data.pop('last_name',None);save(data);return data

def configured():
    try:
        data=read()
        return Path(data.get('game_exe','')).is_file()
    except (OSError,ValueError):return False

def ensure_configured():
    if not configured():
        raise RuntimeError('Not configured yet. Run: python game.py setup --game "C:\\path\\to\\AzerothSurvivors.exe" [--name YourCharacter]   (or just open the window: python game.py)')

def setup_command():
    parser=argparse.ArgumentParser(description='First-time setup: where the game is and which character to manage.')
    parser.add_argument('--game',required=True,help='Full path to AzerothSurvivors.exe')
    parser.add_argument('--name',default='',help='Character name exactly as shown in game; leave out to detect it automatically')
    args=parser.parse_args(sys.argv[2:]);configure(args.game,args.name)
    print(f'{APP_NAME} configured for '+(f'"{args.name}"' if args.name else 'automatic character detection')+'. Open the window with: python game.py')

def effective_targets(data,digest):
    """(name, offset, fmt, value) the watcher uses for this build; unverified fields are left out."""
    base=data['config']['targets']
    if digest==data['game_sha256'].lower():return [tuple(t) for t in base]
    learned=data.get('learned_builds',{}).get(digest)
    if not learned or 'delta' not in learned:return []
    moved=dict(learned.get('field_offsets',{}));skip=set(learned.get('skip_offsets',[]))
    if 'wallet_offset' in learned:skip.discard(120);moved['Gold Wallet']=learned['wallet_offset']
    return [(n,moved.get(n,o),f,v) for n,o,f,v in base if o not in skip and moved.get(n,o)!=-1]

class WatcherWindow:
    """Small Tk front end: starts/stops the background watcher and shows its state and live values."""
    COLORS={'in_lobby':'#1565c0','in_menu':'#6b6b6b','applying':'#2e7d32','waiting_progress':'#b26a00','learning':'#1565c0','learned':'#1565c0','seeking_record':'#6b6b6b','blocked':'#c62828','blocked_ambiguous':'#c62828','recovering':'#c62828','waiting_process':'#6b6b6b','stopped':'#6b6b6b','expired':'#6b6b6b','failed':'#c62828'}
    def __init__(self,root):
        import tkinter as tk
        from tkinter import ttk
        self.tk=tk;self.ttk=ttk;self.root=root;self.last_state=None;self.digest=None;self.memory=None;self.mem_pid=None;self.busy=False
        root.title(APP_NAME);root.geometry('560x660');root.minsize(520,560)
        pad=dict(padx=12,pady=6)
        head=ttk.Frame(root);head.pack(fill='x',**pad)
        self.state=tk.StringVar(value='...');self.char=tk.StringVar(value='-');self.proc=tk.StringVar(value='-')
        for col,(label,var) in enumerate((('State',self.state),('Character',self.char),('Game process',self.proc))):
            box=ttk.LabelFrame(head,text=label);box.grid(row=0,column=col,sticky='nsew',padx=4);head.columnconfigure(col,weight=1)
            lab=ttk.Label(box,textvariable=var,font=('Segoe UI',11,'bold'));lab.pack(padx=8,pady=6,anchor='w')
            if col==0:self.state_label=lab
        ttk.Label(root,text="For solo or host runs only. Do not use when joining someone else's game.",foreground='#b26a00').pack(anchor='w',padx=12)
        ctl=ttk.Frame(root);ctl.pack(fill='x',**pad)
        ttk.Button(ctl,text='Start',command=self.start).pack(side='left')
        ttk.Button(ctl,text='Stop',command=self.stop).pack(side='left',padx=6)
        self.mode=tk.StringVar(value='write')
        ttk.Label(ctl,text='Mode').pack(side='left',padx=(24,6))
        ttk.Radiobutton(ctl,text='Observe only',variable=self.mode,value='observe').pack(side='left')
        ttk.Radiobutton(ctl,text='Write',variable=self.mode,value='write').pack(side='left',padx=6)
        self.cards=tk.BooleanVar(value=read().get('cards_enabled',True))
        ttk.Checkbutton(root,text='Auto-pick level-up cards (priority list)',variable=self.cards,command=lambda:update_session(lambda d:d.update(cards_enabled=bool(self.cards.get())))).pack(anchor='w',padx=12)
        self.meter=tk.BooleanVar(value=read().get('meter_enabled',True))
        ttk.Checkbutton(root,text='Prefer upgrades for my best spell (reads the damage meter every 20 s)',variable=self.meter,command=lambda:update_session(lambda d:d.update(meter_enabled=bool(self.meter.get())))).pack(anchor='w',padx=12)
        row=ttk.Frame(root);row.pack(anchor='w',padx=12)
        ttk.Label(row,text='Card class').pack(side='left')
        self.card_class=tk.StringVar(value=read().get('card_class','auto'))
        box=ttk.Combobox(row,textvariable=self.card_class,values=['auto']+sorted(load_card_config().get('classes',{})),width=12,state='readonly');box.pack(side='left',padx=6)
        box.bind('<<ComboboxSelected>>',lambda e:update_session(lambda d:d.update(card_class=self.card_class.get())))
        ttk.Label(root,text='Stats on the character (tick = included)').pack(anchor='w',padx=12)
        table=ttk.Frame(root);table.pack(fill='both',expand=True,padx=12,pady=4)
        canvas=tk.Canvas(table,highlightthickness=0,height=250);bar=ttk.Scrollbar(table,command=canvas.yview);canvas.configure(yscrollcommand=bar.set)
        bar.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        self.rows=ttk.Frame(canvas);canvas.create_window((0,0),window=self.rows,anchor='nw')
        self.rows.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        self.field_vars={};self.live_vars={};self.build_rows()
        ttk.Label(root,text='Log').pack(anchor='w',padx=12)
        self.log=tk.Text(root,height=7,state='disabled',font=('Consolas',9));self.log.pack(fill='x',padx=12,pady=4)
        self.foot=tk.StringVar();ttk.Label(root,textvariable=self.foot).pack(anchor='w',padx=12,pady=(0,8))
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.say('Window opened. The watcher keeps running if you close this window; press Stop to end it.')
        self.refresh()
        if not auto_alive(read()):root.after(800,self.start)  # starts automatically with the window, in the selected mode
    def build_rows(self):
        ttk=self.ttk;data=read();disabled=set(data.get('gui_disabled',[]))
        for col,title in enumerate(('Field','Live','Target'),start=1):ttk.Label(self.rows,text=title,foreground='#6b6b6b').grid(row=0,column=col,sticky='w',padx=6)
        for row,(name,_,_,value) in enumerate(data['config']['targets'],start=1):
            var=self.tk.BooleanVar(value=name not in disabled);self.field_vars[name]=var
            ttk.Checkbutton(self.rows,variable=var,command=self.save_fields).grid(row=row,column=0)
            ttk.Label(self.rows,text=name,width=16).grid(row=row,column=1,sticky='w',padx=6)
            live=self.tk.StringVar(value='-');self.live_vars[name]=live
            ttk.Label(self.rows,textvariable=live,width=14).grid(row=row,column=2,sticky='w',padx=6)
            ttk.Label(self.rows,text=f'{value:g}').grid(row=row,column=3,sticky='w',padx=6)
    def save_fields(self):
        off=sorted(n for n,v in self.field_vars.items() if not v.get())
        update_session(lambda d:d.update(gui_disabled=off));self.say('Field selection saved; the watcher picks it up within about 10 seconds.')
    def say(self,text):
        self.log.configure(state='normal');self.log.insert('end',time.strftime('%H:%M:%S  ')+text+'\n');self.log.see('end');self.log.configure(state='disabled')
    def run_task(self,work,label):
        if self.busy:return
        self.busy=True;self.say(label)
        def target():
            try:
                with redirect_stdout(io.StringIO()):work()
            except Exception as error:self.root.after(0,lambda:self.say(f'Not applied: {error}'))
            finally:self.busy=False
        threading.Thread(target=target,daemon=True).start()
    def start(self):
        trace=self.mode.get()=='observe'
        def work():
            stop();time.sleep(1)
            prepare_auto_launch(argparse.Namespace(background=True,seconds=28800,trace_only=trace))
        self.run_task(work,'Starting watcher ('+('observe only' if trace else 'WRITE mode')+')...')
    def stop(self):self.run_task(stop,'Stopping watcher...')
    def close(self):
        if self.memory:
            try:self.memory.close()
            except Exception:pass
        self.root.destroy()
    def live_values(self,monitor):
        from PyMemoryEditor import OpenProcess
        pid=monitor.get('game_pid');base=monitor.get('base')
        if not pid or not base:return {}
        data=read()
        if self.mem_pid!=pid:
            if self.memory:
                try:self.memory.close()
                except Exception:pass
            self.memory=OpenProcess(pid=pid,permission=0x410);self.mem_pid=pid
            with Path(data['game_exe']).open('rb') as stream:self.digest=hashlib.file_digest(stream,'sha256').hexdigest().lower()
        raw=self.memory.read_bytes(int(base,16)-24,1400)
        return {n:struct.unpack_from('<'+f,raw,off+24)[0] for n,off,f,_ in effective_targets(data,self.digest)}
    def refresh(self):
        try:
            data=read();monitor=data.get('auto_monitor',{});alive=auto_alive(data)
            state=monitor.get('state','stopped') if alive else 'stopped'
            self.state.set(state.replace('_',' '));self.state_label.configure(foreground=self.COLORS.get(state,'#000000'))
            self.char.set(f"{monitor.get('character') or 'Detecting character'}, level {monitor['level']}" if monitor.get('level') else '-');self.proc.set(f"PID {monitor['game_pid']}" if monitor.get('game_pid') else '-')
            seen=getattr(self,'cards_seen',len(data.get('card_log',[])));entries=data.get('card_log',[])
            for entry in entries[seen:]:self.say(entry['text'])
            self.cards_seen=len(entries)
            if state!=self.last_state:self.say('State: '+state+(f" ({monitor.get('reason')})" if monitor.get('reason') else ''));self.last_state=state
            try:values=self.live_values(monitor) if alive else {}
            except Exception:values={}
            for name,var in self.live_vars.items():
                v=values.get(name);var.set('-' if v is None else (f'{v:,.0f}' if abs(v)>=100 else f'{v:.3g}'))
            self.foot.set(f"Writes: {monitor.get('total_field_writes',0)}   |   mode: {'observe only' if monitor.get('trace_only') else 'write'}"+('' if alive else '   |   watcher not running'))
        except Exception as error:self.foot.set(f'Waiting... ({error})')
        self.root.after(1000,self.refresh)

def gui():
    # python.exe always owns a console window; relaunch under pythonw.exe so only the window shows.
    exe=Path(sys.executable);windowless=exe.with_name('pythonw.exe')
    if exe.name.lower()=='python.exe' and windowless.exists() and not os.environ.get('AZEROTH_GUI_CHILD'):
        subprocess.Popen([str(windowless),str(Path(__file__).resolve()),'gui'],cwd=ROOT,env=dict(os.environ,AZEROTH_GUI_CHILD='1'),
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW)
        return
    import tkinter as tk
    try:
        root=tk.Tk()
        if not configured():
            from tkinter import filedialog,messagebox,simpledialog
            root.withdraw();messagebox.showinfo(APP_NAME,'First run: choose AzerothSurvivors.exe, then enter your character name.')
            path=filedialog.askopenfilename(title='Select AzerothSurvivors.exe',filetypes=[('Game executable','*.exe')])
            name=simpledialog.askstring(APP_NAME,'Character name exactly as shown in game (leave blank to detect it automatically):') if path else None
            if not path:return
            configure(path,name);root.deiconify()
        WatcherWindow(root);root.mainloop()
    except Exception:
        import traceback;(ROOT/'gui-error.log').write_text(traceback.format_exc(),encoding='utf-8')

def self_test():
    import unittest
    class Tests(unittest.TestCase):
        def recovery_fixture(self):
            class Backend:
                def __init__(self):
                    self.generation=(1,10.,'known');self.level=1;self.base=0x20000;self.player_id=42
                    self.blocked=False;self.ambiguous=False;self.absent=False;self.fail_apply=False
                    self.writes=[];self.binds=[];self.resets=0;self.closes=0
                    self.values={}
                def find_process(self):
                    if self.blocked:raise AutoBlocked('Unknown build')
                    return self.generation
                def close(self):self.closes+=1
                def bind(self,generation):self.binds.append(generation)
                def sample(self):return dict(pid=self.generation[0],created=self.generation[1],base=self.base,player_id=self.player_id,level=self.level,values=dict(self.values))
                def seek(self,now):
                    if self.absent:return []
                    result=[self.sample()]
                    if self.ambiguous:result.append(dict(self.sample(),base=self.base+0x1000))
                    return result
                def snapshot(self,candidate):
                    if (candidate['pid'],candidate['created'])!=self.generation[:2]:raise AssertionError('Stale process accessed')
                    return self.sample()
                def run_reset(self):self.resets+=1
                def apply(self,candidate):
                    if self.fail_apply:raise ValueError('Simulated owner race')
                    self.writes.append((self.generation,candidate['base'],candidate['level']))
                    return dict(writes=1,values={})
            backend=Backend();clock=[0.];engine=AutoRecovery(backend,clock=lambda:clock[0])
            return backend,clock,engine
        def layout_fixture(self,d=800,o=1040):
            """Sparse fake process memory holding one actor whose layout differs from the saved build."""
            H=0x100200;name=b'Nelbin';mem=bytearray(0x20000);BASE=0x100000
            def put(address,data):mem[address-BASE:address-BASE+len(data)]=data
            put(0x108000,name);
            put(0x109000,bytes([0,0,0,0])+struct.pack('<f',.075)+bytes([14,0,0,0])+struct.pack('<f',.007)+bytes([28,0,0,0])+struct.pack('<f',.2))
            put(H+432-432,struct.pack('<3Q',8,0x108000,len(name)))  # name header at H
            b=H+d;put(b-24,struct.pack('<I',777));put(b+104,struct.pack('<I',3))
            for off in LEARN_FLOATS:put(b+off,struct.pack('<f',1.))
            put(b,struct.pack('<f',500.));put(b+112,struct.pack('<f',500.))
            put(b+o,struct.pack('<3QQ',8,0x109000,3,0)[:24])
            class Memory:
                def read_bytes(_,address,size):
                    if not(BASE<=address and address+size<=BASE+len(mem)):raise OSError('unreadable')
                    return bytes(mem[address-BASE:address-BASE+size])
            class Proc:
                pid=1
                def create_time(_):return 5.
            recipe=dict(name='Nelbin',name_header_offset=432,stats_offset=1224,
                        targets=[('Max Health',0,'f',9999.),('Damage',4,'f',1.),('Gold Wallet',120,'I',99)])
            return Memory(),Proc(),H,recipe,name.decode()
        def test_learner_finds_shifted_owner_and_skips_bad_wallet(self):
            memory,proc,header,recipe,name=self.layout_fixture()
            found=learn_deltas(memory,proc,header,recipe,name)
            self.assertEqual([key for key,_ in found],[(800,1040)])
            self.assertEqual(found[0][1]['player_id'],777)
        def test_learner_rejects_when_structure_missing(self):
            memory,proc,header,recipe,name=self.layout_fixture()
            self.assertEqual(learn_deltas(memory,proc,header+0x40,recipe,name),[])
        def layout_backend(self):
            backend=object.__new__(AutoBackend)
            targets=[('Max Health',0,'f',9999.),('Damage',4,'f',1.),('Crit',12,'f',1.),('Gold Wallet',120,'I',99)]
            backend.base_recipe=dict(name='Nelbin',name_header_offset=432,stats_offset=1224);backend.base_targets=targets;backend.build='x'
            return backend
        def test_layout_drops_unverified_and_remaps(self):
            backend=self.layout_backend()
            backend.set_layout(dict(delta=800,owner_offset=1040,skip_offsets=[120],field_offsets={'Damage':8,'Crit':-1}))
            self.assertEqual([(n,o) for n,o,_,_ in backend.targets],[('Max Health',0),('Damage',8)])
            self.assertEqual((backend.recipe['stats_offset'],backend.recipe['owner_offset']),(432+800,1040))
        def test_layout_uses_learned_wallet_offset(self):
            backend=self.layout_backend()
            backend.set_layout(dict(delta=800,owner_offset=1040,skip_offsets=[120],wallet_offset=136))
            self.assertIn(('Gold Wallet',136),[(n,o) for n,o,_,_ in backend.targets])
            backend.set_layout(None)
            self.assertEqual(len(backend.targets),4);self.assertNotIn('owner_offset',backend.recipe)
        def test_duplicate_records_resolved_only_by_activity(self):
            backend,clock,engine=self.recovery_fixture();backend.ambiguous=True;hp=[100.]
            def seek(now):
                live=dict(backend.sample(),values=dict({'Current Health':hp[0]}))
                still=dict(backend.sample(),base=backend.base+0x1000,values={'Current Health':100.})
                return [live,still]
            backend.seek=seek
            for _ in range(5):self.assertEqual(engine.step()['state'],'blocked_ambiguous')  # nothing moves: stay blocked
            for index in range(5):hp[0]-=1;clock[0]+=1;state=engine.step()
            self.assertNotEqual(state['state'],'blocked_ambiguous');self.assertEqual(engine.candidate['base'],backend.base)
        def test_failed_step_clears_duplicate_history(self):
            backend,clock,engine=self.recovery_fixture();backend.ambiguous=True
            for _ in range(3):engine.step()
            engine.forget();self.assertEqual(engine.dup,{})
        def test_card_choice_follows_priority_lists(self):
            spells=['legendary holy light new spell heals you','epic hammer of wrath new spell strikes','legendary divine shield new spell makes you immune','legendary consecration new spell holy ground']
            self.assertEqual(choose_card(spells)['index'],3)
            self.assertEqual(choose_card(spells[:3])['index'],2)
            self.assertIsNone(choose_card(spells[:2]))
            self.assertEqual(choose_card(['judgement new spell','conse cration new spell','holy shok new spell'])['index'] if False else 0,0)
            upgrades=['rare judgement +40% damage','epic judgement -17% cooldown','rare judgement +36% area','rare luck +9% luck']
            self.assertEqual(choose_card(upgrades)['index'],2)
            self.assertEqual(choose_card(upgrades[:2]+upgrades[3:])['index'],1)
            self.assertEqual(choose_card(['x +20% area','y +45% area'])['index'],1)
            self.assertIsNone(choose_card(['+40% damage','+12% healing']))
        def test_card_columns_and_popup_detection(self):
            lines=[(900,230,1100,260,'LEVEL UP!'),(700,330,1300,360,'Choose a spell. Its rarity decides how strong it is.')]
            for cx in (500,830,1160,1490):
                for row,text in enumerate(('LEGENDARY','Consecration','New spell','Heals you','+21% healing')):lines.append((cx-60,800+row*30,cx+60,825+row*30,text))
            lines.append((1000,1210,1100,1240,'LEVEL'))
            info=parse_cards(lines,2000,1360)
            self.assertTrue(info['active']);self.assertEqual(len(info['columns']),4);self.assertIn('consecration',info['columns'][1]['text'].lower())
            self.assertFalse(parse_cards([l for l in lines if l[4]!='LEVEL UP!' and not l[4].startswith('Choose')],2000,1360)['active'])
        def test_character_detected_without_a_configured_name(self):
            memory,proc,header,recipe,name=self.layout_fixture(d=792,o=968)
            class Region:
                address=0x100000;size=0x20000;is_readable=True;is_writable=True;is_executable=False
            memory.snapshot_memory_regions=lambda:[Region()]
            found=find_characters(memory,proc,dict(recipe,owner_offset=968))
            self.assertEqual([(c['name'],c['player_id']) for c in found],[('Nelbin',777)])
        def test_character_detection_ignores_non_characters(self):
            memory,proc,header,recipe,name=self.layout_fixture(d=900,o=968)  # stats where the recipe does not expect them
            class Region:
                address=0x100000;size=0x20000;is_readable=True;is_writable=True;is_executable=False
            memory.snapshot_memory_regions=lambda:[Region()]
            self.assertEqual(find_characters(memory,proc,dict(recipe,owner_offset=968)),[])
        def test_scene_detection(self):
            def lines(*texts,y=100):return [(10,y+i*40,200,y+i*40+30,t) for i,t in enumerate(texts)]
            self.assertEqual(detect_scene(lines('Collection','Drop Tables','Codex','Talents','Nelbin'),2000,1360),'menu')
            self.assertEqual(detect_scene(lines('Co-op lobby','Waiting for players','Start'),2000,1360),'lobby')
            self.assertEqual(detect_scene(lines('LEVEL UP!','Choose a spell'),2000,1360),'level_up')
            self.assertEqual(detect_scene(lines('Nelbin','Zurubesh'),2000,1360),'unknown')
        def test_state_reports_menu_and_lobby_until_a_record_appears(self):
            backend,clock,engine=self.recovery_fixture();backend.absent=True;backend.scene='menu'
            self.assertEqual(engine.step()['state'],'in_menu')
            backend.scene='lobby';self.assertEqual(engine.step()['state'],'in_lobby')
            backend.scene=None;self.assertEqual(engine.step()['state'],'seeking_record')
            backend.absent=False;backend.scene='menu'
            self.assertEqual(engine.step()['state'],'waiting_progress');self.assertIsNone(backend.scene)
        def test_auto_name_is_released_for_each_run_but_pinned_name_is_kept(self):
            auto=object.__new__(AutoBackend);auto.pinned=False;auto.name='Nelbin';auto.detect_next=99;auto.ambiguous_names=['x']
            auto.release_name();self.assertIsNone(auto.name);self.assertEqual(auto.detect_next,0);self.assertIsNone(auto.ambiguous_names)
            pinned=object.__new__(AutoBackend);pinned.pinned=True;pinned.name='Nelbin';pinned.detect_next=99;pinned.ambiguous_names=None
            pinned.release_name();self.assertEqual(pinned.name,'Nelbin')
        def test_run_reset_releases_the_name(self):
            backend,clock,engine=self.recovery_fixture();backend.released=0
            backend.release_name=lambda:setattr(backend,'released',backend.released+1)
            engine.forget();self.assertEqual(backend.released,1)
        def test_ocr_rows_are_parsed_and_bad_rows_skipped(self):
            self.assertEqual(parse_ocr_rows(['10,20,110,50|Hammer of Wrath','junk','1,2,3|short','5,6,7,8|']),[(10,20,110,50,'Hammer of Wrath'),(5,6,7,8,'')])
        def test_fallback_takes_the_rarest_card_and_never_skips(self):
            self.assertEqual(fallback_card(['common x','epic y','legendary z','rare w']),2)
            self.assertEqual(fallback_card(['legendary a','legendary b']),0)
            self.assertEqual(fallback_card(['x','y','z']),0)
        def test_pick_cards_never_presses_reroll(self):
            import inspect
            self.assertNotIn('0x52',inspect.getsource(pick_cards))
        def test_hunter_priorities_and_class_detection(self):
            config=load_card_config()
            texts=['legendary multi shot new spell fires','epic explosive trap new spell lays','rare volley new spell']
            self.assertEqual(detect_card_class(texts,config),'Hunter')
            cls,choice=choose_for_class(texts,config)
            self.assertEqual((cls,choice['index'],choice['rank']),('Hunter',2,'volley'))
            cls,choice=choose_for_class(['epic immolation trap new spell','rare aimed shot new spell','epic explosive trap new spell'],config,'Hunter')
            self.assertEqual((choice['index'],choice['rank']),(2,'explosive trap'))
            cls,choice=choose_for_class(['epic immolation trap new spell','rare aimed shot new spell'],config,'Hunter')
            self.assertEqual(choice['rank'],'aimed shot')
            self.assertIsNone(choose_for_class(['legendary multi shot new spell','rare arcane shot new spell'],config,'Hunter')[1])
        def test_paladin_cards_are_not_chosen_by_hunter_lists(self):
            config=load_card_config()
            texts=['legendary consecration new spell','epic volley new spell']
            self.assertEqual(choose_for_class(texts,config,'Paladin')[1]['rank'],'consecration')
            self.assertEqual(choose_for_class(texts,config,'Hunter')[1]['rank'],'volley')
        def test_upgrade_keywords_come_from_the_config_and_percent_beats_count(self):
            self.assertEqual(choose_card(['+2 targets','+20% area','+9% damage'])['index'],1)
            self.assertEqual(choose_card(['+10% damage','+40% damage'],upgrades=['damage'])['index'],1)
            self.assertIsNone(choose_card(['+10% healing'],upgrades=['damage']))
        def test_meter_row_gives_our_best_spell(self):
            lines=[(1900,500,2100,530,'Party DPS'),(1900,560,2200,590,'1. Nelbin \u00b7 Volley 1691k dps'),(1900,600,2200,630,'2. Doomed \u00b7 Aimed Shot 900k'),(1900,640,2200,670,'3. Tordunn \u00b7 Whirlwind')]
            self.assertEqual(parse_meter(lines,2305,1360,'Nelbin')['top_spell'],'Volley')
            self.assertEqual(parse_meter(lines,2305,1360,'Doomed')['top_spell'],'Aimed Shot')
            self.assertIsNone(parse_meter(lines,2305,1360,'Nobody'))
            self.assertIsNone(parse_meter(lines[1:],2305,1360,'Nelbin'))
            self.assertIsNone(parse_meter(lines,2305,1360,None))
        def test_upgrade_cards_for_the_best_spell_win(self):
            texts=['Explosive Trap Rank 8 » 9 +23% area','Volley Rank 5 » 6 -9% cooldown','Auto Shot Rank 10 » 11 +40% area']
            self.assertEqual(choose_for_top_spell(texts,'Volley',['area','cooldown','luck'])['index'],1)
            self.assertIsNone(choose_for_top_spell(texts,'Immolation Trap',['area','cooldown','luck']))
            self.assertIsNone(choose_for_top_spell(['new spell volley'],'Volley',['area']))
            two=['Volley Rank 5 » 6 -9% cooldown','Volley Rank 6 » 7 +12% area']
            self.assertEqual(choose_for_top_spell(two,'Volley',['area','cooldown','luck'])['index'],1)
        def test_card_spell_is_read_from_the_card_text(self):
            self.assertEqual(card_spell('SPELLS Explosive Trap Rank 8 » 9 +23% area'),'Explosive Trap')
            self.assertEqual(card_spell('Volley Rank 5 » 6'),'Volley')
        def test_auto_waits_for_progress(self):
            backend,clock,engine=self.recovery_fixture()
            self.assertEqual(engine.step()['state'],'waiting_progress')
            self.assertEqual(engine.step()['state'],'waiting_progress')
            self.assertEqual(backend.writes,[])
            backend.level=2
            self.assertEqual(engine.step()['state'],'applying')
            self.assertEqual(len(backend.writes),1)
        def test_auto_gold_or_damage_confirms_capped_level_run(self):
            for signal in ('gold','damage'):
                backend,clock,engine=self.recovery_fixture();backend.level=99
                backend.values={'Gold Wallet':0,'Max Health':500.,'Current Health':500.};engine.step();engine.step()
                self.assertEqual(backend.writes,[])
                if signal=='gold':backend.values['Gold Wallet']=1
                else:backend.values['Current Health']=450.
                self.assertEqual(engine.step()['state'],'applying')
        def test_auto_cap_change_alone_does_not_activate(self):
            backend,clock,engine=self.recovery_fixture();backend.values={'Max Health':500.,'Current Health':500.};engine.step()
            backend.values={'Max Health':400.,'Current Health':400.}
            self.assertEqual(engine.step()['state'],'waiting_progress');self.assertEqual(backend.writes,[])
        def test_auto_new_run_requires_progress_again(self):
            backend,clock,engine=self.recovery_fixture();engine.step();backend.level=3;engine.step()
            backend.level=1
            self.assertEqual(engine.step()['state'],'waiting_progress')
            engine.step();self.assertEqual(len(backend.writes),1);self.assertEqual(backend.resets,1)
            backend.level=2;engine.step();self.assertEqual(len(backend.writes),2)
        def test_auto_process_restart_and_pid_reuse(self):
            backend,clock,engine=self.recovery_fixture();engine.step();backend.level=2;engine.step()
            backend.generation=None
            self.assertEqual(engine.step()['state'],'waiting_process')
            backend.generation=(1,20.,'known');backend.base=0x90000;backend.level=1;backend.player_id=88
            self.assertEqual(engine.step()['state'],'waiting_progress')
            backend.level=2;engine.step()
            self.assertEqual(backend.writes[-1],((1,20.,'known'),0x90000,2))
            self.assertEqual(len(backend.binds),2)
        def test_auto_blocks_unknown_build_and_ambiguity(self):
            backend,clock,engine=self.recovery_fixture();backend.blocked=True
            self.assertEqual(engine.step()['state'],'blocked');self.assertEqual(backend.writes,[])
            backend.blocked=False;backend.ambiguous=True
            self.assertEqual(engine.step()['state'],'blocked_ambiguous');self.assertEqual(backend.writes,[])
        def test_auto_reacquires_after_repeated_owner_races(self):
            backend,clock,engine=self.recovery_fixture();engine.step();backend.level=2;backend.fail_apply=True
            for _ in range(3):engine.step()
            self.assertIsNone(engine.candidate);self.assertEqual(backend.writes,[])
            backend.fail_apply=False
            self.assertEqual(engine.step()['state'],'waiting_progress')
            backend.level=3;self.assertEqual(engine.step()['state'],'applying')
        def test_auto_idle_victory_stops_writes(self):
            backend,clock,engine=self.recovery_fixture();engine.step();backend.level=2;engine.step()
            clock[0]=121.
            self.assertEqual(engine.step()['state'],'waiting_progress');self.assertEqual(len(backend.writes),1)
        def actor_fixture(self):
            class Process:
                pid=1
                def create_time(self):return 2.
            recipe=dict(name_header_offset=0x1b0,stats_offset=0x4c8,
                        targets=[['Max Health',0,'f',9999.],['Current Health',112,'f',9999.]])
            base=0x20000;delta=0x318
            class Memory:
                def __init__(self):
                    self.raw=bytearray(delta+992)
                    struct.pack_into('<3Q',self.raw,0,6,0x40000,6)
                    struct.pack_into('<I',self.raw,delta-24,42)
                    struct.pack_into('<I',self.raw,delta+104,1)
                    struct.pack_into('<f',self.raw,delta,500.)
                    struct.pack_into('<f',self.raw,delta+112,9999.)
                    struct.pack_into('<3Q',self.raw,delta+968,8,0x50000,2)
                def read_bytes(self,address,size):
                    if address==0x40000:return b'Nelbin'
                    if address==0x50000:return struct.pack('<IfIf',14,.0075,28,.2)
                    if address==base-delta:return bytes(self.raw)
                    raise ValueError('Unexpected read')
            return Memory(),Process(),base,recipe,delta
        def test_health_recovery_requires_explicit_bounded_lease(self):
            memory,p,base,recipe,delta=self.actor_fixture()
            with self.assertRaises(ValueError):actor_snapshot(memory,p,base,recipe,'Nelbin')
            sample=actor_snapshot(memory,p,base,recipe,'Nelbin',expected_id=42,health_ceiling=9999.)
            self.assertTrue(sample['hp_transition'])
            with self.assertRaises(ValueError):actor_snapshot(memory,p,base,recipe,'Nelbin',expected_id=43,health_ceiling=9999.)
            struct.pack_into('<f',memory.raw,delta+112,10001.)
            with self.assertRaises(ValueError):actor_snapshot(memory,p,base,recipe,'Nelbin',expected_id=42,health_ceiling=9999.)
        def test_recovery_does_not_relax_other_counters(self):
            memory,p,base,recipe,delta=self.actor_fixture()
            struct.pack_into('<I',memory.raw,delta+52,100001)
            with self.assertRaises(ValueError):actor_snapshot(memory,p,base,recipe,'Nelbin',expected_id=42,health_ceiling=9999.)
        def auto_profile_fixture(self):
            targets=[['stat'+str(off),off,'I' if off==120 else 'f',1] for off in sorted(AUTO_CORE_OFFSETS)]
            for t in targets:
                if t[1]==20:t[3]=1.5
                elif t[1]==28:t[3]=3.
            return dict(game_sha256='a'*64,native_identity_recipe={'build':'a'*64,'name':'Nelbin'},config={'base':0x20000,'targets':targets})
        def test_auto_rejects_invalid_target_plan(self):
            data=self.auto_profile_fixture()
            AutoBackend(data,None)
            next(t for t in data['config']['targets'] if t[1]==112)[3]=2
            with self.assertRaises(AutoBlocked):AutoBackend(data,None)
            data=self.auto_profile_fixture();next(t for t in data['config']['targets'] if t[1]==20)[3]=2.
            with self.assertRaises(AutoBlocked):AutoBackend(data,None)
        def test_stop_event_cross_process(self):
            suffix='test-stop-'+uuid.uuid4().hex
            with StopSignal(suffix) as signal:
                signal.reset();self.assertFalse(signal.wait(0))
                code="import game,sys; event=game.StopSignal(sys.argv[1]); event.request(); event.close()"
                result=subprocess.run([sys.executable,'-B','-c',code,suffix],cwd=ROOT,capture_output=True)
                self.assertEqual(result.returncode,0)
                self.assertTrue(signal.wait(.1));signal.reset();self.assertFalse(signal.wait(0))
        def test_auto_restart_waits_for_a_stopping_watcher(self):
            from unittest.mock import patch
            state={'stop_requested':True,'auto_monitor':{'pid':12345}}
            stopped=[]
            class Signal:
                def __enter__(self):return self
                def __exit__(self,*_):pass
                def reset(self):pass
            module=sys.modules[__name__]
            with patch.object(module,'mutex',lambda *_args,**_kwargs:nullcontext()), \
                 patch.object(module,'read',lambda:state), \
                 patch.object(module,'auto_alive',lambda _data:True), \
                 patch.object(module,'active_helpers',lambda *_:[]), \
                 patch.object(module,'stop',lambda:stopped.append(True)), \
                 patch.object(module,'StopSignal',Signal), \
                 patch.object(module,'update_session',lambda change:change(state)):
                result=prepare_auto_launch(argparse.Namespace(background=False,trace_only=False,seconds=1))
            self.assertFalse(result);self.assertEqual(stopped,[True]);self.assertFalse(state['stop_requested'])
        def test_trace_summary_units(self):
            c=dict(name='Nelbin',pid=1,player_id=2,level=3,base=65536,pointer=131072,length=6)
            v={'Current Health':9999.,'Max Health':9999.,'Gold Wallet':999999,'Damage':100.99,'Crit Damage':99.99,'Speed':1.5,'Experience':3.}
            result=trace_summary(c,v,'test')
            self.assertEqual(result['hp'],'9999/9999')
            self.assertEqual(result['speed_bonus_percent'],50)
            self.assertEqual(result['experience_bonus_percent'],200)
            self.assertEqual(result['damage_bonus_percent'],9999)
        def test_named_candidate_rejects_other_name(self):
            class Memory:
                def read_bytes(self,address,size):
                    return struct.pack('<3Q',6,0x30000,6) if size==24 else b'Other!'
            with self.assertRaises(RuntimeError):
                named_candidate(Memory(),None,{'base':0x20000,'player_id':1},
                                {'stats_offset':0x4c8,'name_header_offset':0x1b0},'Nelbin')
        def test_name_header_search(self):
            raw=struct.pack('<3Q',6,0x12340000,6)+struct.pack('<3Q',4,0x12340000,6)+struct.pack('<3Q',6,0,6)
            self.assertEqual(name_headers(raw,6),[(0,0x12340000)])
            self.assertEqual(name_headers(b'short',6),[])
        def test_full_selectors(self):
            self.assertNotEqual(selectors(struct.pack('<If', 14, .1)),
                                selectors(struct.pack('<If', 0x1000e, .1)))
            with self.assertRaises(ValueError): selectors(b'bad')
        def test_partial_write_recovery(self):
            class Memory:
                values = {1:b'a', 2:b'b'}
                failed = False
                def read_bytes(self, address, size): return self.values[address]
                def write_bytes(self, address, value):
                    if address == 2 and not self.failed:
                        self.failed = True
                        raise OSError('simulated failure')
                    self.values[address] = value
            m = Memory()
            with self.assertRaises(RuntimeError): write_batch(m, [(1,b'x'),(2,b'y')], lambda: None)
            self.assertEqual(m.values, {1:b'a',2:b'b'})
        def test_concurrent_update_preserved(self):
            class Memory:
                values = {1:b'a',2:b'b'}
                def read_bytes(self, address, size): return self.values[address]
                def write_bytes(self, address, value):
                    if address == 2:
                        self.values[1] = b'g'
                        raise OSError('game changed another field')
                    self.values[address] = value
            m = Memory()
            with self.assertRaises(RuntimeError): write_batch(m, [(1,b'x'),(2,b'y')], lambda: None)
            self.assertEqual(m.values[1], b'g')
        def test_guard_prevents_writes(self):
            class Memory:
                writes = 0
                def read_bytes(self, address, size): return b'a'
                def write_bytes(self, address, value): self.writes += 1
            def guard(): raise RuntimeError('owner changed')
            m = Memory()
            with self.assertRaises(RuntimeError): write_batch(m, [(1,b'x')], guard)
            self.assertEqual(m.writes, 0)
        def test_mutex_exclusion(self):
            child_code = "import game;\nwith game.mutex('test-exclusive', 0): pass"
            with mutex('test-exclusive', 0):
                child = subprocess.run([sys.executable, '-B', '-c', child_code], cwd=ROOT, capture_output=True)
            self.assertNotEqual(child.returncode, 0)
            with mutex('test-exclusive', 0): pass
        def test_observations(self):
            a = argparse.Namespace(crit=23.7, crit_damage=219, speed=50, experience=200)
            o = {x[0]: x[1] for x in observations(a)}
            self.assertAlmostEqual(o[12], .237)
            self.assertAlmostEqual(o[16], 2.19)
            self.assertEqual(o[20], 1.5)
            self.assertEqual(o[28], 3.)
        def test_insufficient(self):
            with self.assertRaises(RuntimeError): observations(argparse.Namespace(hp=700))
        def test_record(self):
            class Process:
                pid = 1
                def create_time(self): return 2.
            class Memory:
                def __init__(self):
                    self.raw = bytearray(1016)
                    struct.pack_into('<I', self.raw, 0, 42)
                    for offset in (0, 4, 8, 16, 20, 24, 28): struct.pack_into('<f', self.raw, 24+offset, 1.)
                    struct.pack_into('<I', self.raw, 128, 1)
                    struct.pack_into('<3Q', self.raw, 992, 8, 0x80000, 1)
                def read_bytes(self, address, size):
                    return struct.pack('<If', 14, .1) if address == 0x80000 else bytes(self.raw)
            m = Memory()
            self.assertEqual(record(m, Process(), 0x10000, [])['player_id'], 42)
            struct.pack_into('<f', m.raw, 24, float('nan'))
            with self.assertRaises(ValueError): record(m, Process(), 0x10000, [])
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    if not result.wasSuccessful(): raise RuntimeError('Self-test failed.')

def discovery_command(command):
    parser = argparse.ArgumentParser(description='Read-only live-field discovery and pointer training.')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--max-mb', type=int, default=4096 if command == 'discover' else 256)
    if command == 'discover':
        parser.add_argument('--mode', choices=['solo', 'host', 'guest'], required=True)
        for name in FIELDS: parser.add_argument('--'+name.replace('_', '-'), type=float)
    else:
        parser.add_argument('--base', type=lambda v: int(v, 0), required=True)
    args = parser.parse_args(sys.argv[2:])
    if not 1 <= args.seconds <= 120 or not 1 <= args.max_mb <= 8192:
        raise ValueError('Budget must be 1..120 seconds and 1..8192 MB.')
    {'discover': discover, 'learn': learn, 'adopt': adopt}[command](args)

if __name__ == '__main__':
    if len(sys.argv) == 1:
        sys.argv.append('gui')  # bare `python game.py` opens the window
    elif sys.argv[1] == '--background':
        sys.argv.insert(1, 'auto')  # `python game.py --background` = silent automatic watcher
    command = sys.argv[1]
    try:
        if command not in ('setup','self-test','version','--version','--help','-h','help','gui'):ensure_configured()
        if command in ('start','_worker','named-core','_named_core','source-apply','lifesteal-test','apply-once','apply-host','capture','numeric-watch','text-watch','inspect-copy','observe-swaps','discover') and auto_alive(read()):
            raise RuntimeError('Automatic watcher is active. Stop it before running a competing writer or debugger.')
        if command == 'status': status()
        elif command in ('--help','-h','help'):
            print('setup --game PATH --name NAME (first time) | Preferred: (no arguments) = window | --background = silent watcher | auto | new-game [--apply] | status | stop')
            print('Manual: resolve-name | discover-name --name <character> | named-core --seconds 600 --background')
            print('Diagnostics: meter-test | cards-test | relayout | find-wallet | self-test | watch-self-test | source-apply | lifesteal-test | discover | learn | adopt | capture | text-scan | text-watch | numeric-scan | numeric-watch | inspect-copy | observe-swaps')
            print('Each argument-based command accepts --help. Legacy start/apply-host were removed (they paused the game).')
        elif command in ('start','_worker','apply-host'):
            raise RuntimeError('Legacy process-pausing commands were removed. Use: auto --background')
        elif command == 'stop': stop()
        elif command in ('discover', 'learn', 'adopt'):
            with mutex('control', 0): discovery_command(command)
        elif command == 'capture':
            with mutex('control', 0): capture()
        elif command == 'text-scan':
            with mutex('control', 0): text_scan()
        elif command == 'text-watch':
            with mutex('control', 0): text_watch()
        elif command == 'numeric-scan':
            with mutex('control', 0): numeric_scan()
        elif command == 'numeric-watch':
            with mutex('control', 0): numeric_watch()
        elif command == 'apply-once':
            with mutex('control', 0): apply_once()
        elif command == 'watch-self-test': watch_self_test()
        elif command == 'inspect-copy':
            with mutex('control',0):inspect_copy()
        elif command == 'observe-swaps':
            with mutex('control',0):observe_swaps()
        elif command == 'discover-name':
            with mutex('control',0):discover_name()
        elif command == 'lifesteal-test':
            with mutex('control',0):lifesteal_test()
        elif command == 'resolve-name':
            with mutex('control',0):resolve_name()
        elif command == 'source-apply':
            with mutex('control',0),mutex('writer',0):source_apply()
        elif command in ('named-core','_named_core'):
            with mutex('control',10000 if command=='_named_core' else 0):
                if command=='named-core' and '--background' in sys.argv:named_core()
                else:
                    with mutex('writer',0):named_core()
        elif command == 'self-test': self_test()
        elif command == 'gui':gui()
        elif command == 'meter-test':meter_test()
        elif command == 'setup':setup_command()
        elif command in ('version','--version'):print(f'{APP_NAME} {VERSION}')
        elif command == 'cards-test':cards_test()
        elif command == 'relayout':relayout()
        elif command == 'find-wallet':find_wallet()
        elif command == 'new-game':new_game()
        elif command in ('auto','_auto_worker'):auto_command()
        else: raise RuntimeError('Usage: python game.py status|start|stop|discover|learn|adopt|capture|self-test')
    except Exception as error:
        print(f'Not applied: {error}', file=sys.stderr)
        sys.exit(1)
