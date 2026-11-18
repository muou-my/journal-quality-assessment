"""Managed project/user/custom Skill installation. Never copies user runs."""
import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from build_backend import files,VERSION
NAME='journal-quality-assessment'


def install(target,*,upgrade=False,uninstall=False):
    supplied=Path(target).expanduser()
    if supplied.is_symlink():raise ValueError('Symlink installation targets are not managed by this installer')
    target=supplied.resolve()
    if target.name!=NAME or (ROOT.is_relative_to(target) and target!=ROOT):raise ValueError('Use a directory named journal-quality-assessment without enclosing the source')
    marker=target/'.jqa-install.json'
    if target==ROOT and not uninstall:
        if marker.is_file():return dict(installed=True,unchanged=True,version=VERSION)
        raise ValueError('Source directory is not a managed installation target')
    if uninstall:
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')).get('managed_by')!='jqa':raise ValueError('Refuse to remove an unmanaged directory')
        shutil.rmtree(target)  # Resolved exact managed target checked above; user data is always separate.
        return dict(uninstalled=True,user_data_deleted=False)
    hashes={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files()}
    if target.exists():
        if not marker.is_file():raise ValueError('Existing unmanaged Skill; refuse overwrite')
        previous=json.loads(marker.read_text(encoding='utf-8'))
        intact=all((target/n).is_file() and hashlib.sha256((target/n).read_bytes()).hexdigest()==h for n,h in previous['files'].items())
        if previous['files']==hashes and intact:return dict(installed=True,unchanged=True,version=VERSION)
        if not upgrade or not intact:raise ValueError('Use --upgrade for a clean managed installation; preserve edited copies yourself')
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=target.parent/('.jqa-'+uuid.uuid4().hex[:8]);stage.mkdir()
    for p in files():
        rel=p.relative_to(ROOT);dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
    (stage/'.jqa-install.json').write_text(json.dumps(dict(managed_by='jqa',version=VERSION,files=hashes),indent=2),encoding='utf-8')
    backup=None
    if target.exists():
        backups=target.parent.parent/'.jqa-install-backups';backups.mkdir(exist_ok=True)
        backup=backups/(NAME+'-'+uuid.uuid4().hex)
        # Move only the exact resolved, managed code target, to a sibling backup root.
        if not backup.resolve().is_relative_to(backups.resolve()):raise ValueError('Invalid backup destination')
        target.rename(backup)
    stage.rename(target)
    return dict(installed=True,version=VERSION,location=str(target),previous_code_backup=str(backup) if backup else None,user_data_modified=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group()
    g.add_argument('--project');g.add_argument('--user',action='store_true');g.add_argument('--target')
    p.add_argument('--upgrade',action='store_true');p.add_argument('--uninstall',action='store_true');a=p.parse_args()
    target=Path(a.target) if a.target else (Path(a.project).expanduser() if a.project else Path.home())/'.agents/skills'/NAME
    try:print(json.dumps(install(target,upgrade=a.upgrade,uninstall=a.uninstall),ensure_ascii=True,indent=2))
    except (ValueError,OSError) as exc:p.exit(2,str(exc)+'\n')

if __name__=='__main__':main()
