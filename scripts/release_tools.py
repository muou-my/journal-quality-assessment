"""Allowlist packaging and sensitive-content checks; no publishing/network."""
import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from build_backend import files,build_wheel,build_sdist,VERSION


def scan(root):
    root=Path(root);issues=[];count=0
    forbidden=re.compile(r'(?:[A-Z]:[\\/]|/Users/[^\s/]+|/home/[^\s/]+|single98_\d|REF2021_Final|real-pilot-\d|sk-[A-Za-z0-9]{24,})')
    for p in root.rglob('*'):
        if '.git' in p.relative_to(root).parts:continue  # Git metadata is never public source.
        if not p.is_file() or '__pycache__' in p.parts:continue
        count+=1;rel=p.relative_to(root).as_posix()
        if p.suffix in ('.sqlite3','.db','.xlsx','.xls','.pem') or any(x in p.parts for x in ('.local','pilot_runs','api_runs')):
            issues.append(dict(file=rel,reason='Sensitive runtime/binary path'))
        try:text=p.read_text(encoding='utf-8-sig')
        except UnicodeError:issues.append(dict(file=rel,reason='Unexpected binary/non-UTF8 file'));continue
        # The scanner source itself spells detector patterns, not personal data.
        if p.name!='release_tools.py' and forbidden.search(text):issues.append(dict(file=rel,reason='Personal path/run marker or credential-shaped token'))
        for match in re.finditer(r'"api_key"\s*:\s*"([^"]+)"',text):
            if match.group(1) not in ('YOUR_KEY','offline-secret','offline-test-secret-do-not-log'):
                issues.append(dict(file=rel,reason='Literal credential field'))
    return dict(passed=not issues,files_scanned=count,issues=issues,limitation='Heuristic scan plus allowlist; publisher must review source ownership and unexpected additions')


def build(output):
    output=Path(output).resolve()
    if output==ROOT or output.is_relative_to(ROOT):raise ValueError('Release artifacts must be outside the source Skill')
    output.mkdir(parents=True,exist_ok=False)
    result=scan(ROOT)
    if not result['passed']:raise ValueError('Sensitive scan failed: '+json.dumps(result))
    source_files=files()
    build_wheel(output);build_sdist(output)
    with zipfile.ZipFile(output/f'journal-quality-assessment-{VERSION}.zip','x',compression=zipfile.ZIP_DEFLATED) as z:
        for p in source_files:z.write(p,'journal-quality-assessment/'+p.relative_to(ROOT).as_posix())
    plugin=output/'plugin';skill=plugin/'skills/journal-quality-assessment';skill.mkdir(parents=True)
    for p in source_files:
        dest=skill/p.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
    manifest=dict(**{'$schema':'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'},name='journal-quality-assessment',version=VERSION,
                  description='REF-inspired article and journal evaluation using local Python and user-owned model credentials')
    (plugin/'plugin.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    compat=plugin/'.codex-plugin';compat.mkdir();(compat/'plugin.json').write_text(json.dumps(dict(name=manifest['name'],version=VERSION,
                  description=manifest['description'],skills='./skills/'),indent=2)+'\n',encoding='utf-8')
    with zipfile.ZipFile(output/f'journal-quality-assessment-plugin-{VERSION}.zip','x',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(plugin.rglob('*')):
            if p.is_file():z.write(p,p.relative_to(plugin).as_posix())
    (output/'scan.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    checksum=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in sorted(output.iterdir()) if p.is_file())
    (output/'SHA256SUMS').write_text(checksum,encoding='utf-8')
    return dict(version=VERSION,packaged=True,plugin_submission='not submitted; portal review and platform execution testing pending',scan=result)


if __name__=='__main__':
    p=argparse.ArgumentParser();s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('scan');a.add_argument('--root',default=str(ROOT))
    a=s.add_parser('build');a.add_argument('--output',required=True)
    a=p.parse_args();result=scan(a.root) if a.command=='scan' else build(a.output)
    print(json.dumps(result,ensure_ascii=True,indent=2));sys.exit(0 if result.get('passed',True) else 2)
