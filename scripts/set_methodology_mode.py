#!/usr/bin/env python3
"""Disable shadow processing without restarting services; make the change reviewable in Git."""
import argparse
import json
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('mode',choices=['legacy','shadow'])
args=parser.parse_args()
path=Path(__file__).resolve().parents[1]/'methodology/config.json'
value=json.loads(path.read_text())
value['mode']=args.mode
temporary=path.with_suffix('.json.tmp')
temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
temporary.replace(path)
print('Methodology mode:',args.mode)
