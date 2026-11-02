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
import hmac
import math
import uuid
import ctypes
import threading
import io
from contextlib import redirect_stdout
from contextlib import contextmanager, nullcontext

import psutil

APP_NAME = 'Azeroth Survivors Trainer'
VERSION = '1.9.6'
REPO = 'nelbin4/Azeroth-Survivors-Trainer'
UPDATE_FILES = ('game.py', 'profile.example.json', 'requirements.txt', 'README.md', 'CHANGELOG.md', 'LICENSE', 'docs/TECHNICAL_NOTES.md')
USER_FILES = ()  # files users edit by hand: an update never overwrites an existing copy
ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
SESSION = ROOT / 'session.json'
CHECKED_BUILDS = set()
AUTO_CORE_OFFSETS = frozenset((0,4,8,12,16,20,24,28,32,36,40,44,48,52,56,60,64,68,72,112,120))
INTEGER_OFFSETS = (52,68,120)  # extra targets, DoT stacks, wallet are u32; the rest are f32
AUTO_SOURCE_TARGETS = {14:99.984,19:99.99,28:99.99}  # lifesteal, vs elites/bosses, shielded (kind 19 confirmed against the Run Stats panel)

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
    plain=bool(recipe.get('no_owner'))  # layouts without a verified modifier list: stats only
    level_off=recipe.get('level_offset',104);id_off=recipe.get('id_offset',-24)
    owner_off=0 if plain else recipe.get('owner_offset',968);header=base-delta
    size=delta+(max(r[1] for r in recipe['targets'])+4 if plain else owner_off+24)
    raw=memory.read_bytes(header,size)
    cap,name_pointer,length=struct.unpack_from('<3Q',raw)
    if name is None:
        # Name-free: any well-formed text header at the right distance is accepted; the text is only a structural anchor.
        if not 2<=length<=24 or not length<=cap<=256 or not 0x10000<=name_pointer<0x7ffffffff000:raise ValueError('Invalid character-name header.')
        encoded=memory.read_bytes(name_pointer,length)
        try:name=encoded.decode('utf-8')
        except UnicodeDecodeError:raise ValueError('Invalid character-name header.')
        import re
        if not re.fullmatch(r"[^\W\d_][\w' -]*",name):raise ValueError('Invalid character-name header.')
    else:
        encoded=name.encode('utf-8')
        if length!=len(encoded) or not length<=cap<=256 or not 0x10000<=name_pointer<0x7ffffffff000:
            raise ValueError('Invalid character-name header.')
        if memory.read_bytes(name_pointer,length)!=encoded:raise ValueError('Character name does not match.')
    def u(off):return struct.unpack_from('<I',raw,delta+off)[0]
    def f(off):return struct.unpack_from('<f',raw,delta+off)[0]
    hp_off=next((t[1] for t in recipe['targets'] if t[0]=='Current Health'),112)
    player_id=u(id_off);level=u(level_off)
    if not player_id or (expected_id is not None and player_id!=expected_id) or not 1<=level<=10000:
        raise ValueError('Actor identity or level changed.')
    if plain:
        for entry in recipe['targets']:
            if entry[2]=='f' and (not math.isfinite(f(entry[1])) or not 0<=f(entry[1])<=1e7):raise ValueError('Invalid actor stat.')
    else:
        for off in list(range(0,52,4))+[56,60,64,72,112]:
            if not math.isfinite(f(off)) or not 0<=f(off)<=1e7:raise ValueError('Invalid actor stat.')
    if f(0)<1 or (not plain and (u(52)>100000 or u(68)>100000)):raise ValueError('Invalid health cap or counter.')
    if f(hp_off)>f(0)+1:
        # Only a known, previously injected actor is eligible for this transition.
        if health_ceiling is None or f(hp_off)>health_ceiling+.01:
            raise ValueError('HP exceeds its current cap without an injection lease.')
    if plain:capacity=pointer=count=0;kinds=[];amounts=[]
    else:
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
    if raw[:24]!=check[:24] or raw[delta+id_off:delta+id_off+4]!=check[delta+id_off:delta+id_off+4] or (not plain and raw[delta+owner_off:delta+owner_off+24]!=check[delta+owner_off:delta+owner_off+24]):
        raise ValueError('Actor moved during snapshot.')
    if memory.read_bytes(name_pointer,length)!=encoded:raise ValueError('Name changed during snapshot.')
    values={entry[0]:struct.unpack_from('<'+entry[2],raw,delta+entry[1])[0] for entry in recipe['targets']}
    return dict(pid=process.pid,created=process.create_time(),base=base,player_id=player_id,name=name,
                level=level,owner=base+owner_off,pointer=pointer,capacity=capacity,length=count,kinds=kinds,
                amounts=amounts,values=values,hp_transition=f(hp_off)>f(0)+1)

KNOWN_LAYOUTS = {}  # layouts shipped with the trainer (none: unseen builds are found by their field pattern)

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

