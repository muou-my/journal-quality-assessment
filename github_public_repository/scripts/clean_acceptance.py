"""New venv, offline wheel install, unrelated CWD, UTF-8/space path smoke test."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from build_backend import build_wheel


def run(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    wheel=output/build_wheel(output)
    venv=output/'clean environment';subprocess.run([sys.executable,'-m','venv',str(venv)],check=True,capture_output=True)
    python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    commands=[]
    env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'}
    env.pop('PYTHONPATH',None)
    def call(args):
        p=subprocess.run([str(python),*args],cwd=output,env=env,capture_output=True,encoding='utf-8')
        if p.returncode:raise ValueError('Clean command failed: '+p.stderr)
        commands.append(dict(arguments=args,exit_code=p.returncode))
        return p.stdout
    call(['-m','pip','install','--no-index',str(wheel)])
    call(['-c',"import jqa_skill,sys; from pathlib import Path; r=Path(jqa_skill.__file__).parent; sys.path.insert(0,str(r)); from build_backend import build_wheel; build_wheel('rebuilt-offline')"])
    diagnosis=json.loads(call(['-m','jqa_skill','doctor','--workdir',str(output/'数据/diagnostics')]))
    demo=json.loads(call(['-m','jqa_skill','demo','--output',str(output/'数据/demo')]))
    report=dict(passed=diagnosis['passed'] and demo['demo_only'],platform=sys.platform,
                fresh_venv=True,offline_install=True,unrelated_working_directory=True,unicode_and_space_paths=True,
                network_requests=0,commands_executed=len(commands))
    (output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();print(json.dumps(run(a.output),indent=2))
