"""Small installed launcher and source recovery, independent of editable code.

The installer copies this module and the recovery coordinator into its private
control directory. Undo still works when the editable workspace will not load.
No snapshots or installation records belong in the public source tree.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser

EXCLUDED = {'.git', 'node_modules', '__pycache__', '.venv', 'test-results', 'playwright-report', '.ux46-install-id'}


def read(path):
    if path.is_symlink() or path.stat().st_size > 4*1024*1024: raise ValueError('Invalid installation record')
    return json.loads(path.read_text())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, indent=2); out.write('\n'); out.flush(); os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def inventory(source):
    rows = {}; size = 0
    if source.is_symlink() or not source.is_dir(): raise ValueError('Installed source is missing or is a symbolic link')
    for directory, folders, files in os.walk(source):
        folders[:] = sorted(n for n in folders if n not in EXCLUDED)
        for name in folders + sorted(files):
            if name in EXCLUDED: continue
            path = Path(directory)/name
            if path.is_symlink(): raise ValueError('Source recovery refuses symbolic links; keep linked data outside the source')
            if path.is_dir(): continue
            if not path.is_file(): raise ValueError('Source contains a non-regular file')
            size += path.stat().st_size
            if len(rows) >= 5000 or size > 128*1024*1024: raise ValueError('Source exceeds the recovery-point limit')
            rows[str(path.relative_to(source))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows


def installed(root):
    manifest = read(root/'installation.json')
    if manifest.get('schema_version') != 1 or Path(manifest['state']) != root or not Path(manifest['source']).is_absolute(): raise ValueError('Installation identity does not match')
    source=Path(manifest['source']).resolve()
    if source==root or source in root.parents or root in source.parents: raise ValueError('Editable source and private data must stay separate')
    return manifest


@contextlib.contextmanager
def locked(root):
    (root/'control').mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root/'control/source.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def checkpoint(root, source, label='Before customization', identity=None):
    identity = identity or uuid.uuid4().hex
    if not identity.isalnum() or not 8 <= len(identity) <= 64: raise ValueError('Invalid recovery point ID')
    destination = root/'recovery-points'/identity
    if destination.exists():
        saved = read(destination/'point.json')
        if saved['source'] != str(source): raise ValueError('Recovery point belongs to another source')
        return saved
    files = inventory(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.point-',dir=destination.parent) as temporary:
        stage = Path(temporary)
        for name in files:
            target=stage/'source'/name; target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source/name,target)
        if inventory(stage/'source') != files or inventory(source) != files: raise ValueError('Source changed during the recovery point; try again when editing has stopped')
        point = {'id':identity,'label':label,'source':str(source),'created_at':time.time(),'files':files}
        save(stage/'point.json',point)
        os.rename(stage,destination)
    save(root/'control/latest-source-point.json',{'id':identity})
    return point


def source_status(root):
    manifest=installed(root); source=Path(manifest['source']); current=inventory(source); base=manifest['files']
    changes={'changed':sorted(k for k in base.keys() & current.keys() if base[k] != current[k]),
             'added':sorted(current.keys()-base.keys()),'missing':sorted(base.keys()-current.keys())}
    return {'source':str(source),'release':manifest['release'],'clean':not any(changes.values()),
            'update_conflicts':any(changes.values()),**changes}


def coordinator(manifest):
    path=Path(manifest['control'])/'ux46_recovery.py'
    spec=importlib.util.spec_from_file_location('installed_ux46_recovery',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.SOURCE=Path(manifest['source'])
    return module


def lifecycle(root, action, open_browser=False):
    with locked(root):
        return _lifecycle(root, action, open_browser)


def _lifecycle(root, action, open_browser=False):
    manifest=installed(root); recovery=coordinator(manifest)
    record=recovery.read_json(root/'control/console.json',{})
    config=read(root/'config.json'); port=record.get('port',config.get('port',8877))
    manager=recovery.ServiceManager(root,time.monotonic()+35)
    if record:
        if record.get('root') != str(root) or record.get('source') != manifest['source']: raise ValueError('Console belongs to another installation')
        service={**record,'endpoint':{'port':port}}
        status=manager.inspect(service)
        if status.get('replaced'): raise ValueError('Registered process identity changed; nothing was signalled')
    else:
        service={'id':'console','manager':'process','python':sys.executable,'port':port,'endpoint':{'port':port}}
        status={'running':False}
    if action=='stop':
        state=manager.stop(service) if record else 'stopped'
    elif status['running']:
        # A process alone is not a ready workspace.
        try:
            http_status, _=recovery.Client({'port':port}).request('GET','/api/bootstrap')
            state='ready' if http_status==200 else 'unavailable'
        except (OSError,ValueError): state='unavailable'
    else: state=manager.start(service)
    result={'state':state,'url':f'http://127.0.0.1:{port}/'}
    if open_browser and state=='ready': webbrowser.open(result['url'])
    return result


def restore(root, point_id=None):
    manifest=installed(root); source=Path(manifest['source'])
    if not point_id: point_id=read(root/'control/latest-source-point.json')['id']
    if not point_id.isalnum() or not 8 <= len(point_id) <= 64: raise ValueError('Invalid recovery point ID')
    point=root/'recovery-points'/point_id; saved=read(point/'point.json')
    if saved.get('source') != str(source) or inventory(point/'source') != saved.get('files'): raise ValueError('Recovery point failed identity or checksum verification')
    recovery=coordinator(manifest); record=recovery.read_json(root/'control/console.json',{})
    if record and recovery.process_identity(record.get('pid')):
        raise ValueError('Stop this installation first with ux46 stop, then run undo. Its conversations and drafts are kept.')
    # A saved undo transaction is resumed before considering a new one. Each
    # previous source stays beside the destination, on the same filesystem.
    journal=root/'control/source-undo.json'
    job=read(journal) if journal.exists() else None
    if job and job['state'] != 'complete':
        if job['point'] != point_id: raise ValueError('Finish the pending undo before selecting another point')
    else:
        suffix=uuid.uuid4().hex
        job={'point':point_id,'state':'preparing','stage':str(source.parent/('.ux46-restore-'+suffix)),
             'backup':str(source.parent/('.ux46-before-undo-'+suffix)), 'before':inventory(source)}
        save(journal,job)
    stage=Path(job['stage']); backup=Path(job['backup'])
    if not stage.exists() and job['state']=='preparing':
        shutil.copytree(point/'source',stage)
        (stage/'.ux46-install-id').write_text(manifest['id'])
    if job['state']=='preparing':
        if inventory(stage)!=saved['files']: raise ValueError('Prepared undo failed verification')
        job['state']='prepared';save(journal,job)
    if not backup.exists():
        if inventory(source)!=job['before']: raise ValueError('Source changed while preparing undo; the existing source was kept')
        os.rename(source,backup)
    if not source.exists(): os.rename(stage,source)
    if inventory(source)!=saved['files']: raise ValueError('Undo result needs inspection; the previous source is preserved')
    for name in EXCLUDED- {'.ux46-install-id'}:
        if (backup/name).exists() and not (source/name).exists(): os.rename(backup/name,source/name)
    job['state']='complete';save(journal,job)
    return {'state':'restored','point':point_id,'previous_source':str(backup),'source':str(source)}


def prepare_customization(root, identity=None):
    manifest=installed(root);source=Path(manifest['source'])
    with locked(root):
        point=checkpoint(root,source,identity=identity)
        project=root/'projects/ux46-workspace';project.mkdir(parents=True,exist_ok=True,mode=0o700)
        metadata={'schema_version':1,'id':'ux46-workspace','name':'My UX46 workspace','visibility':'private','sources':[]}
        if not (project/'project.json').exists():save(project/'project.json',metadata)
        registry=read(root/'registry.json')
        existing=[p for p in registry['projects'] if p['id']=='ux46-workspace']
        if existing and any(p['root']!=str(project) for p in existing): raise ValueError('The workspace project name is already used by another project')
        if not existing:
            registry['projects'].append({'id':'ux46-workspace','name':'My UX46 workspace','root':str(project)})
            save(root/'registry.json',registry)
    return {'source':str(source),'project_id':'ux46-workspace','recovery_point':point['id'],
            'undo':['ux46','undo','--point',point['id']],
            'message':'A recovery point is saved. Describe one visible change, test it, and keep or undo it.'}


def main(argv=None, root=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    root=Path(root or os.environ.get('UX46_HOME','~/.ux46')).expanduser().resolve()
    commands={'start','stop','open','customize','snapshot','undo','source-status'}
    if not argv or argv[0] not in commands:
        manifest=installed(root)
        os.execv(sys.executable,[sys.executable,str(Path(manifest['source'])/'tools/ux46'),*argv])
    parser=argparse.ArgumentParser(description='Open, shape and restore your own UX46 installation')
    parser.add_argument('command',choices=sorted(commands));parser.add_argument('--json',action='store_true')
    parser.add_argument('--point');parser.add_argument('--label',default='Saved source')
    args=parser.parse_args(argv)
    try:
        if args.command in {'start','open','stop'}:result=lifecycle(root,args.command,args.command=='open')
        elif args.command=='source-status':result=source_status(root)
        elif args.command=='customize':result=prepare_customization(root)
        else:
            with locked(root):
                result=restore(root,args.point) if args.command=='undo' else checkpoint(root,Path(installed(root)['source']),args.label)
        if args.json:print(json.dumps(result,indent=2))
        elif args.command=='customize':
            print(result['message']);print('Editable project: '+result['source']);print('Undo: ux46 stop, then '+' '.join(result['undo']))
            config=read(root/'config.json')
            if sys.stdin.isatty() and config.get('agent') in {'codex','claude'}:
                return subprocess.call([config.get('cli') or config['agent']],cwd=result['source'])
            print('Open your native agent in this folder, or use Customize my workspace in UX46.')
        elif args.command=='source-status':
            print('Source matches '+result['release'] if result['clean'] else 'Local changes found. An update needs review; nothing was replaced.')
            for key in ('changed','added','missing'):
                for name in result[key]:print(key+': '+name)
        else:print(json.dumps({k:v for k,v in result.items() if k!='files'},indent=2))
        return 0 if result.get('state','ready') in {'ready','stopped','restored'} else 1
    except (OSError,ValueError,KeyError) as error:
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())