def find_plain(memory,process,recipe,cancelled=lambda:False,budget=40.):
    """Read-only, name-free: every record with a text header at the known distance and a plausible stat block."""
    import numpy as np
    delta=recipe['stats_offset']-recipe['name_header_offset'];lvl=recipe['level_offset'];idoff=recipe['id_offset']
    reach=max(r[1] for r in recipe['targets'])+4;span=delta+reach;found=[];deadline=time.monotonic()+budget
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
            base=idx*8+delta;ok=(base+reach<=len(raw))&(base+lvl>=0)&(base+idoff>=0)
            idx,base=idx[ok],base[ok]
            u32=np.frombuffer(raw[:len(raw)//4*4],dtype='<u4');f32=u32.view('<f4')
            with np.errstate(all='ignore'):
                keep=(u32[(base+lvl)//4]>=1)&(u32[(base+lvl)//4]<=10000)&(u32[(base+idoff)//4]!=0)&(f32[base//4]>=100)&(f32[base//4]<=1e7)
            for i,b in zip(idx[keep],base[keep]):
                try:snap=actor_snapshot(memory,process,address+int(b),recipe,None)
                except Exception:continue
                if sum(abs(x)>1e-3 for x in snap['values'].values())>=9:found.append(snap)
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


CORE_CHUNK = 4194304
CORE_FIELDS = {  # extra stats pinned in pattern mode, per game build (offsets read off the character sheet)
    'eb744751b4e17ec1397b0cdc85a22af63b6af63c1da1d9c2861762c3c4d8947c':{'Area':44},
}

def core_window_ok(values):
    """The first eight stat fields in their fixed order: health, damage, haste, crit, crit damage, speed, pickup, experience."""
    hp,damage,haste,crit,critdmg,speed,pickup,exp=values
    return (50<=hp<=1e6 and 0.5<=damage<=2e4 and 0<haste<=10 and 0.01<crit<=200 and 1.2<=critdmg<=200
            and 0.5<=speed<=3 and 10<=pickup<=1e4 and 0.99<=exp<=1e6)

def core_headers(memory,base,only=None):
    """Distances from a text header (cap, pointer, length of a short word) to the stat block; the text is only an anchor."""
    return [d for d,_ in core_header_hits(memory,base,only)]

def core_header_hits(memory,base,only=None):
    import re
    reach=1536;lo=base-reach
    try:raw=memory.read_bytes(lo,reach)
    except Exception:return []
    found=[]
    for pos in range((-lo)%8,reach-24,8):
        distance=base-(lo+pos)
        if only is not None and distance!=only:continue
        cap,pointer,length=struct.unpack_from('<3Q',raw,pos)
        if not(2<=length<=24 and length<=cap<=256 and 0x10000<=pointer<0x7ffffffff000):continue
        try:text=memory.read_bytes(pointer,length).decode('utf-8')
        except Exception:continue
        if re.fullmatch(r"[^\W\d_][\w' -]*",text):found.append((distance,text))
    return found

def core_scan(memory,cancelled=lambda:False,budget=40.,first=(),workers=4):
    """Read-only: stat blocks that match the field pattern and sit after a text header. Chunks near earlier hits go first."""
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    chunks=[]
    for region in memory.snapshot_memory_regions():
        if region.is_readable and region.is_writable and not region.is_executable:
            for off in range(0,region.size,CORE_CHUNK):chunks.append((region.address+off,min(CORE_CHUNK+32,region.size-off)))
    chunks.sort(key=lambda c:0 if any(c[0]<=h<c[0]+CORE_CHUNK for h in first) else 1)
    deadline=time.monotonic()+budget;found=[]
    def work(chunk):
        try:raw=memory.read_bytes(*chunk)
        except Exception:return []
        f=np.frombuffer(raw[:len(raw)//4*4],dtype='<f4')
        if len(f)<9:return []
        n=len(f)-8
        with np.errstate(all='ignore'):
            ok=((f[:n]>=50)&(f[:n]<=1e6)&(f[1:n+1]>=0.5)&(f[1:n+1]<=2e4)&(f[2:n+2]>0)&(f[2:n+2]<=10)
               &(f[3:n+3]>0.01)&(f[3:n+3]<=200)&(f[4:n+4]>=1.2)&(f[4:n+4]<=200)&(f[5:n+5]>=0.5)&(f[5:n+5]<=3)
               &(f[6:n+6]>=10)&(f[6:n+6]<=1e4)&(f[7:n+7]>=0.99)&(f[7:n+7]<=1e6))
        return [chunk[0]+int(i)*4 for i in np.flatnonzero(ok) if i*4<CORE_CHUNK]
    with ThreadPoolExecutor(workers) as pool:
        for start in range(0,len(chunks),workers*4):
            if cancelled():raise InterruptedError('Stop requested.')
            if time.monotonic()>=deadline:break
            for hits in pool.map(work,chunks[start:start+workers*4]):
                for base in hits:
                    distances=core_headers(memory,base)
                    if distances:found.append((base,distances[-1]))
    return found

def core_live(memory,bases,seconds=4.5):
    """Fail closed: a matching pattern alone cannot identify the local player.

    The previous probe wrote to every candidate, including during trace-only
    operation. A read-only, identity-verified replacement is still needed.
    """
    return []

def hook_current():
    """Opt-in hook for a manually identified record on one verified build."""
    import frida
    parser=argparse.ArgumentParser(description='Experimental: keep Damage and Area at +9999% for a manually identified character record.')
    parser.add_argument('--pid',required=True,type=int)
    parser.add_argument('--base',required=True,type=lambda x:int(x,0))
    parser.add_argument('--name',required=True)
    parser.add_argument('--seconds',type=int,default=600,help='Maximum hook duration (default: 600 seconds).')
    args=parser.parse_args(sys.argv[2:])
    if not args.name or len(args.name.encode('utf-8'))>24 or args.base<0x10000 or not 1<=args.seconds<=86400:
        raise ValueError('Invalid name or stat address.')
    proc=psutil.Process(args.pid)
    exe=Path(proc.exe())
    if exe.name.lower()!='azerothsurvivors.exe':raise ValueError('Not the game executable.')
    digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    if digest!='0e8e314df4d78ab211b1bef5c48d78d713ed64260894af342f998d298d74d274':
        raise ValueError('Unsupported game build; no hooks installed.')
    source=r'''const p=ptr(BASE), module=Process.getModuleByName('Azerothsurvivors.exe');
const name=NAME, header=p.sub(912);
function valid(){try {let cap=header.readU64().toNumber(), len=header.add(16).readU64().toNumber();
 return len>0 && len<=24 && cap>=len && cap<=256 && header.add(8).readPointer().readUtf8String(len)===name;
} catch (_) {return false;}}
function check(off,hex){let x=Array.from(new Uint8Array(module.base.add(off).readByteArray(hex.length/2))).map(b=>b.toString(16).padStart(2,'0')).join('');if(x!==hex)throw Error('Instruction mismatch; no hooks installed.');}
if(!valid())throw Error('Character record does not match the provided name.');
check(0x5eca29b,'f30f11b6c8050000');check(0x5eca2ac,'f3440f118ed0050000');
check(0x5eca34c,'f30f1186f4050000');check(0x5eca354,'0f2845f0f30f1186f8050000');
let hooks=[];try {
 hooks.push(Interceptor.attach(module.base.add(0x5eca2ac),{onEnter(){if(this.context.rsi.add(0x5c8).equals(p)&&valid())p.add(4).writeFloat(100.99);}}));
 hooks.push(Interceptor.attach(module.base.add(0x5eca354),{onEnter(){if(this.context.rsi.add(0x5c8).equals(p)&&valid())p.add(44).writeFloat(100.99);}}));
 if(!valid())throw Error('Character record changed.');p.add(4).writeFloat(100.99);p.add(44).writeFloat(100.99);
 send('Damage and Area hooks active.');
} catch(e){hooks.forEach(h=>h.detach());throw e;}'''.replace('BASE',json.dumps(hex(args.base))).replace('NAME',json.dumps(args.name))
    with mutex('hook',0),StopSignal('hook') as signal:
        signal.reset();session=frida.attach(args.pid)
        try:
            ready=threading.Event();errors=[]
            script=session.create_script(source)
            def receive(message,data):
                if message['type']=='error':errors.append(message.get('description','Hook failed.'))
                elif message['type']=='send':print(message['payload'],flush=True)
                ready.set()
            script.on('message',receive);script.load()
            if not ready.wait(5) or errors:raise RuntimeError(errors[0] if errors else 'Hook did not start.')
            print('Use hook-stop or Ctrl+C to detach. Addresses must be rediscovered for a new run.',flush=True)
            deadline=time.monotonic()+args.seconds
            while proc.is_running() and time.monotonic()<deadline and not signal.wait(1):pass
        except KeyboardInterrupt:pass
        finally:session.detach()

def hook_stop():
    """Request the current hook to detach, without requiring a console window."""
    with StopSignal('hook') as signal:signal.request()
    print('Hook stop requested.',flush=True)

class AutoBackend:
    """Live access adapter. Discovery is incremental, cancellable and read-only."""
    def __init__(self,data,signal):
        self.data=data;self.signal=signal;self.process=None;self.memory=None;self.generation=None
        self.recipe=dict(data.get('native_identity_recipe',{}));pinned=self.recipe.get('name')
        # A pinned name (setup --name) is used as is. Otherwise the name is re-detected for every run:
        # players can rename their character in the lobby at any time.
        self.pinned=bool(pinned) and not pinned.startswith('<');self.name=pinned if self.pinned else None
        self.detect_next=0;self.bad_names=set();self.ambiguous_names=None;self.scene=None;self.scene_next=0;self.scene_busy=False
        if self.recipe.get('build','').lower()!=data['game_sha256'].lower():raise AutoBlocked('Identity recipe/build mismatch.')
        targets=[t for t in data['config']['targets'] if t[1] in AUTO_CORE_OFFSETS]
        if len(targets)!=len(AUTO_CORE_OFFSETS) or {t[1] for t in targets}!=AUTO_CORE_OFFSETS:
            raise AutoBlocked('Supported target layout is invalid.')
        for _,off,fmt,value in targets:
            if isinstance(value,bool) or fmt!=('I' if off in INTEGER_OFFSETS else 'f') or not math.isfinite(value) or not 0<=value<=1e7 or (fmt=='I' and not isinstance(value,int)):
                raise AutoBlocked('Invalid target type/value.')
            if off==20 and value!=1.5:raise AutoBlocked('Speed must stay at +50%.')
            if off==28 and not 1<=value<=1e6:raise AutoBlocked('Experience multiplier must be between 1 and 1,000,000.')
        desired={off:value for _,off,_,value in targets}
        if desired[0]<1 or desired[112]>desired[0] or desired[8]<=0:raise AutoBlocked('Invalid target health or cooldown relationship.')
        self.targets=targets;self.recipe['targets']=targets;self.hash_cache={}
        self.hints={data['config']['base']};self.injected=set();self.regions=[];self.region_index=0;self.offset=0
        self.scan_found={};self.scan_errors=0;self.scan_bytes=0;self.scan_complete=False;self.scan_restart=0;self.last_hint=0
        self.backed_up=set();self.missing_sources=[]
        self.base_recipe=dict(self.recipe);self.base_targets=list(targets);self.build=data['game_sha256'].lower();self.learning=False
        self.learn_headers=None;self.learn_samples=[];self.next_learn=0;self.disabled=set();self.write_only=None;self.core=False;self.core_base=None;self.core_delta=None;self.core_pending=[];self.core_next=0;self.core_name=None
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
        learned=read().get('learned_builds',{}).get(digest) or KNOWN_LAYOUTS.get(digest)
        if learned and 'delta' in learned:
            self.set_layout(learned);self.core=False
            self.learning=False
        else:
            # An unseen build: find the stat block by its field pattern instead of a slow name-based learning.
            self.learning=False;self.core=True;self.core_delta=(read().get('core_builds',{}).get(digest) or {}).get('delta')
    def set_layout(self,learned):
        """Install a learned (or the original) layout; unverified fields are never written."""
        recipe=dict(self.base_recipe);skip=set();moved={}
        if learned:
            moved=dict(learned.get('field_offsets',{}))
            recipe.update(stats_offset=recipe['name_header_offset']+learned['delta'],build=self.build)
            if learned.get('no_owner'):recipe.update(no_owner=True,level_offset=learned['level_offset'],id_offset=learned['id_offset'])
            else:recipe.update(owner_offset=learned['owner_offset'])
            skip=set(learned.get('skip_offsets',[]))
            if 'wallet_offset' in learned:skip.discard(120);moved['Gold Wallet']=learned['wallet_offset']
        self.targets=[(n,moved.get(n,off),f,v) for n,off,f,v in self.base_targets if off not in skip and moved.get(n,off)!=-1]
        recipe['targets']=self.targets;self.recipe=recipe
        only=(learned or {}).get('write_only');self.write_only=set(only) if only else None
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
            if not results and not self.pinned:self.bad_names.add(self.name);self.name=None;self.detect_next=0  # not a character: try the next name on screen
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
        self.learn_headers=None;self.learn_samples=[];self.next_learn=0;self.core_base=None;self.core_pending=[];self.core_next=0;self.core_name=None
        saved=read().get('core_builds',{}).get(self.build) or {}
        if saved.get('pid')==generation[0] and saved.get('created')==generation[1]:self.core_base=saved.get('base');self.core_delta=saved.get('delta')
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
        if self.core:return self.core_candidate(candidate['base'])
        key=(self.generation[0],self.generation[1],candidate['base'],candidate['player_id'])
        for attempt in range(3):
            try:
                return actor_snapshot(self.memory,self.process,candidate['base'],self.recipe,self.name,
                    expected_id=candidate['player_id'],health_ceiling=9999. if key in self.injected else None)
            except (ValueError,RuntimeError):
                if attempt==2:raise
                if self.signal.wait(.005):raise InterruptedError('Stop requested.')
    def applied_names(self):
        if self.core:return ['Damage']+sorted(CORE_FIELDS.get(self.build,{}))
        return [t[0] for t in self.targets if not self.write_only or t[0] in self.write_only]
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
        if self.learning and not self.pinned:
            # An unseen build: the saved layout cannot find records (it matches junk such as "in"), so read the name off the screen.
            try:names=[n for n in window_name_candidates(self.process.pid) if n not in self.bad_names]
            except Exception:names=[]
            self.ambiguous_names=None;self.name=names[0] if names else None
            return []
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
    def core_candidate(self,base):
        raw=self.memory.read_bytes(base,32);values=struct.unpack('<8f',raw)
        if not core_window_ok(values):raise ValueError('Stat block changed.')
        if not core_headers(self.memory,base):raise ValueError('Text header missing.')  # any header: nearby text changes as the game runs
        return dict(pid=self.process.pid,created=self.process.create_time(),base=base,player_id=1,name='',level=0,values={'Damage':values[1],'Max Health':values[0]},
                    pointer=0,capacity=0,length=0,kinds=[],amounts=[])
    def seek_core(self,now):
        if self.core_base:
            try:return [self.core_candidate(self.core_base)]
            except Exception:self.core_base=None
        if self.core_pending:
            live=core_live(self.memory,[b for b,_ in self.core_pending],3.)
            if live:return self.core_found(live)
        if now<self.core_next:return []
        self.core_next=now+8
        found=core_scan(self.memory,cancelled=lambda:self.signal.wait(0),first=[b for b,_ in self.core_pending])
        self.core_pending=found
        if not found:return []
        live=core_live(self.memory,[b for b,_ in found])
        return self.core_found(live) if live else []
    def core_found(self,live):
        delta=dict(self.core_pending)[live[0]]
        if len(live)>1:
            # Several live characters (companions, a hosted lobby): yours is the one whose header text is the name in the player frame.
            texts={b:{x for _,x in core_header_hits(self.memory,b) if len(x)>=3} for b in live}
            try:chosen=window_player_name(self.process.pid,sorted(set().union(*texts.values())))
            except Exception:chosen=None
            remembered=(read().get('core_builds',{}).get(self.build) or {}).get('name')  # the screen shows the name only in a run
            if not chosen and remembered in set().union(*texts.values()):chosen=remembered
            mine=[b for b in live if chosen and chosen in texts[b]]
            if len(mine)!=1:self.ambiguous_names=['several characters'];return [self.core_candidate(b) for b in live]
            live=mine;delta=dict(self.core_pending)[live[0]];self.core_name=chosen
        self.core_base=live[0];self.core_delta=delta;self.core_pending=[];digest=self.build
        pid,created=self.generation[0],self.generation[1];base=self.core_base;name=self.core_name
        def store(d):
            old=d.setdefault('core_builds',{}).get(digest) or {}
            d['core_builds'][digest]=dict(delta=delta,found=time.time(),pid=pid,created=created,base=base,name=name or old.get('name'))
        update_session(store)
        return [self.core_candidate(self.core_base)]
    def core_apply(self,candidate):
        base=candidate['base'];fields={'Damage':4,**CORE_FIELDS.get(self.build,{})};pins=[]
        for name,offset in fields.items():
            want=next((v for n,o,f,v in self.base_targets if n==name),None)
            if want is not None and name not in self.disabled:pins.append((name,base+offset,struct.pack('<f',want),want))
        if not pins:return dict(writes=0,values=candidate['values'])
        writes=0;end=time.monotonic()+.4;n=0
        while time.monotonic()<end:
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            if n%10==0:self.core_candidate(base)  # stop at once if the record moved or changed shape
            for name,address,target,want in pins:
                if self.memory.read_bytes(address,4)!=target:self.memory.write_bytes(address,target);writes+=1
            n+=1;time.sleep(.02)
        return dict(writes=writes,values=dict(candidate['values'],**{name:want for name,address,target,want in pins}))
    def seek_plain(self,now):
        found={}
        for base in sorted(self.hints):
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            try:found[base]=self.at(base)
            except Exception:pass
        if found:return list(found.values())
        if now<self.detect_next:return []
        self.detect_next=now+10
        candidates=find_plain(self.memory,self.process,dict(self.recipe,targets=self.targets),cancelled=lambda:self.signal.wait(0))
        self.hints.update(c['base'] for c in candidates)
        names={c['name'] for c in candidates}
        if len({c['player_id'] for c in candidates})>1:
            try:chosen=window_player_name(self.process.pid,sorted(names))
            except Exception:chosen=None
            if chosen:candidates=[c for c in candidates if c['name']==chosen]
            else:self.ambiguous_names=sorted(names)
        return candidates
    def seek(self,now):
        if self.core:return self.seek_core(now)
        if self.recipe.get('no_owner'):return self.seek_plain(now)
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
        if self.core:return self.core_apply(candidate)
        result=self.apply_once(candidate)
        if self.recipe.get('no_owner'):self.hold(candidate['base'],.4)  # these stats are recalculated every couple of seconds: pin them between steps
        return result
    def hold(self,base,seconds):
        """Rewrite the pinned fields every 20 ms for a short while; stops at once if the record's level changes or disappears."""
        level_address=base+self.recipe['level_offset'];level=struct.unpack('<I',self.memory.read_bytes(level_address,4))[0]
        pins=[(base+off,struct.pack('<'+fmt,value)) for name,off,fmt,value in self.targets if name not in self.disabled and (not self.write_only or name in self.write_only)]
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            if self.signal.wait(0):raise InterruptedError('Stop requested.')
            if struct.unpack('<I',self.memory.read_bytes(level_address,4))[0]!=level:return
            for address,target in pins:self.memory.write_bytes(address,target)
            time.sleep(.02)
    def apply_once(self,candidate):
        fresh=self.snapshot(candidate);changes=[];names=[];missing=[]
        for name,off,fmt,value in self.targets:
            if name in self.disabled or (self.write_only and name not in self.write_only):continue
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
            if getattr(self.backend,'core',False) and not reset:self.active=True;self.last_progress=now  # pattern-found record: apply at once
            elif not reset and (fresh['level']>self.baseline or gold_gain or damage):
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
                child=subprocess.Popen(argv,cwd=ROOT,env=child_env(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW)  # own process group: a Ctrl+C aimed at the launching console must not stop the watcher
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
        deadline=time.monotonic()+args.seconds;last_save=0;last_state=None;last_saved=None;last_identity=None;talents_done=[None]
        try:
            backend=AutoBackend(data,signal)
            if args.trace_only:
                backend.apply=lambda candidate:dict(writes=0,values=candidate['values'])
            engine=AutoRecovery(backend)
            while time.monotonic()<deadline and not signal.wait(0):
                state=engine.step()
                if engine.generation and engine.generation!=talents_done[0] and not args.trace_only and state.get('state') not in ('waiting_process','starting'):
                    talents_done[0]=engine.generation
                    if read().get('talent_auto',False):
                        def talents_job():
                            try:talents_apply(read().get('talent_cap',TALENT_DEFAULT_CAP))
                            except Exception:pass
                        threading.Thread(target=talents_job,daemon=True).start()
                if state.get('state')!=last_state:
                    print(json.dumps(state),flush=True);last_state=state.get('state')
                if time.monotonic()-last_save>=10 or state.get('state')!=last_saved:
                    runtime.update(state);runtime['reason']=state.get('reason');runtime['character']=backend.name;runtime.update(heartbeat=time.time(),game_pid=engine.generation[0] if engine.generation else None,
                        game_created=engine.generation[1] if engine.generation else None,scan_bytes=backend.scan_bytes,
                        scan_errors=backend.scan_errors,missing_source_kinds=backend.missing_sources,applied=backend.applied_names())
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
            cwd=ROOT,env=child_env(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
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

def capture_window(handle,path,top=0.,bottom=1.,left=0.,right=1.):
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
        pixels=np.frombuffer(buffer,dtype=np.uint8).reshape(height,width,4)[int(top*height):int(bottom*height),int(left*width):int(right*width),[2,1,0]]
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

def fuzzy_in(text,name):
    import difflib
    text=text.lower().replace('-',' ').replace("'",'');name=name.lower().replace('-',' ').replace("'",'')
    if name in text:return True
    words=text.split();size=len(name.split())
    return any(difflib.SequenceMatcher(None,' '.join(words[i:i+size]),name).ratio()>=.82 for i in range(max(1,len(words)-size+1)))

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

NAME_STOPWORDS = {'gold','damage','done','level','boss','next','lobby','menu','options','kills','time','exp','wave','host','players','player','you','the','and'}

def window_name_candidates(pid):
    """Names shown top-left in the player frame, nearest the top first (single-word OCR lines only)."""
    import re
    handle=game_window(pid)
    if not handle:return []
    path=shot_path('name')
    width,height=capture_window(handle,path)
    lines=sorted((y1,t.strip()) for x1,y1,x2,y2,t in ocr_lines(path) if x1<width*.3 and y1<height*.25)
    return [t for _,t in lines if re.fullmatch(r"[^\W\d_][\w'-]{2,23}",t) and t.lower() not in NAME_STOPWORDS]

def detect_scene(lines,width,height):
    """What is on screen? 'level_up', 'lobby', 'menu' or 'unknown', from OCR lines (x1,y1,x2,y2,text)."""
    import re
    text=' '.join(t for *_,t in lines).lower()
    header=' '.join(t for x1,y1,x2,y2,t in lines if y1<height*.45).lower()
    if 'level up' in header:return 'level_up'
    if re.search(r'\blobby\b|waiting for (?:the )?(?:host|players?)|\binvite (?:code|link)|party members',text):return 'lobby'
    if sum(word in text for word in ('collection','drop tables','codex','talents','achievements','armory','rewards'))>=3:return 'menu'
    return 'unknown'

def child_env(**extra):
    """Environment for a re-launched copy of this program. Inside the bundled exe a child must not reuse the parent's
    temporary folder (it vanishes when the parent exits), so ask the bootloader for a clean start."""
    return dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1',**extra)

def parse_version(text):
    return tuple(int(part) for part in text.strip().split('.'))

def http_get(url,limit,timeout):
    """HTTPS only, size-capped."""
    import urllib.request
    if not url.startswith('https://'):raise ValueError('Updates are fetched over HTTPS only.')
    request=urllib.request.Request(url,headers={'User-Agent':APP_NAME.replace(' ','-')+'/'+VERSION})
    with urllib.request.urlopen(request,timeout=timeout) as response:
        data=response.read(limit+1)
    if len(data)>limit:raise ValueError('Update download is larger than expected.')
    return data

def apply_update_zip(data,target,current):
    """Install a GitHub source archive into target if it is newer and its game.py compiles. Returns the new version or None."""
    import re,zipfile,shutil
    target=Path(target)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names=archive.namelist();prefix=names[0].split('/')[0]+'/'
        files={rel:archive.read(prefix+rel) for rel in UPDATE_FILES if prefix+rel in names}
    if 'game.py' not in files:raise ValueError('Update archive has no game.py.')
    source=files['game.py'].decode('utf-8');compile(source,'game.py','exec')  # never install code that does not even compile
    found=re.search(r"^VERSION = '([\d.]+)'",source,re.M)
    if not found or parse_version(found.group(1))<=parse_version(current):return None
    backup=target/'.update_backup'/current;backup.mkdir(parents=True,exist_ok=True)
    for rel,content in files.items():
        destination=target/rel
        if rel in USER_FILES and destination.exists():continue
        destination.parent.mkdir(parents=True,exist_ok=True)
        if destination.exists():
            (backup/rel).parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,backup/rel)
        temporary=destination.with_name(destination.name+'.new');temporary.write_bytes(content);os.replace(temporary,destination)
    return found.group(1)

def apply_update_source(data,target,current):
    """Validate and atomically replace only game.py, preserving a backup."""
    import re,shutil
    source=data.decode('utf-8')
    compile(source,'game.py','exec')
    found=re.search(r"^VERSION = '([\d.]+)'",source,re.M)
    if not found or parse_version(found.group(1))<=parse_version(current):return None
    target=Path(target);destination=target/'game.py'
    if destination.exists():
        backup=target/'.update_backup'/current;backup.mkdir(parents=True,exist_ok=True)
        shutil.copy2(destination,backup/'game.py')
    temporary=target/'game.py.new'
    try:
        temporary.write_bytes(data);os.replace(temporary,destination)
    finally:
        if temporary.exists():temporary.unlink()
    return found.group(1)

def update_from_zip(target=None):
    """Fetch only the source file used by the launcher."""
    url=f'https://raw.githubusercontent.com/{REPO}/main/game.py'
    return apply_update_source(http_get(url,4000000,6),target or ROOT,VERSION)

def update_from_git():
    """Developer checkout: only a clean fast-forward; local work is never touched."""
    def git(*args):return subprocess.run(['git','-C',str(ROOT),*args],capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    if git('fetch','--quiet','origin','main').returncode:return None
    behind=git('merge-base','--is-ancestor','HEAD','origin/main').returncode==0 and git('rev-parse','HEAD').stdout!=git('rev-parse','origin/main').stdout
    if not behind or git('status','--porcelain','--untracked-files=no').stdout.strip():return None
    return 'latest' if git('merge','--ff-only','origin/main').returncode==0 else None

def auto_update():
    """At startup: fetch the latest release from GitHub. True means files were replaced and the caller should restart."""
    if os.environ.get('AZEROTH_UPDATED'):return False
    try:
        if SESSION.exists() and not read().get('auto_update',True):return False
        version=update_from_git() if not getattr(sys,'frozen',False) and (ROOT/'.git').exists() else update_from_zip()
    except Exception:return False  # offline or GitHub unreachable: just start normally
    if version:
        try:
            if SESSION.exists():update_session(lambda d:d.update(update_notice=f'Updated to version {version}.'))
        except Exception:pass
    return bool(version)

def restart_after_update():
    subprocess.Popen([sys.executable,str(Path(__file__).resolve()),*sys.argv[1:]],cwd=ROOT,env=child_env(AZEROTH_UPDATED='1'),
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW)
    sys.exit(0)

TALENT_ORIGINAL = 51
TALENT_DEFAULT_CAP = 150
TALENT_SITE_LIMIT = 60  # the game has about 20 copies; anything wildly different means the signature no longer fits

def talent_imm_offset(original_hex):
    return 2 if bytes.fromhex(original_hex)[0] == 0x41 else 1  # REX prefix before mov r32, imm32

def talent_patch_bytes(original_hex, cap):
    """The instruction with its 32-bit constant replaced; refuses anything that does not load 51."""
    raw = bytearray.fromhex(original_hex);offset = talent_imm_offset(original_hex)
    if len(raw) < offset + 4 or struct.unpack_from('<I', raw, offset)[0] != TALENT_ORIGINAL:raise ValueError('That instruction does not load 51.')
    if not 1 <= cap <= 999:raise ValueError('Talent cap must be 1 to 999.')
    struct.pack_into('<I', raw, offset, cap);return bytes(raw)

def find_talent_cap_sites(code, base_rva=0):
    """The talent point cap. Points come from a loop whose running cost ends at 875 (0x36b) when the cap is reached, and
    then returns 51. Each inlined copy is: cmp reg,0x36b ; jne loop ; mov reg,51. Matching the whole pattern (not just
    'mov reg,51') is what separates these from the many unrelated 51s in the game."""
    import re
    pattern = re.compile(rb'(?:\x81[\xf8-\xff]|\x3d|\x41\x81[\xf8-\xff]|\x49\x81[\xf8-\xff])\x6b\x03\x00\x00(?:\x75.|\x0f\x85....)((?:\x41)?[\xb8-\xbf]\x33\x00\x00\x00)', re.S)
    return [dict(rva=base_rva + match.start(1), original=match.group(1).hex()) for match in pattern.finditer(code)]

def validate_site_count(sites):
    if not 1 <= len(sites) <= TALENT_SITE_LIMIT:
        raise RuntimeError(f'Found {len(sites)} talent cap spots where about 20 were expected. This game version does not fit; the game was not touched.')
    return sites

def read_text_section(exe):
    """(code bytes, RVA) of the .text section of the game file on disk."""
    data = Path(exe).read_bytes()
    pe = struct.unpack_from('<I', data, 0x3c)[0];count = struct.unpack_from('<H', data, pe + 6)[0];optional = struct.unpack_from('<H', data, pe + 20)[0]
    offset = pe + 24 + optional
    for _ in range(count):
        name = data[offset:offset + 8].rstrip(b'\0').decode(errors='replace');size, rva, raw_size, raw = struct.unpack_from('<IIII', data, offset + 8)
        if name == '.text':return data[raw:raw + raw_size], rva
        offset += 40
    raise RuntimeError('No .text section in the game file.')

class ProcessCode:
    """Read and write another process's code pages with plain Windows calls. No hooks, no injection."""
    def __init__(self, pid):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong];self.api.OpenProcess.restype = ctypes.c_void_p
        self.api.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.WriteProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.VirtualProtectEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        self.api.FlushInstructionCache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.api.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, pid)  # VM_OPERATION | VM_READ | VM_WRITE | QUERY
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
    def read(self, address, size):
        buffer = ctypes.create_string_buffer(size);done = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(done)) or done.value != size:raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw
    def write_code(self, address, data):
        old = ctypes.c_ulong();done = ctypes.c_size_t()
        if not self.api.VirtualProtectEx(self.handle, address, len(data), 0x40, ctypes.byref(old)):raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.WriteProcessMemory(self.handle, address, data, len(data), ctypes.byref(done)) or done.value != len(data):raise ctypes.WinError(ctypes.get_last_error())
        finally:
            restored = ctypes.c_ulong();self.api.VirtualProtectEx(self.handle, address, len(data), old.value, ctypes.byref(restored))
            self.api.FlushInstructionCache(self.handle, address, len(data))
    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle = None
    def __enter__(self):return self
    def __exit__(self, *_):self.close()

@contextmanager
def paused_process(pid):
    """Stops the game for the few milliseconds a multi-site code patch takes, so it can never run with some copies of the
    cap changed and others not (mixed caps corrupted the game's memory when sites were changed one at a time). Always resumes."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True);native = ctypes.WinDLL('ntdll')
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong];kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    for name in ('NtSuspendProcess', 'NtResumeProcess'):
        getattr(native, name).argtypes = [ctypes.c_void_p];getattr(native, name).restype = ctypes.c_long
    handle = kernel.OpenProcess(0x800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    suspended = False
    try:
        if native.NtSuspendProcess(handle) < 0:raise RuntimeError('Could not pause the game briefly.')
        suspended = True
        yield
    finally:
        try:
            if suspended and native.NtResumeProcess(handle) < 0:raise RuntimeError('The game did not resume; close it and start it again.')
        finally:kernel.CloseHandle(handle)

def talent_state(code, base, sites):
    """(sites currently patched, total sites, cap shown by the first patched site)."""
    patched = 0;cap = None
    for site in sites:
        original = bytes.fromhex(site['original']);current = code.read(base + site['rva'], len(original));offset = talent_imm_offset(site['original'])
        if current[:offset] != original[:offset]:return 0, len(sites), None
        value = struct.unpack_from('<I', current, offset)[0]
        if value != TALENT_ORIGINAL:patched += 1;cap = cap or value
    return patched, len(sites), cap

def set_talent_cap(code, base, sites, cap, pause=nullcontext):
    """Change every cap site together (cap 51 restores). Everything is checked before the game is paused, written inside a
    short pause, verified by readback, and rolled back if anything fails."""
    def plan_for():
        plan = []
        for site in sites:
            original = bytes.fromhex(site['original']);address = base + site['rva'];offset = talent_imm_offset(site['original'])
            current = code.read(address, len(original))
            if current[:offset] != original[:offset] or current[offset + 4:] != original[offset + 4:]:raise RuntimeError(f"Unexpected code at {site['rva']:#x}; this is not the game version these spots belong to. The game was not touched.")
            plan.append((address, current, talent_patch_bytes(site['original'], cap)))
        return plan
    plan_for()  # dry run: fail early, without pausing anything
    written = []
    with pause():
        plan = plan_for()
        try:
            for address, current, target in plan:
                code.write_code(address, target);written.append((address, current))
            for address, current, target in plan:
                if code.read(address, len(target)) != target:raise RuntimeError('Readback did not match.')
        except Exception:
            for address, current in reversed(written):code.write_code(address, current)
            raise
    return len(plan)

TALENT_CACHE = {}

def talents_target(data):
    """(process, module base, build digest) for the running game, or a RuntimeError saying what is missing."""
    from PyMemoryEditor import OpenProcess
    matches = [p for p in psutil.process_iter(['exe']) if p.info['exe'] and os.path.normcase(p.info['exe']) == os.path.normcase(data['game_exe'])]
    if len(matches) != 1:raise RuntimeError('Start the game first (expected one game process).')
    process = matches[0];key = (process.pid, process.create_time())
    if key not in TALENT_CACHE:
        with Path(process.exe()).open('rb') as stream:digest = hashlib.file_digest(stream, 'sha256').hexdigest().lower()
        with OpenProcess(pid=process.pid, permission=0x410) as memory:
            modules = [m for m in memory.get_modules() if m.name.lower() == Path(data['game_exe']).name.lower()]
        if len(modules) != 1:raise RuntimeError('Could not locate the game module.')
        TALENT_CACHE.clear();TALENT_CACHE[key] = (modules[0].base_address, digest)  # hashing 147 MB once per game start
    base, digest = TALENT_CACHE[key]
    return process, base, digest

def talent_sites_for(data, digest, exe):
    """The cap sites of this game version: remembered in session.json, or found by scanning the game file on disk."""
    known = data.get('learned_builds', {}).get(digest, {}).get('talent_sites')
    if known:return known
    code, rva = read_text_section(exe);sites = validate_site_count(find_talent_cap_sites(code, rva))
    update_session(lambda d: d.setdefault('learned_builds', {}).setdefault(digest, {}).update(talent_sites=sites))
    return sites

def talents_apply(cap):
    data = read();process, base, digest = talents_target(data);sites = talent_sites_for(data, digest, process.exe())
    with ProcessCode(process.pid) as code:return set_talent_cap(code, base, sites, cap, lambda: paused_process(process.pid))

def talents_command():
    parser = argparse.ArgumentParser(description='Change the talent point cap in the running game (memory only; re-apply after a restart).')
    parser.add_argument('action', choices=['apply', 'restore', 'status'])
    parser.add_argument('--cap', type=int, default=TALENT_DEFAULT_CAP)
    args = parser.parse_args(sys.argv[2:])
    if args.action == 'status':
        data = read();process, base, digest = talents_target(data);sites = talent_sites_for(data, digest, process.exe())
        with ProcessCode(process.pid) as code:print(json.dumps(dict(build=digest, spots=len(sites), patched_cap=talent_state(code, base, sites))))
    else:print(f"{'Patched' if args.action == 'apply' else 'Restored'} {talents_apply(args.cap if args.action == 'apply' else TALENT_ORIGINAL)} spot(s).")

CLASS_NAMES = {1: 'Warrior', 2: 'Paladin', 3: 'Hunter', 4: 'Rogue', 5: 'Priest', 7: 'Shaman', 8: 'Mage', 9: 'Warlock', 11: 'Druid'}
CLASS_ORDER = (1, 2, 3, 4, 5, 7, 8, 9, 11)  # the order the game's talent table lists them in (three trees each)
SAVE_KEY_PREFIX = b'azeroth-survivors/sa'

def pe_layout(data):
    """(image base, {section name: (rva, virtual size, raw offset, raw size)}) of a 64-bit Windows executable."""
    pe = struct.unpack_from('<I', data, 0x3c)[0];count = struct.unpack_from('<H', data, pe + 6)[0];optional = struct.unpack_from('<H', data, pe + 20)[0]
    image = struct.unpack_from('<Q', data, pe + 24 + 24)[0];offset = pe + 24 + optional;sections = {}
    for _ in range(count):
        name = data[offset:offset + 8].rstrip(b'\0').decode(errors='replace');size, rva, raw_size, raw = struct.unpack_from('<IIII', data, offset + 8)
        sections[name] = (rva, size, raw, raw_size);offset += 40
    return image, sections

def split_talent_runs(records):
    """The talent table is one array of equally sized records; each tree is an unbroken run of them."""
    runs = []
    for record in sorted(records, key=lambda r: r['rva']):
        if runs and record['rva'] - runs[-1][-1]['rva'] == 0xB0:runs[-1].append(record)
        else:runs.append([record])
    return [run for run in runs if len(run) >= 10]

def assign_talent_classes(runs):
    """{class id: [[(talent id, max rank), ...] per tree]}; refuses anything that is not 3 trees per known class."""
    if len(runs) != 3 * len(CLASS_ORDER):raise RuntimeError(f'Expected {3 * len(CLASS_ORDER)} talent trees in the game file, found {len(runs)}. This game version is not supported.')
    table = {}
    for number, class_id in enumerate(CLASS_ORDER):
        trees = [[(t['id'], (t['packed'] >> 16) & 255) for t in run] for run in runs[number * 3:number * 3 + 3]]
        if any(not 1 <= rank <= 5 for tree in trees for _, rank in tree):raise RuntimeError('A talent has an impossible maximum rank. This game version is not supported.')
        table[class_id] = trees
    return table

def read_talent_table(exe):
    """Every talent of every class with its maximum rank, read from the game file (never from the running game)."""
    import numpy as np
    data = Path(exe).read_bytes();image, sections = pe_layout(data);rva, size, raw, raw_size = sections['.rdata'];pool = data[raw:raw + raw_size]
    words = np.frombuffer(pool[:len(pool) // 8 * 8], dtype='<u8');index = np.arange(0, len(words) - 30)
    inside = lambda w: (w >= image + rva) & (w < image + rva + raw_size)
    mask = (words[index + 1] >= 1) & (words[index + 1] <= 40) & (words[index + 3] >= 1) & (words[index + 3] <= 40) & (words[index + 5] >= 1) & (words[index + 5] <= 60) & inside(words[index]) & inside(words[index + 2]) & inside(words[index + 4])
    def text(pointer, length):
        start = int(pointer) - image - rva;return pool[start:start + int(length)]
    records = []
    for k in index[mask]:
        identifier, icon = text(words[k], words[k + 1]), text(words[k + 4], words[k + 5])
        if identifier and identifier.replace(b'_', b'').isalnum() and icon.startswith((b'Spell_', b'Ability_', b'INV_')):
            records.append(dict(rva=rva + int(k) * 8, id=identifier.decode(), packed=struct.unpack_from('<I', pool, (int(k) + 13) * 8)[0]))
    return assign_talent_classes(split_talent_runs(records))

def rse1_crypt(key, header, data):
    out = bytearray(data)
    for start in range(0, len(data), 20):
        stream = hmac.new(key, b'ks' + header + struct.pack('<Q', start // 20), hashlib.sha1).digest()
        for i in range(min(20, len(data) - start)):out[start + i] ^= stream[i]
    return bytes(out)

def rse1_unseal(key, text):
    """The game's save envelope: RSE1: + base64(16-byte header, XOR ciphertext, 20-byte HMAC-SHA1 tag). Raises ValueError if the tag does not match."""
    import base64
    if not text.startswith('RSE1:'):raise ValueError('Not an RSE1 save.')
    raw = base64.b64decode(text[5:], validate=True);header, cipher, tag = raw[:16], raw[16:-20], raw[-20:]
    if not hmac.compare_digest(hmac.new(key, b'tag' + header + cipher, hashlib.sha1).digest(), tag):raise ValueError('Tag mismatch.')
    return rse1_crypt(key, header, cipher).decode('utf-8')

def rse1_seal(key, plain):
    import base64, secrets
    header = secrets.token_bytes(16);cipher = rse1_crypt(key, header, plain.encode('utf-8'))
    return 'RSE1:' + base64.b64encode(header + cipher + hmac.new(key, b'tag' + header + cipher, hashlib.sha1).digest()).decode()

def find_save_key(blob, sealed_text):
    """The save key is a constant inside the game file (starting with a fixed prefix, sometimes stored XOR 0x36). Only a
    candidate that authenticates the player's own save is accepted, so nothing is guessed."""
    for mask in (0x36, 0):
        needle = bytes(x ^ mask for x in SAVE_KEY_PREFIX);start = 0
        while True:
            at = blob.find(needle, start)
            if at < 0:break
            block = bytes(x ^ mask for x in blob[at:at + 64])
            for end in range(len(SAVE_KEY_PREFIX), len(block) + 1):
                if end == len(block) or all(x == 0 for x in block[end:(end // 16 + 1) * 16]):
                    try:rse1_unseal(block[:end], sealed_text);return block[:end]
                    except ValueError:pass
                    break
            start = at + 1
    raise RuntimeError('Could not find the save key in the game file; this game version is not supported.')

def max_alloc(trees):
    return ','.join(f'{name}:{rank}' for tree in trees for name, rank in tree)

def check_table_against_save(plain, table):
    """Every talent already in the save must belong to the class the table says it does; otherwise the table is wrong."""
    for line in plain.split('\n'):
        parts = line.split('|')
        if parts[0] == 'talent' and len(parts) > 3 and parts[3] and parts[1].isdigit() and int(parts[1]) in table:
            known = {name for tree in table[int(parts[1])] for name, _ in tree}
            for item in parts[3].split(','):
                if item.split(':')[0] not in known:raise RuntimeError(f"The save has a talent ({item.split(':')[0]}) the game table does not list for class {parts[1]}. Nothing was changed.")

def edit_talents(plain, table, class_ids):
    """Set every talent of the chosen classes to its maximum, in both the active allocation and the Spec 1 preset; all other lines stay as they are."""
    lines = plain.split('\n');done = set()
    for number, line in enumerate(lines):
        parts = line.split('|')
        for class_id in class_ids:
            if parts[0] == 'talent' and len(parts) >= 4 and parts[1] == str(class_id):
                lines[number] = '|'.join(parts[:3] + [max_alloc(table[class_id])]);done.add(('talent', class_id))
            elif parts[0] == 'spec' and len(parts) >= 4 and parts[1] == str(class_id):
                lines[number] = '|'.join(parts[:3] + [max_alloc(table[class_id])] + parts[4:]);done.add(('spec', class_id))
    missing = [class_id for class_id in class_ids if ('talent', class_id) not in done]
    if missing:raise RuntimeError(f'The save has no talent record for class {missing[0]}. Nothing was changed.')
    return '\n'.join(lines)

def save_folder():
    folder = Path(os.environ.get('APPDATA', '')) / 'wenilla-rogue'
    if not (folder / 'characters.txt').is_file():raise RuntimeError(f'Your game save was not found ({folder / "characters.txt"}). Start the game once first.')
    return folder

def create_max_talent_save(selection):
    """selection: a class id or 'all'. Writes a new importable save with every talent at its maximum, after backing up the current one."""
    data = read();exe = Path(data['game_exe']);folder = save_folder()
    current = (folder / 'characters.txt').read_text(encoding='utf-8').strip()
    key = find_save_key(exe.read_bytes(), current);plain = rse1_unseal(key, current);table = read_talent_table(exe)
    check_table_against_save(plain, table)
    class_ids = list(CLASS_ORDER) if selection == 'all' else [selection]
    edited = edit_talents(plain, table, class_ids);sealed = rse1_seal(key, edited)
    if rse1_unseal(key, sealed) != edited:raise RuntimeError('The new save did not verify. Nothing was written.')
    saves = folder / 'Saves';saves.mkdir(exist_ok=True)
    backup = saves / f"AzerothSurvivors-before-max-talents-{time.strftime('%Y%m%d-%H%M%S')}.sav";backup.write_text(current, encoding='utf-8')
    label = 'all' if selection == 'all' else CLASS_NAMES[selection].lower()
    out = saves / f'AzerothSurvivors-max-talents-{label}.sav';out.write_text(sealed, encoding='utf-8')
    points = {class_id: sum(rank for tree in table[class_id] for _, rank in tree) for class_id in class_ids}
    return dict(path=out, backup=backup, classes=class_ids, talents=sum(len(tree) for c in class_ids for tree in table[c]), points=points)

def max_talents_command():
    parser = argparse.ArgumentParser(description='Write an importable save with every talent at its maximum (backs up your current save first). Import it in the game: Options > Saves > Import Save.')
    parser.add_argument('--class', dest='klass', default='all', choices=['all'] + [n.lower() for n in CLASS_NAMES.values()])
    args = parser.parse_args(sys.argv[2:])
    selection = 'all' if args.klass == 'all' else next(c for c, n in CLASS_NAMES.items() if n.lower() == args.klass)
    info = create_max_talent_save(selection)
    print(f"Saved {info['path']}\n{info['talents']} talents, points per class: " + ', '.join(f'{CLASS_NAMES[c]} {p}' for c, p in info['points'].items()) + f"\nBackup of your current save: {info['backup']}\nApply the talent cap first (python game.py talents apply), then import in the game: Options > Saves > Import Save.")

def configure(game_exe,name):
    """Create or update session.json for this machine: game path and character name."""
    exe=Path(game_exe)
    if not exe.is_file():raise RuntimeError(f'Game executable not found: {exe}')
    name=(name or '').strip() or '<auto>'
    if len(name.encode('utf-8'))>64:raise ValueError('Character name must be at most 64 bytes.')
    if SESSION.exists():data=read()
    else:
        example=RESOURCE_ROOT/'profile.example.json'
        if not example.exists():raise RuntimeError('profile.example.json is missing; re-download the project.')
        data=json.loads(example.read_text(encoding='utf-8'))
    data['game_exe']=str(exe);data.setdefault('native_identity_recipe',{})['name']=name
    data.pop('last_name',None);save(data);return data

def seed_session():
    """Create session.json from the example profile with no game path yet (the window asks for it)."""
    example=RESOURCE_ROOT/'profile.example.json'
    if not example.exists():raise RuntimeError('profile.example.json is missing; re-download the project.')
    data=json.loads(example.read_text(encoding='utf-8'));data.pop('game_exe',None);data.setdefault('native_identity_recipe',{})['name']='<auto>';save(data)

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
    args=parser.parse_args(sys.argv[2:]);configure(args.game,None)
    print(f'{APP_NAME} configured. Open the window with: python game.py')

def effective_targets(data,digest):
    """(name, offset, fmt, value) the watcher uses for this build; unverified fields are left out."""
    base=data['config']['targets']
    if digest==data['game_sha256'].lower():return [tuple(t) for t in base]
    learned=data.get('learned_builds',{}).get(digest) or KNOWN_LAYOUTS.get(digest)
    if not learned or 'delta' not in learned:return []
    moved=dict(learned.get('field_offsets',{}));skip=set(learned.get('skip_offsets',[]))
    if 'wallet_offset' in learned:skip.discard(120);moved['Gold Wallet']=learned['wallet_offset']
    return [(n,moved.get(n,o),f,v) for n,o,f,v in base if o not in skip and moved.get(n,o)!=-1]

def describe_state(monitor,state):
    """One plain sentence for the window, instead of a state code."""
    reason=monitor.get('reason')
    sentences={
        'starting':'Starting up...',
        'waiting_process':'Waiting for Azeroth Survivors to start.',
        'in_menu':"You're in the menu. Start a run (solo or host) to begin.",
        'in_lobby':"You're in the lobby. Start the run to begin.",
        'seeking_record':'Looking for your character. Start a run (solo or host).',
        'waiting_progress':'Character found. Your stats are applied once you level up or take damage.',
        'applying':'Your stats are being applied and kept at your targets.',
        'learning':'Learning this new game version (read-only). Stay in a run and level up once.',
        'learned':'Learned this game version. Continuing.',
        'recovering':'Reconnecting to your character...',
        'blocked_ambiguous':'Several characters were found and yours could not be told apart.',
        'blocked':'Blocked'+(f': {reason}' if reason else '.'),
        'failed':'The watcher stopped because of a problem'+(f': {reason}' if reason else '.'),
        'expired':'The watcher ran out of time. Press Start.',
        'stopped':'Stopped. Press Start to apply your stats.',
    }
    text=sentences.get(state,state.replace('_',' ').capitalize())
    phase=monitor.get('phase')
    if state=='learning':
        text={'need_name':'Learning this new game version. Waiting to read your character name on screen (leave the player frame visible).',
              'scan':'Learning this new game version: searching the game for your character...',
              'confirming':'Learning this new game version: confirming what was found...',
              'no_match':'This game version stores your character differently and could not be matched yet. See the Log and the README.',
              'ambiguous':'This game version could not be matched unambiguously yet.'}.get(phase,text)
    if state in ('applying','waiting_progress') and monitor.get('trace_only'):text='Observe only: reading your character without changing anything.'
    return text

STATE_TITLES = {'starting': 'Starting up', 'waiting_process': 'Waiting for the game', 'in_menu': 'In the menu', 'in_lobby': 'In the lobby',
                'seeking_record': 'Looking for your character', 'waiting_progress': 'Waiting for you to level up', 'applying': 'Applying your stats',
                'learning': 'Learning this game version', 'learned': 'Learned this game version', 'recovering': 'Reconnecting',
                'blocked_ambiguous': 'Which character is yours?', 'blocked': 'Blocked', 'failed': 'Stopped by a problem', 'expired': 'Timed out', 'stopped': 'Stopped'}

def parse_target(text,offset,fmt):
    """Validate a target typed in the window. Raises ValueError for anything the watcher would refuse."""
    value=float(str(text).replace(',','').strip())
    if not math.isfinite(value) or not 0<=value<=1e7:raise ValueError('out of range')
    if offset==28 and not 1<=value<=1e6:raise ValueError('experience must be 1 to 1,000,000')
    if offset==20 and value!=1.5:raise ValueError('speed is locked')
    if fmt=='I':
        if value!=int(value):raise ValueError('whole numbers only')
        return int(value)
    return value

class WatcherWindow:
    """Small Tk front end: one status card with Start/Stop, the talents box, and folded sections for stats, log and settings."""
    COLORS={'in_lobby':'#1565c0','in_menu':'#6b6b6b','applying':'#2e7d32','waiting_progress':'#b26a00','learning':'#1565c0','learned':'#1565c0','seeking_record':'#6b6b6b','blocked':'#c62828','blocked_ambiguous':'#c62828','recovering':'#c62828','waiting_process':'#6b6b6b','stopped':'#6b6b6b','expired':'#6b6b6b','failed':'#c62828'}
    def __init__(self,root):
        import tkinter as tk
        from tkinter import ttk
        self.tk=tk;self.ttk=ttk;self.root=root;self.last_state=None;self.digest=None;self.memory=None;self.mem_pid=None;self.busy=False;self.alive=False
        root.title(APP_NAME);root.minsize(500,0)
        card=ttk.Frame(root,padding=(14,12),relief='groove');card.pack(fill='x',padx=12,pady=(12,6))
        self.dot=tk.Canvas(card,width=14,height=14,highlightthickness=0);self.dot_id=self.dot.create_oval(2,2,12,12,fill='#6b6b6b',outline='');self.dot.pack(side='left',padx=(0,10))
        text=ttk.Frame(card);text.pack(side='left',fill='x',expand=True)
        self.headline=tk.StringVar(value='...');self.sub=tk.StringVar(value='')
        ttk.Label(text,textvariable=self.headline,font=('Segoe UI',12,'bold')).pack(anchor='w')
        ttk.Label(text,textvariable=self.sub,foreground='#555555',wraplength=330).pack(anchor='w')
        self.toggle_button=ttk.Button(card,text='Start',width=8,command=self.toggle_run);self.toggle_button.pack(side='right')
        self.mode=tk.StringVar(value='write')
        self.talent_cap=tk.StringVar(value=str(read().get('talent_cap',TALENT_DEFAULT_CAP)))
        self.talent_auto=tk.BooleanVar(value=read().get('talent_auto',False))
        self.field_vars={};self.live_vars={};self.row_widgets={}
        tabs=ttk.Notebook(root);tabs.pack(fill='both',expand=True,padx=12,pady=(0,6))
        stats=ttk.Frame(tabs,padding=(6,6));talents=ttk.Frame(tabs,padding=(6,6));logs=ttk.Frame(tabs,padding=(6,6))
        tabs.add(stats,text='Stats');tabs.add(talents,text='Talents');tabs.add(logs,text='Log')
        self.build_stats(stats);self.build_talents(talents);self.build_log(logs)
        foot=ttk.Frame(root);foot.pack(fill='x',padx=12,pady=(0,10))
        ttk.Button(foot,text='Settings',command=self.open_settings).pack(side='left')
        self.foot=tk.StringVar();ttk.Label(foot,textvariable=self.foot,foreground='#6b6b6b').pack(side='left',padx=10)
        ttk.Label(foot,text='Solo or host only',foreground='#b26a00').pack(side='right')
        root.protocol('WM_DELETE_WINDOW',self.close)
        self.say('Window opened. The watcher keeps running if you close this window; press Stop to end it.')
        notice=read().get('update_notice')
        if notice:
            self.say(notice);update_session(lambda d:d.pop('update_notice',None))
        self.refresh()
        if not configured():root.after(400,self.locate_game)  # first run: the window is up, now ask where the game is
        elif not auto_alive(read()):root.after(800,self.start)  # starts automatically with the window, in the selected mode
    def build_talents(self,body):
        tk=self.tk;ttk=self.ttk
        first=ttk.Frame(body);first.pack(fill='x',pady=(0,6))
        self.cap_check=ttk.Checkbutton(first,text=f'Raise talent cap to {self.talent_cap.get()}',variable=self.talent_auto,command=self.talent_toggle);self.cap_check.pack(side='left')
        self.talent_status=tk.StringVar(value='');ttk.Label(first,textvariable=self.talent_status,foreground='#2e7d32').pack(side='right')
        second=ttk.Frame(body);second.pack(fill='x',pady=(0,4))
        ttk.Label(second,text='Max talents for').pack(side='left')
        names=['All classes']+[CLASS_NAMES[c] for c in CLASS_ORDER]
        self.class_choice=ttk.Combobox(second,values=names,state='readonly',width=14);self.class_choice.set(read().get('max_talent_class','All classes'))
        if self.class_choice.get() not in names:self.class_choice.set('All classes')
        self.class_choice.pack(side='left',padx=6)
        ttk.Button(second,text='Create save',command=self.max_talents_create).pack(side='left')
        ttk.Button(second,text='Open Saves folder',command=self.open_saves).pack(side='left',padx=6)
        self.save_status=tk.StringVar(value='Then in the game: Options > Saves > Import Save.')
        ttk.Label(body,textvariable=self.save_status,foreground='#555555',wraplength=470).pack(anchor='w',pady=(2,0))
    def open_settings(self):
        tk=self.tk;ttk=self.ttk
        if getattr(self,'settings_window',None) and self.settings_window.winfo_exists():self.settings_window.lift();return
        win=self.settings_window=tk.Toplevel(self.root);win.title('Settings');win.transient(self.root);body=ttk.Frame(win,padding=14);body.pack(fill='both',expand=True)
        modes=ttk.Frame(body);modes.pack(fill='x',pady=(0,6))
        ttk.Label(modes,text='Mode').pack(side='left')
        ttk.Radiobutton(modes,text='Write',variable=self.mode,value='write').pack(side='left',padx=(8,4))
        ttk.Radiobutton(modes,text='Observe only',variable=self.mode,value='observe').pack(side='left')
        cap=ttk.Frame(body);cap.pack(fill='x',pady=(0,6))
        ttk.Label(cap,text='Talent point cap').pack(side='left')
        entry=ttk.Entry(cap,textvariable=self.talent_cap,width=6);entry.pack(side='left',padx=6)
        for event in ('<Return>','<FocusOut>'):entry.bind(event,lambda e:self.cap_changed())
        self.build_settings(body)
        ttk.Button(body,text="What's new",command=self.whats_new).pack(anchor='w',pady=(8,0))
    def cap_changed(self):
        try:cap=self.talent_cap_value()
        except ValueError as error:
            self.say(str(error));self.talent_cap.set(str(read().get('talent_cap',TALENT_DEFAULT_CAP)));return
        if cap==read().get('talent_cap',TALENT_DEFAULT_CAP):return
        update_session(lambda d:d.update(talent_cap=cap));self.cap_check.configure(text=f'Raise talent cap to {cap}')
        if self.talent_auto.get():self.talent_apply()
    def talent_toggle(self):
        update_session(lambda d:d.update(talent_auto=bool(self.talent_auto.get())))
        if self.talent_auto.get():self.talent_apply()
        else:self.talent_restore()
    def build_rows(self):
        ttk=self.ttk;data=read();disabled=set(data.get('gui_disabled',[]))
        for col,title in enumerate(('Field','Live','Target'),start=1):ttk.Label(self.rows,text=title,foreground='#6b6b6b').grid(row=0,column=col,sticky='w',padx=6)
        for row,(name,offset,fmt,value) in enumerate(data['config']['targets'],start=1):
            var=self.tk.BooleanVar(value=name not in disabled);self.field_vars[name]=var
            check=ttk.Checkbutton(self.rows,variable=var,command=self.save_fields);check.grid(row=row,column=0)
            ttk.Label(self.rows,text=name,width=16).grid(row=row,column=1,sticky='w',padx=6)
            live=self.tk.StringVar(value='-');self.live_vars[name]=live
            ttk.Label(self.rows,textvariable=live,width=14).grid(row=row,column=2,sticky='w',padx=6)
            target=self.tk.StringVar(value=f'{value:g}')
            entry=ttk.Entry(self.rows,textvariable=target,width=11,state='disabled' if offset==20 else 'normal');entry.grid(row=row,column=3,sticky='w',padx=6)
            for event in ('<Return>','<FocusOut>'):entry.bind(event,lambda e,n=name,o=offset,f=fmt,v=target:self.commit_target(n,o,f,v))
            self.row_widgets[name]=(check,entry,offset)
    def build_stats(self,body):
        tk=self.tk;ttk=self.ttk
        canvas=tk.Canvas(body,highlightthickness=0,height=230,width=470);bar=ttk.Scrollbar(body,command=canvas.yview);canvas.configure(yscrollcommand=bar.set)
        bar.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        self.rows=ttk.Frame(canvas);canvas.create_window((0,0),window=self.rows,anchor='nw')
        self.rows.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        self.build_rows()
    def build_log(self,body):
        self.log=self.tk.Text(body,height=7,state='disabled',font=('Consolas',9));self.log.pack(fill='x')
    def build_settings(self,body):
        ttk=self.ttk;tk=self.tk
        self.updates=tk.BooleanVar(value=read().get('auto_update',True))
        ttk.Checkbutton(body,text=f'Check GitHub for updates at startup (version {VERSION})',variable=self.updates,command=lambda:update_session(lambda d:d.update(auto_update=bool(self.updates.get())))).pack(anchor='w')
    def locate_game(self):
        from tkinter import filedialog
        path=filedialog.askopenfilename(parent=self.root,title='Select AzerothSurvivors.exe (the game you downloaded)',filetypes=[('Game executable','AzerothSurvivors.exe'),('Executable','*.exe')])
        if not path:self.say('Game not located yet. Press Locate game when you are ready.');return
        try:configure(path,None)
        except Exception as error:self.say(f'Could not use that file: {error}');return
        self.say(f'Game set to {path}.');self.start()
    def toggle_run(self):
        if not configured():self.locate_game()
        elif self.alive:self.stop()
        else:self.start()
    def open_saves(self):
        try:os.startfile(save_folder()/'Saves')
        except Exception as error:self.say(f'Could not open the Saves folder: {error}')
    def max_talents_create(self):
        choice=self.class_choice.get();selection='all' if choice=='All classes' else next(c for c,n in CLASS_NAMES.items() if n==choice)
        def work():
            info=create_max_talent_save(selection);update_session(lambda d:d.update(max_talent_class=choice))
            total=sum(info['points'].values());what=f"{CLASS_NAMES[selection]}: {info['talents']} talents, {total} points" if selection!='all' else f"all {len(info['classes'])} classes: {info['talents']} talents"
            message=f"Saved {info['path'].name} ({what}). Now import it: Options > Saves > Import Save. (Apply the cap first.)"
            self.root.after(0,lambda:(self.say(message),self.say('Backup of your save: '+info['backup'].name),self.save_status.set(message)))
        self.run_task(work,'Creating the max-talent save (your current save is backed up first)...')
    def describe_talents(self,data):
        try:process,base,digest=talents_target(data)
        except Exception:return 'Start the game to use talents.'
        sites=data.get('learned_builds',{}).get(digest,{}).get('talent_sites')
        if not sites:return 'Press Apply: the cap spots for this game version are found from the game file first.'
        try:
            with ProcessCode(process.pid) as code:patched,total,cap=talent_state(code,base,sites)
        except Exception as error:return f'Talent spots could not be read ({error}).'
        return f'Cap raised to {cap} ({patched} of {total} places patched).' if patched else f'Not patched ({total} places known).'
    def talent_cap_value(self):
        try:cap=int(self.talent_cap.get())
        except ValueError:cap=0
        if not 1<=cap<=999:raise ValueError('Talent cap must be a whole number from 1 to 999.')
        return cap
    def talent_apply(self):
        def work():
            cap=self.talent_cap_value();count=talents_apply(cap)
            update_session(lambda d:d.update(talent_cap=cap));self.root.after(0,lambda:self.say(f'Talent cap set to {cap} ({count} spots patched together).'))
        self.run_task(work,'Applying the talent cap (the game pauses for a few milliseconds)...')
    def talent_restore(self):
        def work():
            count=talents_apply(TALENT_ORIGINAL);self.root.after(0,lambda:self.say(f'Talent cap restored to {TALENT_ORIGINAL} ({count} spots).'))
        self.run_task(work,'Restoring the talent cap...')
    def commit_target(self,name,offset,fmt,var):
        current=next((t[3] for t in read()['config']['targets'] if t[0]==name),None)
        try:value=parse_target(var.get(),offset,fmt)
        except ValueError as error:
            self.say(f'{name}: not changed ({error}).');var.set(f'{current:g}');return
        if value==current:var.set(f'{value:g}');return
        def save_it(d):
            for t in d['config']['targets']:
                if t[0]==name:t[3]=value
        update_session(save_it);var.set(f'{value:g}');self.say(f'{name} target set to {value:g}.')
        if auto_alive(read()):self.say('Restarting the watcher to use it...');self.start()
    def whats_new(self):
        from tkinter import messagebox
        try:
            text=(ROOT/'CHANGELOG.md').read_text(encoding='utf-8');parts=text.split('\n## ')
            notes='## '+parts[1].strip() if len(parts)>1 else text
        except OSError:notes='No changelog found.'
        messagebox.showinfo(f'{APP_NAME} {VERSION}',notes)
    def save_fields(self):
        off=sorted(n for n,v in self.field_vars.items() if not v.get())
        update_session(lambda d:d.update(gui_disabled=off));self.say('Field selection saved; the watcher picks it up within about 10 seconds.')
    def say(self,text):
        self.log.configure(state='normal');self.log.insert('end',time.strftime('%H:%M:%S  ')+text+'\n');self.log.see('end');self.log.configure(state='disabled')
        if getattr(sys,'frozen',False):print(f'[GUI] {text}',flush=True)
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
        if not configured():return self.locate_game()
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
            if not configured():
                self.alive=False;self.headline.set('Game not set up yet');self.sub.set('Locate AzerothSurvivors.exe to begin.')
                self.dot.itemconfigure(self.dot_id,fill='#b26a00');self.toggle_button.configure(text='Locate game',width=12)
                self.root.after(1000,self.refresh);return
            self.toggle_button.configure(width=8)
            data=read();monitor=data.get('auto_monitor',{});alive=auto_alive(data);self.alive=alive
            state=monitor.get('state','stopped') if alive else 'stopped'
            self.headline.set(STATE_TITLES.get(state,state.replace('_',' ').capitalize()));self.dot.itemconfigure(self.dot_id,fill=self.COLORS.get(state,'#6b6b6b'))
            level=monitor.get('level')
            self.sub.set(f"Level {level}" if level and alive and state in ('applying','waiting_progress') else describe_state(monitor if alive else dict(),state))
            self.toggle_button.configure(text='Stop' if alive else 'Start')
            if not self.busy and time.monotonic()>=getattr(self,'talent_next',0):
                self.talent_next=time.monotonic()+4;self.talent_status.set(self.describe_talents(data))
            if state!=self.last_state:self.say('State: '+state+(f" ({monitor.get('reason')})" if monitor.get('reason') else ''));self.last_state=state
            try:values=self.live_values(monitor) if alive else {}
            except Exception:values={}
            applied=monitor.get('applied') if alive else None
            for name,(check,entry,offset) in self.row_widgets.items():
                on=applied is None or name in applied
                check.configure(state='normal' if on else 'disabled');entry.configure(state='normal' if on and offset!=20 else 'disabled')
            for name,var in self.live_vars.items():
                v=values.get(name)
                if applied is not None and name not in applied:var.set('not on this build')
                else:var.set('-' if v is None else (f'{v:,.0f}' if abs(v)>=100 else f'{v:.3g}'))
            self.foot.set(f"Writes: {monitor.get('total_field_writes',0)}"+('' if alive else '  |  watcher not running'))
        except Exception as error:self.foot.set(f'Waiting... ({error})')
        self.root.after(1000,self.refresh)

def gui():
    # python.exe always owns a console window; relaunch under pythonw.exe so only the window shows.
    exe=Path(sys.executable);windowless=exe.with_name('pythonw.exe')
    if exe.name.lower()=='python.exe' and windowless.exists() and not os.environ.get('AZEROTH_GUI_CHILD'):
        subprocess.Popen([str(windowless),str(Path(__file__).resolve()),'gui'],cwd=ROOT,env=child_env(AZEROTH_GUI_CHILD='1'),
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW)
        return
    import tkinter as tk
    try:
        root=tk.Tk()
        icon=RESOURCE_ROOT/'assets'/'trainer-icon.ico'
        if icon.exists():root.iconbitmap(default=str(icon))
        if not SESSION.exists():seed_session()
        WatcherWindow(root);root.mainloop()
    except Exception:
        import traceback
        with (ROOT/'trainer.log').open('a',encoding='utf-8') as log:traceback.print_exc(file=log)

def self_test():
    import unittest
    class Tests(unittest.TestCase):
        def test_hook_stop_signal_is_shared_between_handles(self):
            suffix='hook-test-'+uuid.uuid4().hex
            with StopSignal(suffix) as first, StopSignal(suffix) as second:
                first.reset();self.assertFalse(first.wait(0))
                second.request();self.assertTrue(first.wait(0))
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
        def test_character_detected_without_a_configured_name(self):
            memory,proc,header,recipe,name=self.layout_fixture(d=792,o=968)
            class Region:
                address=0x100000;size=0x20000;is_readable=True;is_writable=True;is_executable=False
            memory.snapshot_memory_regions=lambda:[Region()]
            found=find_characters(memory,proc,dict(recipe,owner_offset=968))
            self.assertEqual([(c['name'],c['player_id']) for c in found],[('Nelbin',777)])
        def test_core_scan_finds_the_stat_block_by_its_field_pattern(self):
            BASE=0x100000;mem=bytearray(0x20000);H=0x100400
            def put(address,data):mem[address-BASE:address-BASE+len(data)]=data
            put(0x108000,b'Anyone');put(H,struct.pack('<3Q',8,0x108000,6))
            put(H+700,struct.pack('<8f',800.,2.5,.6,.24,2.3,1.3,30.,1.))
            put(0x10a000,struct.pack('<8f',53.4,39.3,0.,53.4,39.4,3.,53.4,39.3))  # position-like data: must be ignored
            class Memory:
                def read_bytes(_,address,size):
                    if not(BASE<=address and address+size<=BASE+len(mem)):raise OSError('unreadable')
                    return bytes(mem[address-BASE:address-BASE+size])
                def write_bytes(_,address,data):put(address,data)
                def snapshot_memory_regions(_):
                    class Region:address=BASE;size=len(mem);is_readable=True;is_writable=True;is_executable=False
                    return [Region()]
            found=core_scan(Memory())
            self.assertEqual(found,[(H+700,700)])
            self.assertEqual(core_live(Memory(),[H+700],.3),[])  # nothing recalculates it here, so it counts as a stale copy
            self.assertEqual(struct.unpack('<f',bytes(mem[H+704-BASE:H+708-BASE]))[0],2.5)  # and is restored
        def test_core_live_never_probes_candidates_by_writing(self):
            class Memory:
                def write_bytes(self,*args):raise AssertionError('Discovery must not write')
                def read_bytes(self,*args):raise AssertionError('Unverified candidates must not be probed')
            self.assertEqual(core_live(Memory(),[0x123000],0),[])
        def test_stats_only_layout_is_found_without_a_name(self):
            BASE=0x100000;H=0x100200;mem=bytearray(0x20000)
            def put(address,data):mem[address-BASE:address-BASE+len(data)]=data
            put(0x108000,b'Anyone');put(H,struct.pack('<3Q',8,0x108000,6))
            b=H+912;put(b-144,struct.pack('<I',13863894));put(b-104,struct.pack('<I',35))
            names=dict(zip(('Max Health','Damage','Crit','Crit Damage','Speed','Pickup','Experience','Armor','Regeneration','Area','Gold Bonus','Echo','Current Health'),(0,4,12,16,20,24,28,32,40,44,52,60,116)))
            for name,off in names.items():put(b+off,struct.pack('<f',500. if off in (0,116) else 1.5))
            put(b+56,struct.pack('<I',1))
            class Memory:
                def read_bytes(_,address,size):
                    if not(BASE<=address and address+size<=BASE+len(mem)):raise OSError('unreadable')
                    return bytes(mem[address-BASE:address-BASE+size])
                def snapshot_memory_regions(_):
                    class Region:address=BASE;size=len(mem);is_readable=True;is_writable=True;is_executable=False
                    return [Region()]
            class Proc:
                pid=1
                def create_time(_):return 5.
            targets=[(n,o,'f',1.) for n,o in names.items()]+[('Targets',56,'I',9)]
            recipe=dict(name_header_offset=432,stats_offset=432+912,no_owner=True,level_offset=-104,id_offset=-144,targets=targets)
            found=find_plain(Memory(),Proc(),recipe)
            self.assertEqual([(c['name'],c['level'],c['base']) for c in found],[('Anyone',35,b)])
            self.assertEqual(found[0]['kinds'],[])
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
        def update_archive(self,version,source=None,extra=None):
            import zipfile
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w') as archive:
                archive.writestr('Repo-main/game.py',source if source is not None else f"VERSION = '{version}'\nprint('x')\n")
                archive.writestr('Repo-main/README.md','new readme')
                archive.writestr('Repo-main/session.json','must never be installed')
                archive.writestr('Repo-main/../evil.txt','x')
            return buffer.getvalue()
        def test_update_installs_newer_versions_and_leaves_private_files(self):
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                folder=Path(folder);(folder/'game.py').write_text("VERSION = '1.0.0'\n");(folder/'session.json').write_text('{"private": true}')
                self.assertEqual(apply_update_zip(self.update_archive('1.2.0'),folder,'1.0.0'),'1.2.0')
                self.assertIn("1.2.0",(folder/'game.py').read_text())
                self.assertEqual((folder/'session.json').read_text(),'{"private": true}');self.assertEqual((folder/'README.md').read_text(),'new readme')
                self.assertIn("1.0.0",(folder/'.update_backup'/'1.0.0'/'game.py').read_text());self.assertFalse((folder.parent/'evil.txt').exists())
        def test_update_ignores_same_or_older_and_rejects_broken_code(self):
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                folder=Path(folder);(folder/'game.py').write_text("VERSION = '1.1.0'\n")
                self.assertIsNone(apply_update_zip(self.update_archive('1.1.0'),folder,'1.1.0'))
                self.assertIsNone(apply_update_zip(self.update_archive('1.0.9'),folder,'1.1.0'))
                with self.assertRaises(SyntaxError):apply_update_zip(self.update_archive('9.0.0',source="VERSION = '9.0.0'\ndef broken(:\n"),folder,'1.1.0')
                self.assertEqual((folder/'game.py').read_text(),"VERSION = '1.1.0'\n")
        def test_restart_relaunches_the_same_command_once(self):
            calls=[];real=(subprocess.Popen,sys.exit,sys.argv)
            subprocess.Popen=lambda args,**kw:calls.append((args,kw));sys.argv=['game.py','gui']
            def fake_exit(code=0):raise SystemExit(code)
            sys.exit=fake_exit
            try:
                with self.assertRaises(SystemExit):restart_after_update()
            finally:subprocess.Popen,sys.exit,sys.argv=real
            args,kw=calls[0]
            self.assertEqual(args[-1],'gui');self.assertEqual(kw['env']['AZEROTH_UPDATED'],'1')
            os.environ['AZEROTH_UPDATED']='1'
            try:self.assertFalse(auto_update())  # the flag prevents an update loop after a restart
            finally:del os.environ['AZEROTH_UPDATED']
        def test_source_update_changes_only_game_and_rejects_bad_code(self):
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                folder=Path(folder);game=folder/'game.py';profile=folder/'profile.example.json'
                game.write_text("VERSION = '1.0.0'\n",encoding='utf-8');profile.write_text('original',encoding='utf-8')
                self.assertEqual(apply_update_source(b"VERSION = '1.1.0'\n",folder,'1.0.0'),'1.1.0')
                self.assertEqual(game.read_text(),"VERSION = '1.1.0'\n")
                self.assertEqual((folder/'.update_backup'/'1.0.0'/'game.py').read_text(),"VERSION = '1.0.0'\n")
                self.assertEqual(profile.read_text(),'original')
                with self.assertRaises(SyntaxError):apply_update_source(b"VERSION = '2.0.0'\ndef broken(:\n",folder,'1.1.0')
                self.assertEqual(game.read_text(),"VERSION = '1.1.0'\n")
        def test_versions_compare_numerically_and_http_is_refused(self):
            self.assertTrue(parse_version('1.10.0')>parse_version('1.9.9'))
            with self.assertRaises(ValueError):http_get('http://example.com/x',10,1)
        def test_window_status_is_plain_english(self):
            self.assertIn('menu',describe_state({},'in_menu').lower())
            self.assertIn('applied',describe_state({},'applying'))
            self.assertTrue(describe_state({'trace_only':True},'applying').startswith('Observe only'))
            self.assertIn('Speed locked',describe_state({'reason':'Speed locked'},'blocked'))
            self.assertEqual(describe_state({},'something_new'),'Something new')
        def test_typed_targets_are_validated(self):
            self.assertEqual(parse_target('9,999',24,'f'),9999.0)
            self.assertEqual(parse_target('1000.99',28,'f'),1000.99)
            self.assertEqual(parse_target('9998',68,'I'),9998)
            for text,offset,fmt in (('abc',24,'f'),('-1',24,'f'),('1e9',24,'f'),('0.5',28,'f'),('2000000',28,'f'),('3',20,'f'),('1.5',52,'I'),('nan',24,'f')):
                with self.assertRaises(ValueError):parse_target(text,offset,fmt)
            self.assertEqual(parse_target('1.5',20,'f'),1.5)
        def talent_memory(self,offsets=(100,300,500)):
            memory=bytearray(1000);sites=[]
            for offset,form in zip(offsets,[bytes.fromhex('bf33000000'),bytes.fromhex('b833000000'),bytes.fromhex('41b933000000')]):
                memory[offset:offset+len(form)]=form;sites.append(dict(rva=offset,original=form.hex()))
            class Code:
                def __init__(inner):inner.writes=0
                def read(inner,address,size):return bytes(memory[address:address+size])
                def write_code(inner,address,data):memory[address:address+len(data)]=data;inner.writes+=1
            return Code(),sites,memory
        def test_talent_patch_bytes_replace_only_the_constant(self):
            self.assertEqual(talent_patch_bytes('bf33000000',150).hex(),'bf96000000')
            self.assertEqual(talent_patch_bytes('41b933000000',150).hex(),'41b996000000')
            for bad in (('bf34000000',150),('bf33000000',0),('bf33000000',1000)):
                with self.assertRaises(ValueError):talent_patch_bytes(*bad)
        def test_the_cap_sites_are_the_cmp_36b_loop_exits_and_nothing_else(self):
            loop_exit=bytes.fromhex('83c103'+'81fa6b030000'+'75dc'+'b833000000'+'c3')           # cmp edx,0x36b ; jne ; mov eax,51 ; ret
            rex_exit=bytes.fromhex('83c603'+'3d6b030000'+'75d1'+'41be33000000'+'31c9')           # cmp eax,0x36b ; jne ; mov r14d,51
            near_jcc=bytes.fromhex('81f96b030000'+'0f85d1000000'+'bf33000000')                    # long jne form
            decoys=bytes.fromhex('b833000000c3')+bytes.fromhex('81fa6b030000'+'75dc'+'b834000000')+bytes.fromhex('81fa6a030000'+'75dc'+'b833000000')+bytes.fromhex('488d05ba1c8704'+'ba33000000'+'c3')
            code=b'\x90'*4+loop_exit+b'\xcc\xcc'+rex_exit+b'\xcc\xcc'+near_jcc+b'\xcc'+decoys
            sites=find_talent_cap_sites(code,1000)
            self.assertEqual([(x['rva'],x['original']) for x in sites],[(1000+4+3+6+2,'b833000000'),(1000+4+len(loop_exit)+2+3+5+2,'41be33000000'),(1000+4+len(loop_exit)+2+len(rex_exit)+2+6+6,'bf33000000')])
        def test_site_count_is_sanity_checked(self):
            with self.assertRaises(RuntimeError):validate_site_count([])
            with self.assertRaises(RuntimeError):validate_site_count([dict(rva=i,original='bf33000000') for i in range(TALENT_SITE_LIMIT+1)])
            self.assertEqual(len(validate_site_count([dict(rva=1,original='bf33000000')])),1)
        def test_talent_apply_changes_every_site_inside_one_pause_and_restores(self):
            code,sites,memory=self.talent_memory();events=[]
            from contextlib import contextmanager
            @contextmanager
            def pause():
                events.append('pause');yield;events.append('resume')
            self.assertEqual(set_talent_cap(code,0,sites,150,pause),3)
            self.assertEqual(events,['pause','resume']);self.assertEqual(talent_state(code,0,sites),(3,3,150))
            set_talent_cap(code,0,sites,51,pause);self.assertEqual(talent_state(code,0,sites),(0,3,None))
        def test_talent_patch_never_pauses_or_writes_for_the_wrong_game_version(self):
            code,sites,memory=self.talent_memory();memory[500:506]=bytes(6);events=[]
            from contextlib import contextmanager
            @contextmanager
            def pause():
                events.append('pause');yield
            with self.assertRaises(RuntimeError):set_talent_cap(code,0,sites,150,pause)
            self.assertEqual(events,[]);self.assertEqual(code.writes,0)
        def test_a_failed_write_rolls_everything_back_and_still_resumes(self):
            code,sites,memory=self.talent_memory();events=[];snapshot=bytes(memory);original=code.write_code;calls=[]
            def failing(address,data):
                calls.append(address)
                if len(calls)==3:raise OSError('write failed')
                original(address,data)
            code.write_code=failing
            from contextlib import contextmanager
            @contextmanager
            def pause():
                events.append('pause')
                try:yield
                finally:events.append('resume')
            with self.assertRaises(OSError):set_talent_cap(code,0,sites,150,pause)
            self.assertEqual(bytes(memory),snapshot);self.assertEqual(events,['pause','resume'])
        def talent_records(self,trees=27,per_tree=13,stride=0xB0):
            records=[];rva=1000
            for tree in range(trees):
                for n in range(per_tree):
                    records.append(dict(rva=rva,id=f't{tree}_{n}',packed=(n%7)|((n%3)<<8)|(((tree+n)%5+1)<<16)));rva+=stride
                rva+=0x400  # a gap between trees
            return records
        def test_talent_runs_become_three_trees_per_class(self):
            table=assign_talent_classes(split_talent_runs(self.talent_records()))
            self.assertEqual(sorted(table),sorted(CLASS_ORDER));self.assertTrue(all(len(trees)==3 and all(len(tree)==13 for tree in trees) for trees in table.values()))
            self.assertEqual(table[1][0][0][0],'t0_0');self.assertEqual(table[11][2][-1][0],'t26_12')
            with self.assertRaises(RuntimeError):assign_talent_classes(split_talent_runs(self.talent_records(trees=26)))
            bad=self.talent_records()
            bad[5]['packed']=9<<16
            with self.assertRaises(RuntimeError):assign_talent_classes(split_talent_runs(bad))
        def test_save_envelope_round_trips_and_detects_tampering(self):
            key=b'azeroth-survivors/save/test-key'
            plain='stat|kills=1\ntalent|8|100|a:1\n\u00e9'
            sealed=rse1_seal(key,plain);self.assertTrue(sealed.startswith('RSE1:'));self.assertEqual(rse1_unseal(key,sealed),plain)
            self.assertNotEqual(rse1_seal(key,plain),sealed)  # fresh random header each time
            import base64
            raw=bytearray(base64.b64decode(sealed[5:]));raw[20]^=1
            with self.assertRaises(ValueError):rse1_unseal(key,'RSE1:'+base64.b64encode(bytes(raw)).decode())
            with self.assertRaises(ValueError):rse1_unseal(b'azeroth-survivors/save/other',sealed)
            with self.assertRaises(ValueError):rse1_unseal(key,'nope')
        def test_the_save_key_is_found_only_when_it_authenticates_the_save(self):
            key=SAVE_KEY_PREFIX+b've/v1'
            sealed=rse1_seal(key,'x')
            decoy=SAVE_KEY_PREFIX+b've/WRONG'+bytes(8)
            plain_blob=b'\x00'*30+decoy+b'\x00'*40+key+bytes(16)+b'tail'
            self.assertEqual(find_save_key(plain_blob,sealed),key)
            masked=b'junk'+bytes(x^0x36 for x in key+bytes(16))
            self.assertEqual(find_save_key(masked,sealed),key)
            with self.assertRaises(RuntimeError):find_save_key(decoy+b'\x00'*64,sealed)
        def test_editing_talents_touches_only_the_chosen_classes(self):
            table={8:[[('a',5),('b',3)],[('c',1)],[('d',2)]],2:[[('p',4)],[('q',4)],[('r',4)]]}
            plain='\n'.join(['stat|kills=1','talent|8|100|a:1','talent|2|200|p:2','talent|1|5|','spec|8|0|a:1|b:2|','spec|2|1|p:2||x:1','look|8|'])
            edited=edit_talents(plain,table,[8]).split('\n')
            self.assertEqual(edited[1],'talent|8|100|a:5,b:3,c:1,d:2');self.assertEqual(edited[4],'spec|8|0|a:5,b:3,c:1,d:2|b:2|')
            self.assertEqual([edited[i] for i in (0,2,3,5,6)],['stat|kills=1','talent|2|200|p:2','talent|1|5|','spec|2|1|p:2||x:1','look|8|'])
            both=edit_talents(plain,table,[8,2]).split('\n');self.assertEqual(both[2],'talent|2|200|p:4,q:4,r:4');self.assertEqual(both[5],'spec|2|1|p:4,q:4,r:4||x:1')
            with self.assertRaises(RuntimeError):edit_talents('talent|8|1|a:1',table,[2])
            self.assertEqual(max_alloc(table[8]),'a:5,b:3,c:1,d:2')
        def test_a_table_that_disagrees_with_the_save_is_refused(self):
            table={8:[[('a',5)],[('c',1)],[('d',2)]]}
            check_table_against_save('talent|8|1|a:2,c:1',table)
            with self.assertRaises(RuntimeError):check_table_against_save('talent|8|1|a:2,zzz:1',table)
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
            targets=[['stat'+str(off),off,'I' if off in INTEGER_OFFSETS else 'f',1] for off in sorted(AUTO_CORE_OFFSETS)]
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
            if getattr(sys,'frozen',False):self.skipTest('needs a Python interpreter for the child process')
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
        def test_first_run_seed(self):
            import tempfile
            global SESSION
            old = SESSION
            with tempfile.TemporaryDirectory() as folder:
                SESSION = Path(folder) / 'session.json'
                fake = Path(folder) / 'AzerothSurvivors.exe'
                try:
                    seed_session()
                    self.assertFalse(configured())
                    self.assertTrue(read()['config']['targets'])
                    fake.write_bytes(b'MZ')
                    configure(str(fake), None)
                    self.assertTrue(configured())
                    self.assertEqual(read()['native_identity_recipe']['name'], '<auto>')
                finally:
                    SESSION = old
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
    if '--no-update' in sys.argv:sys.argv.remove('--no-update');os.environ['AZEROTH_UPDATED']='1'
    try:
        if command=='gui' or (command=='auto' and '--background' in sys.argv):
            if auto_update():restart_after_update()
        if command not in ('setup','self-test','version','--version','--help','-h','help','gui','hook-current','hook-stop'):ensure_configured()
        if command in ('start','_worker','named-core','_named_core','source-apply','lifesteal-test','apply-once','apply-host','capture','numeric-watch','text-watch','inspect-copy','observe-swaps','discover') and auto_alive(read()):
            raise RuntimeError('Automatic watcher is active. Stop it before running a competing writer or debugger.')
        if command == 'hook-current': hook_current()
        elif command == 'hook-stop': hook_stop()
        elif command == 'status': status()
        elif command in ('--help','-h','help'):
            print('setup --game PATH --name NAME (first time) | Preferred: (no arguments) = window | --background = silent watcher | auto | new-game [--apply] | status | stop')
            print('Manual: resolve-name | discover-name --name <character> | named-core --seconds 600 --background')
            print('Experimental: hook-current --pid PID --base ADDRESS --name CHARACTER [--seconds N] | hook-stop')
            print('Diagnostics: talents apply|restore|status | max-talents [--class mage] | relayout | find-wallet | self-test | watch-self-test | source-apply | lifesteal-test | discover | learn | adopt | capture | text-scan | text-watch | numeric-scan | numeric-watch | inspect-copy | observe-swaps')
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
        elif command == 'talents':talents_command()
        elif command == 'max-talents':max_talents_command()
        elif command == 'setup':setup_command()
        elif command in ('version','--version'):print(f'{APP_NAME} {VERSION}')
        elif command == 'relayout':relayout()
        elif command == 'find-wallet':find_wallet()
        elif command == 'new-game':new_game()
        elif command in ('auto','_auto_worker'):auto_command()
        else: raise RuntimeError('Usage: python game.py status|start|stop|discover|learn|adopt|capture|self-test')
    except Exception as error:
        print(f'Not applied: {error}', file=sys.stderr)
        sys.exit(1)
