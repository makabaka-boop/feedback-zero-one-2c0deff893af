#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py — corpus_service 的 JSON Lines 命令行入口

协议（UTF-8）：
  输入：stdin 每行一个 JSON 请求  {"method": <方法名>, "args": {<关键字参数>}}
  输出：stdout 每行一个 JSON 响应信封
        {"ok": true,  "data": ...}
        {"ok": false, "error": {"code": ..., "message": ..., "details": {...}}}

用法：
  python3 cli.py <db路径>                 # 从 stdin 读 JSON Lines，逐行应答
  python3 cli.py <db路径> '<单条请求JSON>' # 执行单条请求后退出

示例：
  printf '%s\n' \
    '{"method":"initialize_labels","args":{"labels":["pos","neg"]}}' \
    '{"method":"register_sample","args":{"sample_id":"s1","text":"good","split":"train"}}' \
    '{"method":"status"}' | python3 cli.py /tmp/demo.db
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_service import CorpusService  # noqa: E402

PUBLIC_METHODS = (
    "initialize_labels", "register_sample", "reassign_split",
    "add_duplicate", "remove_duplicate",
    "submit_annotation", "submit_arbitration",
    "deliver", "get_delivery", "get_sample", "describe_group", "status",
)


def _err(code, message):
    return {"ok": False, "error": {"code": code, "message": message, "details": {}}}


def handle(svc, req):
    if not isinstance(req, dict):
        return _err("INVALID_INPUT", "request must be an object {\"method\":..., \"args\":{...}}")
    method = req.get("method")
    args = req.get("args", {})
    if method not in PUBLIC_METHODS:
        return _err("INVALID_INPUT",
                    "unknown method %r; available: %s" % (method, list(PUBLIC_METHODS)))
    if not isinstance(args, dict):
        return _err("INVALID_INPUT", "args must be an object of keyword arguments")
    try:
        return getattr(svc, method)(**args)
    except TypeError as e:
        return _err("INVALID_INPUT", "bad arguments for %s: %s" % (method, e))


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    svc = CorpusService(argv[1])
    if len(argv) > 2:  # 单条请求模式
        try:
            req = json.loads(" ".join(argv[2:]))
        except ValueError as e:
            req = None
            resp = _err("INVALID_INPUT", "request is not valid JSON: %s" % e)
        if req is not None:
            resp = handle(svc, req)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        return 0
    for line in sys.stdin:  # JSON Lines 模式
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            resp = _err("INVALID_INPUT", "line is not valid JSON: %s" % e)
        else:
            resp = handle(svc, req)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
