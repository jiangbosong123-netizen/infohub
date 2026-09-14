"""Validate this documentation package without importing InfoHub or accessing production.

Requires PyYAML, jsonschema and openapi-spec-validator in a disposable validation env.
Usage: python docs/spec/evidence/validate_docs.py
"""
import ast
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / 'docs/spec'
contract = yaml.safe_load((DOCS / 'openapi.yaml').read_text())
validate_spec(contract)
schemas = contract['components']['schemas']
example_count = 0
for name, schema in schemas.items():
    Draft202012Validator.check_schema(schema)
    wrapper = {'$ref': '#/components/schemas/' + name, 'components': contract['components']}
    validator = Draft202012Validator(wrapper, format_checker=FormatChecker())
    for sample in schema.get('examples', []):
        validator.validate(sample)
        example_count += 1

# Prose examples must satisfy the same contract as the machine schemas.
nlp_text = (DOCS / 'NLP_AND_EVALUATION.md').read_text()
impact = json.loads(re.search(r'```json\n(.*?)\n```', nlp_text, flags=re.S).group(1))
Draft202012Validator({'$ref':'#/components/schemas/ImpactOutput', 'components':contract['components']}).validate(impact)
api_blocks=re.findall(r'```json\n(.*?)\n```',(DOCS/'API_CONTRACT.md').read_text(),flags=re.S)
for name,block in zip(['Item','ItemListResponse','Change'],api_blocks):
    Draft202012Validator({'$ref':'#/components/schemas/'+name,'components':contract['components']},
        format_checker=FormatChecker()).validate(json.loads(block))
horizon = Draft202012Validator(schemas['Horizon'])
assert not horizon.is_valid({'bucket':'quarter','min_days':1,'max_days':90})
assert horizon.is_valid({'bucket':'quarter','min_days':8,'max_days':90})
assert not Draft202012Validator(schemas['Confidence']).is_valid({
    'raw_confidence':0.7,'calibrated_confidence':0.7,'calibration_version':None,'uncertainty_reason':None})
for path, operations in contract['paths'].items():
    for method, op in operations.items():
        assert op['security'] and op['x-required-scopes'], path
        assert len({(p['in'],p['name']) for p in op.get('parameters',[])}) == len(op.get('parameters',[])), path
        for code, response in op['responses'].items():
            if code.startswith('2'):
                assert response['content']['application/json']['schema'], path

def anchors(text):
    slugs=set()
    for heading in re.findall(r'^#{1,6}\s+(.+)$',text,flags=re.M):
        heading=heading.lower().replace('`','')
        slug=''.join(c for c in heading if c.isspace() or c in '-_' or unicodedata.category(c)[0] in 'LN')
        slugs.add(re.sub(r'\s','-',slug))
    return slugs

files=[ROOT/'README.md',ROOT/'SPEC.md',ROOT/'docs/AIHOT_ALIGNMENT.md',ROOT/'docs/PROJECT_REVIEW.md']+list(DOCS.rglob('*.md'))
links_checked=0
for file in files:
    text=file.read_text()
    assert len(re.findall(r'^```',text,flags=re.M)) % 2 == 0, f'Unclosed fence: {file}'
    without_code=re.sub(r'```.*?```','',text,flags=re.S)
    for link in re.findall(r'(?<!!)\[[^\]]+\]\(([^)]+)\)',without_code):
        if re.match(r'^[a-z][a-z0-9+.-]*:',link): continue
        url,_,fragment=unquote(link.strip('<>')).partition('#')
        target=(file.parent/url).resolve() if url else file
        assert target.exists(), f'{file.relative_to(ROOT)} -> missing {link}'
        if fragment and target.suffix=='.md':
            assert fragment in anchors(target.read_text()), f'{file.relative_to(ROOT)} -> missing anchor {link}'
        links_checked+=1

for file in (DOCS/'evidence').glob('*.json'): json.loads(file.read_text())
for file in (DOCS/'evidence').glob('*.py'): ast.parse(file.read_text(), filename=str(file))
print(json.dumps({'openapi':'3.1.0 valid','paths':len(contract['paths']), 'schemas':len(schemas),
    'complete_openapi_examples':example_count,'prose_examples':'4 valid',
    'negative_horizon_example':'correctly rejected','local_links_checked':links_checked,
    'json_and_python_evidence':'parseable','runtime_implementation_tested':False},indent=2))
