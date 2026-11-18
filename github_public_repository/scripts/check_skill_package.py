"""Dependency-free structural check for this package's simple YAML subset.

Not a replacement for the official quick_validate.py or a general YAML parser.
"""
import json
import re
from pathlib import Path


def check(root):
    root=Path(root).resolve()
    text=(root/'SKILL.md').read_text(encoding='utf-8')
    match=re.match(r'^---\n(.*?)\n---\n',text,re.S)
    if not match:raise ValueError('Missing YAML frontmatter')
    fields={}
    for line in match.group(1).splitlines():
        key,value=line.split(': ',1)
        if key in fields:raise ValueError('Duplicate frontmatter key')
        fields[key]=value
    if set(fields)!={'name','description'}:raise ValueError('Unexpected frontmatter keys')
    if fields['name']!=root.name or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*',fields['name']):raise ValueError('Invalid Skill name')
    if len(fields['name'])>64 or not 0<len(fields['description'])<=1024:raise ValueError('Invalid metadata length')
    # This package intentionally uses plain single-line values without YAML syntax.
    if any(c in fields['description'] for c in '\n:#{}[]&*!|>\'"%@`'):raise ValueError('Frontmatter requires a full YAML parser')
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if not (root/target).is_file():raise ValueError('Missing local reference: '+target)
    ui=(root/'agents/openai.yaml').read_text(encoding='utf-8')
    if '$'+fields['name'] not in ui:raise ValueError('UI default prompt must name Skill')
    for required in ('scripts/jqa.py','core/data_processing.py','core/task_manager.py','core/execution_logger.py','references/version_registry.json'):
        if not (root/required).is_file():raise ValueError('Missing executable resource: '+required)
    return dict(structural_check='passed',skill_name=fields['name'],repo_discovery_location=str(root),
        repo_scoped_layout=root.parent.name=='skills' and root.parent.parent.name=='.agents',
        explicit_invocation='$'+fields['name'],official_validator_executed=False,
        live_desktop_catalog_refresh_verified=False)


if __name__=='__main__':
    print(json.dumps(check(Path(__file__).resolve().parents[1]),ensure_ascii=True,indent=2))
