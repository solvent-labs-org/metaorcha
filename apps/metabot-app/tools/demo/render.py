#!/usr/bin/env python3
"""Render a workflow template: __CWD__ and __MONITOR__ replaced, printed as JSON.

usage: render.py TEMPLATE.json CWD MONITOR_CONNECTION
"""
import json
import sys


def main():
    template, cwd, monitor = sys.argv[1:4]
    with open(template, encoding="utf-8") as f:
        text = f.read()
    doc = json.loads(text.replace("__CWD__", json.dumps(cwd)[1:-1]).replace("__MONITOR__", monitor))
    print(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
