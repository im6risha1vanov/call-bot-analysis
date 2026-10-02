"""Verified, complete immutable methodology snapshots, independent of production mode."""
from __future__ import annotations
import hashlib
import importlib
import json
from pathlib import Path

ROOT = Path(__file__).parent / 'packages'
LEGACY = 'legacy_52e4d575'
COURSE = 'course_2025_v1'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def verify():
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    for package in manifest.values():
        for file, digest in package['files'].items():
            if hashlib.sha256((ROOT / file).read_bytes()).hexdigest() != digest:
                raise ValueError('Comparison package hash mismatch')
        if sha(json.dumps(package['files'], sort_keys=True)) != package['sha256']:
            raise ValueError('Invalid package manifest')
    return manifest


def modules(version):
    verify()
    if version == LEGACY:
        return importlib.import_module('comparison.packages.legacy.analysis')
    if version == COURSE:
        return importlib.import_module('comparison.packages.course.methodology.evaluation')
    raise ValueError('Unknown pinned methodology')


def plan(version):
    if version == LEGACY:
        return ('score', 'short', 'review')
    if version == COURSE:
        return ('score', 'short', 'review')  # short/review are deterministic native templates
    raise ValueError('Unknown pinned methodology')


def prompt(version, stage):
    mod = modules(version)
    if version == LEGACY:
        return getattr(mod, stage.upper() + '_PROMPT')
    if stage == 'score':
        return mod.score_prompt()
    raise ValueError('Course reports use the pinned deterministic renderer')
