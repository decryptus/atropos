"""Fail if unittest discovery misses a statically declared test method."""
import ast
import argparse
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--installed', action='store_true', help='do not add the source checkout to imports')
parser.add_argument('directories', nargs='*')
args = parser.parse_args()
if not args.installed:
    sys.path.insert(0, str(root))
directories = [root / value for value in args.directories] or [root / "tests"]
declared = 0
for path in [p for directory in directories for p in directory.glob('test_*.py')]:
    tree = ast.parse(path.read_text(), filename=str(path))
    declared += sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith('test_') for node in ast.walk(tree))
collected = 0
errors = []
for directory in directories:
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory))
    collected += suite.countTestCases()
    errors.extend(loader.errors)
print(f'Declared: {declared}; collected: {collected}')
if errors or not declared or declared != collected:
    raise SystemExit('Test collection failed or missed declarations')
