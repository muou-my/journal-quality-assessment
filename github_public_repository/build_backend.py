"""Small dependency-free PEP517 wheel backend; packages only allowlisted sources."""
import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
NAME='journal_quality_assessment_skill'
VERSION='0.2.0'
TOP_FILES={'README_CN.md','CONTRIBUTING.md','installation.md','usage.md','methodology.md','RELEASE_NOTES.md','SKILL.md','README.md','README_EN.md','LICENSE','NOTICE','CHANGELOG.md','VERSION',
 'pyproject.toml','requirements.txt','.gitignore','.gitattributes','build_backend.py','CITATION.cff','SECURITY.md','PRIVACY.md','__init__.py','__main__.py'}
TOP_DIRS={'core','scripts','pilot','references','assets','agents','tests','examples','config','jqa_skill','.github'}


def files():
    return [p for p in sorted(ROOT.rglob('*')) if p.is_file() and
        (p.relative_to(ROOT).parts[0] in TOP_DIRS or p.relative_to(ROOT).as_posix() in TOP_FILES) and
        '__pycache__' not in p.parts and p.suffix not in ('.pyc','.sqlite3','.db')]


def build_wheel(wheel_directory,config_settings=None,metadata_directory=None):
    name=f'{NAME}-{VERSION}-py3-none-any.whl';destination=Path(wheel_directory);destination.mkdir(parents=True,exist_ok=True)
    path=destination/name
    if path.exists():raise FileExistsError('Do not overwrite a release wheel')
    info=f'{NAME}-{VERSION}.dist-info';payload={}
    for p in files():
        rel=p.relative_to(ROOT).as_posix()
        if rel.startswith('jqa_skill/'):continue
        payload['jqa_skill/'+rel]=p.read_bytes()
    bootstrap=ROOT/'jqa_skill' if (ROOT/'jqa_skill').is_dir() else ROOT
    for namepart in ('__init__.py','__main__.py'):payload['jqa_skill/'+namepart]=(bootstrap/namepart).read_bytes()
    payload[info+'/METADATA']=f'Metadata-Version: 2.1\nName: journal-quality-assessment-skill\nVersion: {VERSION}\nSummary: Portable REF-inspired journal assessment\nRequires-Python: >=3.10\nLicense: MIT\n\n'.encode()
    payload[info+'/WHEEL']=b'Wheel-Version: 1.0\nGenerator: jqa-stdlib-backend\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
    payload[info+'/entry_points.txt']=b'[console_scripts]\njqa = jqa_skill:main\n'
    record=io.StringIO();writer=csv.writer(record,lineterminator='\n')
    for n,data in payload.items():writer.writerow([n,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode(),len(data)])
    writer.writerow([info+'/RECORD','','']);payload[info+'/RECORD']=record.getvalue().encode()
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for n,data in payload.items():z.writestr(n,data)
    return path.name


def get_requires_for_build_wheel(config_settings=None):return []
def get_requires_for_build_sdist(config_settings=None):return []
def build_sdist(sdist_directory,config_settings=None):
    dest=Path(sdist_directory);dest.mkdir(parents=True,exist_ok=True);name=f'{NAME}-{VERSION}.tar.gz'
    with tarfile.open(dest/name,'x:gz') as tar:
        for p in files():tar.add(p,arcname=f'{NAME}-{VERSION}/'+p.relative_to(ROOT).as_posix())
    return name
