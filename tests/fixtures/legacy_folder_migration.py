"""隔离容器内迁移；主服务停止后执行。小型日志保留，音乐不整库备份。"""
import hashlib
import json
import os
import sqlite3
import uuid
import unicodedata
from pathlib import Path

from app import media
from app.config import Config
from app.db import Database,dumps,now
from app.engine import Engine

root=Path('/music');data=Path('/data');plan=json.loads(Path('/plan/迁移清单.json').read_text())
log=data/'migration-v1.2';log.mkdir(exist_ok=True)
rows=plan['audio']+plan['auxiliary'];audio_paths={r['path'] for r in plan['audio']}
c=sqlite3.connect(data/'music.sqlite3');c.row_factory=sqlite3.Row
assert c.execute("select value from settings where key='queue_paused'").fetchone()[0]=='true'
assert c.execute("select count(*) from jobs where state='running'").fetchone()[0]==0
assert not list((data/'staging').glob('*/intent.json')), '先恢复已准备发布的任务，再迁移'
oldpaths={r['path'] for r in rows}

def stamp(p,r):
    return p.is_file() and p.stat().st_size==r['size'] and p.stat().st_mtime_ns==r['mtime_ns']

def temp(r):
    old=root/r['path'];return old.with_name('.liusheng-v120-'+hashlib.sha256(r['path'].encode()).hexdigest()[:20]+old.suffix)

# Preflight before any changes; symlinks outside root are never followed.
for r in rows:
    src=root/r['path'];dst=root/r['new_path'];tmp=temp(r)
    assert src.resolve().is_relative_to(root) and dst.resolve().is_relative_to(root)
    assert stamp(src,r) or stamp(tmp,r) or stamp(dst,r), r['path']
    if src!=dst and dst.exists() and r['new_path'] not in oldpaths:
        assert stamp(dst,r) and not src.exists(), '目标已被占用:'+r['new_path']
print(json.dumps({'phase':'preflight','files':len(rows)}),flush=True)
# All moving files leave their original names first, so rename cycles cannot overwrite.
for i,r in enumerate(rows):
    src=root/r['path'];dst=root/r['new_path'];tmp=temp(r)
    if src==dst:continue
    if stamp(tmp,r):continue
    if not src.exists() and stamp(dst,r):continue
    assert stamp(src,r),r['path']
    assert not tmp.exists()
    src.rename(tmp)
    if i%500==0:print(json.dumps({'phase':'staging_names','count':i}),flush=True)
results=[]
for i,r in enumerate(rows):
    src=root/r['path'];dst=root/r['new_path'];tmp=temp(r)
    if src!=dst and tmp.exists():
        dst.parent.mkdir(exist_ok=True)
        assert not dst.exists(),'目标存在:'+str(dst)
        inode=tmp.stat().st_ino
        tmp.rename(dst)
        assert dst.stat().st_ino==inode
    assert stamp(dst,r),r['new_path']
    if 'inode' in r:assert dst.stat().st_ino==r['inode']
    results.append({'old':r['path'],'new':r['new_path'],'size':r['size'],'same_inode':True})
    if i%500==0:print(json.dumps({'phase':'move_verified','count':i}),flush=True)
