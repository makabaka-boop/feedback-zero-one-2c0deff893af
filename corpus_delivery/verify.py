#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify.py — 独立验证脚本：仅通过公开 API 检验 corpus_service 是否满足交付规范。

覆盖：
  1) 标签集合一次性初始化          7) 仲裁证据失效（修订身份粒度）
  2) 样本登记与归属修订            8) 组隔离：完整纳入 / 归属一致 / 互不染指
  3) 重复边增删与传递连通归组      9) 交错发布：交付不可变、重复调用重新核验
  4) 双人标注与修订身份           10) 孤立样本仲裁、空交付、未知引用
  5) 仲裁提交的引用校验           11) 重启持久性（不丢失、不暴露半次提交）
  6) 归属变化不使仲裁失效         12) 线程并发 / 13) 跨进程并发：串行等价

退出码 0 = 全部通过；1 = 存在失败项。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from corpus_service import CorpusService  # noqa: E402

FAILURES = []
MAIN_REVISIONS = []   # 主库中每次成功标注提交生成的修订身份，用于"不复用"核对


def check(name, cond, info=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         "" if cond or not info else " :: " + info))
    if not cond:
        FAILURES.append(name)


def show(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def expect_ok(resp, name):
    check(name, isinstance(resp, dict) and resp.get("ok") is True, show(resp))
    return resp.get("data") if isinstance(resp, dict) and resp.get("ok") else None


def expect_err(resp, name, code=None, needle=None):
    ok = isinstance(resp, dict) and resp.get("ok") is False and "error" in resp
    if ok and code is not None:
        ok = resp["error"].get("code") == code
    if ok and needle is not None:
        ok = needle.lower() in resp["error"].get("message", "").lower()
    check(name, ok, show(resp))


def note_submit(data):
    if data and "revision_id" in data:
        MAIN_REVISIONS.append(data["revision_id"])


def evidence_of(svc, sample_id):
    """读取当前组状态，构造与当前状态完全一致的仲裁证据。"""
    d = svc.describe_group(sample_id)["data"]
    members = d["members"]
    evidence = {m: {a: i["revision_id"] for a, i in d["annotations"][m].items()}
                for m in members}
    return members, evidence


def main():
    tmp = tempfile.mkdtemp(prefix="corpus_verify_")
    db = os.path.join(tmp, "corpus.db")
    print("工作目录: %s" % tmp)
    svc = CorpusService(db)

    # ---------------------------------------------------------------- 1. 初始化
    print("\n== 1. 标签集合一次性初始化 ==")
    expect_err(svc.register_sample("x", "t", "train"),
               "init: 未初始化时拒绝登记", code="STATE", needle="not initialized")
    expect_err(svc.initialize_labels([]), "init: 空标签集合拒绝", code="INVALID_INPUT")
    expect_err(svc.initialize_labels(["pos", "pos"]), "init: 重复标签拒绝", code="INVALID_INPUT")
    expect_err(svc.initialize_labels(["pos", 7]), "init: 非字符串标签拒绝", code="INVALID_INPUT")
    d = expect_ok(svc.initialize_labels(["pos", "neg"]), "init: 合法初始化")
    check("init: 返回标签集合", d == {"labels": ["pos", "neg"]}, show(d))
    expect_err(svc.initialize_labels(["pos"]), "init: 重复初始化拒绝", code="STATE")

    # ---------------------------------------------------------------- 2. 登记
    print("\n== 2. 样本登记与归属修订 ==")
    expect_ok(svc.register_sample("s1", "good movie", "train"), "reg: s1")
    expect_err(svc.register_sample("s1", "other", "eval"),
               "reg: 重复标识拒绝", code="CONFLICT", needle="already registered")
    expect_ok(svc.register_sample("s2", "good movie", "train"),
              "reg: s2 与 s1 同文但独立保留")
    expect_ok(svc.register_sample("s3", "awful plot", "train"), "reg: s3")
    expect_ok(svc.register_sample("s4", "nice acting", "eval"), "reg: s4")
    expect_ok(svc.register_sample("s5", "nice acting", "eval"), "reg: s5")
    expect_ok(svc.register_sample("s6", "meh", "eval"), "reg: s6")
    expect_err(svc.register_sample("", "t", "train"), "reg: 空标识拒绝", code="INVALID_INPUT")
    expect_err(svc.register_sample("s7", "t", "validation"),
               "reg: 非法归属拒绝", code="INVALID_INPUT")
    expect_err(svc.register_sample("s7", 123, "train"),
               "reg: 非字符串正文拒绝", code="INVALID_INPUT")
    expect_err(svc.reassign_split("ghost", "eval"),
               "reg: 未知样本改归属拒绝", code="UNKNOWN_REFERENCE")
    d = expect_ok(svc.reassign_split("s6", "eval"), "reg: 归属同值修订为合法空操作")
    check("reg: 正文不可修改（无修改接口）",
          not hasattr(svc, "update_text") and not hasattr(svc, "edit_text"))

    # ---------------------------------------------------------------- 3. 重复边
    print("\n== 3. 重复关系维护与传递连通归组 ==")
    expect_ok(svc.add_duplicate("s1", "s2"), "edge: 添加 (s1,s2)")
    expect_err(svc.add_duplicate("s2", "s1"),
               "edge: 反向重复添加拒绝", code="CONFLICT", needle="already exists")
    expect_err(svc.add_duplicate("s1", "s1"), "edge: 自环拒绝", code="INVALID_INPUT")
    expect_err(svc.add_duplicate("s1", "ghost"),
               "edge: 未知样本拒绝", code="UNKNOWN_REFERENCE")
    d = expect_ok(svc.add_duplicate("s2", "s3"), "edge: 添加 (s2,s3)")
    check("edge: 传递连通归组 {s1,s2,s3}", d["group"] == ["s1", "s2", "s3"], show(d))
    expect_ok(svc.add_duplicate("s4", "s5"), "edge: 添加 (s4,s5)")
    expect_err(svc.remove_duplicate("s1", "s3"),
               "edge: 删除不存在的边拒绝（连通≠边）", code="CONFLICT", needle="does not exist")
    d = expect_ok(svc.remove_duplicate("s2", "s1"), "edge: 反向删除同边 (s1,s2)")
    check("edge: 删除后 s1 孤立自成一组", d["group_of_a"] == ["s1"], show(d))
    check("edge: 删除后 {s2,s3} 仍连通", d["group_of_b"] == ["s2", "s3"], show(d))
    expect_ok(svc.add_duplicate("s1", "s2"), "edge: 重新添加 (s1,s2)")

    # ---------------------------------------------------------------- 4. 标注
    print("\n== 4. 双人标注与修订身份 ==")
    d1 = expect_ok(svc.submit_annotation("s1", "alice", "pos"), "ann: s1/alice=pos")
    note_submit(d1)
    d2 = expect_ok(svc.submit_annotation("s1", "alice", "pos"),
                   "ann: 同人重复提交替换（标签未变也成功）")
    note_submit(d2)
    check("ann: 标签未变也生成新修订身份",
          d1["revision_id"] != d2["revision_id"] and d2["replaced_previous"] is True,
          show([d1, d2]))
    d = expect_ok(svc.submit_annotation("s1", "bob", "pos"), "ann: s1/bob=pos")
    note_submit(d)
    expect_err(svc.submit_annotation("s1", "carol", "pos"),
               "ann: 第三人提交拒绝", code="CONFLICT", needle="third annotator")
    expect_err(svc.submit_annotation("s1", "alice", "positive"),
               "ann: 集合外标签拒绝", code="INVALID_INPUT", needle="label")
    expect_err(svc.submit_annotation("ghost", "alice", "pos"),
               "ann: 未知样本拒绝", code="UNKNOWN_REFERENCE")
    expect_err(svc.submit_annotation("s1", "", "pos"),
               "ann: 空标注者拒绝", code="INVALID_INPUT")
    for sid in ("s2", "s3"):
        for who in ("alice", "bob"):
            note_submit(expect_ok(svc.submit_annotation(sid, who, "pos"),
                                  "ann: %s/%s=pos" % (sid, who)))
    g = svc.describe_group("s1")["data"]
    check("ann: 组 {s1,s2,s3} 自动确认 pos",
          g["confirmation"] == {"status": "auto", "label": "pos"}, show(g["confirmation"]))
    note_submit(expect_ok(svc.submit_annotation("s4", "alice", "pos"), "ann: s4/alice=pos"))
    note_submit(expect_ok(svc.submit_annotation("s4", "bob", "neg"), "ann: s4/bob=neg"))
    note_submit(expect_ok(svc.submit_annotation("s5", "alice", "neg"), "ann: s5/alice=neg"))
    note_submit(expect_ok(svc.submit_annotation("s5", "bob", "neg"), "ann: s5/bob=neg"))
    g = svc.describe_group("s4")["data"]
    check("ann: 组 {s4,s5} 齐全但不一致 → 未确认",
          g["confirmation"]["status"] == "unconfirmed", show(g["confirmation"]))
    note_submit(expect_ok(svc.submit_annotation("s6", "alice", "pos"), "ann: s6/alice=pos"))

    # ---------------------------------------------------------------- 5. 仲裁校验
    print("\n== 5. 仲裁提交：完整成员与当前修订引用 ==")
    members, ev = evidence_of(svc, "s4")
    expect_err(svc.submit_arbitration(["s4"], {"s4": ev["s4"]}, "neg"),
               "arb: 成员不完整拒绝", code="CONFLICT", needle="complete current duplicate group")
    expect_err(svc.submit_arbitration(["s4", "s5", "s6"], {**ev, "s6": {}}, "neg"),
               "arb: 成员超出实际组拒绝", code="CONFLICT")
    bad_ev = json.loads(json.dumps(ev))
    bad_ev["s4"]["alice"] = "rev-999999"
    expect_err(svc.submit_arbitration(members, bad_ev, "neg"),
               "arb: 伪造修订身份拒绝", code="CONFLICT", needle="evidence")
    expect_err(svc.submit_arbitration(members, {"s4": ev["s4"]}, "neg"),
               "arb: 证据缺少成员拒绝", code="CONFLICT", needle="evidence")
    expect_err(svc.submit_arbitration(members, ev, "purple"),
               "arb: 集合外标签拒绝", code="INVALID_INPUT")
    expect_err(svc.submit_arbitration(["s6"], evidence_of(svc, "s6")[1], "pos"),
               "arb: 缺少双人标注不得仲裁豁免", code="CONFLICT", needle="dual annotation")
    expect_err(svc.submit_arbitration(["s4", "ghost"], ev, "neg"),
               "arb: 未知样本拒绝", code="UNKNOWN_REFERENCE")
    arb1 = expect_ok(svc.submit_arbitration(members, ev, "pos"), "arb: 首次仲裁 pos")
    arb2 = expect_ok(svc.submit_arbitration(members, ev, "neg"), "arb: 再次仲裁 neg 替换前者")
    check("arb: 仲裁身份不复用", arb1["arb_id"] != arb2["arb_id"], show([arb1, arb2]))
    g = svc.describe_group("s4")["data"]
    check("arb: 当前仲裁为最新一次（neg）",
          g["confirmation"]["status"] == "arbitration"
          and g["confirmation"]["label"] == "neg"
          and g["confirmation"]["arbitration"]["arb_id"] == arb2["arb_id"],
          show(g["confirmation"]))

    # ------------------------------------------------- 6. 归属变化不使仲裁失效
    print("\n== 6. 归属变化本身不使仲裁失效 ==")
    expect_ok(svc.reassign_split("s4", "train"), "split: s4 → train")
    expect_ok(svc.reassign_split("s5", "train"), "split: s5 → train")
    expect_err(svc.deliver(["s4"]), "deliver: 组未完整纳入拒绝",
               code="CONFLICT", needle="complete duplicate group")
    delA = expect_ok(svc.deliver(["s4", "s5"]),
                     "deliver: 归属变化后仲裁仍有效（delA）")
    gA = delA["groups"][0]
    check("delA: 最终标签来自仲裁 neg",
          gA["label"] == "neg" and gA["confirmation"] == "arbitration", show(gA))
    check("delA: 附仲裁内容且为 arb2",
          gA.get("arbitration", {}).get("arb_id") == arb2["arb_id"], show(gA))
    check("delA: 完整数据（样本/标签/归属/成员/修订）",
          sorted(i["sample_id"] for i in delA["items"]) == ["s4", "s5"]
          and all(i["label"] == "neg" and i["split"] == "train" for i in delA["items"])
          and gA["members"] == ["s4", "s5"]
          and sorted(gA["annotations"]["s4"]) == ["alice", "bob"], show(delA))
    expect_ok(svc.reassign_split("s4", "eval"), "split: s4 还原 eval")
    expect_ok(svc.reassign_split("s5", "eval"), "split: s5 还原 eval")

    # ------------------------------------------------- 7. 仲裁证据失效
    print("\n== 7. 仲裁证据失效（修订身份粒度） ==")
    d = expect_ok(svc.submit_annotation("s5", "alice", "neg"),
                  "ann: s5/alice 重标（标签未变，修订身份更新）")
    note_submit(d)
    expect_err(svc.deliver(["s4", "s5"]),
               "deliver: 证据失效阻断交付", code="CONFLICT", needle="evidence")
    members, ev = evidence_of(svc, "s4")
    arb3 = expect_ok(svc.submit_arbitration(members, ev, "neg"),
                     "arb: 以当前修订重新仲裁")
    delB = expect_ok(svc.deliver(["s4", "s5"]), "deliver: 重新仲裁后交付（delB）")
    check("delB: 依赖新仲裁 arb3",
          delB["groups"][0]["arbitration"]["arb_id"] == arb3["arb_id"], show(delB["groups"][0]))
    check("delB: 与 delA 为独立交付", delB["delivery_id"] != delA["delivery_id"])
    dA_now = expect_ok(svc.get_delivery(delA["delivery_id"]), "delA: 可按标识重读")
    check("delA: 快照不被后来修订影响（仍引用 arb2 与旧修订）",
          dA_now == delA and dA_now["groups"][0]["arbitration"]["arb_id"] == arb2["arb_id"],
          show(dA_now))

    # ------------------------------------------------- 8. 组隔离
    print("\n== 8. 组隔离：完整纳入 / 归属一致 / 互不染指 ==")
    expect_ok(svc.reassign_split("s3", "eval"), "split: s3 → eval（制造组内不一致）")
    expect_err(svc.deliver(["s1", "s2", "s3"]),
               "group: 组内归属不一致拒绝", code="CONFLICT", needle="share one split")
    expect_ok(svc.reassign_split("s3", "train"), "split: s3 还原 train")
    expect_err(svc.deliver(["s1", "s2"]),
               "group: 缺组成员拒绝", code="CONFLICT", needle="complete duplicate group")
    expect_err(svc.deliver(["s1", "s2", "s3", "s4"]),
               "group: 牵连未完整他组拒绝", code="CONFLICT", needle="complete duplicate group")
    expect_err(svc.deliver(["s6"]),
               "group: 孤立样本标注不足拒绝", code="CONFLICT", needle="exactly two")
    del1 = expect_ok(svc.deliver(["s1", "s2", "s3"]),
                     "group: 他组未确认不阻塞本组交付（del1）")
    g1 = del1["groups"][0]
    check("del1: 自动确认 pos 且不依赖仲裁",
          g1["confirmation"] == "auto" and g1["label"] == "pos"
          and "arbitration" not in g1, show(g1))
    check("del1: 同文样本 s1/s2 独立出现在交付中",
          [i["sample_id"] for i in del1["items"]] == ["s1", "s2", "s3"]
          and del1["items"][0]["text"] == del1["items"][1]["text"] == "good movie",
          show(del1["items"]))
    expect_err(svc.deliver(["s1", "s1", "s2", "s3"]),
               "deliver: 集合含重复 id 拒绝", code="INVALID_INPUT", needle="duplicate")
    expect_err(svc.deliver(["s1", "ghost"]),
               "deliver: 未知样本拒绝", code="UNKNOWN_REFERENCE")

    # ------------------------------------------------- 9. 交错发布与不可变性
    print("\n== 9. 交错发布：交付不可变、重复调用重新核验 ==")
    note_submit(expect_ok(svc.submit_annotation("s1", "alice", "neg"),
                          "ann: s1/alice 改为 neg（破坏一致性）"))
    expect_err(svc.deliver(["s1", "s2", "s3"]),
               "deliver: 已交付组后续失一致 → 重新核验拒绝", code="CONFLICT",
               needle="inconsistent")
    d1_now = expect_ok(svc.get_delivery(del1["delivery_id"]), "del1: 重读")
    check("del1: 成功交付不可修改（仍 pos、仍引用原修订）",
          d1_now == del1, show(d1_now))
    note_submit(expect_ok(svc.submit_annotation("s1", "alice", "pos"),
                          "ann: s1/alice 改回 pos"))
    del2 = expect_ok(svc.deliver(["s1", "s2", "s3"]), "deliver: 恢复后再次交付（del2）")
    check("del2: 独立交付标识", del2["delivery_id"] != del1["delivery_id"])
    check("del2: 失败交付不消耗交付序号（无半次提交）",
          del2["seq"] == del1["seq"] + 1, show({"del1": del1["seq"], "del2": del2["seq"]}))
    del3 = expect_ok(svc.deliver(["s1", "s2", "s3"]), "deliver: 重复调用产生独立交付（del3）")
    check("del3: 与 del2 内容一致但标识独立",
          del3["delivery_id"] != del2["delivery_id"]
          and del3["items"] == del2["items"] and del3["groups"] == del2["groups"])

    # ------------------------------------------------- 10. 孤立样本仲裁 / 空交付
    print("\n== 10. 孤立样本仲裁、空交付、未知引用 ==")
    note_submit(expect_ok(svc.submit_annotation("s6", "bob", "neg"), "ann: s6/bob=neg"))
    members6, ev6 = evidence_of(svc, "s6")
    check("s6: 孤立样本自成一组", members6 == ["s6"], show(members6))
    arb6 = expect_ok(svc.submit_arbitration(members6, ev6, "pos"), "arb: 单样本组仲裁")
    del6 = expect_ok(svc.deliver(["s6"]), "deliver: 单样本组按仲裁交付")
    check("del6: 标签 pos 且附仲裁",
          del6["groups"][0]["label"] == "pos"
          and del6["groups"][0]["arbitration"]["arb_id"] == arb6["arb_id"], show(del6))
    delE = expect_ok(svc.deliver([]), "deliver: 空集合合法")
    check("delE: 空交付内容为空", delE["sample_count"] == 0
          and delE["items"] == [] and delE["groups"] == [], show(delE))
    expect_err(svc.get_delivery("del-999999"),
               "get: 未知交付标识拒绝", code="UNKNOWN_REFERENCE")

    # ------------------------------------------------- 11. 重启持久性
    print("\n== 11. 重启持久性 ==")
    before = svc.describe_group("s1")["data"]
    del svc  # 模拟进程退出
    svc = CorpusService(db)
    expect_err(svc.initialize_labels(["pos", "neg"]),
               "restart: 标签集合持久（重复初始化拒绝）", code="STATE")
    d1_re = expect_ok(svc.get_delivery(del1["delivery_id"]), "restart: del1 完整重读")
    dA_re = expect_ok(svc.get_delivery(delA["delivery_id"]), "restart: delA 完整重读")
    dE_re = expect_ok(svc.get_delivery(delE["delivery_id"]), "restart: 空交付完整重读")
    check("restart: 交付内容逐字节一致",
          d1_re == del1 and dA_re == delA and dE_re == delE)
    after = svc.describe_group("s1")["data"]
    check("restart: 已确认修订不丢失（组状态一致）", after == before, show(after))
    st = svc.status()["data"]
    check("restart: 计数持久（6 样本 / 3 边 / 7 交付）",
          st["counts"]["samples"] == 6 and st["counts"]["edges"] == 3
          and st["counts"]["deliveries"] == 7, show(st["counts"]))
    check("提交生成的修订身份全局不复用",
          len(MAIN_REVISIONS) > 0 and len(set(MAIN_REVISIONS)) == len(MAIN_REVISIONS)
          and st["counts"]["annotation_revisions"] == len(MAIN_REVISIONS),
          "%d revisions" % len(MAIN_REVISIONS))

    # ------------------------------------------------- 12. 线程并发
    print("\n== 12. 线程并发：修订与交付串行等价 ==")
    db2 = os.path.join(tmp, "concurrent.db")
    svc2 = CorpusService(db2)
    svc2.initialize_labels(["pos", "neg"])
    for sid in ("t1", "t2", "t3"):
        svc2.register_sample(sid, "text-" + sid, "train")
    svc2.add_duplicate("t1", "t2")
    svc2.add_duplicate("t2", "t3")
    for sid in ("t1", "t2", "t3"):
        svc2.submit_annotation(sid, "alice", "pos")
        svc2.submit_annotation(sid, "bob", "pos")

    stop = threading.Event()
    internal_errors, success_payloads, business_errors = [], [], []

    def annotator_worker(who):
        n = 0
        while not stop.is_set():
            for sid in ("t1", "t2", "t3"):
                r = svc2.submit_annotation(sid, who, "neg" if n % 6 == 0 else "pos")
                if not r["ok"]:
                    (internal_errors if r["error"]["code"] == "INTERNAL"
                     else business_errors).append(r)
                n += 1

    def deliver_worker():
        while not stop.is_set():
            r = svc2.deliver(["t1", "t2", "t3"])
            if r["ok"]:
                success_payloads.append(r["data"])
            elif r["error"]["code"] == "INTERNAL":
                internal_errors.append(r)
            else:
                business_errors.append(r)

    threads = ([threading.Thread(target=annotator_worker, args=(w,))
                for w in ("alice", "bob")]
               + [threading.Thread(target=deliver_worker) for _ in range(3)])
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join()

    check("并发(线程): 无 INTERNAL 错误", not internal_errors, show(internal_errors[:2]))
    coherent = all(
        p["sample_count"] == 3 and len(p["groups"]) == 1
        and len({i["label"] for m in p["groups"][0]["annotations"].values()
                 for i in m.values()}) == 1
        and all(p["groups"][0]["label"] == i["label"] for i in p["items"])
        for p in success_payloads)
    check("并发(线程): 每个成功交付快照内部一致", coherent and bool(success_payloads),
          "%d successes / %d business-blocked" % (len(success_payloads), len(business_errors)))
    check("并发(线程): 交付标识唯一",
          len({p["delivery_id"] for p in success_payloads}) == len(success_payloads))
    st2 = svc2.status()["data"]
    check("并发(线程): 持久化交付数 == 成功交付数（无半次提交）",
          st2["counts"]["deliveries"] == len(success_payloads), show(st2["counts"]))

    # ------------------------------------------------- 13. 跨进程并发
    print("\n== 13. 跨进程并发：SQLite 串行化写者 ==")
    db3 = os.path.join(tmp, "xproc.db")
    svc3 = CorpusService(db3)
    svc3.initialize_labels(["pos", "neg"])
    for sid in ("u1", "u2", "u3"):
        svc3.register_sample(sid, "text-" + sid, "eval")
    svc3.add_duplicate("u1", "u2")
    svc3.add_duplicate("u2", "u3")
    for sid in ("u1", "u2", "u3"):
        svc3.submit_annotation(sid, "alice", "pos")
        svc3.submit_annotation(sid, "bob", "pos")

    worker = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from corpus_service import CorpusService
svc = CorpusService(sys.argv[2])
res = {"ok_deliveries": 0, "ok_annotations": 0, "errors": []}
for _ in range(10):
    r = svc.submit_annotation("u1", "alice", "pos")
    if r["ok"]: res["ok_annotations"] += 1
    else: res["errors"].append(r)
    r = svc.deliver(["u1", "u2", "u3"])
    if r["ok"]: res["ok_deliveries"] += 1
    else: res["errors"].append(r)
print(json.dumps(res))
'''
    proj = os.path.dirname(os.path.abspath(__file__))
    procs = [subprocess.Popen([sys.executable, "-c", worker, proj, db3],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        check("跨进程: 子进程正常退出", p.returncode == 0, err.strip())
        if p.returncode == 0:
            results.append(json.loads(out.strip().splitlines()[-1]))
    check("跨进程: 两进程各 10 次交付全部成功且无错误",
          len(results) == 2
          and all(r["ok_deliveries"] == 10 and r["ok_annotations"] == 10
                  and not r["errors"] for r in results), show(results))
    st3 = svc3.status()["data"]
    check("跨进程: 交付总数 20（串行等价，无丢失无重复）",
          st3["counts"]["deliveries"] == 20, show(st3["counts"]))

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 60)
    if FAILURES:
        print("失败 %d 项:" % len(FAILURES))
        for name in FAILURES:
            print("  - %s" % name)
        return 1
    print("全部验证通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