(log/'moves.json').write_text(dumps(results))
# Update persisted relative paths before exposing the wider root to the app.
if not c.execute("SELECT 1 FROM settings WHERE key='folder_migration'").fetchone():
    c.execute('BEGIN IMMEDIATE')
    by_old={r['path']:r for r in plan['audio']}
    for a in c.execute('SELECT id,path FROM assets').fetchall():
        old='示例用户喜欢的音乐/'+a['path']
        new=by_old[old]['new_path'] if old in by_old else old
        c.execute('UPDATE assets SET path=? WHERE id=?',(new,a['id']))
    for j in c.execute("select id,payload,kind from jobs where kind in ('acquire','import')").fetchall():
        payload=json.loads(j['payload']);key=payload.get('key')
        ps=[p for p in c.execute('select id,snapshot from playlists').fetchall() if any(t['key']==key for t in json.loads(p['snapshot']))]
        payload['playlist_ids']=[p['id'] for p in ps] or []
        c.execute('UPDATE jobs SET payload=? WHERE id=?',(dumps(payload),j['id']))
    for j in c.execute('SELECT id,result FROM jobs WHERE result IS NOT NULL').fetchall():
        result=json.loads(j['result'])
        old='示例用户喜欢的音乐/'+result.get('path','')
        if old in plan['pathmap']:
            result['previous_path']=result['path'];result['path']=plan['pathmap'][old]
            c.execute('UPDATE jobs SET result=? WHERE id=?',(dumps(result),j['id']))
    c.execute("INSERT INTO settings VALUES ('folder_migration',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(dumps('1.2.0'),))
    c.commit();(log/'database-migrated').write_text('done')
c.close()
db=Database(data/'music.sqlite3')
assert db.get('queue_paused')
db.execute('UPDATE playlists SET folder=? WHERE id=?',('示例用户喜欢的音乐','netease:10001'))
db.execute('UPDATE playlists SET folder=? WHERE id=?',('示例账号喜欢的音乐','netease:10002'))
db.set('default_playlist', 'local:' + hashlib.sha256('手动收藏'.encode()).hexdigest()[:20])
# Rewrite provenance paths and bootstrap mapping, keeping source history.
for p in (data/'records').glob('*.json'):
    try:
        d=json.loads(p.read_text());old=d.get('path')
        full='示例用户喜欢的音乐/'+old if old else None
        if full in plan['pathmap']:
            d['path']=plan['pathmap'][full];media.atomic_bytes(p,dumps(d).encode())
    except (ValueError,TypeError):pass
seed=data/'bootstrap.json'
if seed.exists():
    original_seed=log/'bootstrap-original.json'
    if not original_seed.exists():original_seed.write_bytes(seed.read_bytes())
    d=json.loads(original_seed.read_text());d['files']={plan['pathmap'].get('示例用户喜欢的音乐/'+k,'示例用户喜欢的音乐/'+k):v for k,v in d.get('files',{}).items()};media.atomic_bytes(seed,dumps(d).encode())
# Preserve existing user M3U ordering while translating their old relative entries.
for r in plan['auxiliary']:
    if Path(r['path']).suffix.lower() not in ('.m3u','.m3u8'):continue
    p=root/r['new_path'];content=p.read_text(encoding='utf-8-sig');rewritten=[]
    for line in content.splitlines():
        if not line or line.startswith('#'):rewritten.append(line);continue
        old=str(Path(os.path.normpath(str(Path(r['path']).parent/line))))
        target=plan['pathmap'].get(old)
        if target is None:
            matches=[v for k,v in plan['pathmap'].items() if unicodedata.normalize('NFC',k)==unicodedata.normalize('NFC',old)]
            target=matches[0] if len(matches)==1 else None
        rewritten.append(os.path.relpath(root/target,p.parent) if target else line)
    media.atomic_bytes(p,('\n'.join(rewritten)+'\n').encode())
# Remove only now-empty nested directories after successful relocation.
parents=set()
for row in rows:
    p=(root/row['path']).parent
    while p!=root and len(p.relative_to(root).parts)>1:
        parents.add(p);p=p.parent
for p in sorted(parents,key=lambda p:len(p.parts),reverse=True):
    try:p.rmdir()
    except OSError:pass
print(json.dumps({'phase':'catalog'}),flush=True)
e=Engine(Config(data,root,Path('/inbox'),Path('/app/web'),False),db,None)
# The read-only inventory already parsed every file. Seed metadata without repeating disk reads.
prior_rows={r['path']:r for r in db.rows('SELECT * FROM assets')}
values=[]
for r in plan['audio']:
    p=root/r['new_path'];st=p.stat();prior=prior_rows.get(r['new_path'])
    if r.get('error'):
        continue
    tags=r['suggested_tags'].copy();old=json.loads(prior['data']) if prior else {}
    tags.update(locked=old.get('locked',False),source=old.get('source','本地文件'),
                lyrics_status='同步歌词' if tags['lyrics_synced'] else '缺少同步歌词',
                sidecar_mtime_ns=p.with_suffix('.lrc').stat().st_mtime_ns if p.with_suffix('.lrc').exists() else 0)
    aid=prior['id'] if prior else uuid.uuid5(uuid.NAMESPACE_URL,'liusheng:'+r['path']).hex
    values.append((aid,r['new_path'],r['track_key'],dumps(tags),st.st_size,st.st_mtime_ns,prior['sha256'] if prior else None,1,now()))
with db.connect() as connection:
    connection.executemany('INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET data=excluded.data,size=excluded.size,mtime_ns=excluded.mtime_ns,present=1',values)
for folder in sorted({Path(r['new_path']).parts[0] for r in plan['audio']}):e.local_playlist(folder)
db.set('last_scan',{'at':now(),'count':len(plan['audio']),'errors':plan['summary']['errors'],'method':'verified inventory migration'})
# Existing music present in both subscribed playlists gets independent folder copies.
for p in db.rows("SELECT * FROM playlists WHERE platform!='local'"):
    for t in json.loads(p['snapshot']):
        a=e.asset_for(t['key'])
        if a:e.copy_to_playlist(a,p['id'])
e.export_all()
print(json.dumps({'phase':'complete','assets':db.one('SELECT count(*) n FROM assets WHERE present=1')['n'],'playlists':db.rows('SELECT id,name,folder FROM playlists')},ensure_ascii=False),flush=True)
